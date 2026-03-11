"""
Pillar 3 — The Evolution Engine: Self-reflection cycle.

Orchestrates the full reflection loop for a repository:
  1. Analyze interaction metrics (query patterns + retrieval efficiency)
  2. Call LLM to propose improvements
  3. Apply parameter changes autonomously (via param_tuner)
  4. Apply prompt improvements autonomously (via param_tuner)
  5. Regenerate the repo worldview
  6. Log everything in evolution_log

Can be triggered:
  - Manually via POST /evolution/cycle/{owner}/{name}
  - Automatically from the indexing pipeline (when interaction threshold is met)
  - Via the MCP tool: reflect_and_improve
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text

from src.config import settings
from src.evolution.insights import analyze_query_patterns, analyze_retrieval_efficiency
from src.evolution.param_tuner import apply_parameter_changes, apply_prompt_improvements
from src.evolution.worldview_generator import generate_worldview, get_latest_worldview_text
from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_REFLECTION_SYSTEM = """\
You are NexusCode's evolution engine — an AI system that improves itself.

You will receive:
1. Query pattern analysis: which query types have weak or strong retrieval quality
2. Retrieval efficiency analysis: which parameter settings correlate with better outcomes
3. Current codebase worldview: semantic understanding of the repository

Your task: propose specific, bounded improvements to retrieval parameters and system prompts
that will improve NexusCode's performance for this repository.

CONSTRAINTS:
- Parameter changes must stay within these bounds:
  hnsw_ef_search: 10–200
  retrieval_rrf_k: 30–150
  retrieval_candidate_multiplier: 2–10
  reranker_top_n: 10–50
  query_relevance_threshold: 0.10–0.70
  ask_max_iterations: 1–5
  plan_max_iterations: 2–8

- Prompt improvements should be:
  * Specific, actionable, and short (1-3 sentences)
  * Target either "ask_system_prompt" or "planning_system_prompt"
  * Informed by the actual weak query patterns observed

- Only propose changes where there is clear signal. If the data is insufficient,
  say so in the rationale and propose nothing.

Respond with ONLY valid JSON, no preamble:
{
  "parameter_changes": {
    "hnsw_ef_search": {"suggested": 60, "reason": "..."},
    "ask_max_iterations": {"suggested": 4, "reason": "..."}
  },
  "prompt_improvements": [
    {
      "target": "ask_system_prompt",
      "change": "When the query involves async error handling, explicitly search for exception handling patterns and middleware.",
      "reason": "Complex async error queries consistently score below 0.5 quality."
    }
  ],
  "new_patterns": ["pattern discovered about this codebase"],
  "rationale": "Summary of why these changes are proposed."
}
"""


@dataclass
class EvolutionCycleResult:
    cycle_number: int
    repo_owner: str
    repo_name: str
    metrics_analyzed: int
    parameters_changed: list[dict] = field(default_factory=list)
    prompts_improved: list[dict] = field(default_factory=list)
    discovered_patterns: list[str] = field(default_factory=list)
    new_worldview_version: int | None = None
    status: str = "complete"
    error: str | None = None


async def _get_next_cycle_number(repo_owner: str, repo_name: str) -> int:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT MAX(cycle_number) AS max_n FROM evolution_log
                    WHERE repo_owner = :owner AND repo_name = :name
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).mappings().first()
    return int(row["max_n"] or 0) + 1


async def _create_log_entry(repo_owner: str, repo_name: str, cycle_number: int, lookback_days: int) -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                INSERT INTO evolution_log (
                    repo_owner, repo_name, cycle_number,
                    lookback_days, status, cycle_started_at
                ) VALUES (
                    :owner, :name, :cycle, :days, 'analyzing', :started
                )
                ON CONFLICT (repo_owner, repo_name, cycle_number) DO UPDATE
                    SET status = 'analyzing', cycle_started_at = :started
                RETURNING id
            """),
            {
                "owner": repo_owner,
                "name": repo_name,
                "cycle": cycle_number,
                "days": lookback_days,
                "started": datetime.now(timezone.utc),
            },
        )
        log_id = result.scalar_one()
        await session.commit()
    return log_id


