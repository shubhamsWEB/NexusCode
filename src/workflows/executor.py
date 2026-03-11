"""
DAG Executor — runs a workflow by executing steps in topological order.

Features:
- Parallel execution of independent steps (same wave)
- Per-step retry with exponential backoff
- Human checkpoint support (pause/resume)
- Streaming status events via async generator
- Token usage tracking
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from src.utils.logging import get_secure_logger
from src.workflows.context import ExecutionContext
from src.workflows.models import StepDef, StepType, WorkflowDef
from src.workflows.parser import topological_order
from src.workflows.registry import (
    create_checkpoint,
    create_run,
    get_workflow_by_name,
    update_run_context,
    update_run_status,
    upsert_step,
)

logger = get_secure_logger(__name__)


class WorkflowExecutor:
    """
    Executes a WorkflowDef given a trigger payload.
    Yields SSE-compatible status dicts when used as an async generator.

    Pass workflow_id and pre_created_run_id when the API has already
    created the run record — executor will reuse it instead of creating a new one.
    """

    def __init__(
        self,
        wf_def: WorkflowDef,
        trigger_payload: dict[str, Any],
        workflow_id: str = "",
        pre_created_run_id: str | None = None,
    ) -> None:
        self.wf_def = wf_def
        self.trigger_payload = trigger_payload
        self.workflow_id = workflow_id
        self.exec_ctx = ExecutionContext(
            trigger_payload=trigger_payload,
            workflow_context=wf_def.context,
        )
        # If the API pre-created the run record, store it so stream() reuses it
        self.run_id: str | None = pre_created_run_id
        self._total_tokens = 0

    async def run(self) -> dict[str, Any]:
        """
        Execute the workflow synchronously (non-streaming).
        Returns the final result dict.
        """
        result: dict[str, Any] = {"status": "completed"}
        async for event in self.stream():
            if event.get("type") == "workflow_complete" or event.get("type") == "workflow_error":
                result = event
        return result

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """
        Execute the workflow, yielding SSE-compatible event dicts.

        Event types:
          {"type": "workflow_started",  "run_id": "...", "workflow": "..."}
          {"type": "step_started",      "run_id": "...", "step_id": "...", "role": "..."}
          {"type": "step_complete",     "run_id": "...", "step_id": "...", "tokens": N}
          {"type": "step_failed",       "run_id": "...", "step_id": "...", "error": "..."}
          {"type": "checkpoint_created","run_id": "...", "step_id": "...", "checkpoint_id": "..."}
          {"type": "workflow_complete", "run_id": "...", "tokens_total": N}
          {"type": "workflow_error",    "run_id": "...", "error": "..."}
        """
        # Use pre-created run record from the API, or create one now (e.g. standalone calls)
        if self.run_id is None:
            # Resolve workflow_id if not provided
            wf_id = self.workflow_id
            if not wf_id:
                wf_record = await get_workflow_by_name(self.wf_def.name)
                wf_id = wf_record["id"] if wf_record else ""
            self.run_id = await create_run(
                workflow_id=wf_id,
                workflow_name=self.wf_def.name,
                trigger_payload=self.trigger_payload,
            )

        await update_run_status(self.run_id, "running")

        yield {
            "type": "workflow_started",
            "run_id": self.run_id,
            "workflow": self.wf_def.name,
        }

        try:
            waves = topological_order(self.wf_def)

            for wave in waves:
                if len(wave) == 1:
                    # Single step — run directly
                    async for evt in self._execute_step(wave[0]):
                        yield evt
                else:
                    # Multiple independent steps — run in parallel
                    tasks = [self._collect_step(s) for s in wave]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for step, res in zip(wave, results):
                        if isinstance(res, BaseException):
                            yield {
                                "type": "step_failed",
                                "run_id": self.run_id,
                                "step_id": step.id,
                                "error": str(res),
                            }
                        else:
                            for evt in res:
                                yield evt

            await update_run_status(
                self.run_id,
                "completed",
                total_tokens=self._total_tokens,
            )
            await update_run_context(self.run_id, self.exec_ctx.to_snapshot())

            yield {
                "type": "workflow_complete",
                "run_id": self.run_id,
                "workflow": self.wf_def.name,
                "tokens_total": self._total_tokens,
            }

        except Exception as exc:
            logger.exception("workflow execution failed: %s", exc)
            await update_run_status(self.run_id, "failed", error_message=str(exc))
            yield {
                "type": "workflow_error",
                "run_id": self.run_id,
                "error": str(exc),
            }

    async def _collect_step(self, step: StepDef) -> list[dict[str, Any]]:
        """Collect all events from a step into a list (for parallel execution)."""
        events: list[dict[str, Any]] = []
        async for evt in self._execute_step(step):
            events.append(evt)
        return events

    async def _execute_step(self, step: StepDef) -> AsyncIterator[dict[str, Any]]:
        """Execute a single step with retry logic."""
        yield {
            "type": "step_started",
            "run_id": self.run_id,
            "step_id": step.id,
            "step_type": step.type.value,
            "role": step.role if step.role else None,
        }

        await upsert_step(
            run_id=self.run_id,
            step_id=step.id,
            status="running",
            step_type=step.type.value,
            agent_role=step.role if step.role else None,
        )

        for attempt in range(step.max_retries + 1):
            try:
                if step.type == StepType.human_checkpoint:
                    async for evt in self._run_checkpoint_step(step):
                        yield evt
                    return

                elif step.type == StepType.action:
                    output = await self._run_action_step(step)
                elif step.type == StepType.agent:
                    output_text, tokens = await self._run_agent_step(step)
                    self._total_tokens += tokens

                    # Collect any PDFs generated during this step and attach them
                    from src.tools.pdf_generator import get_documents_for_step
                    output_json: dict[str, Any] = {"text": output_text}
                    try:
                        pdf_docs = await get_documents_for_step(self.run_id, step.id)
                        if pdf_docs:
                            output_json["documents"] = [
                                {
                                    "doc_id": d["id"],
                                    "filename": d["filename"] + ".pdf",
                                    "size_bytes": d["size_bytes"],
                                }
                                for d in pdf_docs
                            ]
                    except Exception as _pdf_exc:
                        logger.warning("executor: could not fetch pdf docs for step %s: %s", step.id, _pdf_exc)

                    await upsert_step(
                        run_id=self.run_id,
                        step_id=step.id,
                        status="completed",
                        output=output_json,
                        tokens_used=tokens,
                    )
                    self.exec_ctx.set_step_output(step.id, output_text)
                    yield {
                        "type": "step_complete",
                        "run_id": self.run_id,
                        "step_id": step.id,
                        "tokens": tokens,
                    }
                    return
                else:
                    output = {}

                await upsert_step(
                    run_id=self.run_id,
                    step_id=step.id,
                    status="completed",
                    output=output,
                )
                self.exec_ctx.set_step_output(step.id, output)
                yield {
                    "type": "step_complete",
                    "run_id": self.run_id,
                    "step_id": step.id,
                    "tokens": 0,
                }
                return

            except Exception as exc:
                if attempt < step.max_retries:
                    delay = step.retry_delay_seconds * (2 ** attempt)
                    logger.warning(
                        "step %s failed (attempt %d/%d), retrying in %ds: %s",
                        step.id, attempt + 1, step.max_retries + 1, delay, exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    await upsert_step(
                        run_id=self.run_id,
                        step_id=step.id,
                        status="failed",
                        error_message=str(exc),
                    )
                    yield {
                        "type": "step_failed",
                        "run_id": self.run_id,
                        "step_id": step.id,
                        "error": str(exc),
                    }
                    raise

    async def _run_agent_step(self, step: StepDef) -> tuple[str, int]:
        """
        Run an agent step using the AgentLoop with the appropriate role.
        Returns (output_text, tokens_used).
        """
        from src.agent.loop import AgentLoop, AgentLoopConfig
        from src.agent.roles import get_role_config_async
        from src.agent.tool_schemas import ALL_INTERNAL_TOOL_SCHEMAS, RETRIEVAL_TOOL_SCHEMAS
        from src.config import settings
        from src.planning.schemas import ANSWER_TOOL_SCHEMA

        role_config = await get_role_config_async(step.role or "searcher")

        # Build the task prompt with injected context
        # Render Jinja2 templates in the task itself ({{ trigger.* }}, {{ steps.*.output }}, etc.)
        task_parts = [self.exec_ctx.render(step.task or "Complete the assigned task.")]
        for inject in step.context_inject:
            for key, template in inject.items():
                rendered = self.exec_ctx.render(template)
                task_parts.append(f"\n## {key}\n{rendered}")

        # Also inject global workflow context
        if self.exec_ctx.context:
            ctx_str = "\n".join(f"- {k}: {v}" for k, v in self.exec_ctx.context.items())
            task_parts.append(f"\n## Workflow Context\n{ctx_str}")

        full_task = "\n".join(task_parts)

        config = AgentLoopConfig(
            max_iterations=role_config.get("max_iterations", settings.ask_max_iterations),
            cumulative_token_budget=role_config.get("token_budget", settings.agent_token_budget),
            require_search_before_answer=role_config.get("require_search", True),
            planning_max_output_tokens=16000,
        )

        # Build the full tool pool: all internal tools + any registered external MCP tools
        from src.agent.mcp_bridge import get_external_tool_schemas
        external_schemas = get_external_tool_schemas()
        all_available = ALL_INTERNAL_TOOL_SCHEMAS + external_schemas

        # Apply the role's tool allowlist (internal + external by name).
        # If a role has no default_tools configured it is "unscoped" — give it
        # all internal tools plus all external tools so newly-registered MCP
        # servers are immediately available without requiring role edits.
        # Scoped roles (default_tools set) must explicitly include external tool
        # names to use them — this keeps tool lists small and accurate.
        allowed_tools = set(role_config.get("default_tools") or [])
        if allowed_tools:
            all_retrieval = [t for t in all_available if t["name"] in allowed_tools]
            if not all_retrieval:
                all_retrieval = RETRIEVAL_TOOL_SCHEMAS  # safety fallback: core 4 tools
        else:
            # Unscoped role — all internal + all external tools
            all_retrieval = all_available

        # Extract repo from trigger payload if present
        repo_owner = self.trigger_payload.get("repo_owner")
        repo_name = self.trigger_payload.get("repo_name")

        loop = AgentLoop()
        tool_block, stats = await loop.run(
            model=settings.default_model,
            system=role_config["system_prompt"],
            initial_message=full_task,
            retrieval_tools=all_retrieval,
            final_answer_tools=[ANSWER_TOOL_SCHEMA],
            config=config,
            repo_owner=repo_owner,
            repo_name=repo_name,
            extra_context={"run_id": self.run_id, "step_id": step.id},
        )

        # Extract answer text from the final tool call block
        answer_text = ""
        if isinstance(tool_block, dict):
            inp = tool_block.get("input", {})
            answer_text = inp.get("answer") or inp.get("response") or json.dumps(inp)

        tokens_used = stats.get("context_tokens", 0) if isinstance(stats, dict) else 0
        return answer_text, tokens_used

    async def _run_action_step(self, step: StepDef) -> dict[str, Any]:
        """
        Run a non-LLM action step (github, slack, webhook calls).
        Routes to the appropriate action handler.
        """
        if not step.action:
            return {"status": "skipped", "reason": "no action defined"}

        rendered_params = self.exec_ctx.render_dict(step.params)

        action_name = step.action
        logger.info("executing action: %s with params: %s", action_name, rendered_params)

        # Route to action handlers
        if action_name.startswith("github."):
            return await self._github_action(action_name, rendered_params)
        elif action_name.startswith("slack."):
            return await self._slack_action(action_name, rendered_params)
        elif action_name == "webhook.call_url":
            return await self._webhook_call(rendered_params)
        else:
            logger.warning("unknown action: %s — skipping", action_name)
            return {"status": "skipped", "action": action_name}

    async def _run_checkpoint_step(self, step: StepDef) -> AsyncIterator[dict[str, Any]]:
        """Create a human checkpoint and wait for a response (or timeout)."""
        import asyncio

        rendered_prompt = self.exec_ctx.render(step.prompt or "Please review and approve.")

        cp_id = await create_checkpoint(
            run_id=self.run_id,
            step_id=step.id,
            prompt=rendered_prompt,
            options=step.options or [],
            timeout_hours=step.timeout_hours,
        )

        await update_run_status(self.run_id, "waiting_human")

        yield {
            "type": "checkpoint_created",
            "run_id": self.run_id,
            "step_id": step.id,
            "checkpoint_id": cp_id,
            "prompt": rendered_prompt,
        }

        # Poll for response up to timeout
        deadline = time.monotonic() + (step.timeout_hours * 3600)
        poll_interval = 10  # seconds

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)

            from sqlalchemy import text

            from src.storage.db import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                cp = (
                    await session.execute(
                        text("SELECT status, response FROM human_checkpoints WHERE id = :id"),
                        {"id": cp_id},
                    )
                ).mappings().first()

            if cp and cp["status"] == "answered":
                await update_run_status(self.run_id, "running")
                self.exec_ctx.set_step_output(step.id, cp["response"])
                await upsert_step(
                    run_id=self.run_id,
                    step_id=step.id,
                    status="completed",
                    output={"response": cp["response"]},
                )
                yield {
                    "type": "step_complete",
                    "run_id": self.run_id,
                    "step_id": step.id,
                    "checkpoint_response": cp["response"],
                }
                return

        # Timeout reached
        if step.on_timeout == "skip":
            await update_run_status(self.run_id, "running")
            await upsert_step(run_id=self.run_id, step_id=step.id, status="skipped")
            yield {"type": "step_complete", "run_id": self.run_id, "step_id": step.id, "skipped": True}
        else:
            await update_run_status(self.run_id, "failed", error_message="Human checkpoint timed out")
            raise TimeoutError(f"Human checkpoint for step {step.id!r} timed out")

    async def _github_action(self, action: str, params: dict) -> dict[str, Any]:
        """Execute a GitHub action."""
        try:
            from src.github.client import get_github_client
            get_github_client()

            if action == "github.get_pr_diff":
                # Placeholder — implement with actual GitHub API
                return {"status": "ok", "diff": f"[diff for PR #{params.get('pr_number')}]"}
            elif action == "github.post_pr_comment":
                return {"status": "ok", "comment": "Comment posted (placeholder)"}
            else:
                return {"status": "skipped", "reason": f"unimplemented github action: {action}"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def _slack_action(self, action: str, params: dict) -> dict[str, Any]:
        """Execute a Slack action (placeholder — wire to real Slack API)."""
        return {"status": "skipped", "reason": "Slack integration not configured"}

    async def _webhook_call(self, params: dict) -> dict[str, Any]:
        """Call an external webhook URL."""
        import httpx
        url = params.get("url", "")
        if not url:
            return {"status": "error", "reason": "no url specified"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=params.get("body", {}))
                return {"status": "ok", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}


async def trigger_workflow(
    workflow_name: str,
    trigger_payload: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """
    High-level entry point: look up a workflow by name, parse it, execute it.
    Yields SSE event dicts.
    """
    from src.workflows.parser import parse_workflow

    wf_record = await get_workflow_by_name(workflow_name)
    if not wf_record:
        yield {"type": "workflow_error", "error": f"Workflow {workflow_name!r} not found"}
        return

    if not wf_record.get("is_active", True):
        yield {"type": "workflow_error", "error": f"Workflow {workflow_name!r} is disabled"}
        return

    try:
        wf_def = parse_workflow(wf_record["yaml_definition"])
    except Exception as exc:
        yield {"type": "workflow_error", "error": f"Parse error: {exc}"}
        return

    executor = WorkflowExecutor(wf_def, trigger_payload)
    async for event in executor.stream():
        yield event
