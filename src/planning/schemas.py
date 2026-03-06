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
    # Query analysis metadata
    query_complexity: str = ""  # "simple" | "moderate" | "complex"
    sub_queries_count: int = 0  # number of decomposed sub-queries
    grounding_warnings: list[str] = Field(default_factory=list)  # post-retrieval gaps
    quality_score: float = Field(0.0, description="Context retrieval confidence (0.0-1.0 scale)")


# ── SPARC summary ─────────────────────────────────────────────────────────────


class SPARCSummary(BaseModel):
    """SPARC methodology summary — 1-3 sentences per phase."""

    specification: str = Field(
        "", description="S: What needs to be built and why (requirements + acceptance criteria)"
    )
    pseudocode: str = Field(
        "", description="P: Key algorithmic logic in pseudocode (omit for trivial changes)"
    )
    architecture: str = Field(
        "", description="A: How this flows through the existing system architecture"
    )
    refinement: str = Field(
        "", description="R: Edge cases, trade-offs, and failure modes considered"
    )
    completion: str = Field(
        "", description="C: How to verify the implementation is done (tests + checks)"
    )


# ── Top-level plan ────────────────────────────────────────────────────────────


class ImplementationPlan(BaseModel):
    """
    Unified response from POST /plan.

    response_type="plan"   → implementation task (has files, steps, risks)
    response_type="answer" → question/explanation (has answer + key_files only)
    """

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    query: str
    response_type: Literal["plan", "answer", "analysis", "out_of_scope"] = Field(
        "plan",
        description=(
            "'plan' for implementation tasks (files/steps/risks), "
            "'answer' for questions/explanations, "
            "'analysis' for improvement/review/audit queries (deep analysis + grounded suggestions), "
            "'out_of_scope' when the query is unrelated to the indexed codebase"
        ),
    )

    # ── Out-of-scope fields (response_type == "out_of_scope") ─────────────────
    out_of_scope_reason: str = Field(
        "",
        description="Human-readable explanation of why the query was considered out of scope",
    )
    relevance_score: float = Field(
        0.0,
        description="Best cosine similarity score from the relevance gate check (0.0-1.0)",
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
    design_decisions: list[str] = Field(
        default_factory=list,
        description="Key design decisions with rationale (e.g. 'Base64-in-JSON instead of multipart because...')",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Identified constraints (framework, runtime, API contract, backward compat, performance)",
    )
    design_alternatives: list[dict] = Field(
        default_factory=list,
        description="Alternative approaches considered, each with approach/pros/cons/rejected_reason",
    )
    failure_modes: list[dict] = Field(
        default_factory=list,
        description="Potential failure cases with scenario, cause, and mitigation",
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
    sparc: SPARCSummary | None = Field(
        None, description="SPARC methodology summary (populated for response_type='plan')"
    )
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
    model: str | None = Field(
        None,
        description="LLM model to use (e.g. 'gpt-4o', 'claude-opus-4-6'). Defaults to server config.",
    )
    search_quality: str = Field(
        "thorough",
        description="HNSW search quality preset: 'fast', 'balanced', or 'thorough'.",
    )


class AskRequest(BaseModel):
    """Request schema for POST /ask — codebase Q&A."""

    query: str = Field(
        ..., min_length=5, description="Natural language question about the codebase"
    )
    repo_owner: str | None = Field(None, description="Scope to a specific repo owner")
    repo_name: str | None = Field(None, description="Scope to a specific repo name")
    stream: bool = Field(
        True, description="If true, return an SSE stream; if false, wait for full JSON"
    )
    session_id: str | None = Field(
        None, description="Chat session ID — server generates if omitted, returned in response"
    )
    model: str | None = Field(
        None,
        description="LLM model to use (e.g. 'gpt-4o', 'claude-opus-4-6'). Defaults to server config.",
    )
    search_quality: str = Field(
        "balanced",
        description="HNSW search quality preset: 'fast', 'balanced', or 'thorough'.",
    )


# ── JSON Schema used as Claude's tool input ───────────────────────────────────
# Defined inline (not derived from Pydantic) to keep it clean for the API.

PLAN_TOOL_SCHEMA: dict = {
    "name": "output_implementation_plan",
    "description": (
        "Output a concise, actionable implementation plan. A coding agent will execute "
        "this directly — be precise and skip explanations. Reference only real file paths "
        "and symbols from the context. NEVER include stack inventories, web research, "
        "or concept explanations in any field."
    ),
    "input_schema": {
        "type": "object",
        "required": ["query", "summary", "design_decisions", "files", "steps", "sparc_summary"],
        "properties": {
            "query": {"type": "string"},
            "summary": {
                "type": "string",
                "description": "2-4 sentences on data/control flow and affected layers. No package lists or web research.",
            },
            "clarifying_assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only for genuine ambiguity. Max 3 items. Prefer reasonable defaults.",
            },
            "design_decisions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Non-obvious design decisions with WHY. e.g. 'Base64-in-JSON to preserve JSON contract.' Max 5.",
            },
            "constraints": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Binding constraints (framework, runtime, API, backward compat, performance).",
            },
            "design_alternatives": {
                "type": "array",
                "description": "≥2 viable approaches for non-trivial changes (selected + rejected with justification).",
                "items": {
                    "type": "object",
                    "required": ["approach", "pros", "cons"],
                    "properties": {
                        "approach": {"type": "string", "description": "Core idea of this approach"},
                        "pros": {"type": "array", "items": {"type": "string"}},
                        "cons": {"type": "array", "items": {"type": "string"}},
                        "rejected_reason": {
                            "type": "string",
                            "description": "Why this approach was rejected (empty for the selected approach)",
                        },
                    },
                },
            },
            "failure_modes": {
                "type": "array",
                "description": "Failure modes for API/architectural changes (assume adversarial inputs).",
                "items": {
                    "type": "object",
                    "required": ["scenario", "cause", "mitigation"],
                    "properties": {
                        "scenario": {"type": "string", "description": "What can go wrong"},
                        "cause": {"type": "string", "description": "Why it would happen"},
                        "mitigation": {"type": "string", "description": "Guard or fallback"},
                    },
                },
            },
            "files": {
                "type": "array",
                "description": "All files that need to change. Only files from the context.",
                "items": {
                    "type": "object",
                    "required": ["path", "action", "reason"],
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to repo root (must exist in context)",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["create", "modify", "delete", "rename", "move"],
                        },
                        "reason": {
                            "type": "string",
                            "description": "1 sentence — why this file changes",
                        },
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
                                    "description": {
                                        "type": "string",
                                        "description": "Exact edit: e.g. 'Add field image_data: str | None = None after line 42'. No protocol explanations.",
                                    },
                                    "pseudocode": {
                                        "type": "string",
                                        "description": "Only for non-trivial logic (>5 lines). Skip for simple changes.",
                                    },
                                    "line_hint": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "steps": {
                "type": "array",
                "description": "Ordered execution steps. Concrete actions, not explanations.",
                "items": {
                    "type": "object",
                    "required": ["step_number", "title", "description"],
                    "properties": {
                        "step_number": {"type": "integer"},
                        "title": {"type": "string", "description": "Short title, max 8 words"},
                        "description": {
                            "type": "string",
                            "description": "What to do — concrete action. Not a concept explanation.",
                        },
                        "files_involved": {"type": "array", "items": {"type": "string"}},
                        "depends_on_steps": {"type": "array", "items": {"type": "integer"}},
                        "verification": {
                            "type": "string",
                            "description": "Specific command or assertion to verify this step.",
                        },
                    },
                },
            },
            "risks": {
                "type": "array",
                "description": "Max 3 genuine implementation risks that could cause bugs.",
                "items": {
                    "type": "object",
                    "required": ["severity", "description", "mitigation"],
                    "properties": {
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "description": {
                            "type": "string",
                            "description": "1-2 sentences. Only genuine implementation bugs. Not concept explanations.",
                        },
                        "affected_symbols": {"type": "array", "items": {"type": "string"}},
                        "mitigation": {"type": "string", "description": "1 sentence."},
                    },
                },
            },
            "test_plan": {
                "type": "string",
                "description": "Specific test commands or assertions. Not generic advice.",
            },
            "sparc_summary": {
                "type": "object",
                "description": "SPARC methodology (1-3 sentences per phase).",
                "properties": {
                    "specification": {
                        "type": "string",
                        "description": "S: What needs to be built — requirements and acceptance criteria",
                    },
                    "pseudocode": {
                        "type": "string",
                        "description": "P: High-level pseudocode for key non-trivial logic. Skip for simple field additions.",
                    },
                    "architecture": {
                        "type": "string",
                        "description": "A: How the change flows through the system.",
                    },
                    "refinement": {
                        "type": "string",
                        "description": "R: Edge cases and trade-offs.",
                    },
                    "completion": {
                        "type": "string",
                        "description": "C: Verification approach.",
                    },
                },
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
        "Use for queries asking to IMPROVE, ENHANCE, REVIEW, AUDIT, or OPTIMIZE existing code. "
        "Examples: 'improve the retriever', 'review the chunker', 'optimize the search pipeline'. "
        "Respond with deep grounded analysis of the CURRENT implementation, then specific "
        "improvements citing real file paths and symbols."
    ),
    "input_schema": {
        "type": "object",
        "required": ["analysis"],
        "properties": {
            "analysis": {
                "type": "string",
                "description": (
                    "Deep markdown analysis with sections (in order):\n"
                    "## Current Implementation — file:line citations\n"
                    "## What Works Well — evidence from code\n"
                    "## Issues & Gaps — cite file:line for each\n"
                    "## Concrete Improvements — file/function specific\n"
                    "## Implementation Guidance — pseudocode if needed"
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
