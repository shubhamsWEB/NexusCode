"""
Workflow execution context — Jinja2 templating and inter-step data passing.
"""
from __future__ import annotations

import json
from typing import Any

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

try:
    from jinja2 import Environment, Undefined, UndefinedError

    class _MissingMarker(Undefined):
        """
        Renders missing template variables as ``[MISSING: field_name]`` instead of
        raising UndefinedError or silently producing an empty string.

        This ensures agents receive a clear, human-readable signal when a trigger
        payload field was not provided, rather than raw Jinja2 syntax or blank text.

        Example: ``{{ trigger.service }}`` with an empty payload → ``[MISSING: service]``
        """

        def __str__(self) -> str:
            return f"[MISSING: {self._undefined_name or 'unknown'}]"

        def __repr__(self) -> str:
            return self.__str__()

        def __iter__(self):
            return iter([])

        def __len__(self) -> int:
            return 0

        def __bool__(self) -> bool:
            return False

    _jinja_env = Environment(undefined=_MissingMarker)
    _JINJA_AVAILABLE = True
except ImportError:
    _JINJA_AVAILABLE = False
    logger.warning("jinja2 not installed — template rendering disabled")


class ExecutionContext:
    """
    Holds the mutable state of a running workflow.
    Provides Jinja2 template rendering with access to:
      - {{ trigger.* }}  — trigger payload fields
      - {{ context.* }}  — workflow-level context variables
      - {{ steps.<id>.output }}  — prior step outputs
    """

    def __init__(
        self,
        trigger_payload: dict[str, Any],
        workflow_context: dict[str, Any],
    ) -> None:
        self.trigger = trigger_payload
        self.context: dict[str, Any] = {}
        self._step_outputs: dict[str, Any] = {}

        # Render workflow-level context fields (they may reference trigger.*)
        for key, value in workflow_context.items():
            if isinstance(value, str):
                self.context[key] = self.render(value)
            else:
                self.context[key] = value

    def set_step_output(self, step_id: str, output: Any) -> None:
        """Store the output of a completed step."""
        self._step_outputs[step_id] = output

    def get_step_output(self, step_id: str) -> Any:
        """Retrieve the output of a completed step."""
        return self._step_outputs.get(step_id)

    def render(self, template_str: str, state: dict | None = None) -> str:
        """
        Render a Jinja2 template string with full execution context.

        Available template variables:
          - ``{{ trigger.FIELD }}``   — individual field from the trigger payload
          - ``{{ trigger_json }}``    — entire trigger payload as a pretty-printed JSON
                                        string; use this when the payload structure is
                                        unknown (e.g. alerts from different monitoring
                                        tools with different schemas)
          - ``{{ context.KEY }}``     — workflow-level context values
          - ``{{ steps.ID.output }}`` — output of a prior step
          - ``{{ state.FIELD }}``     — typed GraphState field (graph-style workflows)

        Missing trigger fields render as ``[MISSING: field_name]`` (via _MissingMarker)
        rather than raw Jinja2 syntax or silent empty strings.
        """
        if not _JINJA_AVAILABLE or "{{" not in template_str:
            return template_str
        try:
            tmpl = _jinja_env.from_string(template_str)
            return tmpl.render(
                trigger=self.trigger,
                trigger_json=json.dumps(self.trigger, indent=2, default=str),
                context=self.context,
                steps=_StepProxy(self._step_outputs),
                state=state or {},
            )
        except UndefinedError as exc:
            # Shouldn't happen with _MissingMarker, but kept as safety net
            logger.warning("template render UndefinedError: %s — using [MISSING] markers", exc)
            return template_str
        except Exception as exc:
            logger.warning("template render unexpected error: %s", exc)
            return template_str

    def render_dict(self, d: dict[str, Any]) -> dict[str, Any]:
        """Recursively render all string values in a dict."""
        result: dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, str):
                result[k] = self.render(v)
            elif isinstance(v, dict):
                result[k] = self.render_dict(v)
            else:
                result[k] = v
        return result

    def to_snapshot(self) -> dict[str, Any]:
        """Serialisable snapshot of current context for DB persistence."""
        return {
            "trigger": self.trigger,
            "context": self.context,
            "step_outputs": {k: _safe_serialise(v) for k, v in self._step_outputs.items()},
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> ExecutionContext:
        """Restore from a DB-persisted snapshot."""
        obj = cls(
            trigger_payload=snapshot.get("trigger", {}),
            workflow_context={},
        )
        obj.context = snapshot.get("context", {})
        obj._step_outputs = snapshot.get("step_outputs", {})
        return obj


class _StepProxy:
    """Allows {{ steps.my_step.output }} template access."""

    def __init__(self, outputs: dict[str, Any]) -> None:
        self._outputs = outputs

    def __getattr__(self, step_id: str) -> _StepResult:
        return _StepResult(self._outputs.get(step_id))


class _StepResult:
    def __init__(self, output: Any) -> None:
        self.output = output if output is not None else ""

    def __str__(self) -> str:
        if isinstance(self.output, str):
            return self.output
        return json.dumps(self.output, default=str)


def _safe_serialise(value: Any) -> Any:
    """Make a value JSON-serialisable."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {k: _safe_serialise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_serialise(i) for i in value]
    return str(value)
