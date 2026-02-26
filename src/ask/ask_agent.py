"""
Ask Mode LLM agent.

Answers natural-language questions about the codebase in a mentor tone:
clear, direct, grounded in real code, with concrete citations.

Deliberately uses a DIFFERENT system prompt from the planning module:
  - Planning: "every token must be actionable", structured JSON tool output
  - Ask:      conversational markdown prose, inline code citations, mentor voice

Retrieval is shared with the planning pipeline (retrieve_planning_context),
but the LLM call, system prompt, tool schema, and user message are all
specific to Ask Mode.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
import time
from collections.abc import AsyncIterator

from src.config import settings
from src.planning.retriever import PlanningContext

logger = logging.getLogger(__name__)

# Serialize Anthropic calls — Ask Mode shares the same rate-limit budget
# as Planning.  One concurrent ask + one concurrent plan would double
# per-minute token usage and risk 429s.
_ask_semaphore = _asyncio.Semaphore(1)

# Lazy singleton — shares the pool with claude_planner if both are imported
_anthropic_client = None


def _get_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


# ── System prompt ──────────────────────────────────────────────────────────────

ASK_SYSTEM_PROMPT = """\
You are a senior engineer mentoring a junior developer who has questions about \
a live codebase. Your job is to EXPLAIN — not to build plans.

TONE
────
Friendly, direct, authoritative. Think of a trusted senior teammate answering \
a Slack message — not writing a design doc. Use first-person ("I" / "you") \
naturally. Avoid corporate jargon or excessive hedging.

WHAT GREAT ANSWERS LOOK LIKE
─────────────────────────────
1. Open with a direct, clear answer in 1–2 sentences. No preamble.
2. Walk through how it actually works, citing the real code:
   • Use backtick paths like `src/pipeline/pipeline.py` (lines 42–80) inline.
   • When walking through a flow, trace it file by file so the reader
     can follow along in their editor.
3. Use code snippets (fenced blocks) when showing the relevant lines helps
   more than prose alone.
4. Use analogies or plain-English summaries for abstract concepts.
5. Close with 2–3 specific follow-up questions the junior might want
   to ask next — make them concrete and grounded in the codebase.

GROUNDING RULES
───────────────
• Only reference files, functions, classes, and line ranges that appear
  in the provided codebase context. Never invent paths or symbols.
• If you cite a symbol, spell it exactly as it appears in the context.
• If the context is insufficient to fully answer, be honest about what
  is missing and offer to help the user search for it.
• Do NOT reproduce the grounding-warning text verbatim in your answer —
  just respect the constraints it describes.

OUTPUT FORMAT
─────────────
Use the `answer_question` tool. Fields:
  answer         — full markdown answer (prose + code blocks + inline citations)
  cited_files    — list of "path:line_range" strings for every file cited
  follow_up_hints — 2–3 natural follow-up questions (strings, not bullet points)
