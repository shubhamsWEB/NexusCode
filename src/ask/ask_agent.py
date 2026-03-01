"""
Ask Mode LLM agent.

Answers natural-language questions about the codebase in a mentor tone:
clear, direct, grounded in real code, with concrete citations.

Deliberately uses a DIFFERENT system prompt from the planning module:
  - Planning: "every token must be actionable", structured JSON tool output
  - Ask:      conversational markdown prose, inline code citations, mentor voice

Uses the provider abstraction layer (src.llm) to support multiple LLM backends
with per-request model selection.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from src.config import settings
from src.llm import get_provider
from src.llm.tool_converter import from_anthropic_schema
from src.llm.types import LLMResponse, LLMStreamEvent
from src.planning.retriever import PlanningContext
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────────

ASK_SYSTEM_PROMPT = """\
You are a senior engineer mentoring a junior developer who has questions about \
a live codebase. Your job is to EXPLAIN - not to build plans.

TONE
────
Friendly, direct, authoritative. Think of a trusted senior teammate answering \
a Slack message - not writing a design doc. Use first-person ("I" / "you") \
naturally. Avoid corporate jargon or excessive hedging.

WHAT GREAT ANSWERS LOOK LIKE
─────────────────────────────
1. Open with a direct, clear answer in 1-2 sentences. No preamble.
2. Walk through how it actually works, citing the real code:
   • Use backtick paths like `src/pipeline/pipeline.py` (lines 42-80) inline.
   • When walking through a flow, trace it file by file so the reader
     can follow along in their editor.
3. Use code snippets (fenced blocks) when showing the relevant lines helps
   more than prose alone.
4. Use analogies or plain-English summaries for abstract concepts.
5. Close with 2-3 specific follow-up questions the junior might want
   to ask next - make them concrete and grounded in the codebase.

GROUNDING RULES
───────────────
• Only reference files, functions, classes, and line ranges that appear
  in the provided codebase context. Never invent paths or symbols.
• If you cite a symbol, spell it exactly as it appears in the context.
• Do NOT reproduce the grounding-warning text verbatim in your answer -
  just respect the constraints it describes.

HARD CONSTRAINT — NEVER USE PRETRAINING KNOWLEDGE
──────────────────────────────────────────────────
If the ⚠ Grounding Warnings or 🚨 CRITICAL block in the user message
contains MISSING_PATH, MISSING_SYMBOL, or NO_RESULTS warnings, you MUST:
  1. Tell the user exactly which files or symbols are NOT in the index.
  2. Explain that those files need to be indexed first.
  3. DO NOT attempt to answer the question from your pretraining knowledge.
     This includes phrases like "I can still walk you through…",
     "Based on general knowledge…", or "Typically, this works by…".
  4. Suggest the user check registered repos (GET /repos) and trigger
     indexing for the missing repository.
This is a hard constraint — not a suggestion. An answer that fills in
missing context from pretraining is worse than no answer at all because
it will be wrong about the specific codebase the user is asking about.

OUTPUT FORMAT
─────────────
Use the `answer_question` tool. Fields:
  answer         - full markdown answer (prose + code blocks + inline citations)
  cited_files    - list of "path:line_range" strings for every file cited
  follow_up_hints - 2-3 natural follow-up questions (strings, not bullet points)
