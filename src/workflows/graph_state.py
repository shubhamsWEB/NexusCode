"""
Enterprise Workflow Shared State — the single object that flows through every
node in a LangGraph-powered workflow.

REDUCER RULES (LangGraph semantics):
  - Plain str/int/bool fields:  NO reducer needed. LangGraph's default is
                                last-write-wins when a node returns a new value.
                                Only wrap with Annotated when you need NON-default
                                behavior (accumulation, merging).

  - Annotated[list, operator.add]:  Accumulate. Each node APPENDS to the list;
                                    the list grows across the whole workflow run.
                                    Use for: code_context, errors, artifacts, etc.

  - Annotated[dict, _merge_dict]:   Shallow merge. Each node can write individual
                                    keys without clobbering keys written by other
                                    nodes. Use for: trigger, step_outputs, loop_counts.

  - Annotated[int, operator.add]:   Accumulate integers. Use for: total_tokens.

WHY THIS MATTERS FOR NexusCode:
  The most important accumulating field is `code_context`. When the PM Agent
  searches the codebase, those results are appended to code_context. When the
  Designer Agent runs next, it RECEIVES that already-retrieved context so it
  does not redundantly re-search the same files. The Coder Agent gets the union
  of everything PM and Designer found. This is the core benefit of shared state:
  codebase knowledge is retrieved once and shared across all downstream agents.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict


# ── Custom reducers (only for non-default behavior) ───────────────────────────


def _merge_dict(a: dict, b: dict) -> dict:
    """
    Shallow merge reducer: b values override a values for matching keys,
    new keys in b are added to a.
    Required because LangGraph's default for dicts is full overwrite (same as
    strings). We need merge so that two nodes can each write different keys
    into step_outputs without clobbering each other.
    """
    return {**a, **b}


# ── Shared Graph State ────────────────────────────────────────────────────────


class GraphState(TypedDict, total=False):
    """
    The single shared state object that flows through every node.

    total=False means every field is optional in the TypedDict schema.
    This is idiomatic for LangGraph: nodes return PARTIAL dicts containing
    only the fields they updated; LangGraph merges those partials into the
    running state using the reducers defined above.

    Example node — PM Agent writes its output:
        async def pm_agent_node(state: GraphState) -> dict:
            prd_text = await run_pm_agent(state["trigger"]["feature_request"])
            return {
                "prd": prd_text,
                "step_outputs": {"pm_agent": prd_text},   # merged into existing dict
                "code_context": [prd_context_snippet],    # appended to list
                "artifacts": [{"step": "pm_agent", ...}], # appended to list
                "total_tokens": tokens_used,              # added to running total
            }

    Example node — Reviewer reads what came before and writes verdict:
        async def reviewer_node(state: GraphState) -> dict:
            # state["prd"] is what PM wrote
            # state["code_diff"] is what Coder wrote
            # state["code_context"] is ALL codebase snippets from ALL prior agents
            verdict = await review(state["code_diff"], state["code_context"])
            return {"review_verdict": verdict}
    """

    # ── Workflow metadata ──────────────────────────────────────────────────
    # Plain str — last-write-wins (LangGraph default, no Annotated needed)
    run_id: str               # workflow run UUID, also used as LangGraph thread_id
    workflow_name: str
    session_id: str           # agent artifact store session key

    # ── Trigger input ─────────────────────────────────────────────────────
    # dict with _merge_dict reducer: trigger fields can be enriched mid-workflow
    trigger: Annotated[dict, _merge_dict]     # raw trigger payload (feature_request, repo, etc.)
    wf_context: Annotated[dict, _merge_dict]  # workflow-level context variables from YAML

    # ── Step data bus ─────────────────────────────────────────────────────
    # dict with _merge_dict reducer: each node writes its own key, never clobbers others
    step_outputs: Annotated[dict, _merge_dict]  # {step_id: output_text} — Jinja2 bridge

    # ── Loop safety counters ───────────────────────────────────────────────
    loop_counts: Annotated[dict, _merge_dict]   # {step_id: visit_count}

    # ══════════════════════════════════════════════════════════════════════
    # SHARED CODEBASE KNOWLEDGE — The Core of NexusCode's Value
    # ══════════════════════════════════════════════════════════════════════

    # Accumulated NexusCode search results and code snippets.
    # EVERY agent that retrieves codebase context appends to this list.
    # EVERY downstream agent receives it pre-populated — no redundant searches.
    #
    # Flow example:
    #   PM Agent   → searches "authentication flow" → appends 3 snippets
    #   Designer   → reads those 3 snippets + searches "UI components" → appends 2 more
    #   Coder      → receives 5 snippets already found, searches "error handling" → appends 2
    #   Reviewer   → 7 snippets of context from the whole workflow, no searching needed
    #
    # Each entry is a short string: "file.py:42 — <snippet>" or a retrieved chunk summary.
    code_context: Annotated[list[str], operator.add]

    # Full agent message history — every prompt/response within this workflow run.
    # Useful for audit, evaluation, and the observability layer.
    messages: Annotated[list[dict], operator.add]

    # ══════════════════════════════════════════════════════════════════════
    # Enterprise domain fields — typed outputs from specific roles
    # All plain str — last-write-wins (LangGraph default, no Annotated needed)
    # ══════════════════════════════════════════════════════════════════════

    # PM Agent outputs
    prd: str                              # Product Requirements Document (full text)
    acceptance_criteria: str              # testable, numbered criteria

    # PM Agent list outputs — accumulate if PM revises across loop iterations
    user_stories: Annotated[list[str], operator.add]

    # Designer Agent outputs
    component_spec: str                   # UI component specification
    ui_notes: str                         # design decisions and constraints

    # Designer list outputs
    design_references: Annotated[list[str], operator.add]  # Figma URLs, design tokens

    # Planner Agent outputs
    implementation_plan: str              # step-by-step technical plan with file refs

    # Planner list outputs
    files_to_change: Annotated[list[str], operator.add]    # file paths the plan targets

    # Coder Agent outputs
    code_diff: str                        # the full generated code change

    # Coder list outputs
    files_changed: Annotated[list[str], operator.add]      # files actually touched

    # Reviewer Agent outputs
    review_verdict: str                   # exactly: "approved" | "needs_revision" | "escalate"
    review_notes: str                     # reviewer commentary and inline feedback

    # Reviewer list outputs (accumulate across revision loops)
    critical_issues: Annotated[list[str], operator.add]    # blocking issues per iteration

    # QA Agent outputs
    test_plan: str                        # full test plan document
    test_results: str                     # execution results

    # QA list outputs
    test_cases: Annotated[list[str], operator.add]         # individual runnable test cases

    # DevOps Agent outputs
    deployment_plan: str                  # runbook: pre-deploy, deploy, rollback
    infrastructure_notes: str            # env vars, migrations, IAM, network changes needed

    # Human checkpoint outputs
    human_code_decision: str             # response from human_code_review: retry|proceed|fail

    # Supervisor / synthesis outputs
    final_report: str                     # unified executive summary

    # list outputs
    next_steps: Annotated[list[str], operator.add]

    # ── Integration side-effects ──────────────────────────────────────────
    # Written by integration steps, readable by all subsequent agents
    jira_issue_key: str                   # e.g. "PROJ-123"
    github_pr_url: str                    # e.g. "https://github.com/org/repo/pull/42"
    slack_message_ts: str                 # Slack message timestamp for threading replies

    # ── Accumulated timeline ───────────────────────────────────────────────
    # Every step appends one entry so the whole run is auditable
    artifacts: Annotated[list[dict], operator.add]

    # ── Resource tracking ──────────────────────────────────────────────────
    total_tokens: Annotated[int, operator.add]

    # ── Error journal ──────────────────────────────────────────────────────
    errors: Annotated[list[str], operator.add]


# ── Initialiser ───────────────────────────────────────────────────────────────


def initial_state(
    run_id: str,
    workflow_name: str,
    trigger_payload: dict,
    workflow_context: dict | None = None,
    session_id: str = "",
) -> GraphState:
    """
    Build the fully-initialised GraphState for the start of a workflow run.

    All accumulating list fields start empty ([] + any additions = additions).
    All string fields start as "" (last-write-wins, first write sets the value).
    All dict fields start as {} (merge reducer, first write adds keys).
    total_tokens starts at 0 (add reducer, each node contributes its usage).
    """
    return GraphState(
        # ── Metadata ──────────────────────────────────────────────────────
        run_id=run_id,
        workflow_name=workflow_name,
        session_id=session_id or run_id,
        # ── Input ─────────────────────────────────────────────────────────
        trigger=trigger_payload,
        wf_context=workflow_context or {},
        # ── Buses ─────────────────────────────────────────────────────────
        step_outputs={},
        loop_counts={},
        # ── Shared codebase knowledge (THE most important accumulating field)
        code_context=[],
        messages=[],
        # ── PM Agent ──────────────────────────────────────────────────────
        prd="",
        acceptance_criteria="",
        user_stories=[],
        # ── Designer Agent ─────────────────────────────────────────────────
        component_spec="",
        ui_notes="",
        design_references=[],
        # ── Planner ────────────────────────────────────────────────────────
        implementation_plan="",
        files_to_change=[],
        # ── Coder ──────────────────────────────────────────────────────────
        code_diff="",
        files_changed=[],
        # ── Reviewer ───────────────────────────────────────────────────────
        review_verdict="",
        review_notes="",
        critical_issues=[],
        # ── QA Agent ───────────────────────────────────────────────────────
        test_plan="",
        test_results="",
        test_cases=[],
        # ── DevOps Agent ───────────────────────────────────────────────────
        deployment_plan="",
        infrastructure_notes="",
        # ── Human checkpoints ──────────────────────────────────────────────
        human_code_decision="",
        # ── Supervisor ─────────────────────────────────────────────────────
        final_report="",
        next_steps=[],
        # ── Integrations ───────────────────────────────────────────────────
        jira_issue_key="",
        github_pr_url="",
        slack_message_ts="",
        # ── Tracking ───────────────────────────────────────────────────────
        artifacts=[],
        total_tokens=0,
        errors=[],
    )


# ── Context bridge ────────────────────────────────────────────────────────────


def state_to_jinja_context(state: GraphState) -> dict:
    """
    Convert GraphState into a dict for Jinja2 template rendering.
    Bridges the typed state back to {{ steps.X.output }} and {{ state.prd }}
    syntax used in YAML workflow step task definitions.
    """
    from src.workflows.context import _StepProxy

    return {
        "trigger": state.get("trigger", {}),
        "context": state.get("wf_context", {}),
        "steps": _StepProxy(state.get("step_outputs", {})),
        "state": state,
    }


# ── code_context helpers ──────────────────────────────────────────────────────


def extract_code_context_from_output(agent_output: str, step_id: str) -> list[str]:
    """
    Extract codebase snippets cited in an agent's output to add to code_context.

    Looks for file:line patterns and code blocks that the agent cited.
    Each returned entry is a short string: "step_id: <cited snippet>"
    that subsequent agents can read without re-searching.
    """
    import re

    snippets: list[str] = []
    # Match "file_path:line_number" citation patterns agents commonly output
    file_refs = re.findall(r'`([^`]+\.[a-z]{1,5}):(\d+)`', agent_output)
    for fpath, lineno in file_refs[:10]:  # cap at 10 per step
        snippets.append(f"{step_id}→{fpath}:{lineno}")

    # If agent output is short enough, include a truncated version as context
    if len(agent_output) > 0 and not snippets:
        # No explicit file citations — include a truncated output summary
        truncated = agent_output[:400].replace("\n", " ")
        snippets.append(f"{step_id}: {truncated}")

    return snippets


def build_code_context_prompt(code_context: list[str], max_chars: int = 3000) -> str:
    """
    Format the accumulated code_context list into a readable block
    to prepend into agent prompts as "Previously retrieved codebase context".

    Each downstream agent receives this block so it knows what was already
    found and can avoid redundant searches.
    """
    if not code_context:
        return ""

    lines = "\n".join(f"  • {entry}" for entry in code_context)
    if len(lines) > max_chars:
        lines = lines[:max_chars] + "\n  … (truncated)"

    return (
        "## Previously Retrieved Codebase Context\n"
        "The following codebase context was already retrieved by earlier agents in this workflow.\n"
        "Read this carefully before searching — you may not need to re-search these areas:\n\n"
        f"{lines}\n"
    )