async def _complete_log_entry(
    log_id: int,
    cycle_number: int,
    repo_owner: str,
    repo_name: str,
    metrics_count: int,
    param_changes: list[dict],
    prompt_changes: list[dict],
    patterns: list[str],
    worldview_version: int | None,
    status: str,
    error: str | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE evolution_log
                SET status = :status,
                    cycle_completed_at = :completed,
                    metrics_analyzed_count = :metrics,
                    improvements_proposed = :proposed,
                    improvements_applied = :applied,
                    parameter_changes = :param_changes,
                    prompt_changes = :prompt_changes,
                    discovered_patterns = :patterns,
                    new_worldview_version = :wv_version,
                    error_message = :error
                WHERE id = :log_id
            """),
            {
                "log_id": log_id,
                "status": status,
                "completed": datetime.now(timezone.utc),
                "metrics": metrics_count,
                "proposed": len(param_changes) + len(prompt_changes),
                "applied": len(param_changes) + len(prompt_changes),
                "param_changes": json.dumps(param_changes) if param_changes else None,
                "prompt_changes": json.dumps(prompt_changes) if prompt_changes else None,
                "patterns": patterns or None,
                "wv_version": worldview_version,
                "error": error,
            },
        )
        await session.commit()


async def _call_llm_for_improvements(
    query_insights_summary: str,
    retrieval_insights_summary: str,
    worldview_text: str,
    model: str,
) -> dict:
    """Call the LLM to synthesize improvement proposals."""
    from src.llm.client import get_client

    client = get_client()
    prompt = (
        "## Query Pattern Analysis\n"
        f"{query_insights_summary}\n\n"
        "## Retrieval Efficiency Analysis\n"
        f"{retrieval_insights_summary}\n\n"
        "## Current Codebase Worldview\n"
        f"{worldview_text[:2000] if worldview_text else '(No worldview available yet.)'}"
    )

    response = await client.messages.create(
        model=model,
        max_tokens=2048,
        system=_REFLECTION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Strip markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]

    return json.loads(raw)


async def should_run_cycle(repo_owner: str, repo_name: str) -> bool:
    """
    Return True if the interaction threshold since the last cycle has been met.
    Used by the pipeline to decide whether to auto-trigger.
    """
    if not settings.evolution_enabled:
        return False

    async with AsyncSessionLocal() as session:
        # Find the last completed cycle
        last_cycle = (
            await session.execute(
                text("""
                    SELECT cycle_completed_at FROM evolution_log
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND status = 'complete'
                    ORDER BY cycle_number DESC LIMIT 1
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).mappings().first()

        # Count interactions since last cycle (or ever)
        since_clause = (
            "AND created_at > :since"
            if last_cycle and last_cycle["cycle_completed_at"]
            else ""
        )
        count_row = (
            await session.execute(
                text(f"""
                    SELECT COUNT(*) AS cnt FROM interaction_metrics
                    WHERE repo_owner = :owner AND repo_name = :name
                    {since_clause}
                """),
                {
                    "owner": repo_owner,
                    "name": repo_name,
                    "since": last_cycle["cycle_completed_at"] if last_cycle and last_cycle["cycle_completed_at"] else None,
                },
            )
        ).mappings().first()

    count = int(count_row["cnt"] or 0)
    return count >= settings.evolution_min_interactions_to_reflect


