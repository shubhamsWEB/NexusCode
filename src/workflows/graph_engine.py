"""
LangGraph-powered Graph Engine for enterprise workflow orchestration.

Replaces the topological DAG executor for workflows that use conditional
routing (any step with a `routes` field).

Architecture:
  WorkflowDef (YAML) → StateGraph[GraphState] → compile(checkpointer) → run

Each YAML step becomes a LangGraph node. The node function:
  1. Rebuilds an ExecutionContext from GraphState for Jinja2 template rendering.
  2. Runs the appropriate handler (agent, action, human_checkpoint, integration, router).
  3. Returns a partial GraphState update — LangGraph merges it via reducers.

Conditional edges are built from StepDef.routes. Route conditions are Python
expressions evaluated safely against the current state dict.

LangSmith tracing is enabled automatically when settings.langsmith_tracing=True
by setting LANGCHAIN_* environment variables before graph compilation.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from src.utils.logging import get_secure_logger
from src.workflows.graph_state import GraphState, state_to_jinja_context
from src.workflows.models import RouteCondition, StepDef, StepType, WorkflowDef

logger = get_secure_logger(__name__)

# ── LangSmith env setup ───────────────────────────────────────────────────────


def _configure_langsmith() -> None:
    """Configure LangSmith tracing via env vars (LangGraph reads these automatically)."""
    from src.config import settings

    if not settings.langsmith_tracing:
        return
    if not settings.langsmith_api_key:
        logger.warning("graph_engine: langsmith_tracing=True but no langsmith_api_key set — tracing disabled")
        return

    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
    os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith_endpoint
    logger.info("graph_engine: LangSmith tracing enabled (project=%s)", settings.langsmith_project)


# ── Safe condition evaluator ─────────────────────────────────────────────────


_SAFE_BUILTINS = {"True": True, "False": False, "None": None, "len": len, "int": int, "str": str}


def _eval_condition(condition: str, state: GraphState) -> bool:
    """
    Evaluate a route condition expression against the current state.

    The expression runs in a restricted environment where only state fields
    and a minimal set of builtins are available. This is safe because conditions
    come from operator-authored YAML definitions, not end-user input.

    Examples:
        "review_verdict == 'approved'"
        "loop_counts.get('coder_agent', 0) >= 3"
        "prd != '' and review_verdict != 'escalate'"
    """
    if not condition:
        return True  # no condition = default/fallback, always matches

    # Build evaluation context from state fields
    ctx: dict[str, Any] = {
        **_SAFE_BUILTINS,
        **{k: v for k, v in state.items() if not k.startswith("_")},
        # Convenience shorthand — avoid state['loop_counts'].get(...)
        "loop_counts": state.get("loop_counts") or {},
    }
    try:
        return bool(eval(condition, {"__builtins__": {}}, ctx))  # noqa: S307
    except Exception as exc:
        logger.warning("graph_engine: condition eval failed %r: %s", condition, exc)
        return False


# ── Node factory ──────────────────────────────────────────────────────────────


def _make_node(step: StepDef, workflow_id: str, trigger_payload_ref: dict) -> Any:
    """
    Return an async function suitable as a LangGraph node for the given step.
    The returned function receives GraphState and returns a partial state update dict.
    """

    async def node_fn(state: GraphState) -> dict:
        run_id = state.get("run_id", "")
        step_id = step.id

        # ── Increment loop counter for this node ──────────────────────────
        loop_counts = dict(state.get("loop_counts") or {})
        loop_counts[step_id] = loop_counts.get(step_id, 0) + 1

        # ── Build Jinja2 context from current state ───────────────────────
        from src.workflows.context import ExecutionContext

        exec_ctx = ExecutionContext(
            trigger_payload=state.get("trigger", {}),
            workflow_context=state.get("wf_context", {}),
        )
        # Restore step outputs so templates like {{ steps.pm_agent.output }} work
        for sid, out in (state.get("step_outputs") or {}).items():
            exec_ctx.set_step_output(sid, out)

        # ── DB: mark step as running ──────────────────────────────────────
        try:
            from src.workflows.registry import upsert_step
            await upsert_step(
                run_id=run_id,
                step_id=step_id,
                status="running",
                step_type=step.type.value,
                agent_role=step.role,
            )
        except Exception as db_exc:
            logger.warning("graph_engine: upsert_step running failed: %s", db_exc)

        output_text = ""
        tokens = 0
        errors: list[str] = []
        extra_state: dict = {}

        try:
            if step.type == StepType.agent:
                output_text, tokens = await _run_agent_step(step, exec_ctx, state, run_id)

            elif step.type == StepType.action:
                result = await _run_action_step(step, exec_ctx)
                output_text = json.dumps(result)

            elif step.type == StepType.integration:
                result = await _run_integration_step(step, exec_ctx, state)
                output_text = json.dumps(result)
                # Surface integration outputs to typed state fields
                extra_state = _extract_integration_fields(step, result)

            elif step.type == StepType.human_checkpoint:
                # Human checkpoints pause the graph — return a sentinel so
                # the caller can yield a checkpoint event and the graph resumes
                # when the checkpoint is answered (via the DB polling loop).
                output_text = await _run_checkpoint_step(step, exec_ctx, state, run_id)

            elif step.type == StepType.router:
                # Router nodes don't execute work — they only route.
                # The conditional edges handle the actual routing; this node
                # just passes state through and records its traversal.
                output_text = f"router:{step_id}"

        except Exception as exc:
            error_msg = f"Step {step_id!r} failed: {exc}"
            logger.exception("graph_engine: %s", error_msg)
            errors = [error_msg]
            output_text = error_msg

        # ── DB: mark step as completed ────────────────────────────────────
        try:
            from src.workflows.registry import upsert_step
            await upsert_step(
                run_id=run_id,
                step_id=step_id,
                status="failed" if errors else "completed",
                output={"text": output_text},
                tokens_used=tokens,
                error_message=errors[0] if errors else None,
            )
        except Exception as db_exc:
            logger.warning("graph_engine: upsert_step completed failed: %s", db_exc)

        # ── Extract and accumulate codebase context from agent output ─────
        from src.workflows.graph_state import extract_code_context_from_output
        new_code_context = extract_code_context_from_output(output_text, step_id)

        # ── Build partial state update ─────────────────────────────────────
        update: dict = {
            "step_outputs": {step_id: output_text},
            "loop_counts": loop_counts,
            "total_tokens": tokens,
            "code_context": new_code_context,  # operator.add appends to existing list
            "messages": [{"role": step.role or step.type.value, "step": step_id, "content": output_text[:1000]}],
            "artifacts": [{
                "step_id": step_id,
                "role": step.role,
                "type": step.type.value,
                "output": output_text[:500],  # truncated for artifact timeline
                "tokens": tokens,
                "timestamp": time.time(),
            }],
        }

        if errors:
            update["errors"] = errors

        # Map output to typed GraphState field if configured
        if step.state_output_key and output_text:
            update[step.state_output_key] = output_text

        # Merge any integration-specific field extractions
        update.update(extra_state)

        return update

    node_fn.__name__ = f"node_{step.id}"
    return node_fn


# ── Step handlers ─────────────────────────────────────────────────────────────


try:
    from langsmith import traceable as _traceable
    _agent_step_traceable = _traceable(name="agent_step", run_type="chain")
except ImportError:
    def _agent_step_traceable(fn):  # type: ignore[misc]
        return fn


@_agent_step_traceable
async def _run_agent_step(
    step: StepDef,
    exec_ctx: Any,
    state: GraphState,
    run_id: str,
) -> tuple[str, int]:
    """Run an agent step — delegates to the existing AgentLoop logic."""
    from src.agent.loop import AgentLoop, AgentLoopConfig
    from src.agent.roles import get_role_config_async
    from src.agent.tool_schemas import ALL_INTERNAL_TOOL_SCHEMAS, RETRIEVAL_TOOL_SCHEMAS
    from src.config import settings
    from src.planning.schemas import ANSWER_TOOL_SCHEMA, CODE_OUTPUT_TOOL_SCHEMA

    # Roles that write code/tests use a dedicated output tool (FILE: block format).
    # All other roles use the standard Q&A answer tool.
    _CODE_ROLES = {"coder", "tester"}  # devops_agent uses ANSWER_TOOL — it calls GitHub MCP tools then outputs PR_URL
    final_tool = CODE_OUTPUT_TOOL_SCHEMA if (step.role or "") in _CODE_ROLES else ANSWER_TOOL_SCHEMA

    role_config = await get_role_config_async(step.role or "searcher")

    # Build accumulated codebase context FIRST — needed for both system prompt and task
    from src.workflows.graph_state import build_code_context_prompt
    prior_code_context = build_code_context_prompt(state.get("code_context") or [])

    # Render system prompt via LangChain-style template (injects repo, feature, state)
    from src.agent.prompt_templates import render_system_prompt
    trigger = state.get("trigger", {})
    rendered_system = render_system_prompt(
        role=step.role or "searcher",
        state=state,
        trigger=trigger,
        prior_context_block=prior_code_context,
    )
    # Use rendered template if available, fall back to role_config system_prompt
    effective_system = rendered_system or role_config["system_prompt"]

    # Build task prompt with Jinja2 rendering (pass state so {{ state.* }} works)
    state_dict = dict(state)
    task_parts = [exec_ctx.render(step.task or "Complete the assigned task.", state=state_dict)]
    for inject in step.context_inject:
        for key, template in inject.items():
            rendered = exec_ctx.render(template, state=state_dict)
            task_parts.append(f"\n## {key}\n{rendered}")

    if exec_ctx.context:
        ctx_str = "\n".join(f"- {k}: {v}" for k, v in exec_ctx.context.items())
        task_parts.append(f"\n## Workflow Context\n{ctx_str}")

    # Inject current enterprise state as additional context
    state_summary = _build_state_summary(state)
    if state_summary:
        task_parts.append(f"\n## Current Workflow State\n{state_summary}")

    # Prepend accumulated codebase context so the agent reads it before its task
    if prior_code_context:
        task_parts.insert(0, prior_code_context)

    full_task = "\n".join(task_parts)

    config = AgentLoopConfig(
        max_iterations=role_config.get("max_iterations", settings.ask_max_iterations),
        cumulative_token_budget=role_config.get("token_budget", settings.agent_token_budget),
        require_search_before_answer=role_config.get("require_search", True),
        planning_max_output_tokens=16000,
    )

    from src.agent.mcp_bridge import get_external_tool_schemas
    external_schemas = get_external_tool_schemas()

    # Include integration tool schemas for this role
    from src.integrations.registry import get_tools_for_role
    integration_schemas = get_tools_for_role(step.role or "searcher")

    all_available = ALL_INTERNAL_TOOL_SCHEMAS + external_schemas + integration_schemas

    allowed_tools = set(role_config.get("default_tools") or [])
    if allowed_tools:
        # Allow integration tools by prefix if role has them
        all_retrieval = [
            t for t in all_available
            if t["name"] in allowed_tools or any(
                t["name"].startswith(prefix)
                for prefix in ["jira_", "slack_", "github_", "figma_", "notion_"]
                if any(it["name"] == t["name"] for it in integration_schemas)
            )
        ]
        if not all_retrieval:
            all_retrieval = RETRIEVAL_TOOL_SCHEMAS
    else:
        all_retrieval = all_available

    trigger = state.get("trigger", {})
    repo_owner = trigger.get("repo_owner")
    repo_name = trigger.get("repo_name")

    # Per-step wall-clock timeout: 20 minutes per step hard cap.
    # This prevents a single hung LLM call from freezing the entire workflow forever.
    step_timeout = role_config.get("step_timeout_seconds", 1200)  # 20 min default

    loop = AgentLoop()
    try:
        tool_block, stats = await asyncio.wait_for(
            loop.run(
                model=settings.default_model,
                system=effective_system,
                initial_message=full_task,
                retrieval_tools=all_retrieval,
                final_answer_tools=[final_tool],
                config=config,
                repo_owner=repo_owner,
                repo_name=repo_name,
                extra_context={"run_id": run_id, "step_id": step.id},
            ),
            timeout=step_timeout,
        )
    except asyncio.TimeoutError:
        logger.error(
            "graph_engine: step %r timed out after %ds (run=%s)",
            step.id, step_timeout, run_id,
        )
        raise TimeoutError(
            f"Step '{step.id}' exceeded the {step_timeout}s time limit. "
            "The LLM call or tool execution did not complete. Check API keys and model availability."
        )

    answer_text = ""
    if isinstance(tool_block, dict):
        inp = tool_block.get("input", {})
        # CODE_OUTPUT_TOOL_SCHEMA uses "files" field; ANSWER_TOOL_SCHEMA uses "answer"
        answer_text = (
            inp.get("files")          # coder/tester: FILE: block content
            or inp.get("answer")      # Q&A roles: markdown answer
            or inp.get("response")    # fallback
            or json.dumps(inp)
        )

    tokens_used = stats.get("context_tokens", 0) if isinstance(stats, dict) else 0
    return answer_text, tokens_used


async def _run_action_step(step: StepDef, exec_ctx: Any) -> dict:
    """Run a non-LLM action step (github, slack, webhook)."""
    if not step.action:
        return {"status": "skipped", "reason": "no action defined"}

    rendered_params = exec_ctx.render_dict(step.params)
    action_name = step.action

    if action_name.startswith("github."):
        return await _github_action(action_name, rendered_params)
    elif action_name.startswith("slack."):
        return await _slack_action(action_name, rendered_params)
    elif action_name == "webhook.call_url":
        return await _webhook_call(rendered_params)
    else:
        return {"status": "skipped", "action": action_name}


async def _run_integration_step(
    step: StepDef,
    exec_ctx: Any,
    state: GraphState,
) -> dict:
    """
    Run a typed integration step (jira.create_issue, slack.send_message, etc.).
    Credentials are fetched transparently — the LLM never sees them.
    """
    if not step.integration:
        return {"status": "skipped", "reason": "no integration defined"}

    rendered_params = exec_ctx.render_dict(step.params)
    # Also inject state fields into params so YAML can reference {{ state.prd }}
    jinja_ctx = state_to_jinja_context(state)
    from jinja2 import Environment
    env = Environment()
    for k, v in rendered_params.items():
        if isinstance(v, str) and "{{" in v:
            try:
                rendered_params[k] = env.from_string(v).render(**jinja_ctx)
            except Exception:
                pass

    from src.integrations.dispatcher import dispatch_integration
    return await dispatch_integration(step.integration, rendered_params)


async def _run_checkpoint_step(
    step: StepDef,
    exec_ctx: Any,
    state: GraphState,
    run_id: str,
) -> str:
    """
    Create a human checkpoint and wait for a response.
    Returns the human's response string when answered.
    """
    from src.workflows.registry import create_checkpoint

    rendered_prompt = exec_ctx.render(step.prompt or "Please review and approve.", state=dict(state))
    cp_id = await create_checkpoint(
        run_id=run_id,
        step_id=step.id,
        prompt=rendered_prompt,
        options=step.options or [],
        timeout_hours=step.timeout_hours,
    )

    from src.workflows.registry import update_run_status
    await update_run_status(run_id, "waiting_human")

    deadline = time.monotonic() + (step.timeout_hours * 3600)
    poll_interval = 10

    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        from sqlalchemy import text
        from src.storage.db import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            cp = (await session.execute(
                text("SELECT status, response FROM human_checkpoints WHERE id = :id"),
                {"id": cp_id},
            )).mappings().first()

        if cp and cp["status"] == "answered":
            await update_run_status(run_id, "running")
            return cp["response"] or ""

    # Timeout
    if step.on_timeout == "skip":
        await update_run_status(run_id, "running")
        return "timeout:skipped"
    else:
        await update_run_status(run_id, "failed", error_message="Human checkpoint timed out")
        raise TimeoutError(f"Human checkpoint for step {step.id!r} timed out")


def _build_state_summary(state: GraphState) -> str:
    """Build a concise state summary injected into agent prompts."""
    parts = []
    fields = [
        ("prd", "PRD"),
        ("component_spec", "Component Spec"),
        ("implementation_plan", "Implementation Plan"),
        ("code_diff", "Code Changes"),
        ("review_verdict", "Review Verdict"),
        ("review_notes", "Review Notes"),
        ("test_plan", "Test Plan"),
        ("deployment_plan", "Deployment Plan"),
    ]
    for key, label in fields:
        val = state.get(key, "")
        if val:
            preview = val[:300] + ("..." if len(val) > 300 else "")
            parts.append(f"**{label}**: {preview}")
    return "\n\n".join(parts)


def _extract_integration_fields(step: StepDef, result: dict) -> dict:
    """Extract typed GraphState fields from an integration step result."""
    if not step.integration:
        return {}
    svc = step.integration.split(".")[0]
    extra: dict = {}
    if svc == "jira" and "key" in result:
        extra["jira_issue_key"] = result["key"]
    elif svc == "github" and "html_url" in result:
        extra["github_pr_url"] = result["html_url"]
    elif svc == "slack" and "ts" in result:
        extra["slack_message_ts"] = result["ts"]
    return extra


# ── Placeholder action handlers (same as executor.py) ────────────────────────


async def _github_action(action: str, params: dict) -> dict:
    try:
        from src.github.client import get_github_client
        get_github_client()
        if action == "github.get_pr_diff":
            return {"status": "ok", "diff": f"[diff for PR #{params.get('pr_number')}]"}
        elif action == "github.post_pr_comment":
            return {"status": "ok", "comment": "Comment posted"}
        return {"status": "skipped", "reason": f"unimplemented: {action}"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


async def _slack_action(action: str, params: dict) -> dict:
    return {"status": "skipped", "reason": "Use integration step for Slack"}


async def _webhook_call(params: dict) -> dict:
    import httpx
    url = params.get("url", "")
    if not url:
        return {"status": "error", "reason": "no url"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=params.get("body", {}))
            return {"status": "ok", "http_status": resp.status_code}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ── Graph builder ─────────────────────────────────────────────────────────────


class GraphEngine:
    """
    Builds and runs LangGraph StateGraphs from WorkflowDef objects.

    Compiled graphs are cached per workflow name+version so that repeated runs
    do not pay the compilation cost on every invocation.

    Usage:
        engine = GraphEngine(checkpointer)
        async for event in engine.stream(wf_def, trigger_payload, run_id):
            yield event
    """

    def __init__(self, checkpointer: Any) -> None:
        self.checkpointer = checkpointer
        # Cache: (workflow_name, version) → compiled LangGraph graph
        self._graph_cache: dict[tuple[str, int], Any] = {}
        _configure_langsmith()

    def _build(self, wf_def: WorkflowDef) -> Any:
        """
        Compile a LangGraph StateGraph from a WorkflowDef.

        Results are cached by (name, version) so the first call compiles
        and every subsequent call for the same workflow version is instant.
        """
        cache_key = (wf_def.name, getattr(wf_def, "version", 0))
        if cache_key in self._graph_cache:
            logger.debug("graph_engine: cache hit for %r v%s", wf_def.name, cache_key[1])
            return self._graph_cache[cache_key]

        from langgraph.graph import END, START, StateGraph

        builder: StateGraph = StateGraph(GraphState)

        # Add all step nodes
        for step in wf_def.steps:
            node_fn = _make_node(step, wf_def.name, {})
            builder.add_node(step.id, node_fn)

        # Determine entry point (steps with no depends_on)
        entry_steps = [s for s in wf_def.steps if not s.depends_on]
        if not entry_steps:
            raise ValueError(f"Workflow {wf_def.name!r} has no entry step (all steps have depends_on)")

        # Use START (idiomatic LangGraph) instead of deprecated set_entry_point()
        builder.add_edge(START, entry_steps[0].id)

        # Build reverse dependency map: source_id → [target_ids]
        reverse_deps: dict[str, list[str]] = {s.id: [] for s in wf_def.steps}
        for step in wf_def.steps:
            for dep in step.depends_on:
                if dep in reverse_deps:
                    reverse_deps[dep].append(step.id)

        has_explicit_deps = any(s.depends_on for s in wf_def.steps)

        if not has_explicit_deps:
            # Pure sequential workflow — wire by position.
            # Any step that has routes (regardless of step type) gets conditional
            # edges; all others get a direct edge to the next step or END.
            steps = wf_def.steps
            for i, step in enumerate(steps):
                if step.routes:
                    self._add_conditional_edges(builder, step, END)
                elif i + 1 < len(steps):
                    builder.add_edge(step.id, steps[i + 1].id)
                else:
                    builder.add_edge(step.id, END)
        else:
            # Explicit dependency graph — use reverse_deps map
            for step in wf_def.steps:
                if step.routes:
                    self._add_conditional_edges(builder, step, END)
                elif reverse_deps[step.id]:
                    for target_id in reverse_deps[step.id]:
                        builder.add_edge(step.id, target_id)
                else:
                    builder.add_edge(step.id, END)

        compiled = builder.compile(checkpointer=self.checkpointer)
        self._graph_cache[cache_key] = compiled
        logger.info(
            "graph_engine: compiled and cached %r v%s (%d steps)",
            wf_def.name, cache_key[1], len(wf_def.steps),
        )
        return compiled

    def _add_conditional_edges(self, builder: Any, step: StepDef, END: Any) -> None:
        """Register conditional edges for a step that has routes."""
        routes = step.routes
        max_loops = step.max_loops

        # Build target map: router return value → node_id or END
        target_map: dict[str, Any] = {"__end__": END}
        for route in routes:
            if route.goto != "END":
                target_map[route.goto] = route.goto

        def make_router(step_routes: list[RouteCondition], step_id: str, ml: int):
            def router(state: GraphState) -> str:
                loop_counts = state.get("loop_counts") or {}
                for route in step_routes:
                    # Check loop safety cap for backward edges
                    if route.goto != "END":
                        target_visits = loop_counts.get(route.goto, 0)
                        if target_visits >= ml:
                            logger.warning(
                                "graph_engine: loop cap hit for %r→%r (%d>=%d), skipping route",
                                step_id, route.goto, target_visits, ml,
                            )
                            continue  # try next route
                    # Evaluate condition
                    if route.condition is None or _eval_condition(route.condition, state):
                        return route.goto if route.goto != "END" else "__end__"
                return "__end__"
            return router

        builder.add_conditional_edges(
            step.id,
            make_router(routes, step.id, max_loops),
            target_map,
        )

    async def stream(
        self,
        wf_def: WorkflowDef,
        trigger_payload: dict,
        run_id: str,
        workflow_context: dict | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Execute a graph-style workflow, yielding SSE-compatible event dicts.
        Compatible with the existing WorkflowExecutor event schema.
        """
        from src.workflows.graph_state import initial_state
        from src.workflows.registry import update_run_context, update_run_status

        compiled = self._build(wf_def)

        init = initial_state(
            run_id=run_id,
            workflow_name=wf_def.name,
            trigger_payload=trigger_payload,
            workflow_context=workflow_context or wf_def.context,
        )

        config = {"configurable": {"thread_id": run_id}}

        await update_run_status(run_id, "running")
        yield {"type": "workflow_started", "run_id": run_id, "workflow": wf_def.name}

        try:
            async for event in compiled.astream(init, config, stream_mode="updates"):
                # event = {node_name: partial_state_update}
                for node_name, update in event.items():
                    if node_name == "__end__":
                        continue
                    tokens = update.get("total_tokens", 0) if isinstance(update, dict) else 0
                    errors = update.get("errors", []) if isinstance(update, dict) else []

                    if errors:
                        yield {
                            "type": "step_failed",
                            "run_id": run_id,
                            "step_id": node_name,
                            "error": errors[-1],
                        }
                    else:
                        yield {
                            "type": "step_complete",
                            "run_id": run_id,
                            "step_id": node_name,
                            "tokens": tokens,
                        }

            # Retrieve final state for context snapshot
            final_state = await compiled.aget_state(config)
            if final_state and final_state.values:
                await update_run_context(run_id, {
                    "trigger": final_state.values.get("trigger", {}),
                    "context": final_state.values.get("wf_context", {}),
                    "step_outputs": final_state.values.get("step_outputs", {}),
                    "graph_state": {
                        k: v for k, v in final_state.values.items()
                        if k not in ("trigger", "wf_context", "step_outputs")
                    },
                })
                total_tokens = final_state.values.get("total_tokens", 0)
            else:
                total_tokens = 0

            await update_run_status(run_id, "completed", total_tokens=total_tokens)
            yield {
                "type": "workflow_complete",
                "run_id": run_id,
                "workflow": wf_def.name,
                "tokens_total": total_tokens,
            }

        except Exception as exc:
            logger.exception("graph_engine: workflow %r failed: %s", wf_def.name, exc)
            await update_run_status(run_id, "failed", error_message=str(exc))
            yield {"type": "workflow_error", "run_id": run_id, "error": str(exc)}


