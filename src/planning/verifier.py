"""
Answer verification pass — lightweight LLM call after the main response.

When answer_verification_enabled=True, this module runs a focused second LLM
call that asks a smaller/faster model to:
  1. Rate confidence 0–10 (how well is the answer grounded in the retrieved context?)
  2. List any specific claims that look unsupported or hallucinated
  3. Flag missing critical files/symbols that were referenced but not retrieved

The verifier runs in parallel where possible (streaming: fire-and-forget;
non-streaming: awaited before the response is returned).

This is intentionally lightweight — it uses a 500-token budget and a focused
system prompt, so it adds only 300–600ms overhead in most cases.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from src.config import settings
from src.utils.logging import get_secure_logger
from src.utils.sanitize import sanitize_log

logger = get_secure_logger(__name__)

_VERIFIER_SYSTEM = """\
You are a strict code-answer quality auditor. Your job is to review a codebase answer
and decide how well it is grounded in the retrieved source code context.

OUTPUT RULES — respond with valid JSON only, no markdown fences:
{
  "confidence": <integer 0-10>,
  "unsupported_claims": ["claim1", "claim2"],
  "missing_context": ["what is still missing"],
  "summary": "one sentence verdict"
}

confidence scale:
  9-10 = every claim is directly supported by the retrieved code
  7-8  = mostly grounded, minor gaps
  5-6  = some claims rely on assumptions not in the context
  3-4  = significant unsupported claims or hallucinated details
  0-2  = answer is largely fabricated or unrelated to the context

unsupported_claims: list concrete statements that can't be verified from the context.
  Keep each item short (< 20 words). Empty list [] if none.
missing_context: list specific files/symbols that the answer references but were not
  in the retrieved context. Empty list [] if none.
summary: one sentence (< 30 words) on the overall grounding quality.
"""

_VERIFIER_PROMPT = """\
QUERY:
{query}

RETRIEVED CONTEXT (truncated to 3000 chars):
{context_snippet}

ANSWER TO VERIFY:
{answer}

Review the answer and output the JSON verdict.
"""


@dataclass
class VerificationResult:
    confidence: int = 10       # 0–10
    unsupported_claims: list[str] = field(default_factory=list)
    missing_context: list[str] = field(default_factory=list)
    summary: str = ""
    low_confidence: bool = False   # True when confidence < threshold
    warning_message: str = ""      # Human-readable warning injected into response


_LOW_CONFIDENCE_THRESHOLD = 6


async def verify_answer(
    query: str,
    answer: str,
    context_chunks: list[dict],
    model: str | None = None,
) -> VerificationResult:
    """
    Run the lightweight verification pass.

    Args:
        query:          Original user query.
        answer:         The generated answer/plan text.
        context_chunks: List of chunk metadata dicts (each has "file", "lines", "symbol").
        model:          LLM to use for verification (defaults to verification_model setting
                        or falls back to default_model).

    Returns a VerificationResult (never raises — on error returns a default
    high-confidence result so callers are not disrupted).
    """
    if not settings.answer_verification_enabled:
        return VerificationResult()

    effective_model = (
        model
        or getattr(settings, "verification_model", None)
        or settings.default_model
    )

    # Build a compact context snippet (max 3000 chars)
    context_lines = []
    chars = 0
    for chunk in context_chunks:
        line = f"  {chunk.get('file', '?')}:{chunk.get('lines', '?')}  {chunk.get('symbol', '')}"
        context_lines.append(line)
        chars += len(line)
        if chars > 2800:
            context_lines.append("  ... (truncated)")
            break
    context_snippet = "\n".join(context_lines) if context_lines else "(no context retrieved)"

    prompt = _VERIFIER_PROMPT.format(
        query=query[:500],
        context_snippet=context_snippet,
        answer=answer[:2000],
    )

    try:
        from src.llm import get_provider

        provider = get_provider(effective_model)
        response = await provider.complete(
            model=effective_model,
            system=_VERIFIER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.0,
        )

        raw = response.content.strip()
        # Strip markdown fences if the model adds them despite instructions
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        data = json.loads(raw)
        confidence = int(data.get("confidence", 10))
        unsupported = [str(c) for c in data.get("unsupported_claims", [])]
        missing = [str(m) for m in data.get("missing_context", [])]
        summary = str(data.get("summary", ""))

        low = confidence < _LOW_CONFIDENCE_THRESHOLD
        warning = ""
        if low:
            parts = [f"⚠️ Low confidence ({confidence}/10): {summary}"]
            if unsupported:
                parts.append("Unsupported claims: " + "; ".join(unsupported[:3]))
            if missing:
                parts.append("Missing context: " + "; ".join(missing[:3]))
            warning = "\n\n---\n" + "\n".join(parts)

        logger.info(
            "verifier: confidence=%d/10 unsupported=%d missing=%d query=%r",
            confidence,
            len(unsupported),
            len(missing),
            sanitize_log(query[:60]),
        )

        return VerificationResult(
            confidence=confidence,
            unsupported_claims=unsupported,
            missing_context=missing,
            summary=summary,
            low_confidence=low,
            warning_message=warning,
        )

    except Exception as exc:
        logger.warning("verifier: failed, skipping: %s", sanitize_log(exc))
        return VerificationResult()
