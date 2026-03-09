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

import asyncio
import json
import re
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


# ── XML <item> sanitiser ──────────────────────────────────────────────────────
# Claude's streaming tool_use sometimes emits XML-like <item>…</item> strings
# instead of proper JSON arrays.  This function detects those strings and
# converts them to native Python lists before Pydantic validation.

def _parse_xml_items(text: str) -> list:
    """Parse XML-like <item> nested strings into Python lists."""
    tokens = re.split(r"(</?item>)", text)
    
    stack: list[list] = [[]]
    current_text: list[str] = []

    for token in tokens:
        if not token:
            continue
        if token == "<item>":
            if current_text:
                val = "".join(current_text).strip()
                if val:
                    stack[-1].append(val)
                current_text = []
            stack.append([])
        elif token == "</item>":
            if current_text:
                val = "".join(current_text).strip()
                if val:
                    # Attempt JSON parse in case it's an object string
                    if val.startswith("{") and val.endswith("}"):
                        try:
                            val = json.loads(val)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    stack[-1].append(val)
                current_text = []
            
            if len(stack) > 1:
                item_content = stack.pop()
                if len(item_content) == 1:
                    # Flatten single-element lists
                    stack[-1].append(item_content[0])
                elif len(item_content) > 1:
                    stack[-1].append(item_content)
        else:
            current_text.append(token)
            
    if current_text:
        val = "".join(current_text).strip()
        if val:
            stack[0].append(val)
            
    return stack[0]


def _sanitize_tool_data(data: dict) -> dict:
    """Recursively replace XML <item> strings with proper Python lists/dicts."""
    cleaned: dict = {}
    for key, value in data.items():
        if isinstance(value, str) and "<item>" in value:
            parsed = _parse_xml_items(value)
            # Try assembling lists of lists of 2 elements into dicts
            assembled: list = []
            for item in parsed:
                if isinstance(item, list) and all(isinstance(el, list) and len(el) == 2 for el in item):
                    assembled.append({el[0]: el[1] for el in item})
                else:
                    assembled.append(item)
            cleaned[key] = assembled
        elif isinstance(value, dict):
            cleaned[key] = _sanitize_tool_data(value)
        elif isinstance(value, list):
            cleaned[key] = [
                _sanitize_tool_data(el) if isinstance(el, dict) else el
                for el in value
            ]
        else:
            cleaned[key] = value
    return cleaned

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

STRUCTURED REASONING (for implementation plans AND analysis)
─────────────────────────────────────────────────────────────
Phase 1 — PROBLEM: Define what needs to be solved and WHY, in 1-3 sentences.
Phase 2 — ARCHITECTURE: Map the current system — flow structure, key files,
          existing state, relevant infrastructure. Use tables for clarity.
Phase 3 — OPTIONS: Generate ≥2 viable approaches. For each: describe the
          approach, sketch the implementation, list pros and cons.
Phase 4 — RECOMMEND: Pick the best option and explain WHY with specific
          architectural reasoning.
Phase 5 — PLAN: List prerequisites (coordination/setup), then concrete dev
          tasks. Trace ALL affected files end-to-end. Cite file:line.
Phase 6 — QUESTIONS: Surface open questions that need team input, with
          suggested owners (e.g., "Analytics Team", "AEM Team", "Dev Team").

OUTPUT FORMAT
─────────────
When using output_implementation_plan or analyze_and_improve:
• Start with a clear Problem Statement — not a summary of what you found.
• Present Current Architecture as descriptive context with tables (not issues).
• Always present ≥2 solution options with pros/cons before recommending one.
• Separate prerequisites (coordination) from dev tasks (code changes).
• End with open questions and references — do not leave loose ends inline.
"""

# ── Final answer tools ────────────────────────────────────────────────────────

_FINAL_ANSWER_TOOLS = [
    PLAN_TOOL_SCHEMA,
    ANALYZE_IMPROVE_TOOL_SCHEMA,
    ANSWER_TOOL_SCHEMA,
]

# ── Helpers ────────────────────────────────────────────────────────────────────


_OLLAMA_TOOL_HINT = """
TOOL ARGUMENT FORMAT (IMPORTANT)
─────────────────────────────────
Always pass tool arguments as a JSON object with the exact field names.
Examples:
  search_codebase  → {"query": "JWT token validation"}
  get_symbol       → {"name": "authenticate"}
  find_callers     → {"symbol": "authenticate"}
  get_file_context → {"path": "src/api/app.py"}
