"""
Cross-encoder reranker using ms-marco-MiniLM-L-6-v2.

Runs entirely locally (~60MB model download, CPU inference).
Takes the top-N candidates from the RRF merge and re-scores each
(query, chunk_text) pair with a dedicated relevance model.

This gives a 30-40% precision improvement over pure vector search
for free — no API calls, no latency dependency on external services.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.config import settings
from src.utils.sanitize import sanitize_log

if TYPE_CHECKING:
    from src.retrieval.searcher import SearchResult

logger = logging.getLogger(__name__)

# Lazy-loaded — only downloaded on first use
_model = None


def _truncate_to_line(text: str, max_chars: int) -> str:
    """Truncate text to the last complete line within max_chars."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > 0:
        return truncated[:last_newline]
    return truncated


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder

        logger.info("Loading cross-encoder model: %s", settings.reranker_model)
        _model = CrossEncoder(settings.reranker_model, max_length=512)
        logger.info("Cross-encoder model loaded")
    return _model


def rerank(
    query: str,
    results: list[SearchResult],
    top_n: int | None = None,
) -> list[SearchResult]:
    """
    Re-score results using the cross-encoder and return sorted by rerank_score.

    Uses raw_content for scoring (not enriched_content) so the model sees
    clean code without the metadata header — the header would dilute signal.

    Args:
        query:   The original user query string.
        results: Candidates to rerank (from RRF or semantic search).
        top_n:   How many to return after reranking (None = all).
    """
    if not results:
        return results

    model = _get_model()

    pairs = [(query, _truncate_to_line(r.raw_content, 1500)) for r in results]

    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception as exc:
        logger.warning("Reranker failed, returning original order: %s", sanitize_log(exc))
        return results[:top_n] if top_n else results

    for result, score in zip(results, scores):
        result.rerank_score = float(score)

    reranked = sorted(results, key=lambda r: r.rerank_score, reverse=True)

    if top_n:
        return reranked[:top_n]
    return reranked
