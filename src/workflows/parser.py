"""
YAML DSL parser and DAG validator for workflow definitions.
"""
from __future__ import annotations

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from src.utils.logging import get_secure_logger
from src.workflows.models import StepDef, WorkflowDef

logger = get_secure_logger(__name__)


class WorkflowParseError(ValueError):
    """Raised when a workflow definition is invalid."""


def parse_workflow(yaml_text: str) -> WorkflowDef:
    """
    Parse a YAML workflow definition into a WorkflowDef model.
    Raises WorkflowParseError on invalid YAML or schema violations.
    """
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"Invalid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise WorkflowParseError("Workflow definition must be a YAML mapping")

    try:
        wf = WorkflowDef.model_validate(raw)
    except ValidationError as exc:
        raise WorkflowParseError(f"Schema validation failed: {exc}") from exc

    _validate_dag(wf)
    return wf


def _validate_dag(wf: WorkflowDef) -> None:
    """Validate that:
    1. All step IDs are unique
    2. All depends_on references point to existing step IDs
    3. No cycles exist (topological sort)
    """
    step_ids = {s.id for s in wf.steps}

    # Check uniqueness
    if len(step_ids) != len(wf.steps):
        seen: set[str] = set()
        for s in wf.steps:
            if s.id in seen:
                raise WorkflowParseError(f"Duplicate step id: {s.id!r}")
            seen.add(s.id)

    # Check all depends_on references exist
    for step in wf.steps:
        for dep in step.depends_on:
            if dep not in step_ids:
                raise WorkflowParseError(
                    f"Step {step.id!r} depends on unknown step {dep!r}"
                )

    # Topological sort (Kahn's algorithm) to detect cycles
    in_degree: dict[str, int] = {s.id: 0 for s in wf.steps}
    adjacency: dict[str, list[str]] = {s.id: [] for s in wf.steps}
    for step in wf.steps:
        for dep in step.depends_on:
            adjacency[dep].append(step.id)
            in_degree[step.id] += 1

    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for neighbor in adjacency[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited != len(wf.steps):
        raise WorkflowParseError(
            "Workflow contains a dependency cycle — check your depends_on fields"
        )


def topological_order(wf: WorkflowDef) -> list[list[StepDef]]:
    """
    Return steps grouped into execution waves (each wave can run in parallel).
    Wave 0 = steps with no dependencies, Wave 1 = steps that depend only on Wave 0, etc.
    """
    step_map = {s.id: s for s in wf.steps}
    in_degree: dict[str, int] = {s.id: 0 for s in wf.steps}
    adjacency: dict[str, list[str]] = {s.id: [] for s in wf.steps}

    for step in wf.steps:
        for dep in step.depends_on:
            adjacency[dep].append(step.id)
            in_degree[step.id] += 1

    waves: list[list[StepDef]] = []
    ready = [sid for sid, deg in in_degree.items() if deg == 0]

    while ready:
        wave = [step_map[sid] for sid in sorted(ready)]  # sort for determinism
        waves.append(wave)
        next_ready: list[str] = []
        for step in wave:
            for neighbor in adjacency[step.id]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_ready.append(neighbor)
        ready = next_ready

    return waves
