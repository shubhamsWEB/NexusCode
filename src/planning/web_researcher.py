"""
Stack-aware web research phase for implementation planning.

The key insight: web research is ONLY useful when it knows what the codebase
already has installed.  Without that knowledge it returns generic tutorials
("here's how to add rate limiting to FastAPI") that compete with the codebase
plan rather than complementing it.

Flow:
  1. Phase 0a of the retriever extracts a stack fingerprint (installed packages,
     language, framework, active imports) and passes it here.
  2. This module searches the web with that context, asking:
       "Given you already have X, Y, Z — what's the best integration pattern
        for this task?  What's missing?  Any version-specific gotchas?"
  3. The result is a gap-analysis document injected into the planner prompt
     ALONGSIDE the codebase context — not as a competing standalone how-to.

Falls back gracefully to "" on any failure so planning always continues.
"""

from __future__ import annotations

import logging

from src.config import settings

logger = logging.getLogger(__name__)


# ── System prompt — gap-analysis mode, NOT tutorial mode ─────────────────────

_RESEARCH_SYSTEM = """\
You are a senior software architect acting as a stack-aware integration advisor.

You will receive:
  1. The codebase's STACK FINGERPRINT — exact packages already installed, \
language, framework, actively-used imports.
  2. The task the engineer needs to implement.

Your ONLY job is to answer these questions (do NOT write a tutorial):
  A. Given the existing stack, what is the right integration pattern for this task?
     (Use what's already there — do not reinvent.)
  B. Which packages needed for this task are ALREADY installed in the stack?
     Which are genuinely missing and would need to be added?
  C. Are there version-specific gotchas, breaking changes, or compatibility \
issues between the detected package versions and this task?
  D. What security or performance pitfalls are specific to THIS stack + task combo?
  E. Are there existing utilities or abstractions in the stack that the engineer \
might miss (e.g., already has a rate-limiting middleware, already has a JWT library)?

Rules:
  - Start every answer by listing what ALREADY EXISTS in the stack that is \
relevant to this task.
  - Only suggest a new package if nothing in the existing stack can handle the task.
  - If a new package IS needed, name it explicitly and explain why the existing \
ones can't cover it.
  - Be concise — max 450 words.
  - Use these exact markdown sections:
      ## Already in Stack (relevant to this task)
      ## Gaps & What to Add (if any)
      ## Integration Pattern
      ## Stack-Specific Gotchas
  - Do NOT explain the task back to the engineer.
  - Do NOT write code or pseudocode — the plan generator handles that.
  - Do NOT suggest architectural changes unrelated to the task.
"""


async def research_implementation(query: str, stack_context: str = "") -> str:
    """
    Call Claude with web_search_20250305 to gather stack-aware implementation
    research for `query`.

    `stack_context` is the codebase stack fingerprint from Phase 0a.
    When provided, the web search focuses on gaps and integration patterns
    rather than explaining the task from scratch.

    Returns a markdown string or "" on any failure.
    Designed to run as an asyncio background task alongside codebase retrieval.
    """
    if not settings.anthropic_api_key:
        logger.debug("web_researcher: skipping (no ANTHROPIC_API_KEY)")
        return ""

    try:
        import anthropic
    except ImportError:
        logger.debug("web_researcher: skipping (anthropic not installed)")
        return ""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # ── Build a stack-aware user message ─────────────────────────────────────
    if stack_context:
        user_content = (
            f"{stack_context}\n\n"
            f"---\n\n"
            f"## Task to Implement\n{query}\n\n"
            f"Search for: given the stack above, what already handles this task, "
            f"what's missing, and what are the integration gotchas? "
            f"Do NOT explain what the task is — focus on what the engineer needs "
            f"to know that is NOT already obvious from the stack."
        )
    else:
        # Fallback: no stack context available — ask for the best approach
        # but note we don't know the stack
        user_content = (
            f"## Task to Implement\n{query}\n\n"
            f"Note: No codebase stack fingerprint is available. "
            f"Search for the best approach and common integration patterns "
            f"in modern Python/FastAPI codebases (2025 best practices). "
            f"List what package you'd recommend and why."
        )

    import asyncio

    loop = asyncio.get_event_loop()

    def _call():
        return client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1200,
            system=_RESEARCH_SYSTEM,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 3,
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )

    try:
        response = await loop.run_in_executor(None, _call)
        notes = _extract_text(response)
        if notes:
            had_stack = bool(stack_context)
            logger.info(
                "web_researcher: done — %d chars, stack_context=%s",
                len(notes),
                had_stack,
            )
        return notes
    except Exception as exc:
        # Graceful degradation — web search is enrichment, not required
        logger.warning("web_researcher: search failed (planning continues without it): %s", exc)
        return ""


def _extract_text(response) -> str:
    """Extract all text blocks from a Claude response into a single string."""
    parts: list[str] = []
    for block in response.content:
        if hasattr(block, "text") and block.text:
            parts.append(block.text)
        elif hasattr(block, "type") and block.type == "tool_result":
            if hasattr(block, "content"):
                for sub in block.content or []:
                    if hasattr(sub, "text") and sub.text:
                        parts.append(sub.text)
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    # Ensure it starts with the expected section header
    if text and not text.lstrip().startswith("## Already in Stack"):
        # If Claude didn't follow the format, wrap it
        text = "## Stack Integration Analysis\n\n" + text
    return text
