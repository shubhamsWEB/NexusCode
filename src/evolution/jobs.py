"""
Pillar 3 — The Evolution Engine: RQ background job entry points.

These synchronous wrappers are called by Redis Queue (RQ) workers.
They bridge the sync RQ world to the async evolution cycle.
"""

from __future__ import annotations

import asyncio

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)


def run_reflection_cycle_job(
    repo_owner: str,
    repo_name: str,
    lookback_days: int = 30,
    force: bool = False,
) -> dict:
    """
    RQ worker entry point for a self-reflection cycle.

    Runs the full async cycle synchronously via asyncio.run().
    Returns a summary dict for RQ job result storage.
    """
    logger.info(
        "evolution job: starting reflection cycle for %s/%s (lookback=%d days)",
        repo_owner,
        repo_name,
        lookback_days,
    )

    result = asyncio.run(_async_reflection_cycle(repo_owner, repo_name, lookback_days, force))

    summary = {
        "repo": f"{repo_owner}/{repo_name}",
        "cycle_number": result.cycle_number,
        "status": result.status,
        "metrics_analyzed": result.metrics_analyzed,
        "parameters_changed": len(result.parameters_changed),
        "prompts_improved": len(result.prompts_improved),
        "discovered_patterns": len(result.discovered_patterns),
        "new_worldview_version": result.new_worldview_version,
    }

    if result.error:
        summary["error"] = result.error

    logger.info("evolution job: complete — %s", summary)
    return summary


async def _async_reflection_cycle(
    repo_owner: str,
    repo_name: str,
    lookback_days: int,
    force: bool,
):
    """Async implementation called by the sync RQ wrapper."""
    from src.evolution.reflection_cycle import run_reflection_cycle

    return await run_reflection_cycle(
        repo_owner=repo_owner,
        repo_name=repo_name,
        lookback_days=lookback_days,
        force=force,
    )


def generate_worldview_job(repo_owner: str, repo_name: str) -> dict:
    """
    RQ worker entry point for standalone worldview generation.
    Called after repo indexing if evolution_worldview_update_on_index is True.
    """
    logger.info("evolution job: generating worldview for %s/%s", repo_owner, repo_name)

    async def _run():
        from src.evolution.worldview_generator import generate_worldview
        return await generate_worldview(repo_owner, repo_name)

    doc = asyncio.run(_run())
    if doc:
        return {
            "repo": f"{repo_owner}/{repo_name}",
            "worldview_version": doc.version,
            "key_patterns": len(doc.key_patterns),
            "difficult_zones": len(doc.difficult_zones),
        }
    return {"repo": f"{repo_owner}/{repo_name}", "worldview_version": None, "skipped": True}
