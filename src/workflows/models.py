"""
Pydantic models for the Workflow DSL.
These represent parsed workflow definitions in memory.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TriggerType(str, Enum):
    webhook = "webhook"
    schedule = "schedule"
    manual = "manual"
    event = "event"


class StepType(str, Enum):
    agent = "agent"
    action = "action"
    human_checkpoint = "human_checkpoint"


class AgentRole(str, Enum):
    searcher = "searcher"
    planner = "planner"
    reviewer = "reviewer"
    coder = "coder"
    tester = "tester"
    supervisor = "supervisor"


class TriggerConfig(BaseModel):
    type: TriggerType = TriggerType.manual
    filter: dict[str, Any] = Field(default_factory=dict)
    cron_expr: str | None = None
    webhook_path: str | None = None
    event_topic: str | None = None


class StepDef(BaseModel):
    id: str
    type: StepType = StepType.agent
    role: AgentRole | None = None
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


class WorkflowDef(BaseModel):
    name: str
    description: str = ""
    version: int = 1
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    context: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepDef] = Field(default_factory=list)
