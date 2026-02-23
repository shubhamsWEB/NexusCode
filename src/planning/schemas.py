"""
Pydantic schemas for the implementation planning feature.

The ImplementationPlan is the top-level output of POST /plan.
It describes every file change, the ordered execution steps,
risks, and a test plan.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

# ── Leaf schemas ──────────────────────────────────────────────────────────────


class CodeChange(BaseModel):
    """A single change within a file (add/modify/delete a symbol or block)."""

    kind: Literal["add", "modify", "delete", "move"] = "modify"
    symbol: str | None = Field(None, description="Qualified symbol name (e.g. 'AuthService.login')")
    description: str = Field(..., description="What exactly changes and why")
    pseudocode: str | None = Field(None, description="Pseudo-code sketch for complex logic")
    line_hint: str | None = Field(None, description="Approximate line range, e.g. '42-55'")


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
    verification: str | None = Field(
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
    stack_fingerprint: str = ""
    web_research_used: bool = False
    web_research_notes: str = ""


# ── Top-level plan ────────────────────────────────────────────────────────────


class ImplementationPlan(BaseModel):
    """
    Unified response from POST /plan.

    response_type="plan"   → implementation task (has files, steps, risks)
    response_type="answer" → question/explanation (has answer + key_files only)
    """

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    query: str
    response_type: Literal["plan", "answer", "analysis"] = Field(
        "plan",
        description=(
            "'plan' for implementation tasks (files/steps/risks), "
            "'answer' for questions/explanations, "
            "'analysis' for improvement/review/audit queries (deep analysis + grounded suggestions)"
        ),
    )

    # ── Answer fields (response_type == "answer") ──────────────────────────────
    answer: str = Field(
        "",
        description="Rich markdown answer when the query is a question or explanation request",
    )
    key_files: list[str] = Field(
        default_factory=list,
        description="File paths most relevant to the answer (for quick navigation)",
    )

    # ── Analysis fields (response_type == "analysis") ─────────────────────────
    analysis: str = Field(
        "",
        description=(
            "Deep markdown analysis for improvement/review queries. "
            "Covers: current state, specific issues found, grounded improvements, "
            "and implementation guidance — all referencing real file paths and symbols."
        ),
    )

    # ── Plan fields (response_type == "plan") ─────────────────────────────────
    summary: str = Field(
        "",
        description="2-3 sentence high-level summary of the implementation approach",
    )
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
    metadata: PlanMetadata | None = None


# ── Request / response wrappers ───────────────────────────────────────────────


class PlanRequest(BaseModel):
    query: str = Field(..., min_length=5, description="Bug/feature/refactor description")
    repo_owner: str | None = Field(None, description="Scope to a specific repo owner")
    repo_name: str | None = Field(None, description="Scope to a specific repo name")
    stream: bool = Field(False, description="If true, return an SSE stream instead of JSON")
    web_research: bool = Field(
        True,
        description=(
            "Search the web for best practices before generating the plan. "
            "Runs in parallel with codebase retrieval at no extra latency. "
            "Set false to skip (useful when offline or for speed tests)."
        ),
    )


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
                        "path": {
                            "type": "string",
                            "description": "File path relative to repo root",
                        },
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


# ── Answer tool — for questions, explanations, analysis ───────────────────────

ANSWER_TOOL_SCHEMA: dict = {
    "name": "answer_codebase_question",
    "description": (
        "Use this when the query is a question, explanation request, or analysis task "
        "that does NOT require making code changes. Examples: 'what does X do?', "
        "'how does Y work?', 'why is Z failing?', 'where is the rate limiter?', "
        "'explain the data flow', 'what patterns does this use?'. "
        "For tasks that require editing/creating/deleting files, use "
        "output_implementation_plan instead."
    ),
    "input_schema": {
        "type": "object",
        "required": ["answer"],
        "properties": {
            "answer": {
                "type": "string",
                "description": (
                    "Detailed markdown answer. Use headers (##), code blocks, bullet lists. "
                    "Reference actual file paths and function names from the codebase context. "
                    "Be specific and thorough — this is the complete response to the user."
                ),
            },
            "key_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths most relevant to this answer (for navigation)",
            },
        },
    },
}


# ── Analysis / improve tool — for "how to improve X", code review, audit ──────

ANALYZE_IMPROVE_TOOL_SCHEMA: dict = {
    "name": "analyze_and_improve",
    "description": (
        "Use this when the query asks how to IMPROVE, ENHANCE, REVIEW, AUDIT, or OPTIMIZE "
        "something that already exists in the codebase. "
        "Examples: 'how can I make /plan better?', 'how to improve the retriever?', "
        "'review the chunker', 'what are the weaknesses of the auth system?', "
        "'how to make the response quality better?', 'optimize the search pipeline'. "
        "Respond with a DEEP, GROUNDED analysis — not a generic implementation plan. "
        "You must analyze the CURRENT implementation first, then give specific improvements "
        "grounded in real file paths and symbol names."
    ),
    "input_schema": {
        "type": "object",
        "required": ["analysis"],
        "properties": {
            "analysis": {
                "type": "string",
                "description": (
                    "Deep markdown analysis. MUST use these sections in order:\n"
                    "## Current Implementation\n"
                    "  — What it does now, how it works, reference actual file:line\n"
                    "## What Works Well\n"
                    "  — Specific strengths with evidence from the code\n"
                    "## Issues & Gaps\n"
                    "  — Specific weaknesses, anti-patterns, missed opportunities "
                    "(cite file:line for each)\n"
                    "## Concrete Improvements\n"
                    "  — Specific, grounded changes. For each: what to change, "
                    "which file/function, why it matters\n"
                    "## Implementation Guidance\n"
                    "  — If changes are needed: exact files, functions to modify, "
                    "pseudocode for non-trivial logic"
                ),
            },
            "key_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The primary files relevant to this analysis",
            },
            "priority": {
                "type": "string",
                "enum": ["quick-wins", "architectural", "both"],
                "description": "Nature of the improvements: quick wins, architectural changes, or both",
            },
        },
    },
}
