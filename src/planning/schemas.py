"""
Pydantic schemas for the implementation planning feature.

The ImplementationPlan is the top-level output of POST /plan.
It describes every file change, the ordered execution steps,
risks, and a test plan.
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── Leaf schemas ──────────────────────────────────────────────────────────────

class CodeChange(BaseModel):
    """A single change within a file (add/modify/delete a symbol or block)."""
    kind: Literal["add", "modify", "delete", "move"] = "modify"
    symbol: Optional[str] = Field(None, description="Qualified symbol name (e.g. 'AuthService.login')")
    description: str = Field(..., description="What exactly changes and why")
    pseudocode: Optional[str] = Field(None, description="Pseudo-code sketch for complex logic")
    line_hint: Optional[str] = Field(None, description="Approximate line range, e.g. '42-55'")


class FileChange(BaseModel):
    """All changes required for a single file."""
    path: str = Field(..., description="File path relative to repo root")
    action: Literal["create", "modify", "delete", "rename", "move"] = "modify"
    reason: str = Field(..., description="Why this file needs to change")
    changes: list[CodeChange] = Field(default_factory=list)


class Step(BaseModel):
    """One ordered implementation step."""
    step_number: int
    title: str
    description: str = Field(..., description="What to do in this step")
    files_involved: list[str] = Field(
        default_factory=list,
        description="File paths touched by this step",
    )
    depends_on_steps: list[int] = Field(
        default_factory=list,
        description="Step numbers that must complete before this one",
    )
    verification: Optional[str] = Field(
        None,
        description="How to confirm this step succeeded (test, manual check, etc.)",
    )


class Risk(BaseModel):
    """A potential risk introduced by this plan."""
    severity: Literal["low", "medium", "high"]
    description: str
    affected_symbols: list[str] = Field(default_factory=list)
    mitigation: str


class PlanMetadata(BaseModel):
    """Telemetry attached to each plan response."""
    model: str
    context_tokens: int
    context_files: int
    retrieval_log: str
    elapsed_ms: float


# ── Top-level plan ────────────────────────────────────────────────────────────

class ImplementationPlan(BaseModel):
    """Complete implementation plan returned by POST /plan."""
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    query: str
    summary: str = Field(..., description="2-3 sentence high-level summary of the approach")
    clarifying_assumptions: list[str] = Field(
        default_factory=list,
        description="Assumptions made where the query was ambiguous",
    )
    files: list[FileChange] = Field(
        default_factory=list,
        description="All files that need to change, with per-change detail",
    )
    steps: list[Step] = Field(
        default_factory=list,
        description="Ordered execution steps with dependencies",
    )
    risks: list[Risk] = Field(
        default_factory=list,
        description="Risks and mitigations",
    )
    test_plan: str = Field("", description="What to test and how after implementation")
    metadata: Optional[PlanMetadata] = None


# ── Request / response wrappers ───────────────────────────────────────────────

class PlanRequest(BaseModel):
    query: str = Field(..., min_length=5, description="Bug/feature/refactor description")
    repo_owner: Optional[str] = Field(None, description="Scope to a specific repo owner")
    repo_name: Optional[str] = Field(None, description="Scope to a specific repo name")
    stream: bool = Field(False, description="If true, return an SSE stream instead of JSON")


# ── JSON Schema used as Claude's tool input ───────────────────────────────────
# Defined inline (not derived from Pydantic) to keep it clean for the API.

PLAN_TOOL_SCHEMA: dict = {
    "name": "output_implementation_plan",
    "description": (
        "Output a complete, actionable implementation plan for the requested change. "
        "Be specific: reference real file paths, symbol names, and line numbers from the context."
    ),
    "input_schema": {
        "type": "object",
        "required": ["query", "summary", "files", "steps"],
        "properties": {
            "query": {"type": "string"},
            "summary": {
                "type": "string",
                "description": "2-3 sentence high-level summary of the overall approach",
            },
            "clarifying_assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Assumptions made where the query was ambiguous",
            },
            "files": {
                "type": "array",
                "description": "All files that need to change",
                "items": {
                    "type": "object",
                    "required": ["path", "action", "reason"],
                    "properties": {
                        "path": {"type": "string", "description": "File path relative to repo root"},
                        "action": {
                            "type": "string",
                            "enum": ["create", "modify", "delete", "rename", "move"],
                        },
                        "reason": {"type": "string"},
                        "changes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["description"],
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": ["add", "modify", "delete", "move"],
                                    },
                                    "symbol": {"type": "string"},
                                    "description": {"type": "string"},
                                    "pseudocode": {"type": "string"},
                                    "line_hint": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "steps": {
                "type": "array",
                "description": "Ordered execution steps",
                "items": {
                    "type": "object",
                    "required": ["step_number", "title", "description"],
                    "properties": {
                        "step_number": {"type": "integer"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "files_involved": {"type": "array", "items": {"type": "string"}},
                        "depends_on_steps": {"type": "array", "items": {"type": "integer"}},
                        "verification": {"type": "string"},
                    },
                },
            },
            "risks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["severity", "description", "mitigation"],
                    "properties": {
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "description": {"type": "string"},
                        "affected_symbols": {"type": "array", "items": {"type": "string"}},
                        "mitigation": {"type": "string"},
                    },
                },
            },
            "test_plan": {
                "type": "string",
                "description": "What to test and how after implementing the changes",
            },
        },
    },
}
