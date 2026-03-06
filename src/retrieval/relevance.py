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


async def check_query_relevance(
    query: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> RelevanceResult:
    """
    Fast relevance pre-flight: single embedding + single DB query.
    No LLM call, no reranking. Typical latency: 50-150ms (embedding cached).

    Returns a RelevanceResult with is_relevant=False when the query should
    be short-circuited without running the full agent loop.
    """
    from src.retrieval.searcher import _semantic_search, embed_query

    threshold = settings.query_relevance_threshold
    soft_threshold = settings.query_relevance_soft_threshold

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
        logger.info("relevance_gate: no indexed content found — no_index")
        return RelevanceResult(is_relevant=False, best_score=0.0, reason="no_index")

    best_score = max(r.score for r in results)
    top_file = results[0].file_path if results else None

    logger.info(
        "relevance_gate: best_score=%.3f threshold=%.2f soft=%.2f file=%s",
        best_score,
        threshold,
        soft_threshold,
        top_file,
    )

    if best_score < threshold:
        return RelevanceResult(
            is_relevant=False,
            best_score=best_score,
            reason="out_of_scope",
            top_file=top_file,
        )

    if best_score < soft_threshold:
        # Ambiguous zone — proceed but mark it
        return RelevanceResult(
            is_relevant=True,
            best_score=best_score,
            reason="ambiguous",
            top_file=top_file,
        )

    return RelevanceResult(
        is_relevant=True,
        best_score=best_score,
        reason="relevant",
        top_file=top_file,
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
