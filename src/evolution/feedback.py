"""
Pillar 1 — The Mirror: User feedback collection.

Provides helpers to attach explicit 1–5 star ratings (and optional free text)
to interaction_metrics rows after the fact.
"""

from __future__ import annotations

from sqlalchemy import text

from src.evolution.telemetry import update_interaction_rating
from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)


async def save_user_rating(
    interaction_id: int,
    rating: int,
    feedback_text: str | None = None,
) -> bool:
    """
    Attach a 1–5 user rating to an existing interaction_metrics row.
    Returns True on success, False on failure.
    """
    if not (1 <= rating <= 5):
        logger.warning("Invalid rating %s for interaction %s", rating, interaction_id)
        return False
    return await update_interaction_rating(interaction_id, rating, feedback_text)


async def get_rated_interactions(
    repo_owner: str,
    repo_name: str,
    days: int = 30,
) -> list[dict]:
    """Return all user-rated interactions for a repo over the last N days."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT id, interaction_type, query, implicit_quality_score,
                           user_rating, user_feedback_text, query_complexity,
                           elapsed_ms, retrieval_iterations, created_at
                    FROM interaction_metrics
                    WHERE repo_owner = :owner
                      AND repo_name  = :name
                      AND user_rating IS NOT NULL
                      AND created_at >= NOW() - INTERVAL '1 day' * :days
                    ORDER BY created_at DESC
                """),
                {"owner": repo_owner, "name": repo_name, "days": days},
            )
        ).mappings().all()

    return [
        {
            "id": r["id"],
            "type": r["interaction_type"],
            "query": r["query"],
            "quality_score": r["implicit_quality_score"],
            "user_rating": r["user_rating"],
            "feedback_text": r["user_feedback_text"],
            "complexity": r["query_complexity"],
            "elapsed_ms": r["elapsed_ms"],
            "iterations": r["retrieval_iterations"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


async def get_feedback_summary(
    repo_owner: str,
    repo_name: str,
    days: int = 30,
) -> dict:
    """Aggregate user feedback stats for a repo."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT
                        COUNT(*) FILTER (WHERE user_rating IS NOT NULL) AS rated_count,
                        AVG(user_rating) AS mean_rating,
                        COUNT(*) FILTER (WHERE user_rating >= 4) AS positive_count,
                        COUNT(*) FILTER (WHERE user_rating <= 2) AS negative_count
                    FROM interaction_metrics
                    WHERE repo_owner = :owner
                      AND repo_name  = :name
                      AND created_at >= NOW() - INTERVAL '1 day' * :days
                """),
                {"owner": repo_owner, "name": repo_name, "days": days},
            )
        ).mappings().first()

    rated = int(row["rated_count"] or 0)
    return {
        "rated_interactions": rated,
        "mean_rating": round(float(row["mean_rating"]), 2) if row["mean_rating"] else None,
        "positive_count": int(row["positive_count"] or 0),
        "negative_count": int(row["negative_count"] or 0),
        "satisfaction_rate": (
            round(int(row["positive_count"] or 0) / rated, 2) if rated > 0 else None
        ),
    }
