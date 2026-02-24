"""
Claude API caller for the /plan endpoint.

Intent-aware response: Claude picks one of two tools depending on query type.

  answer_codebase_question   → query is a question / explanation / analysis
  output_implementation_plan → query requires code changes (add, fix, refactor)

This mirrors how Claude Code itself behaves: questions get conversational
markdown answers; implementation tasks get structured plans with files/steps/risks.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from src.config import settings
from src.planning.retriever import PlanningContext
from src.planning.schemas import (
    ANALYZE_IMPROVE_TOOL_SCHEMA,
    ANSWER_TOOL_SCHEMA,
    PLAN_TOOL_SCHEMA,
    ImplementationPlan,
    PlanMetadata,
)

logger = logging.getLogger(__name__)


# ── System prompt ──────────────────────────────────────────────────────────────

PLANNING_SYSTEM_PROMPT = """\
You are a world-class senior engineer with deep expertise in the codebase you \
have been given. You have read every file. You reason like a principal engineer \
who has built, maintained, and improved this system.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHOOSE THE RIGHT TOOL — your most critical decision
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

─── `analyze_and_improve` ───────────────────────────────────────
Use when the query asks to IMPROVE, ENHANCE, REVIEW, AUDIT, or MAKE
SOMETHING BETTER that already exists.
  • "how can I make /plan better?"
  • "how to improve the retriever / response quality / search?"
  • "what are the weaknesses of X?"
  • "review the chunker / auth / pipeline"
  • "make this respond like a world-class architect"
  • "optimize / refactor / strengthen X"
→ First: analyze the CURRENT implementation thoroughly (cite file:line).
→ Then: give SPECIFIC, GROUNDED improvements (not generic advice).
→ Never suggest features unrelated to what was asked.
→ This is a deep technical review, not a generic how-to guide.

─── `answer_codebase_question` ──────────────────────────────────
Use when the query is a QUESTION about the codebase.
  • "what does X do?", "how does Y work?", "explain Z"
  • "why is this failing?", "where is the auth logic?"
  • "what is the data flow from A to B?"
→ Rich markdown answer with file:line references. No file changes.

─── `output_implementation_plan` ────────────────────────────────
Use when the query is a TASK requiring file edits/creation/deletion.
  • "add X", "fix the bug in Y", "implement Z", "create endpoint W"
→ Structured plan: files + ordered steps + risks + test plan.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTEXT TIERS — read in this priority order
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. FULL COMPONENT SOURCE — complete file contents (highest authority)
   Present for improvement queries. Read every line carefully.
2. RELEVANT CODE CONTEXT — semantic search results (supporting context)
3. STACK FINGERPRINT — installed packages (check before adding deps)
4. GAP ANALYSIS — external research (only for genuine gaps; ignore for
   improvement queries about internal systems)

RULES FOR ALL TOOLS:
  • Reference real file paths and symbols — never invent them.
  • For `output_implementation_plan`: every path must be in the context.
  • For `analyze_and_improve`: cite specific file:line for every issue.
  • For `output_implementation_plan` summary: start with "Reuses: [...] | Adds: [...]"
