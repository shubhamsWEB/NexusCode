"""
Cross-encoder reranker using ms-marco-MiniLM-L-6-v2.

Runs entirely locally (~60MB model download, CPU inference).
Takes the top-N candidates from the RRF merge and re-scores each
(query, chunk_text) pair with a dedicated relevance model.

This gives a 30-40% precision improvement over pure vector search
for free — no API calls, no latency dependency on external services.

Sliding-window scoring (v2)
────────────────────────────
Large functions (>2000 chars) used to be truncated to the first 1500
characters, so the model only ever saw the signature and the start of
the body — missing the logic and return values.

With sliding-window scoring, large chunks are scored in two passes:
  Window 1: first reranker_content_chars characters (signature + opening)
  Window 2: last  reranker_content_chars characters (body + return)
The chunk's final score is the MAX across all windows, so a large
function is correctly ranked if the relevant code is anywhere in it.

Only chunks > reranker_content_chars trigger the second window, so small
chunks have zero extra latency.
"""

from __future__ import annotations

import math
import threading
from typing import TYPE_CHECKING

from src.config import settings
from src.utils.sanitize import sanitize_log

if TYPE_CHECKING:
    from src.retrieval.searcher import SearchResult

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# Lazy-loaded — only downloaded on first use
_model = None
_model_lock = threading.Lock()


def _truncate_to_line(text: str, max_chars: int) -> str:
    """Truncate text to the last complete line within max_chars."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > 0:
        return truncated[:last_newline]
    return truncated


def _build_scoring_windows(text: str, window_chars: int, max_windows: int) -> list[str]:
    """
    Build up to max_windows scoring windows for a chunk of text.

    Small chunks (<=window_chars): single window, no change in behaviour.
    Large chunks: Window 1 = beginning (signature + opening body),
                  Window 2 = tail (body + return values).

    Taking MAX score across windows ensures the chunk is ranked by the
    most relevant portion, not just by its first lines.

    Args:
        text:        Raw chunk content.
        window_chars: Max characters per window (maps to ~512 tokens for code).
        max_windows: 1 = legacy behaviour; 2 = beginning + tail.
    """
    if len(text) <= window_chars or max_windows <= 1:
        return [_truncate_to_line(text, window_chars)]

    windows: list[str] = []

    # Window 1: beginning (signature + first portion of body)
    w1 = _truncate_to_line(text[:window_chars], window_chars)
    if w1:
        windows.append(w1)

    # Window 2: tail — skip to the last window_chars, starting on a clean line
    if max_windows >= 2 and len(text) > window_chars:
        tail_raw = text[-window_chars:]
        first_nl = tail_raw.find("\n")
        tail = tail_raw[first_nl + 1:] if first_nl > 0 else tail_raw
        if tail and tail != w1:
            windows.append(tail)

    return windows if windows else [_truncate_to_line(text, window_chars)]


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:  # double-check after acquiring lock
                from sentence_transformers import CrossEncoder

                logger.info("Loading cross-encoder model: %s", settings.reranker_model)
                # max_length=512 is the hard limit for ms-marco-MiniLM models
                # (512 positional embeddings). Increasing the *content window*
                # (reranker_content_chars) exposes more text to the tokenizer
                # which then selects the 512 most-important tokens.
                _model = CrossEncoder(settings.reranker_model, max_length=512)
                logger.info("Cross-encoder model loaded")
    return _model


def warmup():
    """Pre-load the reranker model at startup so the first request isn't slow."""
    _get_model()


def rerank(
    query: str,
    results: list[SearchResult],
    top_n: int | None = None,
) -> list[SearchResult]:
    """
    Re-score results using the cross-encoder and return sorted by rerank_score.

    Uses raw_content for scoring (not enriched_content) so the model sees
    clean code without the metadata header — the header would dilute signal.

    For large chunks (>reranker_content_chars), scores beginning AND tail
    windows and takes the MAX — this prevents mis-ranking of large functions
    where the critical logic is in the second half of the chunk.

    Args:
        query:   The original user query string.
        results: Candidates to rerank (from RRF or semantic search).
        top_n:   How many to return after reranking (None = all).
    """
    if not results:
        return results

    model = _get_model()
    window_chars = settings.reranker_content_chars
    max_windows = settings.reranker_max_windows

    # Build scoring pairs — potentially multiple windows per chunk
    all_pairs: list[tuple[str, str]] = []
    window_counts: list[int] = []

    for r in results:
        windows = _build_scoring_windows(r.raw_content, window_chars, max_windows)
        for w in windows:
            all_pairs.append((query, w))
        window_counts.append(len(windows))

    try:
        all_scores = model.predict(all_pairs, show_progress_bar=False)
    except Exception as exc:
        logger.warning("Reranker failed, returning original order: %s", sanitize_log(exc))
        return results[:top_n] if top_n else results

    # Assign best score across windows to each result
    idx = 0
    for result, n_windows in zip(results, window_counts):
        window_scores = all_scores[idx : idx + n_windows]
        best_score = float(max(window_scores))
        result.rerank_score = best_score
        # Sigmoid normalization: maps raw logit to [0,1]
        result.quality_score = 1.0 / (1.0 + math.exp(-best_score))
        idx += n_windows

    reranked = sorted(results, key=lambda r: r.rerank_score, reverse=True)

    if top_n:
        return reranked[:top_n]
    return reranked
