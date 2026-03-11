"""
Pillar 3 — The Evolution Engine: A/B experiment framework.

Tracks controlled parameter experiments. Each reflection cycle can create
A/B experiments for proposed changes. Interactions are deterministically
assigned to control or treatment groups, and results are analyzed over time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from src.config import settings
from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)


@dataclass
class ExperimentResult:
    experiment_name: str
    parameter_name: str
    control_value: str
    treatment_value: str
    control_sample_count: int
    treatment_sample_count: int
    control_avg_quality: float | None
    treatment_avg_quality: float | None
    control_avg_latency_ms: float | None
    treatment_avg_latency_ms: float | None
    quality_improvement_pct: float | None
    winner: str  # "control" | "treatment" | "inconclusive"
    confidence: float  # 0.0–1.0 — higher = more data


async def start_ab_experiment(
    repo_owner: str,
    repo_name: str,
    experiment_name: str,
    parameter_name: str,
    control_value: str,
    treatment_value: str,
    evolution_cycle_id: int | None = None,
) -> bool:
    """
    Create and activate an A/B experiment.
    Returns True on success, False if experiment already exists.
    """
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO ab_experiments (
                        repo_owner, repo_name, experiment_name,
                        parameter_name, control_value, treatment_value,
                        started_at, status, evolution_cycle_id
                    ) VALUES (
                        :owner, :name, :exp_name,
                        :param, :control, :treatment,
                        :started, 'active', :cycle_id
                    )
                    ON CONFLICT (repo_owner, repo_name, experiment_name) DO NOTHING
                """),
                {
                    "owner": repo_owner,
                    "name": repo_name,
                    "exp_name": experiment_name,
                    "param": parameter_name,
                    "control": str(control_value),
                    "treatment": str(treatment_value),
                    "started": datetime.now(timezone.utc),
                    "cycle_id": evolution_cycle_id,
                },
            )
            await session.commit()
        logger.info("A/B experiment '%s' started for %s/%s", experiment_name, repo_owner, repo_name)
        return True
    except Exception:
        logger.exception("Failed to create A/B experiment %s", experiment_name)
        return False


def assign_treatment_group(
    repo_owner: str,
    repo_name: str,
    experiment_name: str,
) -> str:
    """
    Deterministically assign this interaction to 'control' or 'treatment'.
    Uses a hash of the repo + experiment name + time bucket (1-hour windows)
    so assignments are stable within an hour but vary across calls.
    """
    now_bucket = str(datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0))
    seed = f"{repo_owner}/{repo_name}/{experiment_name}/{now_bucket}"
    digest = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    threshold = int(settings.evolution_ab_test_sample_fraction * 100)
    return "treatment" if (digest % 100) < threshold else "control"


async def update_experiment_sample(
    repo_owner: str,
    repo_name: str,
    experiment_name: str,
    group: str,
    quality_score: float | None,
    elapsed_ms: float | None,
) -> None:
    """Increment sample count and running averages for an experiment group."""
    try:
        async with AsyncSessionLocal() as session:
            if group == "control":
                await session.execute(
                    text("""
                        UPDATE ab_experiments
                        SET control_sample_count = control_sample_count + 1,
                            control_avg_quality = (
                                COALESCE(control_avg_quality, 0) * control_sample_count
                                + COALESCE(:quality, 0)
                            ) / NULLIF(control_sample_count + 1, 0),
                            control_avg_latency_ms = (
                                COALESCE(control_avg_latency_ms, 0) * control_sample_count
                                + COALESCE(:latency, 0)
                            ) / NULLIF(control_sample_count + 1, 0)
                        WHERE repo_owner = :owner AND repo_name = :name
                          AND experiment_name = :exp_name AND status = 'active'
                    """),
                    {"owner": repo_owner, "name": repo_name, "exp_name": experiment_name,
                     "quality": quality_score, "latency": elapsed_ms},
                )
            else:
                await session.execute(
                    text("""
                        UPDATE ab_experiments
                        SET treatment_sample_count = treatment_sample_count + 1,
                            treatment_avg_quality = (
                                COALESCE(treatment_avg_quality, 0) * treatment_sample_count
                                + COALESCE(:quality, 0)
                            ) / NULLIF(treatment_sample_count + 1, 0),
                            treatment_avg_latency_ms = (
                                COALESCE(treatment_avg_latency_ms, 0) * treatment_sample_count
                                + COALESCE(:latency, 0)
                            ) / NULLIF(treatment_sample_count + 1, 0)
                        WHERE repo_owner = :owner AND repo_name = :name
                          AND experiment_name = :exp_name AND status = 'active'
                    """),
                    {"owner": repo_owner, "name": repo_name, "exp_name": experiment_name,
                     "quality": quality_score, "latency": elapsed_ms},
                )
            await session.commit()
    except Exception:
        logger.debug("A/B sample update failed (non-fatal)")