Never call a tool with empty arguments — the call will fail.
"""


def _build_system_prompt(
    repo_owner: str | None,
    repo_name: str | None,
    model: str | None = None,
) -> str:
    from src.agent.rules import load_rules
    from src.llm.client import is_ollama_model

    prompt = PLANNING_SYSTEM_PROMPT
    if model and is_ollama_model(model):
        prompt += _OLLAMA_TOOL_HINT
    rules = load_rules(repo_owner, repo_name)
    if rules:
        prompt += f"\n\n---\n\n## Codebase-Specific Rules\n\n{rules}"
    return prompt


def _build_initial_message(
    query: str,
    repo_owner: str | None,
    repo_name: str | None,
    web_research_notes: str = "",
) -> str:
    scope = f" (repository: {repo_owner}/{repo_name})" if repo_owner and repo_name else ""
    msg = f"{query}{scope}"
    if web_research_notes:
        msg += f"\n\n---\n\n## Web Research Notes\n{web_research_notes}"
    return msg


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
    data = _sanitize_tool_data(tool_block.get("input", {}))
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
    web_research: bool = True,
    model: str | None = None,
    allowed_repos: list[str] | None = None,
) -> ImplementationPlan:
    """
    Run the Plan Mode agent loop (non-streaming).
    Claude searches the codebase iteratively then outputs an implementation plan.
    """
    from src.agent.loop import AgentLoop, AgentLoopConfig
    from src.agent.mcp_bridge import get_external_tool_schemas
    from src.agent.tool_schemas import RETRIEVAL_TOOL_SCHEMAS

    # ── Relevance gate: reject off-topic queries before spending any tokens ────
    if settings.query_relevance_enabled:
        from src.retrieval.relevance import build_out_of_scope_message, check_query_relevance

        relevance = await check_query_relevance(query, repo_owner, repo_name)
        if not relevance.is_relevant:
            msg = build_out_of_scope_message(query, relevance)
            logger.info(
                "planning: relevance gate rejected query (score=%.3f, reason=%s)",
                relevance.best_score,
                relevance.reason,
            )
            plan = ImplementationPlan(
                query=query,
                response_type="out_of_scope",
                out_of_scope_reason=msg,
                relevance_score=relevance.best_score,
            )
            plan.metadata = PlanMetadata(
                model=model or settings.default_model,
                context_tokens=0,
                context_files=0,
                retrieval_log=f"Relevance gate: score={relevance.best_score:.3f} < threshold={settings.query_relevance_threshold}",
                elapsed_ms=0.0,
            )
            return plan

    effective_model = model or settings.default_model
    all_retrieval = RETRIEVAL_TOOL_SCHEMAS + get_external_tool_schemas()

    # Gate extended thinking on query complexity — simple/moderate queries skip it
    from src.planning.retriever import _analyze_query
    analysis = _analyze_query(query)
    effective_thinking = (
        settings.planning_thinking_budget
        if analysis.complexity == "complex"
        else 0
    )

    # Fire web research as a background task (runs concurrently with query analysis)
    web_notes = ""
    if web_research:
        try:
            from src.planning.retriever import _extract_stack_fingerprint
            from src.planning.web_researcher import research_implementation

            stack_fp = await _extract_stack_fingerprint(repo_owner, repo_name)
            web_notes = await research_implementation(query, stack_context=stack_fp, model=model)
            if web_notes:
                logger.info("planning: web research complete (%d chars)", len(web_notes))
        except Exception as exc:
            logger.warning("planning: web research failed (non-fatal): %s", exc)

    tool_block, stats = await AgentLoop().run(
        model=effective_model,
        system=_build_system_prompt(repo_owner, repo_name, model=effective_model),
        initial_message=_build_initial_message(query, repo_owner, repo_name, web_research_notes=web_notes),
        retrieval_tools=all_retrieval,
        final_answer_tools=_FINAL_ANSWER_TOOLS,
        config=AgentLoopConfig(
            max_iterations=settings.plan_max_iterations,
            cumulative_token_budget=settings.agent_token_budget,
            require_search_before_answer=True,
            thinking_budget=effective_thinking,
            planning_max_output_tokens=settings.planning_max_output_tokens,
        ),
        repo_owner=repo_owner,
        repo_name=repo_name,
        extra_context={"allowed_repos": allowed_repos} if allowed_repos is not None else None,
    )

    plan = _parse_tool_block(tool_block, query, stats, effective_model)
    if plan.metadata:
        plan.metadata.web_research_used = bool(web_notes)
    logger.info(
        "planning: %s responded in %.0fms (iter=%d, tool_calls=%d, tool=%s, web=%s)",
        effective_model,
        stats.get("elapsed_ms", 0),
        stats.get("iterations", 0),
        stats.get("tool_calls", 0),
        tool_block.get("name"),
        bool(web_notes),
    )
    return plan


# ── Public stream generator ────────────────────────────────────────────────────


async def stream_generate_plan(
    query: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    web_research: bool = True,
    model: str | None = None,
    allowed_repos: list[str] | None = None,
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

    # ── Relevance gate ─────────────────────────────────────────────────────────
    if settings.query_relevance_enabled:
        from src.retrieval.relevance import build_out_of_scope_message, check_query_relevance

        relevance = await check_query_relevance(query, repo_owner, repo_name)
        if not relevance.is_relevant:
            msg = build_out_of_scope_message(query, relevance)
            logger.info(
                "planning: relevance gate rejected query (score=%.3f, reason=%s)",
                relevance.best_score,
                relevance.reason,
            )
            plan = ImplementationPlan(
                query=query,
                response_type="out_of_scope",
                out_of_scope_reason=msg,
                relevance_score=relevance.best_score,
            )
            plan.metadata = PlanMetadata(
                model=model or settings.default_model,
                context_tokens=0,
                context_files=0,
                retrieval_log=f"Relevance gate: score={relevance.best_score:.3f} < threshold={settings.query_relevance_threshold}",
                elapsed_ms=0.0,
            )
            yield {"type": "plan_complete", "plan": plan}
            return

    effective_model = model or settings.default_model
    all_retrieval = RETRIEVAL_TOOL_SCHEMAS + get_external_tool_schemas()

    # Gate extended thinking on query complexity — simple/moderate queries skip it
    from src.planning.retriever import _analyze_query
    analysis = _analyze_query(query)
    effective_thinking = (
        settings.planning_thinking_budget
        if analysis.complexity == "complex"
        else 0
    )

    # Fire web research as a background task
    web_notes = ""
    if web_research:
        try:
            from src.planning.retriever import _extract_stack_fingerprint
            from src.planning.web_researcher import research_implementation

            stack_fp = await _extract_stack_fingerprint(repo_owner, repo_name)
            web_notes = await research_implementation(query, stack_context=stack_fp, model=model)
            if web_notes:
                logger.info("planning: web research complete (%d chars)", len(web_notes))
        except Exception as exc:
            logger.warning("planning: web research failed (non-fatal): %s", exc)

    async for event in AgentLoop().stream(
        model=effective_model,
        system=_build_system_prompt(repo_owner, repo_name, model=effective_model),
        initial_message=_build_initial_message(query, repo_owner, repo_name, web_research_notes=web_notes),
        retrieval_tools=all_retrieval,
        final_answer_tools=_FINAL_ANSWER_TOOLS,
        config=AgentLoopConfig(
            max_iterations=settings.plan_max_iterations,
            cumulative_token_budget=settings.agent_token_budget,
            require_search_before_answer=True,
            thinking_budget=effective_thinking,
            planning_max_output_tokens=settings.planning_max_output_tokens,
        ),
        repo_owner=repo_owner,
        repo_name=repo_name,
        extra_context={"allowed_repos": allowed_repos} if allowed_repos is not None else None,
    ):
        if event["type"] == "done":
            plan = _parse_tool_block(event["tool_block"], query, event["stats"], effective_model)
            if plan.metadata:
                plan.metadata.web_research_used = bool(web_notes)
            logger.info(
                "planning: stream complete in %.0fms (iter=%d, tool_calls=%d, tool=%s, web=%s)",
                event["stats"].get("elapsed_ms", 0),
                event["stats"].get("iterations", 0),
                event["stats"].get("tool_calls", 0),
                event["tool_block"].get("name"),
                bool(web_notes),
            )
            yield {"type": "plan_complete", "plan": plan}
        else:
            # Pass through: agent_tool_call, agent_tool_result, thinking, token
            yield event