async def run_reflection_cycle(
    repo_owner: str,
    repo_name: str,
    lookback_days: int = 30,
    force: bool = False,
) -> EvolutionCycleResult:
    """
    Run a full self-reflection cycle for a repository.

    Steps:
    1. Check if cycle is warranted (unless force=True)
    2. Create evolution_log entry
    3. Analyze query patterns + retrieval efficiency
    4. Call LLM for improvement proposals
    5. Apply parameter changes
    6. Apply prompt improvements
    7. Regenerate worldview
    8. Complete log entry

    Returns EvolutionCycleResult with full summary.
    """
    if not force and not await should_run_cycle(repo_owner, repo_name):
        logger.info(
            "evolution: skipping cycle for %s/%s (threshold not met)",
            repo_owner,
            repo_name,
        )
        return EvolutionCycleResult(
            cycle_number=0,
            repo_owner=repo_owner,
            repo_name=repo_name,
            metrics_analyzed=0,
            status="skipped",
        )

    cycle_number = await _get_next_cycle_number(repo_owner, repo_name)
    log_id = await _create_log_entry(repo_owner, repo_name, cycle_number, lookback_days)

    logger.info(
        "evolution: starting cycle #%d for %s/%s",
        cycle_number,
        repo_owner,
        repo_name,
    )

    try:
        # Step 1: Analyse patterns
        query_insights = await analyze_query_patterns(repo_owner, repo_name, days=lookback_days)
        retrieval_insights = await analyze_retrieval_efficiency(repo_owner, repo_name, days=lookback_days)

        # Step 2: Get current worldview
        worldview_text = await get_latest_worldview_text(repo_owner, repo_name)

        # Step 3: LLM generates proposals (only if there's data to work with)
        param_changes: list[dict] = []
        prompt_changes: list[dict] = []
        discovered_patterns: list[str] = []

        if query_insights.total_interactions >= 5:
            try:
                proposal = await _call_llm_for_improvements(
                    query_insights_summary=query_insights.summary,
                    retrieval_insights_summary=retrieval_insights.summary,
                    worldview_text=worldview_text,
                    model=settings.default_model,
                )

                # Step 4: Apply parameter changes
                raw_param_proposals = proposal.get("parameter_changes") or {}
                param_changes = apply_parameter_changes(
                    raw_param_proposals,
                    max_change_pct=settings.evolution_max_param_change_pct,
                )

                # Step 5: Apply prompt improvements
                raw_prompt_proposals = proposal.get("prompt_improvements") or []
                prompt_changes = apply_prompt_improvements(raw_prompt_proposals)

                discovered_patterns = proposal.get("new_patterns") or []

                # Step 6: Create A/B experiments for parameter changes
                from src.evolution.ab_testing import start_ab_experiment

                for change in param_changes:
                    await start_ab_experiment(
                        repo_owner=repo_owner,
                        repo_name=repo_name,
                        experiment_name=f"cycle{cycle_number}_{change['param']}",
                        parameter_name=change["param"],
                        control_value=str(change["old"]),
                        treatment_value=str(change["new"]),
                        evolution_cycle_id=log_id,
                    )

            except json.JSONDecodeError:
                logger.warning("evolution: LLM returned non-JSON proposal — skipping parameter changes")
            except Exception:
                logger.exception("evolution: improvement proposals failed (non-fatal)")
        else:
            logger.info(
                "evolution: only %d interactions found — skipping LLM proposals (need ≥5)",
                query_insights.total_interactions,
            )

        # Step 7: Regenerate worldview
        wv_doc = await generate_worldview(repo_owner, repo_name)
        new_worldview_version = wv_doc.version if wv_doc else None

        # Step 8: Complete log
        await _complete_log_entry(
            log_id=log_id,
            cycle_number=cycle_number,
            repo_owner=repo_owner,
            repo_name=repo_name,
            metrics_count=query_insights.total_interactions,
            param_changes=param_changes,
            prompt_changes=prompt_changes,
            patterns=discovered_patterns,
            worldview_version=new_worldview_version,
            status="complete",
        )

        logger.info(
            "evolution: cycle #%d complete for %s/%s — "
            "%d param changes, %d prompt changes, worldview v%s",
            cycle_number,
            repo_owner,
            repo_name,
            len(param_changes),
            len(prompt_changes),
            new_worldview_version or "none",
        )

        return EvolutionCycleResult(
            cycle_number=cycle_number,
            repo_owner=repo_owner,
            repo_name=repo_name,
            metrics_analyzed=query_insights.total_interactions,
            parameters_changed=param_changes,
            prompts_improved=prompt_changes,
            discovered_patterns=discovered_patterns,
            new_worldview_version=new_worldview_version,
            status="complete",
        )

    except Exception as exc:
        error_msg = str(exc)
        logger.exception("evolution: cycle #%d failed for %s/%s", cycle_number, repo_owner, repo_name)
        await _complete_log_entry(
            log_id=log_id,
            cycle_number=cycle_number,
            repo_owner=repo_owner,
            repo_name=repo_name,
            metrics_count=0,
            param_changes=[],
            prompt_changes=[],
            patterns=[],
            worldview_version=None,
            status="failed",
            error=error_msg[:500],
        )
        return EvolutionCycleResult(
            cycle_number=cycle_number,
            repo_owner=repo_owner,
            repo_name=repo_name,
            metrics_analyzed=0,
            status="failed",
            error=error_msg[:500],
        )