\
"""


# ── Prompt builder ─────────────────────────────────────────────────────────────


def _build_user_message(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    repo_scope = f"{repo_owner}/{repo_name}" if (repo_owner and repo_name) else "all indexed repos"
    parts: list[str] = [
        f"## Query\n{query}",
        f"## Scope\nRepository: {repo_scope}",
    ]

    # ── Tier 1: Full component source (improvement queries only) ─────────────
    # This is the most authoritative context — complete file contents.
    # Placed first so Claude reads the full implementation before anything else.
    if ctx.component_context:
        parts.append(ctx.component_context)

    # ── Tier 2: Relevant code context (semantic search results) ──────────────
    if ctx.primary_context:
        parts.append(f"## Relevant Code Context (semantic search)\n{ctx.primary_context}")

    if ctx.file_maps:
        parts.append(f"## File Structure Maps\n{ctx.file_maps}")

    if ctx.caller_contexts:
        parts.append(f"## Known Callers\n{ctx.caller_contexts}")

    if ctx.expansion_context:
        parts.append(f"## Additional Related Context\n{ctx.expansion_context}")

    # ── Tier 3: Stack fingerprint ─────────────────────────────────────────────
    if ctx.stack_fingerprint:
        parts.append(ctx.stack_fingerprint)

    # ── Tier 4: Gap analysis (skipped for improvement queries) ────────────────
    if ctx.web_research_notes:
        notes = ctx.web_research_notes
        if not notes.lstrip().startswith("## Stack"):
            notes = "## Stack-Aware Gap Analysis\n\n" + notes
        parts.append(notes)

    # ── Instructions ──────────────────────────────────────────────────────────
    if ctx.is_improvement_query:
        parts.append(
            "## Instructions\n"
            "This is an IMPROVEMENT / REVIEW query. Use `analyze_and_improve`.\n\n"
            "You have the complete source of the relevant component files above. "
            "Read them carefully and produce a deep, specific analysis:\n"
            "1. What the current implementation does (cite file:line)\n"
            "2. What works well (with evidence)\n"
            "3. Specific issues and gaps (cite file:line for each)\n"
            "4. Concrete improvements grounded in the actual code\n"
            "5. Implementation guidance for the most important changes\n\n"
            "Be a world-class architect who has READ the code — not someone giving generic advice."
        )
    else:
        parts.append(
            "## Instructions\n"
            "Choose the right tool based on the query intent:\n"
            "- QUESTION or explanation → `answer_codebase_question`\n"
            "- Requires CODE CHANGES → `output_implementation_plan`\n\n"
            "Use only real files and symbols from the codebase context above."
        )

    return "\n\n".join(parts)


# ── Response handler ───────────────────────────────────────────────────────────


def _build_metadata(
    ctx: PlanningContext,
    elapsed_ms: float,
) -> PlanMetadata:
    return PlanMetadata(
        model=settings.anthropic_model,
        context_tokens=ctx.tokens_used,
        context_files=len(ctx.chunks_used),
        retrieval_log=ctx.retrieval_log,
        elapsed_ms=elapsed_ms,
        stack_fingerprint=ctx.stack_fingerprint,
        web_research_used=bool(ctx.web_research_notes),
        web_research_notes=ctx.web_research_notes,
    )


def _parse_response(
    message, query: str, ctx: PlanningContext, elapsed_ms: float
) -> ImplementationPlan:
    """
    Parse a Claude response that may have called either tool.
    Returns an ImplementationPlan with response_type set appropriately.
    """
    tool_block = next(
        (b for b in message.content if b.type == "tool_use"),
        None,
    )

    if tool_block is None:
        # Claude responded in text (shouldn't happen but handle gracefully)
        text = " ".join(b.text for b in message.content if hasattr(b, "text") and b.text).strip()
        plan = ImplementationPlan(
            query=query,
            response_type="answer",
            answer=text or "_No response generated._",
        )
        plan.metadata = _build_metadata(ctx, elapsed_ms)
        return plan

    if tool_block.name == "answer_codebase_question":
        data = tool_block.input
        plan = ImplementationPlan(
            query=query,
            response_type="answer",
            answer=data.get("answer", ""),
            key_files=data.get("key_files", []),
        )
        plan.metadata = _build_metadata(ctx, elapsed_ms)
        return plan

    if tool_block.name == "analyze_and_improve":
        data = tool_block.input
        plan = ImplementationPlan(
            query=query,
            response_type="analysis",
            analysis=data.get("analysis", ""),
            key_files=data.get("key_files", []),
        )
        plan.metadata = _build_metadata(ctx, elapsed_ms)
        return plan

    # output_implementation_plan
    plan_data = tool_block.input
    plan_data["query"] = query
    plan = ImplementationPlan.model_validate(plan_data)
    plan.metadata = _build_metadata(ctx, elapsed_ms)
    return plan


# ── Sync generator ─────────────────────────────────────────────────────────────


async def generate_plan(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> ImplementationPlan:
    """
    Call Claude with two tools and auto tool-choice.
    Claude picks answer_codebase_question or output_implementation_plan
    based on the query intent.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic>=0.40.0"
        ) from exc

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    user_message = _build_user_message(query, ctx, repo_owner, repo_name)

    t0 = time.monotonic()

    import asyncio

    loop = asyncio.get_event_loop()

    def _call():
        _max_retries = 3
        for _attempt in range(_max_retries + 1):
            try:
                return client.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=settings.planning_max_output_tokens,
                    system=PLANNING_SYSTEM_PROMPT,
                    tools=[ANSWER_TOOL_SCHEMA, ANALYZE_IMPROVE_TOOL_SCHEMA, PLAN_TOOL_SCHEMA],
                    tool_choice={"type": "auto"},
                    messages=[{"role": "user", "content": user_message}],
                )
            except anthropic.APIStatusError as exc:
                if exc.status_code == 529 and _attempt < _max_retries:
                    _wait = 2**_attempt  # 1 s, 2 s, 4 s
                    logger.warning(
                        "planning: API overloaded (529), retry %d/%d in %ds",
                        _attempt + 1,
                        _max_retries,
                        _wait,
                    )
                    time.sleep(_wait)
                else:
                    raise
        raise RuntimeError("Anthropic API is overloaded. Please try again in a moment.")

    message = await loop.run_in_executor(None, _call)
    elapsed_ms = (time.monotonic() - t0) * 1000

    tool_used = next((b.name for b in message.content if b.type == "tool_use"), "none")
    logger.info(
        "planning: Claude responded in %.0fms, stop_reason=%s, tool=%s",
        elapsed_ms,
        message.stop_reason,
        tool_used,
    )

    return _parse_response(message, query, ctx, elapsed_ms)


