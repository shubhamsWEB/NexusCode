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

import importlib.util

from src.config import settings
from src.utils.logging import get_secure_logger
from src.utils.sanitize import sanitize_log

logger = get_secure_logger(__name__)

_anthropic_client = None
_openai_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        if importlib.util.find_spec("anthropic") is None:
            return None
        import anthropic

        if not settings.anthropic_api_key:
            return None
        _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        if importlib.util.find_spec("openai") is None:
            return None
        import openai

        if not settings.openai_api_key:
            return None
        _openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


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


async def research_implementation(
    query: str, stack_context: str = "", model: str | None = None
) -> str:
    """
    Gather stack-aware implementation research for `query` using GPT-4o ("GPT-5")
    as the primary engine and falling back to Anthropic.

    `stack_context` is the codebase stack fingerprint from Phase 0a.
    Returns a markdown string or "" on any failure.
    Designed to run as an asyncio background task alongside codebase retrieval.
    """
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

    # 1. Try OpenAI (GPT-4o) First
    openai_client = _get_openai_client()
    if openai_client:
        try:
            logger.info("web_researcher: trying primary model (gpt-4o)")
            response = await openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": _RESEARCH_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
            )
            notes = response.choices[0].message.content or ""
            if notes:
                had_stack = bool(stack_context)
                logger.info(
                    "web_researcher: done (openai) — %d chars, stack_context=%s",
                    len(notes),
                    had_stack,
                )
                return _ensure_format(notes)
        except Exception as exc:
            logger.warning(
                "web_researcher: openai failed, falling back to anthropic: %s", sanitize_log(exc)
            )

    # 2. Try Anthropic as fallback
    anthropic_client = _get_anthropic_client()
    if anthropic_client:
        try:
            logger.info("web_researcher: trying fallback model (anthropic web_search_20250305)")
            response = await anthropic_client.messages.create(
                model=settings.default_model,
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
            notes = _extract_text_anthropic(response)
            if notes:
                had_stack = bool(stack_context)
                logger.info(
                    "web_researcher: done (anthropic fallback) — %d chars, stack_context=%s",
                    len(notes),
                    had_stack,
                )
                return _ensure_format(notes)
        except Exception as exc:
            logger.warning("web_researcher: anthropic failed: %s", sanitize_log(exc))

    return ""


def _ensure_format(text: str) -> str:
    """Ensure it starts with the expected section header."""
    if text and not text.lstrip().startswith("## Already in Stack"):
        text = "## Stack Integration Analysis\n\n" + text
    return text


def _extract_text_anthropic(response) -> str:
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
    return "\n\n".join(p.strip() for p in parts if p.strip())
