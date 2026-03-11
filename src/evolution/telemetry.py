"""
Pillar 1 — The Mirror: Telemetry capture.

Records one row in interaction_metrics after every Ask or Plan completion.
All writes are fire-and-forget (asyncio.create_task) so they never block
the response path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text

from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── Query complexity classifier ───────────────────────────────────────────────

_CROSS_CUTTING = re.compile(
    r"\b(all endpoints|every|everywhere|entire|codebase|refactor|audit|review|migration)\b",
    re.IGNORECASE,
)
_IMPROVEMENT = re.compile(
    r"\b(optimize|improve|fix|debug|performance|slow|scale|bottleneck)\b",
    re.IGNORECASE,
)


def classify_query_complexity(query: str) -> str:
    """Return 'simple' | 'moderate' | 'complex' based on heuristics."""
    word_count = len(query.split())
    cross_cutting = bool(_CROSS_CUTTING.search(query))
    improvement = bool(_IMPROVEMENT.search(query))
    concern_count = query.count("?") + query.count(" and ") + query.count(",")

    if word_count < 15 and not cross_cutting and not improvement:
        return "simple"
    if cross_cutting or improvement or concern_count >= 3 or word_count > 60:
        return "complex"
    return "moderate"


# ── Public recording functions ────────────────────────────────────────────────


async def record_ask_metrics(
    repo_owner: str,
    repo_name: str,
    query: str,
    quality_score: float | None,
    iterations: int,
    tool_calls_count: int,
    context_tokens: int,
    elapsed_ms: float,
    retrieval_params: dict[str, Any],
    session_id: str | None = None,
    user_rating: int | None = None,
) -> int | None:
    """
    Persist one interaction_metrics row for a completed Ask call.
    Returns the new row ID (useful for linking feedback later), or None on error.
    """
    row = {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "interaction_type": "ask",
        "query": query[:2000],  # guard against very long queries
        "implicit_quality_score": quality_score,
        "user_rating": user_rating,
        "retrieval_iterations": iterations,
        "tool_calls_count": tool_calls_count,
        "context_tokens": context_tokens,
        "elapsed_ms": elapsed_ms,
        "query_complexity": classify_query_complexity(query),
        "retrieval_strategy": retrieval_params.get("retrieval_strategy", "hybrid"),
        "hnsw_ef_search_used": retrieval_params.get("hnsw_ef_search"),
        "rrf_k_used": retrieval_params.get("retrieval_rrf_k"),
        "candidate_multiplier_used": retrieval_params.get("retrieval_candidate_multiplier"),
        "reranker_top_n_used": retrieval_params.get("reranker_top_n"),
        "relevance_threshold_used": retrieval_params.get("query_relevance_threshold"),
        "max_iterations_used": retrieval_params.get("ask_max_iterations"),
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc),
    }
    return await _insert_metric(row)


async def record_plan_metrics(
    repo_owner: str,
    repo_name: str,
    query: str,
    elapsed_ms: float,
    context_tokens: int,
    quality_score: float | None,
    retrieval_params: dict[str, Any],
    plan_id: str | None = None,
    user_rating: int | None = None,
) -> int | None:
    """
    Persist one interaction_metrics row for a completed Plan call.
    Returns the new row ID, or None on error.
    """
    row = {
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "interaction_type": "plan",
        "query": query[:2000],
        "implicit_quality_score": quality_score,
        "user_rating": user_rating,
        "context_tokens": context_tokens,
        "elapsed_ms": elapsed_ms,
        "query_complexity": classify_query_complexity(query),
        "retrieval_strategy": retrieval_params.get("retrieval_strategy", "hybrid"),
        "hnsw_ef_search_used": retrieval_params.get("hnsw_ef_search"),
        "rrf_k_used": retrieval_params.get("retrieval_rrf_k"),
        "candidate_multiplier_used": retrieval_params.get("retrieval_candidate_multiplier"),
        "reranker_top_n_used": retrieval_params.get("reranker_top_n"),
        "relevance_threshold_used": retrieval_params.get("query_relevance_threshold"),
        "max_iterations_used": retrieval_params.get("plan_max_iterations"),
        "plan_id": plan_id,
        "created_at": datetime.now(timezone.utc),
    }
    return await _insert_metric(row)


async def update_interaction_rating(
    metric_id: int,
    rating: int,
    feedback_text: str | None = None,
) -> bool:
    """Update the user_rating on an existing interaction_metrics row."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    UPDATE interaction_metrics
                    SET user_rating = :rating,
                        user_feedback_text = :text
                    WHERE id = :id
                """),
                {"rating": rating, "text": feedback_text, "id": metric_id},
            )
            await session.commit()
        return True
    except Exception:
        logger.exception("Failed to update interaction rating id=%s", metric_id)
        return False


# ── Performance aggregation ───────────────────────────────────────────────────


@dataclass
class PerformanceStats:
    repo_owner: str
    repo_name: str
    lookback_days: int
    total_interactions: int
    mean_quality: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    mean_iterations: float | None
    rated_interactions: int
    mean_user_rating: float | None
    by_complexity: dict[str, dict]  # {"simple": {"count": N, "mean_quality": X}, ...}
    low_quality_ratio: float  # fraction with quality < 0.6


async def get_repo_performance_window(
    repo_owner: str,
    repo_name: str,
    days: int = 7,
) -> PerformanceStats:
    """Aggregate interaction_metrics over the last N days for a repo."""
    async with AsyncSessionLocal() as session:
        # Main aggregates
        row = (
            await session.execute(
                text("""
                    SELECT
                        COUNT(*)                                     AS total,
                        AVG(implicit_quality_score)                  AS mean_quality,
                        PERCENTILE_CONT(0.5) WITHIN GROUP
                            (ORDER BY elapsed_ms)                    AS p50_latency,
                        PERCENTILE_CONT(0.95) WITHIN GROUP
                            (ORDER BY elapsed_ms)                    AS p95_latency,
                        AVG(retrieval_iterations)                    AS mean_iterations,
                        COUNT(*) FILTER (WHERE user_rating IS NOT NULL) AS rated_count,
                        AVG(user_rating)                             AS mean_rating,
                        COUNT(*) FILTER (
                            WHERE implicit_quality_score < 0.6
                              AND implicit_quality_score IS NOT NULL) AS low_quality_count
                    FROM interaction_metrics
                    WHERE repo_owner = :owner
                      AND repo_name  = :name
                      AND created_at >= NOW() - INTERVAL '1 day' * :days
                """),
                {"owner": repo_owner, "name": repo_name, "days": days},
            )
        ).mappings().first()

        total = int(row["total"] or 0)
        low_q_count = int(row["low_quality_count"] or 0)

        # Per-complexity breakdown
        complexity_rows = (
            await session.execute(
                text("""
                    SELECT
                        query_complexity,
                        COUNT(*) AS cnt,
                        AVG(implicit_quality_score) AS mean_quality,
                        AVG(elapsed_ms) AS mean_latency
                    FROM interaction_metrics
                    WHERE repo_owner = :owner
                      AND repo_name  = :name
                      AND created_at >= NOW() - INTERVAL '1 day' * :days
                      AND query_complexity IS NOT NULL
                    GROUP BY query_complexity
                """),
                {"owner": repo_owner, "name": repo_name, "days": days},
            )
        ).mappings().all()

    by_complexity = {
        r["query_complexity"]: {
            "count": r["cnt"],
            "mean_quality": round(float(r["mean_quality"]), 3) if r["mean_quality"] else None,
            "mean_latency_ms": round(float(r["mean_latency"]), 1) if r["mean_latency"] else None,
        }
        for r in complexity_rows
    }

    return PerformanceStats(
        repo_owner=repo_owner,
        repo_name=repo_name,
        lookback_days=days,
        total_interactions=total,
        mean_quality=round(float(row["mean_quality"]), 3) if row["mean_quality"] else None,
        p50_latency_ms=round(float(row["p50_latency"]), 1) if row["p50_latency"] else None,
        p95_latency_ms=round(float(row["p95_latency"]), 1) if row["p95_latency"] else None,
        mean_iterations=round(float(row["mean_iterations"]), 2) if row["mean_iterations"] else None,
        rated_interactions=int(row["rated_count"] or 0),
        mean_user_rating=round(float(row["mean_rating"]), 2) if row["mean_rating"] else None,
        by_complexity=by_complexity,
        low_quality_ratio=low_q_count / total if total > 0 else 0.0,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _insert_metric(row: dict) -> int | None:
    """Insert a row into interaction_metrics and return its ID."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    INSERT INTO interaction_metrics (
                        repo_owner, repo_name, interaction_type, query,
                        implicit_quality_score, user_rating, user_feedback_text,
                        retrieval_iterations, tool_calls_count, context_tokens,
                        answer_tokens, elapsed_ms, query_complexity,
                        retrieval_strategy, hnsw_ef_search_used, rrf_k_used,
                        candidate_multiplier_used, reranker_top_n_used,
                        relevance_threshold_used, max_iterations_used,
                        session_id, plan_id, created_at
                    ) VALUES (
                        :repo_owner, :repo_name, :interaction_type, :query,
                        :implicit_quality_score, :user_rating, :user_feedback_text,
                        :retrieval_iterations, :tool_calls_count, :context_tokens,
                        :answer_tokens, :elapsed_ms, :query_complexity,
                        :retrieval_strategy, :hnsw_ef_search_used, :rrf_k_used,
                        :candidate_multiplier_used, :reranker_top_n_used,
                        :relevance_threshold_used, :max_iterations_used,
                        :session_id, :plan_id, :created_at
                    ) RETURNING id
                """),
                {
                    **row,
                    "retrieval_iterations": row.get("retrieval_iterations"),
                    "tool_calls_count": row.get("tool_calls_count"),
                    "user_feedback_text": row.get("user_feedback_text"),
                    "answer_tokens": row.get("answer_tokens"),
                    "session_id": row.get("session_id"),
                    "plan_id": row.get("plan_id"),
                },
            )
            metric_id = result.scalar_one()
            await session.commit()
            return metric_id
    except Exception:
        logger.exception("Failed to record interaction metric")
        return None
