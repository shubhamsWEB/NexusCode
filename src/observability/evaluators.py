"""
LangSmith evaluation datasets and evaluators for NexusCode workflows.

Provides:
  - create_baseline_dataset()   — seed golden examples for PM→Dev workflow quality
  - run_prd_evaluation()        — evaluate PRD completeness against golden examples
  - run_workflow_evaluation()   — full workflow quality evaluation
  - WorkflowEvaluator           — Anthropic-powered custom evaluator

Usage:
    from src.observability.evaluators import create_baseline_dataset, run_prd_evaluation

    # One-time: seed the dataset
    await create_baseline_dataset()

    # After a workflow run: evaluate output quality
    results = await run_prd_evaluation(prd_text, feature_request)
"""

from __future__ import annotations

import json
from typing import Any

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── LangSmith client (lazy, non-fatal if not configured) ──────────────────────


def _get_ls_client():
    """Return a LangSmith Client, or None if langsmith is not configured."""
    try:
        from langsmith import Client
        from src.config import settings
        if not settings.langsmith_api_key:
            return None
        return Client(api_key=settings.langsmith_api_key)
    except ImportError:
        return None
    except Exception as exc:
        logger.warning("evaluators: could not create LangSmith client: %s", exc)
        return None


# ── Golden example datasets ────────────────────────────────────────────────────

_BASELINE_PRD_EXAMPLES = [
    {
        "inputs": {
            "feature_request": "Add OAuth2 login with Google to the authentication flow",
            "repo_name": "example/webapp",
        },
        "outputs": {
            "required_sections": [
                "## Overview", "## Problem Statement", "## User Stories",
                "## Acceptance Criteria", "## Technical Constraints",
                "## Out of Scope", "## Success Metrics",
            ],
            "min_user_stories": 2,
            "min_acceptance_criteria": 3,
        },
    },
    {
        "inputs": {
            "feature_request": "Implement rate limiting on the public API endpoints",
            "repo_name": "example/api-server",
        },
        "outputs": {
            "required_sections": [
                "## Overview", "## User Stories", "## Acceptance Criteria",
                "## Technical Constraints",
            ],
            "min_user_stories": 1,
            "min_acceptance_criteria": 4,
        },
    },
    {
        "inputs": {
            "feature_request": "Add real-time notifications for workflow completion",
            "repo_name": "example/nexuscode",
        },
        "outputs": {
            "required_sections": [
                "## Overview", "## Problem Statement", "## User Stories",
                "## Acceptance Criteria", "## Success Metrics",
            ],
            "min_user_stories": 3,
            "min_acceptance_criteria": 3,
        },
    },
]

_BASELINE_REVIEW_EXAMPLES = [
    {
        "inputs": {
            "code_diff": "def get_user(user_id):\n    query = f'SELECT * FROM users WHERE id = {user_id}'\n    return db.execute(query)",
            "language": "python",
        },
        "outputs": {
            "expected_verdict": "needs_revision",
            "expected_issues": ["sql injection", "parameterized"],
        },
    },
    {
        "inputs": {
            "code_diff": "async def get_user(user_id: int) -> User | None:\n    return await db.query(User).filter(User.id == user_id).first()",
            "language": "python",
        },
        "outputs": {
            "expected_verdict": "approved",
            "expected_issues": [],
        },
    },
]


