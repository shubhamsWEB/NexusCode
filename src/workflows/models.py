"""
Pydantic models for the Workflow DSL.
These represent parsed workflow definitions in memory.

Graph-style workflows extend the base models with:
  - RouteCondition:  a conditional edge (condition expression → goto step)
  - StepDef.routes:  list of RouteCondition defining outgoing conditional edges
  - StepDef.max_loops:  safety cap — how many times a step may loop back here
  - StepDef.state_output_key: which GraphState field this step's output maps to
  - StepType.router: a pure branching node (no LLM — just evaluates conditions)
  - StepType.integration: a non-LLM external service call (Jira, Slack, etc.)

Backward compatibility: workflows with no `routes` on any step run through
the existing DAG executor unchanged.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TriggerType(StrEnum):
    webhook = "webhook"
    schedule = "schedule"
    manual = "manual"
    event = "event"


class StepType(StrEnum):
    agent = "agent"
    action = "action"
    human_checkpoint = "human_checkpoint"
    router = "router"           # pure conditional branch — no LLM call
    integration = "integration" # external service call (Jira, Slack, GitHub, Figma…)


class AgentRole(StrEnum):
    # Dev-focused roles (existing)
    searcher = "searcher"
    planner = "planner"
    reviewer = "reviewer"
    coder = "coder"
    tester = "tester"
    supervisor = "supervisor"
    # Enterprise roles (new)
    pm_agent = "pm_agent"
    designer_agent = "designer_agent"
    qa_agent = "qa_agent"
    devops_agent = "devops_agent"


class RouteCondition(BaseModel):
    """
    A single conditional edge leaving a step.

    condition: a Python expression evaluated against the current GraphState dict.
               Use state field names directly, e.g.:
                 "review_verdict == 'approved'"
                 "loop_counts.get('coder_agent', 0) >= 2"
               Omit (or set None) to make this a default/fallback route.
               Routes are evaluated in order — first match wins.

    goto:      target step_id, or "END" to finish the workflow.

    Example YAML:
        routes:
          - condition: "review_verdict == 'approved'"
            goto: devops_agent
          - condition: "loop_counts.get('coder_agent', 0) >= 3"
            goto: supervisor_step   # escalate after 3 revision loops
          - goto: coder_agent       # default: loop back for revision
    """
    condition: str | None = None   # None = default/fallback (always matches)
    goto: str = "END"              # target step_id or literal "END"


class TriggerConfig(BaseModel):
    type: TriggerType = TriggerType.manual
    filter: dict[str, Any] = Field(default_factory=dict)
    cron_expr: str | None = None
    webhook_path: str | None = None
    event_topic: str | None = None


class StepDef(BaseModel):
    id: str
    type: StepType = StepType.agent
    role: str | None = None
    task: str | None = None
    action: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    parallel_with: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    context_inject: list[dict[str, str]] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    # Human checkpoint fields
    prompt: str | None = None
    options: list[str] = Field(default_factory=list)
    timeout_hours: int = 24
    on_timeout: str = "fail"  # "fail" | "skip" | "continue"
    # Retry config
    max_retries: int = 2
    retry_delay_seconds: int = 5
    # ── Graph routing fields (new) ─────────────────────────────────────────
    routes: list[RouteCondition] = Field(
        default_factory=list,
        description=(
            "Conditional outgoing edges from this step. Evaluated in order; "
            "first matching condition wins. If empty, uses depends_on-derived DAG edges."
        ),
    )
    max_loops: int = Field(
        5,
        description=(
            "Maximum times any backward route from this step may loop back to "
            "an already-visited node. Prevents infinite revision cycles."
        ),
    )
    state_output_key: str | None = Field(
        None,
        description=(
            "GraphState field name that this step's output should be written to "
            "(e.g. 'prd', 'implementation_plan', 'code_diff'). "
            "When set, the agent's final answer text is stored in both "
            "step_outputs[step_id] AND state[state_output_key]."
        ),
    )
    # ── Integration step fields (new) ─────────────────────────────────────
    integration: str | None = Field(
        None,
        description=(
            "Integration service + operation for StepType.integration steps. "
            "Format: 'service.operation', e.g. 'jira.create_issue', 'slack.send_message', "
            "'github.create_pr', 'figma.get_file', 'notion.create_page'."
        ),
    )


class WorkflowDef(BaseModel):
    name: str
    description: str = ""
    version: int = 1
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    context: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepDef] = Field(default_factory=list)

    @property
    def is_graph_style(self) -> bool:
        """True if any step uses conditional routing (routes field is non-empty)."""
        return any(s.routes for s in self.steps)