# ── Streaming generator ────────────────────────────────────────────────────────


async def stream_generate_plan(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> AsyncIterator[dict]:
    """
    Stream plan generation from Claude, yielding incremental events:

      {"type": "token",         "text": "<chunk>"}
          Fired for every output token Claude emits — plain text for
          answer/analysis responses, partial-JSON for plan responses
          (via the Anthropic input_json_delta streaming event).

      {"type": "plan_complete", "plan": ImplementationPlan}
          Fired once when Claude's full response has been received and the
          tool-use output has been parsed into a structured ImplementationPlan.

    Uses a thread-queue bridge so the blocking Anthropic SDK stream runs on a
    thread-pool executor while the async caller can await and yield normally.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic>=0.40.0"
        ) from exc

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")

    import asyncio
    import queue as _queue

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    user_message = _build_user_message(query, ctx, repo_owner, repo_name)
    t0 = time.monotonic()

    # (kind, value) tuples pushed by the background thread
    event_queue: _queue.Queue = _queue.Queue()

    def _stream() -> None:
        _max_retries = 3
        _last_exc: Exception | None = None
        try:
            for _attempt in range(_max_retries + 1):
                try:
                    with client.messages.stream(
                        model=settings.anthropic_model,
                        max_tokens=settings.planning_max_output_tokens,
                        system=PLANNING_SYSTEM_PROMPT,
                        tools=[ANSWER_TOOL_SCHEMA, ANALYZE_IMPROVE_TOOL_SCHEMA, PLAN_TOOL_SCHEMA],
                        tool_choice={"type": "auto"},
                        messages=[{"role": "user", "content": user_message}],
                    ) as stream:
                        for event in stream:
                            event_type = getattr(event, "type", None)

                            # ── answer / analysis responses — plain text tokens
                            if event_type == "text":
                                text = getattr(event, "text", None)
                                if text:
                                    event_queue.put(("token", text))

                            # ── plan responses — partial JSON from tool_use ───
                            elif event_type == "input_json":
                                partial = getattr(event, "partial_json", None)
                                if partial:
                                    event_queue.put(("token", partial))

                        final_msg = stream.get_final_message()
                        event_queue.put(("message", final_msg))
                        return  # success — exit retry loop

                except anthropic.APIStatusError as exc:
                    if exc.status_code == 529 and _attempt < _max_retries:
                        _wait = 2**_attempt  # 1 s, 2 s, 4 s
                        logger.warning(
                            "planning: API overloaded (529), retry %d/%d in %ds",
                            _attempt + 1,
                            _max_retries,
                            _wait,
                        )
                        _last_exc = exc
                        time.sleep(_wait)
                    else:
                        event_queue.put(("error", exc))
                        return

            # All retries exhausted
            event_queue.put(
                (
                    "error",
                    _last_exc
                    or RuntimeError("Anthropic API is overloaded. Please try again in a moment."),
                )
            )
        except Exception as exc:
            event_queue.put(("error", exc))
        finally:
            event_queue.put(("done", None))

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _stream)

    while True:
        try:
            kind, value = event_queue.get_nowait()
        except _queue.Empty:
            await asyncio.sleep(0.02)
            continue

        if kind == "done":
            break
        if kind == "error":
            raise value
        if kind == "token":
            yield {"type": "token", "text": value}
        if kind == "message":
            elapsed_ms = (time.monotonic() - t0) * 1000
            tool_used = next((b.name for b in value.content if b.type == "tool_use"), "none")
            logger.info(
                "planning: stream complete in %.0fms, stop_reason=%s, tool=%s",
                elapsed_ms,
                value.stop_reason,
                tool_used,
            )
            plan = _parse_response(value, query, ctx, elapsed_ms)
            yield {"type": "plan_complete", "plan": plan}