async def create_baseline_dataset(dataset_name: str = "nexuscode-workflow-baseline") -> dict[str, Any]:
    """
    Create (or verify) the baseline evaluation datasets in LangSmith.
    Safe to call repeatedly — skips creation if the dataset already exists.

    Returns a dict with dataset IDs and example counts.
    """
    client = _get_ls_client()
    if client is None:
        logger.info("evaluators: LangSmith not configured — skipping dataset creation")
        return {"status": "skipped", "reason": "LangSmith not configured"}

    results: dict[str, Any] = {}

    # PRD quality dataset
    prd_dataset_name = f"{dataset_name}-prd"
    try:
        try:
            prd_ds = client.read_dataset(dataset_name=prd_dataset_name)
            results["prd_dataset_id"] = str(prd_ds.id)
            results["prd_dataset_status"] = "existing"
        except Exception:
            prd_ds = client.create_dataset(
                prd_dataset_name,
                description="Golden examples for PM Agent PRD quality evaluation",
            )
            client.create_examples(
                inputs=[ex["inputs"] for ex in _BASELINE_PRD_EXAMPLES],
                outputs=[ex["outputs"] for ex in _BASELINE_PRD_EXAMPLES],
                dataset_id=prd_ds.id,
            )
            results["prd_dataset_id"] = str(prd_ds.id)
            results["prd_dataset_status"] = "created"
            results["prd_examples_added"] = len(_BASELINE_PRD_EXAMPLES)
    except Exception as exc:
        logger.error("evaluators: failed to create PRD dataset: %s", exc)
        results["prd_dataset_error"] = str(exc)

    # Code review dataset
    review_dataset_name = f"{dataset_name}-review"
    try:
        try:
            rev_ds = client.read_dataset(dataset_name=review_dataset_name)
            results["review_dataset_id"] = str(rev_ds.id)
            results["review_dataset_status"] = "existing"
        except Exception:
            rev_ds = client.create_dataset(
                review_dataset_name,
                description="Golden examples for Reviewer Agent verdict accuracy",
            )
            client.create_examples(
                inputs=[ex["inputs"] for ex in _BASELINE_REVIEW_EXAMPLES],
                outputs=[ex["outputs"] for ex in _BASELINE_REVIEW_EXAMPLES],
                dataset_id=rev_ds.id,
            )
            results["review_dataset_id"] = str(rev_ds.id)
            results["review_dataset_status"] = "created"
            results["review_examples_added"] = len(_BASELINE_REVIEW_EXAMPLES)
    except Exception as exc:
        logger.error("evaluators: failed to create review dataset: %s", exc)
        results["review_dataset_error"] = str(exc)

    logger.info("evaluators: dataset setup complete: %s", results)
    return results


# ── Evaluator functions ────────────────────────────────────────────────────────


def evaluate_prd_completeness(prd_text: str, expected_outputs: dict) -> dict[str, Any]:
    """
    Rule-based PRD completeness evaluator.
    Checks for required sections and minimum counts.

    Returns {"score": 0.0-1.0, "passed": bool, "details": [...]}
    """
    if not prd_text:
        return {"score": 0.0, "passed": False, "details": ["PRD is empty"]}

    required = expected_outputs.get("required_sections", [])
    min_stories = expected_outputs.get("min_user_stories", 1)
    min_criteria = expected_outputs.get("min_acceptance_criteria", 1)

    details = []
    found_sections = 0

    for section in required:
        if section.lower() in prd_text.lower():
            found_sections += 1
        else:
            details.append(f"Missing section: {section}")

    # Count user stories (lines starting with "As a")
    story_count = prd_text.lower().count("as a ")
    if story_count < min_stories:
        details.append(f"Only {story_count} user stories found (min {min_stories})")

    # Count acceptance criteria (numbered lines or lines with checkboxes)
    import re
    criteria_count = len(re.findall(r'^\s*\d+\.|\[\s*\]|\-\s+\w', prd_text, re.MULTILINE))
    if criteria_count < min_criteria:
        details.append(f"Only {criteria_count} criteria found (min {min_criteria})")

    section_score = found_sections / max(1, len(required))
    story_score = min(1.0, story_count / max(1, min_stories))
    criteria_score = min(1.0, criteria_count / max(1, min_criteria))
    score = (section_score * 0.5) + (story_score * 0.25) + (criteria_score * 0.25)

    return {
        "score": round(score, 3),
        "passed": score >= 0.75,
        "details": details or ["All checks passed"],
        "sections_found": found_sections,
        "sections_required": len(required),
        "user_stories_found": story_count,
        "criteria_found": criteria_count,
    }


