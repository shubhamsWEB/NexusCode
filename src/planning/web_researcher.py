"""
Web research phase for implementation planning.

Uses Anthropic's built-in web_search_20250305 server tool to research
best practices, library recommendations, and implementation patterns for
the user's query BEFORE the code-grounded plan is generated.

This runs in parallel with the codebase retrieval phases so it adds
minimal latency (~0 extra wall-clock time on the happy path).

Returns a markdown string injected into the planning prompt as
"## Web Research Notes".  Falls back to empty string on any failure
so planning always works even when web search is unavailable.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

# ── System prompt for the research call ──────────────────────────────────────

_RESEARCH_SYSTEM = """\
You are a technical research assistant helping a software engineer plan a \
code change. Your job is to search the web and return concise, actionable \
research notes that will help generate a grounded implementation plan.

Search for:
1. The best library or built-in mechanism for this task in the relevant language/framework
2. Key implementation steps or patterns (2025 best practices)
3. Security, performance, or correctness pitfalls to watch for
4. Links to the most relevant official documentation

Rules:
- Be concise — max 400 words total
- Use markdown with short sections
- Prefer official docs and reputable sources (GitHub READMEs, framework docs)
- If the task is straightforward with no external libraries needed, say so briefly
- Do NOT write any implementation code — the plan generator handles that
- Start your response with "## Web Research Notes"
"""


async def research_implementation(query: str) -> str:
    """
    Call Claude with the web_search_20250305 server tool to gather
    implementation research for `query`.

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

    user_content = (
        f"Research the best approach to implement the following in a "
        f"Python/FastAPI codebase:\n\n{query}\n\n"
        f"Focus on library recommendations, key steps, and gotchas."
    )

    import asyncio
    loop = asyncio.get_event_loop()

    def _call():
        return client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
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
        return _extract_text(response)
    except Exception as exc:
        # Graceful degradation — web search is a bonus, not required
        logger.warning(
            "web_researcher: search failed (planning continues without it): %s", exc
        )
        return ""


def _extract_text(response) -> str:
    """Extract all text blocks from a Claude response into a single string."""
    parts: list[str] = []
    for block in response.content:
        # text blocks: direct text response
        if hasattr(block, "text") and block.text:
            parts.append(block.text)
        # tool_result blocks from web_search may contain nested content
        elif hasattr(block, "type") and block.type == "tool_result":
            if hasattr(block, "content"):
                for sub in block.content or []:
                    if hasattr(sub, "text") and sub.text:
                        parts.append(sub.text)
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    # Ensure it starts with the expected header
    if text and not text.startswith("## Web Research"):
        text = "## Web Research Notes\n\n" + text
    return text
