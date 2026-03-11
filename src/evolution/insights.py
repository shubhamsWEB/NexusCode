"""
Pillar 2 — The Memory: Pattern analysis and retrieval insights.

Analyzes accumulated interaction_metrics to discover:
  - Which query types consistently produce low-quality retrievals
  - Which retrieval parameter snapshots correlate with better outcomes
  - Latency bottlenecks and optimization opportunities

These insights feed the Pillar 3 Evolution Engine (reflection_cycle.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text

from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── Schemas ───────────────────────────────────────────────────────────────────


@dataclass
class QueryInsights:
    """Analysis of which query types work well vs. poorly."""

    total_interactions: int
    lookback_days: int

    # Weak patterns: query types with mean quality < 0.6
    weak_query_patterns: list[dict] = field(default_factory=list)
    # [{"complexity": "complex", "count": N, "mean_quality": X, "example_queries": [...]}]

    # Strong patterns: query types with mean quality >= 0.8
    strong_query_patterns: list[dict] = field(default_factory=list)

    # Queries that required many iterations (>2) and still had low quality
    hard_queries: list[str] = field(default_factory=list)

    # Summary string for LLM consumption
    summary: str = ""


@dataclass
class RetrievalInsights:
    """Analysis of retrieval parameter effectiveness."""

    lookback_days: int

    # Current parameter values
    current_params: dict = field(default_factory=dict)

    # Overall stats
    mean_quality: float | None = None
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None

    # Best-performing param combo observed
    best_observed_params: dict = field(default_factory=dict)
    best_observed_quality: float | None = None

    # Suggested adjustments based on variance analysis
    # {"hnsw_ef_search": {"current": 40, "suggested": 60, "reason": "..."}}
    suggested_adjustments: dict = field(default_factory=dict)

    # Summary string for LLM consumption
    summary: str = ""


# ── Query pattern analysis ─────────────────────────────────────────────────────


async def analyze_query_patterns(
    repo_owner: str,
    repo_name: str,
    days: int = 30,
) -> QueryInsights:
    """
    Analyse interaction_metrics to identify weak vs. strong query categories.
    """
    async with AsyncSessionLocal() as session:
        total_row = (
            await session.execute(
                text("""
                    SELECT COUNT(*) AS total
                    FROM interaction_metrics
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND created_at >= NOW() - INTERVAL '1 day' * :days
                """),
                {"owner": repo_owner, "name": repo_name, "days": days},
            )
        ).scalar_one()

        # Group by complexity and get quality stats
        complexity_rows = (
            await session.execute(
                text("""
                    SELECT
                        query_complexity,
                        COUNT(*)                        AS cnt,
                        AVG(implicit_quality_score)     AS mean_quality,
                        AVG(retrieval_iterations)       AS mean_iterations
                    FROM interaction_metrics
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND created_at >= NOW() - INTERVAL '1 day' * :days
                      AND implicit_quality_score IS NOT NULL
                    GROUP BY query_complexity
                """),
                {"owner": repo_owner, "name": repo_name, "days": days},
            )
        ).mappings().all()

        # Find hard queries: many iterations + low quality
        hard_rows = (
            await session.execute(
                text("""
                    SELECT query
                    FROM interaction_metrics
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND created_at >= NOW() - INTERVAL '1 day' * :days
                      AND retrieval_iterations > 2
                      AND implicit_quality_score < 0.5
                    ORDER BY implicit_quality_score ASC
                    LIMIT 10
                """),
                {"owner": repo_owner, "name": repo_name, "days": days},
            )
        ).mappings().all()

    weak = []
    strong = []
    for r in complexity_rows:
        mq = float(r["mean_quality"]) if r["mean_quality"] is not None else None
        entry = {
            "complexity": r["query_complexity"] or "unknown",
            "count": int(r["cnt"]),
            "mean_quality": round(mq, 3) if mq is not None else None,
            "mean_iterations": round(float(r["mean_iterations"]), 1) if r["mean_iterations"] else None,
        }
        if mq is not None:
            if mq < 0.6:
                weak.append(entry)
            elif mq >= 0.8:
                strong.append(entry)

    hard_queries = [r["query"][:200] for r in hard_rows]

    summary_parts = [f"Analysis window: {days} days, {total_row} total interactions."]
    if weak:
        summary_parts.append(
            f"Weak query categories ({len(weak)}): "
            + "; ".join(f"{w['complexity']} (quality={w['mean_quality']}, n={w['count']})" for w in weak)
        )
    if hard_queries:
        summary_parts.append(f"Hard queries (many iterations, low quality): {len(hard_queries)} found.")
    if strong:
        summary_parts.append(
            f"Strong categories: "
            + "; ".join(f"{s['complexity']} (quality={s['mean_quality']})" for s in strong)
        )

    return QueryInsights(
        total_interactions=int(total_row),
        lookback_days=days,
        weak_query_patterns=weak,
        strong_query_patterns=strong,
        hard_queries=hard_queries,
        summary=" ".join(summary_parts),
    )


# ── Retrieval parameter analysis ──────────────────────────────────────────────


async def analyze_retrieval_efficiency(
    repo_owner: str,
    repo_name: str,
    days: int = 30,
) -> RetrievalInsights:
    """
    Analyse which retrieval parameter snapshots correlate with higher quality.
    Returns suggestions for parameter adjustments.
    """
    from src.config import settings as s

    current_params = {
        "hnsw_ef_search": s.hnsw_ef_search,
        "retrieval_rrf_k": s.retrieval_rrf_k,
        "retrieval_candidate_multiplier": s.retrieval_candidate_multiplier,
        "reranker_top_n": s.reranker_top_n,
        "query_relevance_threshold": s.query_relevance_threshold,
    }

    async with AsyncSessionLocal() as session:
        # Aggregate stats
        agg = (
            await session.execute(
                text("""
                    SELECT
                        AVG(implicit_quality_score)         AS mean_quality,
                        PERCENTILE_CONT(0.5) WITHIN GROUP
                            (ORDER BY elapsed_ms)           AS p50_latency,
                        PERCENTILE_CONT(0.95) WITHIN GROUP
                            (ORDER BY elapsed_ms)           AS p95_latency
                    FROM interaction_metrics
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND created_at >= NOW() - INTERVAL '1 day' * :days
                      AND implicit_quality_score IS NOT NULL
                """),
                {"owner": repo_owner, "name": repo_name, "days": days},
            )
        ).mappings().first()

        # Group by hnsw_ef_search to find best quality setting
        ef_rows = (
            await session.execute(
                text("""
                    SELECT
                        hnsw_ef_search_used,
                        COUNT(*)                        AS cnt,
                        AVG(implicit_quality_score)     AS mean_quality,
                        AVG(elapsed_ms)                 AS mean_latency
                    FROM interaction_metrics
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND created_at >= NOW() - INTERVAL '1 day' * :days
                      AND hnsw_ef_search_used IS NOT NULL
                      AND implicit_quality_score IS NOT NULL
                    GROUP BY hnsw_ef_search_used
                    HAVING COUNT(*) >= 5
                    ORDER BY AVG(implicit_quality_score) DESC
                    LIMIT 5
                """),
                {"owner": repo_owner, "name": repo_name, "days": days},
            )
        ).mappings().all()

    mean_q = float(agg["mean_quality"]) if agg["mean_quality"] else None
    p50 = float(agg["p50_latency"]) if agg["p50_latency"] else None
    p95 = float(agg["p95_latency"]) if agg["p95_latency"] else None

    best_ef_row = ef_rows[0] if ef_rows else None
    best_observed_params: dict = {}
    best_observed_quality: float | None = None

    if best_ef_row:
        best_observed_params = {"hnsw_ef_search": best_ef_row["hnsw_ef_search_used"]}
        best_observed_quality = round(float(best_ef_row["mean_quality"]), 3)

    # Build suggestions
    suggested: dict = {}
    if (
        best_ef_row
        and int(best_ef_row["hnsw_ef_search_used"]) != current_params["hnsw_ef_search"]
        and best_observed_quality is not None
        and mean_q is not None
        and best_observed_quality > mean_q + 0.05
    ):
        suggested["hnsw_ef_search"] = {
            "current": current_params["hnsw_ef_search"],
            "suggested": int(best_ef_row["hnsw_ef_search_used"]),
            "reason": (
                f"Interactions with ef_search={int(best_ef_row['hnsw_ef_search_used'])} "
                f"had mean quality {best_observed_quality:.3f} vs overall {mean_q:.3f}"
            ),
        }

    summary_parts = [
        f"Retrieval efficiency ({days}-day window):",
        f"mean quality={round(mean_q, 3) if mean_q else 'N/A'}, "
        f"p50={round(p50, 0) if p50 else 'N/A'}ms, "
        f"p95={round(p95, 0) if p95 else 'N/A'}ms.",
    ]
    if suggested:
        summary_parts.append(f"Suggested parameter changes: {json_summary(suggested)}")
    else:
        summary_parts.append("No clear parameter improvement signal detected.")

    return RetrievalInsights(
        lookback_days=days,
        current_params=current_params,
        mean_quality=round(mean_q, 3) if mean_q else None,
        p50_latency_ms=round(p50, 1) if p50 else None,
        p95_latency_ms=round(p95, 1) if p95 else None,
        best_observed_params=best_observed_params,
        best_observed_quality=best_observed_quality,
        suggested_adjustments=suggested,
        summary=" ".join(summary_parts),
    )


def json_summary(d: dict) -> str:
    import json
    return json.dumps(d, separators=(",", ":"))
