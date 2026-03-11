"""
Pillar 3 — The Evolution Engine: Autonomous parameter tuning.

Applies bounded, logged parameter changes proposed by the reflection cycle.
Every change is recorded in the evolution_log for full auditability.
Changes are applied to the live settings singleton (hot-reload, no restart).
"""

from __future__ import annotations

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── Safe bounds — changes are clamped to these ranges ─────────────────────────
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "hnsw_ef_search": (10, 200),
    "retrieval_rrf_k": (30, 150),
    "retrieval_candidate_multiplier": (2, 10),
    "reranker_top_n": (10, 50),
    "query_relevance_threshold": (0.10, 0.70),
    "ask_max_iterations": (1, 5),
    "plan_max_iterations": (2, 8),
}

# Parameters where values should be cast to int
_INT_PARAMS = {
    "hnsw_ef_search",
    "retrieval_rrf_k",
    "retrieval_candidate_multiplier",
    "reranker_top_n",
    "ask_max_iterations",
    "plan_max_iterations",
}


def apply_parameter_changes(
    proposals: dict[str, dict],
    max_change_pct: float = 20.0,
) -> list[dict]:
    """
    Apply a dict of proposed parameter changes to the live settings singleton.

    ``proposals`` format:
        {
          "hnsw_ef_search": {"suggested": 60, "reason": "..."},
          ...
        }

    Returns a list of change records (what was actually applied):
        [{"param": "hnsw_ef_search", "old": 40, "new": 60, "reason": "..."}]
    """
    from src.config import settings

    applied: list[dict] = []

    for param, proposal in proposals.items():
        if param not in PARAM_BOUNDS:
            logger.warning("param_tuner: unknown parameter %r — skipping", param)
            continue

        suggested = proposal.get("suggested")
        reason = proposal.get("reason", "")
        if suggested is None:
            continue

        lo, hi = PARAM_BOUNDS[param]
        suggested = float(suggested)

        # Clamp to bounds
        clamped = max(lo, min(hi, suggested))

        # Get current value
        current = float(getattr(settings, param, None) or 0)
        if current == 0:
            logger.warning("param_tuner: could not read current value of %r", param)
            continue

        # Enforce max change % guard
        if current != 0:
            change_pct = abs(clamped - current) / abs(current) * 100
            if change_pct > max_change_pct:
                # Cap the change at max_change_pct in the right direction
                delta = current * (max_change_pct / 100) * (1 if clamped > current else -1)
                clamped = current + delta
                clamped = max(lo, min(hi, clamped))
                logger.info(
                    "param_tuner: capped %s change from %.1f%% to %.1f%%",
                    param,
                    change_pct,
                    max_change_pct,
                )

        # Cast to correct type
        new_val: int | float
        if param in _INT_PARAMS:
            new_val = int(round(clamped))
        else:
            new_val = round(clamped, 4)

        old_val: int | float
        if param in _INT_PARAMS:
            old_val = int(current)
        else:
            old_val = round(current, 4)

        if new_val == old_val:
            logger.debug("param_tuner: no change needed for %s (already %s)", param, old_val)
            continue

        # Apply to live settings — mutate the singleton directly
        try:
            object.__setattr__(settings, param, new_val)
            logger.info(
                "param_tuner: %s changed %s → %s (%s)",
                param,
                old_val,
                new_val,
                reason[:100],
            )
            applied.append({
                "param": param,
                "old": old_val,
                "new": new_val,
                "reason": reason,
            })
        except Exception:
            logger.exception("param_tuner: failed to set %s", param)

    return applied


def apply_prompt_improvements(
    improvements: list[dict],
) -> list[dict]:
    """
    Apply proposed prompt improvements to the in-memory prompt strings.

    Each improvement has the format:
        {
          "target": "ask_system_prompt" | "planning_system_prompt",
          "change": "specific instruction to add/modify",
          "reason": "why this helps"
        }

    Returns a list of applied change records.
    """
    applied: list[dict] = []

    for imp in improvements:
        target = imp.get("target", "")
        change = imp.get("change", "")
        reason = imp.get("reason", "")

        if not target or not change:
            continue

        try:
            if target == "ask_system_prompt":
                import src.ask.ask_agent as module
                old_prompt = module.ASK_SYSTEM_PROMPT
                # Append the improvement as a new instruction block
                module.ASK_SYSTEM_PROMPT = (
                    old_prompt.rstrip()
                    + f"\n\n## Learned Refinement\n{change}\n"
                )
                logger.info("param_tuner: updated ask system prompt (+%d chars)", len(change))
                applied.append({"target": target, "change": change[:200], "reason": reason, "old_length": len(old_prompt), "new_length": len(module.ASK_SYSTEM_PROMPT)})

            elif target == "planning_system_prompt":
                import src.planning.claude_planner as module
                old_prompt = module.PLANNING_SYSTEM_PROMPT
                module.PLANNING_SYSTEM_PROMPT = (
                    old_prompt.rstrip()
                    + f"\n\n## Learned Refinement\n{change}\n"
                )
                logger.info("param_tuner: updated planning system prompt (+%d chars)", len(change))
                applied.append({"target": target, "change": change[:200], "reason": reason, "old_length": len(old_prompt), "new_length": len(module.PLANNING_SYSTEM_PROMPT)})

            else:
                logger.warning("param_tuner: unknown prompt target %r", target)

        except Exception:
            logger.exception("param_tuner: failed to apply prompt improvement for target=%r", target)

    return applied