def evaluate_review_verdict(review_text: str, expected_outputs: dict) -> dict[str, Any]:
    """
    Evaluate reviewer verdict accuracy against golden examples.
    Returns {"score": 0.0-1.0, "passed": bool, "predicted_verdict": str}
    """
    review_lower = review_text.lower()

    # Extract verdict from first line or first occurrence
    predicted = "unknown"
    for verdict in ("approved", "needs_revision", "escalate"):
        if verdict in review_lower:
            predicted = verdict
            break

    expected_verdict = expected_outputs.get("expected_verdict", "")
    expected_issues = expected_outputs.get("expected_issues", [])

    verdict_correct = predicted == expected_verdict
    issues_found = all(issue.lower() in review_lower for issue in expected_issues)

    score = (0.6 if verdict_correct else 0.0) + (0.4 if issues_found else 0.0)

    return {
        "score": round(score, 3),
        "passed": verdict_correct,
        "predicted_verdict": predicted,
        "expected_verdict": expected_verdict,
        "issues_detected": issues_found,
    }


async def run_prd_evaluation(
    prd_text: str,
    feature_request: str,
    dataset_name: str = "nexuscode-workflow-baseline-prd",
) -> dict[str, Any]:
    """
    Evaluate a PRD against the closest matching golden example.
    Can be called after a PM Agent step completes.
    """
    client = _get_ls_client()

    # Find best matching example by feature_request similarity
    best_example = _BASELINE_PRD_EXAMPLES[0]  # default to first
    for ex in _BASELINE_PRD_EXAMPLES:
        ex_req = ex["inputs"].get("feature_request", "").lower()
        if any(word in ex_req for word in feature_request.lower().split()):
            best_example = ex
            break

    result = evaluate_prd_completeness(prd_text, best_example["outputs"])

    if client:
        # Log as a LangSmith run for tracking
        try:
            client.create_run(
                name="prd_evaluation",
                run_type="evaluator",
                inputs={"feature_request": feature_request, "prd_length": len(prd_text)},
                outputs=result,
            )
        except Exception as exc:
            logger.debug("evaluators: could not log evaluation run: %s", exc)

    return result


async def run_workflow_evaluation(
    workflow_outputs: dict[str, Any],
    workflow_name: str,
) -> dict[str, Any]:
    """
    Evaluate a complete workflow run across all available metrics.

    workflow_outputs should contain: prd, review_verdict, test_plan, deployment_plan, etc.
    Returns a summary dict with per-step scores and an overall quality score.
    """
    scores: dict[str, Any] = {}
    total_score = 0.0
    count = 0

    if workflow_outputs.get("prd"):
        prd_result = evaluate_prd_completeness(
            workflow_outputs["prd"],
            _BASELINE_PRD_EXAMPLES[0]["outputs"],
        )
        scores["prd"] = prd_result
        total_score += prd_result["score"]
        count += 1

    if workflow_outputs.get("review_verdict"):
        # Check review has structured feedback
        rv = workflow_outputs["review_verdict"]
        scores["review_verdict"] = {
            "score": 1.0 if rv in ("approved", "needs_revision", "escalate") else 0.0,
            "verdict": rv,
        }
        total_score += scores["review_verdict"]["score"]
        count += 1

    if workflow_outputs.get("test_plan"):
        tp = workflow_outputs["test_plan"]
        has_unit = "unit" in tp.lower()
        has_integration = "integration" in tp.lower()
        tp_score = (0.5 if has_unit else 0) + (0.5 if has_integration else 0)
        scores["test_plan"] = {"score": tp_score, "has_unit": has_unit, "has_integration": has_integration}
        total_score += tp_score
        count += 1

    if workflow_outputs.get("deployment_plan"):
        dp = workflow_outputs["deployment_plan"]
        has_rollback = "rollback" in dp.lower()
        has_steps = "step" in dp.lower() or "1." in dp
        dp_score = (0.5 if has_rollback else 0) + (0.5 if has_steps else 0)
        scores["deployment_plan"] = {"score": dp_score, "has_rollback": has_rollback}
        total_score += dp_score
        count += 1

    overall = round(total_score / max(1, count), 3)

    return {
        "workflow": workflow_name,
        "overall_score": overall,
        "passed": overall >= 0.7,
        "step_scores": scores,
        "steps_evaluated": count,
    }