async def analyze_experiment_results(
    repo_owner: str,
    repo_name: str,
    experiment_name: str,
) -> ExperimentResult | None:
    """
    Compute current experiment results.
    Requires at least 10 samples per group to declare a winner.
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT parameter_name, control_value, treatment_value,
                           control_sample_count, treatment_sample_count,
                           control_avg_quality, treatment_avg_quality,
                           control_avg_latency_ms, treatment_avg_latency_ms
                    FROM ab_experiments
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND experiment_name = :exp_name
                """),
                {"owner": repo_owner, "name": repo_name, "exp_name": experiment_name},
            )
        ).mappings().first()

    if not row:
        return None

    ctrl_q = float(row["control_avg_quality"]) if row["control_avg_quality"] else None
    treat_q = float(row["treatment_avg_quality"]) if row["treatment_avg_quality"] else None
    ctrl_n = int(row["control_sample_count"] or 0)
    treat_n = int(row["treatment_sample_count"] or 0)

    # Determine winner
    MIN_SAMPLES = 10
    winner = "inconclusive"
    quality_improvement_pct = None
    confidence = min(1.0, (ctrl_n + treat_n) / 100)  # Simple proxy: 100 samples = full confidence

    if ctrl_q is not None and treat_q is not None and ctrl_n >= MIN_SAMPLES and treat_n >= MIN_SAMPLES:
        if treat_q > ctrl_q + 0.03:  # treatment wins by >3%
            winner = "treatment"
        elif ctrl_q > treat_q + 0.03:  # control wins by >3%
            winner = "control"
        if ctrl_q > 0:
            quality_improvement_pct = round((treat_q - ctrl_q) / ctrl_q * 100, 1)

    return ExperimentResult(
        experiment_name=experiment_name,
        parameter_name=row["parameter_name"],
        control_value=row["control_value"],
        treatment_value=row["treatment_value"],
        control_sample_count=ctrl_n,
        treatment_sample_count=treat_n,
        control_avg_quality=round(ctrl_q, 3) if ctrl_q else None,
        treatment_avg_quality=round(treat_q, 3) if treat_q else None,
        control_avg_latency_ms=round(float(row["control_avg_latency_ms"]), 1) if row["control_avg_latency_ms"] else None,
        treatment_avg_latency_ms=round(float(row["treatment_avg_latency_ms"]), 1) if row["treatment_avg_latency_ms"] else None,
        quality_improvement_pct=quality_improvement_pct,
        winner=winner,
        confidence=round(confidence, 2),
    )


async def rollout_experiment(
    repo_owner: str,
    repo_name: str,
    experiment_name: str,
    winner: str,
) -> None:
    """
    Mark experiment as completed and record the winning value.
    The actual parameter is applied by the caller via param_tuner.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE ab_experiments
                SET status = 'completed',
                    winner = :winner,
                    ended_at = :now
                WHERE repo_owner = :owner AND repo_name = :name
                  AND experiment_name = :exp_name
            """),
            {
                "owner": repo_owner,
                "name": repo_name,
                "exp_name": experiment_name,
                "winner": winner,
                "now": datetime.now(timezone.utc),
            },
        )
        await session.commit()
    logger.info(
        "A/B experiment '%s' rolled out: winner=%s", experiment_name, winner
    )
