"""
Evolution API — exposes feedback, metrics, worldview, and reflection endpoints.

All routes are prefixed with /evolution.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from src.evolution.feedback import (
    get_feedback_summary,
    get_rated_interactions,
    save_user_rating,
)
from src.evolution.telemetry import get_repo_performance_window
from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

router = APIRouter(prefix="/evolution", tags=["evolution"])


# ── Request / Response schemas ────────────────────────────────────────────────


class FeedbackRequest(BaseModel):
    interaction_id: int = Field(..., description="ID returned in Ask/Plan response metadata")
    rating: int = Field(..., ge=1, le=5, description="Star rating 1–5")
    feedback_text: str | None = Field(None, max_length=2000)


class TriggerCycleRequest(BaseModel):
    lookback_days: int = Field(30, ge=1, le=365, description="Analysis window in days")
    force: bool = Field(
        False,
        description="Run even if min_interactions threshold hasn't been reached",
    )


# ── Feedback ──────────────────────────────────────────────────────────────────


@router.post("/feedback", summary="Submit rating for an Ask or Plan interaction")
async def submit_feedback(req: FeedbackRequest) -> dict:
    """
    Attach a 1–5 star rating (and optional text) to a completed interaction.
    The interaction_id is returned in the metadata of every Ask/Plan response.
    """
    ok = await save_user_rating(req.interaction_id, req.rating, req.feedback_text)
    if not ok:
        raise HTTPException(status_code=404, detail="Interaction not found or update failed")
    return {"ok": True, "interaction_id": req.interaction_id, "rating": req.rating}


# ── Metrics ───────────────────────────────────────────────────────────────────


@router.get("/metrics/{owner}/{name}", summary="Aggregated performance metrics for a repo")
async def get_metrics(
    owner: str,
    name: str,
    days: int = Query(7, ge=1, le=365, description="Look-back window in days"),
) -> dict:
    """
    Return mean quality, latency percentiles, iteration counts, and feedback
    summary for all interactions with this repo over the last N days.
    """
    stats = await get_repo_performance_window(owner, name, days)
    feedback = await get_feedback_summary(owner, name, days)
    return {
        "repo": f"{owner}/{name}",
        "lookback_days": days,
        "total_interactions": stats.total_interactions,
        "quality": {
            "mean": stats.mean_quality,
            "low_quality_ratio": round(stats.low_quality_ratio, 3),
        },
        "latency": {
            "p50_ms": stats.p50_latency_ms,
            "p95_ms": stats.p95_latency_ms,
        },
        "agent": {
            "mean_iterations": stats.mean_iterations,
        },
        "by_complexity": stats.by_complexity,
        "feedback": feedback,
    }


# ── Worldview ─────────────────────────────────────────────────────────────────


@router.get("/worldview/{owner}/{name}", summary="Latest semantic worldview for a repo")
async def get_worldview(owner: str, name: str, version: int | None = None) -> dict:
    """
    Return the most recent (or a specific versioned) LLM-generated worldview
    for a repository.
    """
    async with AsyncSessionLocal() as session:
        if version is not None:
            row = (
                await session.execute(
                    text("""
                        SELECT id, version, architecture_summary, key_patterns,
                               difficult_zones, conventions, recent_changes,
                               full_worldview, chunks_sampled, interactions_analyzed,
                               model_used, generated_at
                        FROM repo_worldviews
                        WHERE repo_owner = :owner AND repo_name = :name AND version = :v
                    """),
                    {"owner": owner, "name": name, "v": version},
                )
            ).mappings().first()
        else:
            row = (
                await session.execute(
                    text("""
                        SELECT id, version, architecture_summary, key_patterns,
                               difficult_zones, conventions, recent_changes,
                               full_worldview, chunks_sampled, interactions_analyzed,
                               model_used, generated_at
                        FROM repo_worldviews
                        WHERE repo_owner = :owner AND repo_name = :name
                        ORDER BY version DESC LIMIT 1
                    """),
                    {"owner": owner, "name": name},
                )
            ).mappings().first()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="No worldview found for this repo. Trigger a reflection cycle first.",
        )

    return {
        "repo": f"{owner}/{name}",
        "version": row["version"],
        "architecture_summary": row["architecture_summary"],
        "key_patterns": row["key_patterns"] or [],
        "difficult_zones": row["difficult_zones"] or [],
        "conventions": row["conventions"] or [],
        "recent_changes": row["recent_changes"],
        "full_worldview": row["full_worldview"],
        "metadata": {
            "chunks_sampled": row["chunks_sampled"],
            "interactions_analyzed": row["interactions_analyzed"],
            "model_used": row["model_used"],
            "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
        },
    }


@router.post("/worldview/{owner}/{name}/regenerate", summary="Regenerate worldview immediately")
async def regenerate_worldview(owner: str, name: str) -> dict:
    """
    Regenerate the semantic worldview for a repo immediately, without running a full
    reflection cycle and without checking the interaction threshold.

    Use this when you want a fresh worldview after indexing a repo or after
    significant code changes, regardless of how many interactions have occurred.
    """
    from src.evolution.worldview_generator import generate_worldview

    async with AsyncSessionLocal() as session:
        exists = (
            await session.execute(
                text("SELECT 1 FROM repos WHERE owner = :owner AND name = :name"),
                {"owner": owner, "name": name},
            )
        ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{name} not found")

    try:
        doc = await generate_worldview(owner, name)
    except Exception as exc:
        logger.exception("regenerate_worldview failed for %s/%s", owner, name)
        raise HTTPException(status_code=500, detail=f"Worldview generation failed: {exc}") from exc

    if not doc:
        raise HTTPException(status_code=500, detail="Worldview generation returned no result")

    return {
        "ok": True,
        "repo": f"{owner}/{name}",
        "version": doc.version,
        "chunks_sampled": doc.chunks_sampled,
        "model_used": doc.model_used,
    }


@router.get("/worldview/{owner}/{name}/versions", summary="List all worldview versions")
async def list_worldview_versions(owner: str, name: str) -> list[dict]:
    """Return a summary of all worldview versions for a repo (newest first)."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT version, chunks_sampled, interactions_analyzed,
                           model_used, generated_at
                    FROM repo_worldviews
                    WHERE repo_owner = :owner AND repo_name = :name
                    ORDER BY version DESC
                """),
                {"owner": owner, "name": name},
            )
        ).mappings().all()

    return [
        {
            "version": r["version"],
            "chunks_sampled": r["chunks_sampled"],
            "interactions_analyzed": r["interactions_analyzed"],
            "model_used": r["model_used"],
            "generated_at": r["generated_at"].isoformat() if r["generated_at"] else None,
        }
        for r in rows
    ]


# ── Reflection cycle ──────────────────────────────────────────────────────────


@router.post("/cycle/{owner}/{name}", summary="Trigger a self-reflection cycle")
async def trigger_cycle(owner: str, name: str, req: TriggerCycleRequest | None = None) -> dict:
    """
    Manually trigger a self-reflection cycle for a repository.

    The cycle will:
    1. Analyze recent interaction metrics
    2. Discover patterns and identify weak areas
    3. Propose and autonomously apply parameter + prompt improvements
    4. Generate a new worldview version
    5. Log all changes in the evolution_log table
    """
    import asyncio

    from src.evolution.reflection_cycle import run_reflection_cycle

    lookback_days = (req.lookback_days if req else 30)
    force = (req.force if req else False)

    # Run in background so the HTTP response is immediate
    async def _run():
        try:
            await run_reflection_cycle(owner, name, lookback_days=lookback_days, force=force)
        except Exception:
            logger.exception("Background reflection cycle failed for %s/%s", owner, name)

    asyncio.create_task(_run())

    return {
        "ok": True,
        "repo": f"{owner}/{name}",
        "message": "Reflection cycle started in background. Check /evolution/log for status.",
    }


@router.get("/log/{owner}/{name}", summary="Evolution cycle history for a repo")
async def get_evolution_log(
    owner: str,
    name: str,
    limit: int = Query(10, ge=1, le=100),
) -> list[dict]:
    """Return the most recent evolution cycles for a repo (newest first)."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT cycle_number, cycle_started_at, cycle_completed_at,
                           metrics_analyzed_count, improvements_proposed,
                           improvements_applied, parameter_changes,
                           prompt_changes, discovered_patterns,
                           new_worldview_version, status, error_message
                    FROM evolution_log
                    WHERE repo_owner = :owner AND repo_name = :name
                    ORDER BY cycle_number DESC
                    LIMIT :limit
                """),
                {"owner": owner, "name": name, "limit": limit},
            )
        ).mappings().all()

    return [
        {
            "cycle_number": r["cycle_number"],
            "started_at": r["cycle_started_at"].isoformat() if r["cycle_started_at"] else None,
            "completed_at": r["cycle_completed_at"].isoformat() if r["cycle_completed_at"] else None,
            "metrics_analyzed": r["metrics_analyzed_count"],
            "improvements_proposed": r["improvements_proposed"],
            "improvements_applied": r["improvements_applied"],
            "parameter_changes": r["parameter_changes"],
            "prompt_changes": r["prompt_changes"],
            "discovered_patterns": r["discovered_patterns"] or [],
            "new_worldview_version": r["new_worldview_version"],
            "status": r["status"],
            "error": r["error_message"],
        }
        for r in rows
    ]


# ── A/B Experiments ───────────────────────────────────────────────────────────


@router.get("/experiments/{owner}/{name}", summary="List A/B experiments for a repo")
async def list_experiments(owner: str, name: str) -> list[dict]:
    """List all parameter A/B experiments for a repo."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT experiment_name, parameter_name, control_value, treatment_value,
                           started_at, ended_at, control_sample_count, treatment_sample_count,
                           control_avg_quality, treatment_avg_quality,
                           control_avg_latency_ms, treatment_avg_latency_ms,
                           winner, confidence, status
                    FROM ab_experiments
                    WHERE repo_owner = :owner AND repo_name = :name
                    ORDER BY started_at DESC
                """),
                {"owner": owner, "name": name},
            )
        ).mappings().all()

    return [
        {
            "name": r["experiment_name"],
            "parameter": r["parameter_name"],
            "control": r["control_value"],
            "treatment": r["treatment_value"],
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
            "samples": {
                "control": r["control_sample_count"],
                "treatment": r["treatment_sample_count"],
            },
            "quality": {
                "control_avg": r["control_avg_quality"],
                "treatment_avg": r["treatment_avg_quality"],
            },
            "latency": {
                "control_avg_ms": r["control_avg_latency_ms"],
                "treatment_avg_ms": r["treatment_avg_latency_ms"],
            },
            "winner": r["winner"],
            "confidence": r["confidence"],
            "status": r["status"],
        }
        for r in rows
    ]


# ── Interaction Log ───────────────────────────────────────────────────────────


@router.get("/interactions/{owner}/{name}", summary="Raw per-query interaction log")
async def get_interactions(
    owner: str,
    name: str,
    limit: int = Query(50, ge=1, le=500, description="Max rows to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    interaction_type: str | None = Query(None, pattern="^(ask|plan)$", description="Filter by type"),
    complexity: str | None = Query(None, description="Filter by query complexity (simple/moderate/complex)"),
    min_quality: float | None = Query(None, ge=0.0, le=1.0, description="Minimum quality score"),
    days: int = Query(30, ge=1, le=365, description="Look-back window in days"),
) -> dict:
    """
    Return a paginated list of raw interaction records from interaction_metrics.

    Every Ask and Plan query is recorded here with its quality score, latency,
    iteration count, and the retrieval parameter snapshot that was in effect.
    """
    filters = [
        "im.repo_owner = :owner",
        "im.repo_name = :name",
        "im.created_at > NOW() - INTERVAL '1 day' * :days",
    ]
    params: dict = {"owner": owner, "name": name, "days": days, "limit": limit, "offset": offset}

    if interaction_type:
        filters.append("im.interaction_type = :itype")
        params["itype"] = interaction_type
    if complexity:
        filters.append("im.query_complexity = :complexity")
        params["complexity"] = complexity
    if min_quality is not None:
        filters.append("im.implicit_quality_score >= :min_quality")
        params["min_quality"] = min_quality

    where = " AND ".join(filters)

    async with AsyncSessionLocal() as session:
        # total count
        count_row = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM interaction_metrics im WHERE {where}"),
                params,
            )
        ).scalar()

        rows = (
            await session.execute(
                text(f"""
                    SELECT im.id, im.interaction_type, im.query, im.query_complexity,
                           im.implicit_quality_score, im.user_rating,
                           im.retrieval_iterations, im.tool_calls_count,
                           im.context_tokens, im.answer_tokens, im.elapsed_ms,
                           im.retrieval_strategy, im.hnsw_ef_search_used,
                           im.rrf_k_used, im.reranker_top_n_used,
                           im.relevance_threshold_used, im.max_iterations_used,
                           im.session_id, im.plan_id, im.created_at,
                           -- Ask response: most recent matching chat turn
                           ct.answer          AS ask_answer,
                           ct.cited_files     AS ask_cited_files,
                           -- Plan response: matched plan row
                           ph.response_type   AS plan_response_type,
                           ph.plan_json       AS plan_json
                    FROM interaction_metrics im
                    LEFT JOIN LATERAL (
                        SELECT answer, cited_files
                        FROM chat_turns
                        WHERE session_id = im.session_id::text
                          AND user_query  = im.query
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) ct ON im.interaction_type = 'ask' AND im.session_id IS NOT NULL
                    LEFT JOIN LATERAL (
                        SELECT response_type, plan_json
                        FROM plan_history
                        WHERE plan_id = im.plan_id::text
                        LIMIT 1
                    ) ph ON im.interaction_type = 'plan' AND im.plan_id IS NOT NULL
                    WHERE {where}
                    ORDER BY im.created_at DESC
                    LIMIT :limit OFFSET :offset
                """),
                params,
            )
        ).mappings().all()

    def _plan_response_text(plan_json_str: str | None, response_type: str | None) -> str | None:
        """Extract the human-readable response text from a plan_json blob."""
        if not plan_json_str:
            return None
        try:
            import json as _json
            data = _json.loads(plan_json_str) if isinstance(plan_json_str, str) else plan_json_str
            if response_type == "answer":
                return data.get("answer")
            if response_type == "analysis":
                return data.get("analysis")
            # implementation plan — return problem_statement + brief phase list
            parts = []
            if data.get("problem_statement"):
                parts.append(data["problem_statement"])
            phases = data.get("phases") or []
            if phases:
                phase_lines = "\n".join(
                    f"- **Phase {p.get('phase_number', i+1)}:** {p.get('title', '')}"
                    for i, p in enumerate(phases[:6])
                )
                parts.append(phase_lines)
            return "\n\n".join(parts) if parts else None
        except Exception:
            return None

    return {
        "repo": f"{owner}/{name}",
        "total": count_row or 0,
        "limit": limit,
        "offset": offset,
        "interactions": [
            {
                "id": r["id"],
                "type": r["interaction_type"],
                "query": r["query"],
                "complexity": r["query_complexity"],
                "quality_score": round(r["implicit_quality_score"], 3) if r["implicit_quality_score"] is not None else None,
                "user_rating": r["user_rating"],
                "iterations": r["retrieval_iterations"],
                "tool_calls": r["tool_calls_count"],
                "context_tokens": r["context_tokens"],
                "answer_tokens": r["answer_tokens"],
                "elapsed_ms": round(r["elapsed_ms"]) if r["elapsed_ms"] else None,
                "params": {
                    "strategy": r["retrieval_strategy"],
                    "hnsw_ef": r["hnsw_ef_search_used"],
                    "rrf_k": r["rrf_k_used"],
                    "reranker_top_n": r["reranker_top_n_used"],
                    "rel_threshold": r["relevance_threshold_used"],
                    "max_iter": r["max_iterations_used"],
                },
                "session_id": str(r["session_id"]) if r["session_id"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                # Response content
                "response": (
                    r["ask_answer"]
                    if r["interaction_type"] == "ask"
                    else _plan_response_text(r["plan_json"], r["plan_response_type"])
                ),
                "cited_files": list(r["ask_cited_files"]) if r["ask_cited_files"] else [],
                "plan_response_type": r["plan_response_type"],
            }
            for r in rows
        ],
    }


@router.post(
    "/experiments/{owner}/{name}/{exp_name}/rollout",
    summary="Apply winning experiment result",
)
async def rollout_experiment(
    owner: str,
    name: str,
    exp_name: str,
    winner: str = Query(..., pattern="^(control|treatment)$"),
) -> dict:
    """Permanently apply the winning value from a completed A/B experiment."""
    from src.evolution.ab_testing import rollout_experiment as _rollout

    await _rollout(owner, name, exp_name, winner)
    return {"ok": True, "repo": f"{owner}/{name}", "experiment": exp_name, "applied": winner}