"""

# ── Tool schema ────────────────────────────────────────────────────────────────

ASK_ANSWER_TOOL = {
    "name": "answer_question",
    "description": (
        "Answer a developer's question about the codebase. "
        "The answer must be conversational markdown with inline code citations. "
        "Always cite real file paths and line numbers from the provided context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": (
                    "Full markdown answer. Use inline citations like "
                    "`src/foo/bar.py` (lines 12–30). Include fenced code "
                    "blocks for key snippets. Mentor tone — clear and direct."
                ),
            },
            "cited_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Every file path cited in the answer, with line range, "
                    "e.g. 'src/pipeline/pipeline.py:42-80'. One entry per "
                    "distinct file+range pair."
                ),
            },
            "follow_up_hints": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "2–3 natural follow-up questions the developer might want "
                    "to ask next, grounded in the codebase context. "
                    "Concrete questions, not generic suggestions."
                ),
            },
        },
        "required": ["answer", "cited_files", "follow_up_hints"],
    },
}

# ── Ask response dataclass ─────────────────────────────────────────────────────


class AskResult:
    """Parsed result from the ask agent."""

    __slots__ = ("answer", "cited_files", "follow_up_hints", "elapsed_ms")

    def __init__(
        self,
        answer: str,
        cited_files: list[str],
        follow_up_hints: list[str],
        elapsed_ms: float,
    ):
        self.answer = answer
        self.cited_files = cited_files
        self.follow_up_hints = follow_up_hints
        self.elapsed_ms = elapsed_ms

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "cited_files": self.cited_files,
            "follow_up_hints": self.follow_up_hints,
            "elapsed_ms": self.elapsed_ms,
        }


# ── User message builder ───────────────────────────────────────────────────────


def _build_ask_user_message(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    """
    Build the user-turn message for Ask Mode.

    Includes the same rich code context as the planning pipeline, but the
    instruction section tells Claude to answer conversationally — not plan.
    """
    repo_scope = f"{repo_owner}/{repo_name}" if (repo_owner and repo_name) else "all indexed repos"
    parts: list[str] = [
        f"## Question\n{query}",
        f"## Scope\nRepository: {repo_scope}",
    ]

    # Query complexity (informational only — no planning protocol)
    if ctx.sub_queries and len(ctx.sub_queries) > 1:
        meta = "## Query Decomposition\nThis question touches multiple concerns:\n"
        for i, sq in enumerate(ctx.sub_queries, 1):
            meta += f"  {i}. {sq}\n"
        parts.append(meta)

    # Grounding warnings (critical)
    if ctx.grounding_warnings:
        warning_block = "## ⚠ Grounding Warnings\n"
        warning_block += "The retrieval system found these gaps — respect them:\n\n"
        for w in ctx.grounding_warnings:
            warning_block += f"- {w}\n"
        parts.append(warning_block)

    # Primary code context (same tiers as planning)
    if ctx.component_context:
        parts.append(ctx.component_context)

    if ctx.primary_context:
        parts.append(f"## Relevant Code Context\n{ctx.primary_context}")

    if ctx.file_maps:
        parts.append(f"## File Structure\n{ctx.file_maps}")

    if ctx.dependency_context:
        parts.append(ctx.dependency_context)

    if ctx.caller_contexts:
        parts.append(f"## Known Callers\n{ctx.caller_contexts}")

    if ctx.expansion_context:
        parts.append(f"## Additional Context\n{ctx.expansion_context}")

    # Ask-specific instructions (no planning protocol, no architectural reasoning)
    parts.append(
        "## Instructions\n"
        "Answer the developer's question directly and conversationally.\n"
        "Walk through the relevant code from the context above, citing "
        "file paths and line numbers inline. Use the `answer_question` tool.\n\n"
        "Remember:\n"
        "- Lead with the direct answer.\n"
        "- Cite real files and symbols — only what appears in the context above.\n"
        "- Close with 2–3 concrete follow-up questions."
    )

    return "\n\n".join(parts)


# ── Retry helpers (imported from claude_planner to avoid duplication) ──────────


def _get_retry_helpers():
    from src.planning.claude_planner import (
        RateLimitOrOverloadError,
        _MAX_RETRIES,
        _RETRYABLE_STATUS_CODES,
        _get_retry_after,
    )

    return _MAX_RETRIES, _RETRYABLE_STATUS_CODES, _get_retry_after, RateLimitOrOverloadError


# ── Build call params ──────────────────────────────────────────────────────────


def _build_call_params(user_message: str) -> dict:
    return {
        "model": settings.anthropic_model,
        "max_tokens": 4096,  # Ask needs less than planning — no JSON plans
        "system": ASK_SYSTEM_PROMPT,
        "tools": [ASK_ANSWER_TOOL],
        "tool_choice": {"type": "tool", "name": "answer_question"},  # forced — no ambiguity
        "messages": [{"role": "user", "content": user_message}],
    }


# ── Parse response ─────────────────────────────────────────────────────────────


def _parse_tool_response(message, elapsed_ms: float) -> AskResult:
    tool_block = next(
        (b for b in message.content if b.type == "tool_use"),
        None,
    )

    if tool_block is None:
        # Fallback: Claude responded in text
        text = " ".join(
            b.text for b in message.content if hasattr(b, "text") and b.text
        ).strip()
        return AskResult(
            answer=text or "_No answer generated._",
            cited_files=[],
            follow_up_hints=[],
            elapsed_ms=elapsed_ms,
        )

    data = tool_block.input
    return AskResult(
        answer=data.get("answer", "_No answer generated._"),
        cited_files=data.get("cited_files", []),
        follow_up_hints=data.get("follow_up_hints", []),
        elapsed_ms=elapsed_ms,
    )


# ── Public generate (non-streaming) ───────────────────────────────────────────


async def generate_answer(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> AskResult:
    """
    Call Claude with the Ask Mode prompt and return an AskResult.
    No thinking budget — Ask Mode prioritises speed and conversational flow.
    """
    import anthropic

    _MAX_RETRIES, _RETRYABLE_STATUS_CODES, _get_retry_after, RateLimitOrOverloadError = (
        _get_retry_helpers()
    )

    client = _get_client()
    user_message = _build_ask_user_message(query, ctx, repo_owner, repo_name)
    call_params = _build_call_params(user_message)
    t0 = time.monotonic()

    async with _ask_semaphore:
        logger.info("ask: acquired semaphore, calling Claude…")
        last_exc = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                message = await client.messages.create(**call_params)
                break
            except anthropic.APIStatusError as exc:
                if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    retry_after = _get_retry_after(exc)
                    if retry_after:
                        wait = min(retry_after, 120)
                    elif exc.status_code == 429:
                        wait = min(5 * (2**attempt), 120)
                    else:
                        wait = 2**attempt
                    logger.warning(
                        "ask: Anthropic API %s, retry %d/%d in %.0fs",
                        exc.status_code,
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                    )
                    last_exc = exc
                    await _asyncio.sleep(wait)
                else:
                    raise
        else:
            raise RateLimitOrOverloadError(last_exc)

    elapsed_ms = (time.monotonic() - t0) * 1000
    logger.info("ask: Claude responded in %.0fms", elapsed_ms)
    return _parse_tool_response(message, elapsed_ms)


# ── Public stream generator ────────────────────────────────────────────────────


async def stream_generate_answer(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> AsyncIterator[dict]:
    """
    Stream answer tokens from Claude.

    Yields:
      {"type": "token",           "text": "<chunk>"}
          Plain text / partial-JSON fragments from Claude.

      {"type": "answer_complete", "result": AskResult}
          Fired once when the full response is received and parsed.
    """
    import anthropic

    _MAX_RETRIES, _RETRYABLE_STATUS_CODES, _get_retry_after, RateLimitOrOverloadError = (
        _get_retry_helpers()
    )

    client = _get_client()
    user_message = _build_ask_user_message(query, ctx, repo_owner, repo_name)
    call_params = _build_call_params(user_message)
    t0 = time.monotonic()

    async with _ask_semaphore:
        logger.info("ask: stream acquired semaphore, calling Claude…")
        last_exc = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with client.messages.stream(**call_params) as stream:
                    async for event in stream:
                        event_type = getattr(event, "type", None)

                        # answer_question tool streams via input_json deltas
                        if event_type == "input_json":
                            partial = getattr(event, "partial_json", None)
                            if partial:
                                yield {"type": "token", "text": partial}

                        # Plain text fallback
                        elif event_type == "text":
                            text = getattr(event, "text", None)
                            if text:
                                yield {"type": "token", "text": text}

                    final_msg = await stream.get_final_message()

                elapsed_ms = (time.monotonic() - t0) * 1000
                logger.info("ask: stream complete in %.0fms", elapsed_ms)
                result = _parse_tool_response(final_msg, elapsed_ms)
                yield {"type": "answer_complete", "result": result}
                return

            except anthropic.APIStatusError as exc:
                if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    retry_after = _get_retry_after(exc)
                    if retry_after:
                        wait = min(retry_after, 120)
                    elif exc.status_code == 429:
                        wait = min(5 * (2**attempt), 120)
                    else:
                        wait = 2**attempt
                    logger.warning(
                        "ask: stream API %s, retry %d/%d in %.0fs",
                        exc.status_code,
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                    )
                    last_exc = exc
                    await _asyncio.sleep(wait)
                else:
                    raise

        raise RateLimitOrOverloadError(last_exc)