# ── Module-level checkpointer pool (initialised at app startup) ───────────────

_checkpointer: Any = None


async def init_graph_checkpointer() -> None:
    """
    Initialise the LangGraph PostgreSQL checkpointer.
    Called from app.py lifespan. Non-fatal if postgres is unavailable at startup.
    """
    global _checkpointer
    try:
        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from src.config import settings

        # Build a psycopg3 DSN from the SQLAlchemy DATABASE_URL
        # SQLAlchemy URL: postgresql+asyncpg://user:pass@host/db
        # psycopg3 DSN:   postgresql://user:pass@host/db
        dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

        pool = AsyncConnectionPool(
            conninfo=dsn,
            max_size=5,
            open=False,  # we open it explicitly
        )
        await pool.open()

        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # creates checkpoint tables if they don't exist

        _checkpointer = saver
        logger.info("graph_engine: LangGraph PostgreSQL checkpointer ready")
    except Exception as exc:
        logger.warning("graph_engine: checkpointer init failed (graph workflows will run without persistence): %s", exc)
        # Fall back to an in-memory checkpointer
        try:
            from langgraph.checkpoint.memory import MemorySaver
            _checkpointer = MemorySaver()
            logger.info("graph_engine: falling back to MemorySaver checkpointer")
        except Exception:
            _checkpointer = None


def get_graph_engine() -> GraphEngine | None:
    """Return the module-level GraphEngine instance, or None if not initialised."""
    if _checkpointer is None:
        return None
    return GraphEngine(_checkpointer)
