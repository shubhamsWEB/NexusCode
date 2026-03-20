"""
Query relevance gate — fast pre-flight check before the agent loop.

Embeds the query, runs a top-3 semantic search, and returns a relevance
verdict based on the best cosine similarity score. If the best score is
below the configured threshold, the query is considered unrelated to the
indexed codebase and the caller should skip the full agent loop.

Two-phase logic:
  score < threshold             → out_of_scope (no LLM, no tool calls)
  threshold <= score < soft     → ambiguous  (proceed but flag)
  score >= soft_threshold       → relevant   (full agent loop)

Empty index (no repos indexed) is handled separately and returns a
distinct RelevanceResult.reason = "no_index".
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import settings
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)


@dataclass
class RelevanceResult:
    is_relevant: bool
    best_score: float
    reason: str  # "relevant" | "out_of_scope" | "no_index" | "ambiguous"
    top_file: str | None = None  # best matching file path (for debugging)
    query_complexity: str = "default"  # "simple" | "moderate" | "complex" | "default"


# ── Query complexity heuristic ────────────────────────────────────────────────

_COMPLEX_KW = frozenset({
    "how", "why", "explain", "trace", "flow", "all", "every",
    "end-to-end", "across", "compare", "difference", "relationship",
    "architecture", "dependencies", "integration", "pipeline",
})


def _detect_relevance_complexity(query: str) -> str:
    """
    Quick heuristic to classify query complexity for adaptive threshold selection.

    Returns "simple", "moderate", or "complex".

    Simple:  short, highly specific (symbol lookup, direct file question)
    Complex: long, multi-part, or uses natural-language framing that scores
             lower against code embeddings even for valid codebase queries
    """
    q = query.strip()
    word_count = len(q.split())
    if len(q) < 40 and word_count < 5 and "?" not in q:
        return "simple"
    q_lower = q.lower()
    if (
        len(q) > 150
        or q.count("?") > 1
        or any(kw in q_lower for kw in _COMPLEX_KW)
    ):
        return "complex"
    return "moderate"


async def check_query_relevance(
    query: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> RelevanceResult:
    """
    Fast relevance pre-flight: single embedding + single DB query.
    No LLM call, no reranking. Typical latency: 50-150ms (embedding cached).

    Uses adaptive per-complexity thresholds:
    - Simple queries get a stricter threshold (less forgiving of weak matches)
    - Complex / natural-language queries get a looser threshold (they naturally
      score lower against code embeddings even when clearly codebase-related)

    Returns a RelevanceResult with is_relevant=False when the query should
    be short-circuited without running the full agent loop.
    """
    from src.retrieval.searcher import _semantic_search, embed_query

    complexity = _detect_relevance_complexity(query)
    if complexity == "simple":
        threshold = settings.query_relevance_threshold_simple
        soft_threshold = max(threshold + 0.10, settings.query_relevance_soft_threshold)
    elif complexity == "complex":
        threshold = settings.query_relevance_threshold_complex
        soft_threshold = max(threshold + 0.10, settings.query_relevance_soft_threshold * 0.6)
    else:
        threshold = settings.query_relevance_threshold_moderate
        soft_threshold = settings.query_relevance_soft_threshold

    logger.debug(
        "relevance_gate: complexity=%s threshold=%.2f soft=%.2f",
        complexity, threshold, soft_threshold,
    )

    try:
        query_vector = await embed_query(query)
    except Exception as exc:
        # Embedding failure — let the caller proceed rather than block
        logger.warning("relevance_gate: embedding failed, skipping gate: %s", exc)
        return RelevanceResult(is_relevant=True, best_score=0.0, reason="relevant")

    try:
        results = await _semantic_search(
            vector=query_vector,
            limit=3,
            repo_owner=repo_owner,
            repo_name=repo_name,
            language=None,
            search_quality="fast",  # speed over recall for the gate check
        )
    except Exception as exc:
        # DB failure — let the caller proceed
        logger.warning("relevance_gate: search failed, skipping gate: %s", exc)
        return RelevanceResult(is_relevant=True, best_score=0.0, reason="relevant")

    if not results:
        logger.debug("relevance_gate: no indexed content found — no_index")
        return RelevanceResult(is_relevant=False, best_score=0.0, reason="no_index")

    best_score = max(r.score for r in results)
    top_file = results[0].file_path if results else None

    logger.debug(
        "relevance_gate: best_score=%.3f threshold=%.2f soft=%.2f complexity=%s file=%s",
        best_score,
        threshold,
        soft_threshold,
        complexity,
        top_file,
    )

    if best_score < threshold:
        return RelevanceResult(
            is_relevant=False,
            best_score=best_score,
            reason="out_of_scope",
            top_file=top_file,
            query_complexity=complexity,
        )

    if best_score < soft_threshold:
        return RelevanceResult(
            is_relevant=True,
            best_score=best_score,
            reason="ambiguous",
            top_file=top_file,
            query_complexity=complexity,
        )

    return RelevanceResult(
        is_relevant=True,
        best_score=best_score,
        reason="relevant",
        top_file=top_file,
        query_complexity=complexity,
    )


def build_out_of_scope_message(query: str, result: RelevanceResult) -> str:
    """Human-readable message for out_of_scope responses."""
    if result.reason == "no_index":
        return (
            "No repositories are indexed yet. "
            "Register and index a repo first via `POST /repos` before asking questions."
        )
    return (
        f"This query doesn't appear to relate to the indexed codebase "
        f"(relevance score: {result.best_score:.2f}, threshold: {settings.query_relevance_threshold:.2f}). "
        "Try rephrasing with specific file names, function names, or technical terms "
        "from your codebase. If you believe this is a valid codebase question, "
        f"lower `QUERY_RELEVANCE_THRESHOLD` (currently {settings.query_relevance_threshold})."
    )
