"""
LLM caller for the /plan endpoint — agentic implementation planning.

Instead of pre-fetching context before the LLM call, Claude is given the
retrieval tools and searches the codebase iteratively. Extended thinking is
enabled so Claude can reason through constraints, design alternatives, and
failure modes before committing to a plan.

Tool selection (Claude picks one based on query intent):
  answer_codebase_question   → question / explanation / analysis
  analyze_and_improve        → improvement / review / audit
  output_implementation_plan → code changes required

Flow:
  query → AgentLoop(tools=[search, get_symbol, find_callers, get_file_context])
        → Claude searches iteratively (up to plan_max_iterations turns)
        → Claude calls one of the three final answer tools
        → parse → ImplementationPlan
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from src.config import settings
from src.planning.schemas import (
    ANALYZE_IMPROVE_TOOL_SCHEMA,
    ANSWER_TOOL_SCHEMA,
    PLAN_TOOL_SCHEMA,
    ImplementationPlan,
    PlanMetadata,
    SPARCSummary,
)
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────────

PLANNING_SYSTEM_PROMPT = """\
You are a principal software architect generating implementation plans that a
coding agent will execute directly. Every token must be actionable.

PROCESS
───────
1. Use search_codebase to find the relevant code. Be specific.
2. Use get_symbol for exact function/class definitions.
3. Use find_callers to understand blast radius before planning changes.
4. Use get_file_context to understand a file's full structure.
5. Search multiple times — explore the code thoroughly before planning.
6. Once you understand the system, call the appropriate final tool.

TOOL SELECTION
──────────────
• Query is a QUESTION / EXPLANATION   → answer_codebase_question
• Query requires CODE CHANGES          → output_implementation_plan
• Query asks to IMPROVE / REVIEW       → analyze_and_improve

ABSOLUTE PROHIBITIONS (violating any = plan FAILURE)
─────────────────────────────────────────────────────
• Never output package lists, import frequency tables, or stack inventories.
• Never reproduce web research content verbatim (absorb it, discard the text).
• Never write generic advice like "validate inputs" without citing a file:line.
• Never invent file paths or symbol names — only reference what you found.
• Never include explanations of HTTP protocols, encoding, or API design concepts.

GROUNDING RULES
───────────────
• ONLY reference file paths you found via tool calls.
• ONLY reference symbols you found via tool calls.
• If context is insufficient, say so — do not guess.