"""

# ── Tool schema ────────────────────────────────────────────────────────────────

_ASK_ANSWER_TOOL_DICT = {
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
                    "`src/foo/bar.py` (lines 12-30). Include fenced code "
                    "blocks for key snippets. Mentor tone - clear and direct."
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
                    "2-3 natural follow-up questions the developer might want "
                    "to ask next, grounded in the codebase context. "
                    "Concrete questions, not generic suggestions."
                ),
            },
        },
        "required": ["answer", "cited_files", "follow_up_hints"],
    },
}

# Keep the original dict exported for backward compat
ASK_ANSWER_TOOL = _ASK_ANSWER_TOOL_DICT

# Convert to provider-agnostic schema
_TOOLS = [from_anthropic_schema(_ASK_ANSWER_TOOL_DICT)]

# ── Ask response dataclass ─────────────────────────────────────────────────────


class AskResult:
    """Parsed result from the ask agent."""

    __slots__ = ("answer", "cited_files", "elapsed_ms", "follow_up_hints", "quality_score")

    def __init__(
        self,
        answer: str,
        cited_files: list[str],
        follow_up_hints: list[str],
        elapsed_ms: float,
        quality_score: float = 0.0,
    ):
        self.answer = answer
        self.cited_files = cited_files
        self.follow_up_hints = follow_up_hints
        self.elapsed_ms = elapsed_ms
        self.quality_score = quality_score

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "cited_files": self.cited_files,
            "follow_up_hints": self.follow_up_hints,
            "elapsed_ms": self.elapsed_ms,
            "quality_score": self.quality_score,
        }


# ── User message builder ───────────────────────────────────────────────────────


def _build_ask_user_message(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    repo_scope = f"{repo_owner}/{repo_name}" if (repo_owner and repo_name) else "all indexed repos"
    parts: list[str] = [
        f"## Question\n{query}",
        f"## Scope\nRepository: {repo_scope}",
    ]

    # Enumerate the exact file paths present in the retrieved context.
    # This prevents the LLM from citing paths it invented (e.g. "doc.md" when
    # the real paths are "doc/README.md") or files not in the index at all.
    if ctx.chunks_used:
        files_in_ctx = sorted({c["file"] for c in ctx.chunks_used})
        file_list = "\n".join(f"- `{f}`" for f in files_in_ctx)
        parts.append(
            "## Files Available in Retrieved Context\n"
            "You MUST cite ONLY the exact paths listed below. "
            "Do NOT reference any other file path, even if you know it from training data:\n"
            f"{file_list}"
        )

    if ctx.sub_queries and len(ctx.sub_queries) > 1:
        meta = "## Query Decomposition\nThis question touches multiple concerns:\n"
        for i, sq in enumerate(ctx.sub_queries, 1):
            meta += f"  {i}. {sq}\n"
        parts.append(meta)

    if ctx.grounding_warnings:
        # Separate critical (missing indexed content) from advisory warnings
        critical_types = ("MISSING_PATH:", "MISSING_SYMBOL:", "NO_RESULTS:")
        critical_warnings = [w for w in ctx.grounding_warnings if w.startswith(critical_types)]
        advisory_warnings = [w for w in ctx.grounding_warnings if not w.startswith(critical_types)]

        if critical_warnings:
            critical_block = "## 🚨 CRITICAL: REQUIRED CONTENT IS NOT INDEXED\n"
            critical_block += (
                "The following files/symbols were asked about but are NOT in the index. "
                "You MUST NOT answer from pretraining knowledge. "
                "Your entire response must be to explain what is missing and how to index it:\n\n"
            )
            for w in critical_warnings:
                critical_block += f"- {w}\n"
            critical_block += (
                "\nRequired action: Tell the user which repo/files to index. "
                "Do NOT provide any code walkthrough, explanation, or description "
                "of how the missing code works."
            )
            parts.append(critical_block)

        if advisory_warnings:
            warning_block = "## ⚠ Grounding Warnings\n"
            warning_block += "The retrieval system found these gaps - respect them:\n\n"
            for w in advisory_warnings:
                warning_block += f"- {w}\n"
            parts.append(warning_block)

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

    parts.append(
        "## Instructions\n"
        "Answer the developer's question directly and conversationally.\n"
        "Walk through the relevant code from the context above, citing "
        "file paths and line numbers inline. Use the `answer_question` tool.\n\n"
        "Remember:\n"
        "- Lead with the direct answer.\n"
        "- Cite real files and symbols - only what appears in the context above.\n"
        "- Close with 2-3 concrete follow-up questions."
    )

    return "\n\n".join(parts)


# ── Parse response ─────────────────────────────────────────────────────────────


def _parse_tool_response(
    response: LLMResponse,
    elapsed_ms: float,
    quality_score: float = 0.0,
) -> AskResult:
    if not response.tool_calls:
        # Fallback: LLM responded in text
        return AskResult(
            answer=response.text_content or "_No answer generated._",
            cited_files=[],
            follow_up_hints=[],
            elapsed_ms=elapsed_ms,
            quality_score=quality_score,
        )

    data = response.tool_calls[0].input
    return AskResult(
        answer=data.get("answer", "_No answer generated._"),
        cited_files=data.get("cited_files", []),
        follow_up_hints=data.get("follow_up_hints", []),
        elapsed_ms=elapsed_ms,
        quality_score=quality_score,
    )


# ── Public generate (non-streaming) ───────────────────────────────────────────


async def generate_answer(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    model: str | None = None,
) -> AskResult:
    """
    Call the LLM with the Ask Mode prompt and return an AskResult.
    No thinking budget - Ask Mode prioritises speed and conversational flow.
    """
    effective_model = model or settings.default_model
    provider = get_provider(effective_model)

    user_message = _build_ask_user_message(query, ctx, repo_owner, repo_name)
    t0 = time.monotonic()

    response = await provider.generate(
        model=effective_model,
        system=ASK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        tools=_TOOLS,
        tool_choice={"name": "answer_question"},
        max_tokens=4096,
        thinking_budget=0,
        temperature=0,
    )

    elapsed_ms = (time.monotonic() - t0) * 1000
    logger.info("ask: %s responded in %.0fms", effective_model, elapsed_ms)
    return _parse_tool_response(response, elapsed_ms, quality_score=ctx.quality_score)


# ── Public stream generator ────────────────────────────────────────────────────


async def stream_generate_answer(
    query: str,
    ctx: PlanningContext,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    model: str | None = None,
) -> AsyncIterator[dict]:
    """
    Stream answer tokens from the LLM.

    Yields:
      {"type": "token",           "text": "<chunk>"}
      {"type": "answer_complete", "result": AskResult}
    """
    effective_model = model or settings.default_model
    provider = get_provider(effective_model)

    user_message = _build_ask_user_message(query, ctx, repo_owner, repo_name)
    t0 = time.monotonic()

    async for event in provider.stream(
        model=effective_model,
        system=ASK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        tools=_TOOLS,
        tool_choice={"name": "answer_question"},
        max_tokens=4096,
        thinking_budget=0,
        temperature=0,
    ):
        if isinstance(event, LLMStreamEvent):
            if event.type in ("text", "input_json"):
                yield {"type": "token", "text": event.text}
        elif isinstance(event, LLMResponse):
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.info("ask: stream complete in %.0fms", elapsed_ms)
            result = _parse_tool_response(event, elapsed_ms, quality_score=ctx.quality_score)
            yield {"type": "answer_complete", "result": result}
