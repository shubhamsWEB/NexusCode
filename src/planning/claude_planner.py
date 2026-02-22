"""
Claude API caller for implementation plan generation.

Uses tool_use (forced function call) to guarantee structured JSON output.
The Anthropic SDK is required — install via: pip install anthropic>=0.40.0

Two public functions:
  generate_plan(query, ctx, repo?) -> ImplementationPlan   (sync via asyncio executor)
  stream_plan(query, ctx, repo?)   -> AsyncIterator[str]   (raw text chunks)
"""
from __future__ import annotations

import logging
import time
from typing import AsyncIterator, Optional

from src.config import settings
from src.planning.retriever import PlanningContext
from src.planning.schemas import PLAN_TOOL_SCHEMA, ImplementationPlan, PlanMetadata

logger = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

PLANNING_SYSTEM_PROMPT = """\
You are an expert software architect and senior engineer tasked with generating \
precise, actionable implementation plans.

The user will give you a bug report, feature request, or refactoring task, \
along with relevant code context retrieved from their codebase.

Your plan MUST:
1. Reference **real file paths and symbol names** from the provided context.
2. Include **pseudocode** for any non-trivial logic changes.
3. Order steps so that **dependencies come first** (never step N before the \
step it depends on).
4. Identify **callers** that may break — mention them by file and symbol.
5. Be **honest about risks** — prefer "medium" or "high" over "low" when in doubt.
6. Keep the test_plan concrete (specific assertions, not 'write a test').

Do NOT suggest changes outside the scope of the query.
Do NOT add unrelated refactoring.
Output ONLY through the provided tool — no prose outside the tool call.\
"""


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_user_message(
    query: str,
    ctx: PlanningContext,
    repo_owner: Optional[str],
    repo_name: Optional[str],
) -> str:
    repo_scope = f"{repo_owner}/{repo_name}" if (repo_owner and repo_name) else "all indexed repos"
    parts: list[str] = [
        f"## Task\n{query}",
        f"## Scope\nRepository: {repo_scope}",
    ]

    if ctx.primary_context:
        parts.append(f"## Relevant Code Context\n{ctx.primary_context}")

    if ctx.file_maps:
        parts.append(f"## File Structure Maps\n{ctx.file_maps}")

    if ctx.caller_contexts:
        parts.append(f"## Known Callers\n{ctx.caller_contexts}")

    if ctx.expansion_context:
        parts.append(f"## Additional Related Context\n{ctx.expansion_context}")

    parts.append(
        "## Instructions\n"
        "Generate a complete implementation plan using the output_implementation_plan tool.\n"
        "Be specific — reference exact file paths and symbol names from the context above."
    )

    return "\n\n".join(parts)


# ── Sync generator ────────────────────────────────────────────────────────────

async def generate_plan(
    query: str,
    ctx: PlanningContext,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> ImplementationPlan:
    """
    Call Claude with tool_use to generate a structured ImplementationPlan.
    Runs the blocking Anthropic SDK call in an executor thread.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic>=0.40.0"
        ) from exc

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    user_message = _build_user_message(query, ctx, repo_owner, repo_name)

    t0 = time.monotonic()

    import asyncio
    loop = asyncio.get_event_loop()

    def _call():
        return client.messages.create(
            model=settings.anthropic_model,
            max_tokens=settings.planning_max_output_tokens,
            system=PLANNING_SYSTEM_PROMPT,
            tools=[PLAN_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "output_implementation_plan"},
            messages=[{"role": "user", "content": user_message}],
        )

    message = await loop.run_in_executor(None, _call)

    elapsed_ms = (time.monotonic() - t0) * 1000
    logger.info(
        "planning: Claude responded in %.0fms, stop_reason=%s",
        elapsed_ms,
        message.stop_reason,
    )

    # Extract tool_use block
    tool_block = next(
        (b for b in message.content if b.type == "tool_use"),
        None,
    )
    if tool_block is None:
        raise ValueError(
            f"Claude did not call the output_implementation_plan tool. "
            f"stop_reason={message.stop_reason}"
        )

    plan_data = tool_block.input
    plan_data["query"] = query

    plan = ImplementationPlan.model_validate(plan_data)
    plan.metadata = PlanMetadata(
        model=settings.anthropic_model,
        context_tokens=ctx.tokens_used,
        context_files=len(ctx.chunks_used),
        retrieval_log=ctx.retrieval_log,
        elapsed_ms=elapsed_ms,
    )
    return plan


# ── Streaming generator ───────────────────────────────────────────────────────

async def stream_plan(
    query: str,
    ctx: PlanningContext,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> AsyncIterator[str]:
    """
    Stream raw text chunks from Claude.
    Yields text deltas as they arrive.

    Note: streaming with tool_use gives partial JSON text.
    The caller is responsible for accumulating and parsing the final JSON.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic>=0.40.0"
        ) from exc

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    user_message = _build_user_message(query, ctx, repo_owner, repo_name)

    import asyncio
    import json
    import queue

    chunk_queue: queue.Queue = queue.Queue()

    def _stream():
        try:
            with client.messages.stream(
                model=settings.anthropic_model,
                max_tokens=settings.planning_max_output_tokens,
                system=PLANNING_SYSTEM_PROMPT,
                tools=[PLAN_TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": "output_implementation_plan"},
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                for text in stream.text_stream:
                    chunk_queue.put(text)
        except Exception as exc:
            chunk_queue.put(exc)
        finally:
            chunk_queue.put(None)  # sentinel

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _stream)

    while True:
        # Poll queue with tiny sleep to avoid blocking the event loop
        try:
            item = chunk_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.02)
            continue

        if item is None:
            break
        if isinstance(item, Exception):
            raise item
        yield item