STRUCTURED REASONING (for implementation plans)
────────────────────────────────────────────────
Phase 1 — CONSTRAINTS: Identify binding constraints BEFORE designing anything.
Phase 2 — ALTERNATIVES: For non-trivial changes, generate ≥2 viable approaches.
Phase 3 — FAILURE MODES: For API/architectural changes, list what can go wrong.
Phase 4 — PLAN: Trace ALL affected files end-to-end. Cite file:line for every change.
"""

# ── Final answer tools ────────────────────────────────────────────────────────

_FINAL_ANSWER_TOOLS = [
    PLAN_TOOL_SCHEMA,
    ANALYZE_IMPROVE_TOOL_SCHEMA,
    ANSWER_TOOL_SCHEMA,
]

# ── Helpers ────────────────────────────────────────────────────────────────────


def _build_system_prompt(repo_owner: str | None, repo_name: str | None) -> str:
    from src.agent.rules import load_rules

    rules = load_rules(repo_owner, repo_name)
    if not rules:
        return PLANNING_SYSTEM_PROMPT
    return PLANNING_SYSTEM_PROMPT + f"\n\n---\n\n## Codebase-Specific Rules\n\n{rules}"


def _build_initial_message(
    query: str,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    scope = f" (repository: {repo_owner}/{repo_name})" if repo_owner and repo_name else ""
    return f"{query}{scope}"


def _build_metadata(stats: dict, model: str) -> PlanMetadata:
    return PlanMetadata(
        model=model,
        context_tokens=stats.get("context_tokens", 0),
        context_files=stats.get("tool_calls", 0),  # repurpose as "tool calls made"
        retrieval_log=(
            f"Agentic: {stats.get('iterations', 0)} iterations, "
            f"{stats.get('tool_calls', 0)} tool calls, "
            f"{stats.get('search_tools_called', 0)} searches, "
            f"{stats.get('context_tokens', 0):,} tokens"
        ),
        elapsed_ms=stats.get("elapsed_ms", 0.0),
        web_research_used=False,
    )


def _parse_tool_block(
    tool_block: dict,
    query: str,
    stats: dict,
    model: str,
) -> ImplementationPlan:
    """Parse an agent final answer tool block into an ImplementationPlan."""
    name = tool_block.get("name", "")
    data = tool_block.get("input", {})
    metadata = _build_metadata(stats, model)

    if name == "answer_codebase_question":
        plan = ImplementationPlan(
            query=query,
            response_type="answer",
            answer=data.get("answer", ""),
            key_files=data.get("key_files", []),
        )
        plan.metadata = metadata
        return plan

    if name == "analyze_and_improve":
        plan = ImplementationPlan(
            query=query,
            response_type="analysis",
            analysis=data.get("analysis", ""),
            key_files=data.get("key_files", []),
        )
        plan.metadata = metadata
        return plan

    if name == "_text_fallback":
        plan = ImplementationPlan(
            query=query,
            response_type="answer",
            answer=data.get("answer", "_No response generated._"),
        )
        plan.metadata = metadata
        return plan

    # output_implementation_plan
    plan_data = dict(data)
    plan_data["query"] = query
    plan = ImplementationPlan.model_validate(plan_data)
    plan.metadata = metadata

    sparc_data = plan_data.get("sparc_summary")
    if sparc_data:
        plan.sparc = SPARCSummary.model_validate(sparc_data)

    return plan


# ── Public generate (non-streaming) ───────────────────────────────────────────


async def generate_plan(
    query: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    web_research: bool = True,  # noted: web search not yet wired into agent loop
    model: str | None = None,
) -> ImplementationPlan:
    """
    Run the Plan Mode agent loop (non-streaming).
    Claude searches the codebase iteratively then outputs an implementation plan.
    """
    from src.agent.loop import AgentLoop, AgentLoopConfig
    from src.agent.mcp_bridge import get_external_tool_schemas
    from src.agent.tool_schemas import RETRIEVAL_TOOL_SCHEMAS

    effective_model = model or settings.default_model
    all_retrieval = RETRIEVAL_TOOL_SCHEMAS + get_external_tool_schemas()

    tool_block, stats = await AgentLoop().run(
        model=effective_model,
        system=_build_system_prompt(repo_owner, repo_name),
        initial_message=_build_initial_message(query, repo_owner, repo_name),
        retrieval_tools=all_retrieval,
        final_answer_tools=_FINAL_ANSWER_TOOLS,
        config=AgentLoopConfig(
            max_iterations=settings.plan_max_iterations,
            cumulative_token_budget=settings.agent_token_budget,
            require_search_before_answer=True,
            thinking_budget=settings.planning_thinking_budget,
        ),
        repo_owner=repo_owner,
        repo_name=repo_name,
    )

    plan = _parse_tool_block(tool_block, query, stats, effective_model)
    logger.info(
        "planning: %s responded in %.0fms (iter=%d, tool_calls=%d, tool=%s)",
        effective_model,
        stats.get("elapsed_ms", 0),
        stats.get("iterations", 0),
        stats.get("tool_calls", 0),
        tool_block.get("name"),
    )
    return plan


# ── Public stream generator ────────────────────────────────────────────────────


async def stream_generate_plan(
    query: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    web_research: bool = True,
    model: str | None = None,
) -> AsyncIterator[dict]:
    """
    Stream the Plan Mode agent loop.

    Yields:
      {"type": "agent_tool_call",   "tool": str, "input_summary": str}
      {"type": "agent_tool_result", "tool": str, "tokens": int, "cumulative_tokens": int}
      {"type": "thinking",          "text": str}
      {"type": "token",             "text": str}
      {"type": "plan_complete",     "plan": ImplementationPlan}
    """
    from src.agent.loop import AgentLoop, AgentLoopConfig
    from src.agent.mcp_bridge import get_external_tool_schemas
    from src.agent.tool_schemas import RETRIEVAL_TOOL_SCHEMAS

    effective_model = model or settings.default_model
    all_retrieval = RETRIEVAL_TOOL_SCHEMAS + get_external_tool_schemas()

    async for event in AgentLoop().stream(
        model=effective_model,
        system=_build_system_prompt(repo_owner, repo_name),
        initial_message=_build_initial_message(query, repo_owner, repo_name),
        retrieval_tools=all_retrieval,
        final_answer_tools=_FINAL_ANSWER_TOOLS,
        config=AgentLoopConfig(
            max_iterations=settings.plan_max_iterations,
            cumulative_token_budget=settings.agent_token_budget,
            require_search_before_answer=True,
            thinking_budget=settings.planning_thinking_budget,
        ),
        repo_owner=repo_owner,
        repo_name=repo_name,
    ):
        if event["type"] == "done":
            plan = _parse_tool_block(event["tool_block"], query, event["stats"], effective_model)
            logger.info(
                "planning: stream complete in %.0fms (iter=%d, tool_calls=%d, tool=%s)",
                event["stats"].get("elapsed_ms", 0),
                event["stats"].get("iterations", 0),
                event["stats"].get("tool_calls", 0),
                event["tool_block"].get("name"),
            )
            yield {"type": "plan_complete", "plan": plan}
        else:
            # Pass through: agent_tool_call, agent_tool_result, thinking, token
            yield event
