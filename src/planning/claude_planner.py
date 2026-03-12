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
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

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


@dataclass
class PlannerExecutionContext:
    """Shared execution context used by sync and streaming planner flows."""

    query: str
    repo_owner: str | None
    repo_name: str | None
    effective_model: str
    analysis: Any
    effective_thinking: int
    retrieval_tools: list[dict]
    web_notes: str
    worldview_preamble: str
    extra_context: dict[str, Any] | None
    relevance: Any = None


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
                        with suppress(json.JSONDecodeError, ValueError):
                            val = json.loads(val)
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
5. Use get_semantic_context on the key symbols before finalising the plan —
   it reveals architectural intent the call graph cannot express: which
   component validates, coordinates, or delegates to which. This prevents
   plans that break hidden semantic contracts.
6. Search multiple times — explore the code thoroughly before planning.
7. Once you understand the system, call the appropriate final tool.

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
• For any plan touching more than one component, call get_semantic_context
  on the affected symbols — use the relationships it returns to validate
  that the plan preserves existing architectural contracts.

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
  search_codebase      → {"query": "JWT token validation"}
  get_symbol           → {"name": "authenticate"}
  find_callers         → {"symbol": "authenticate"}
  get_file_context     → {"path": "src/api/app.py"}
  get_semantic_context → {"symbols": ["AuthService", "JWTValidator"]}
Never call a tool with empty arguments — the call will fail.
"""


async def _fetch_worldview_context(repo_owner: str | None, repo_name: str | None) -> str:
    """Return a worldview preamble for the planning system prompt, or '' on miss."""
    if not repo_owner or not repo_name:
        return ""
    try:
        from src.evolution.worldview_generator import get_latest_worldview_text

        wv = await get_latest_worldview_text(repo_owner, repo_name)
        if wv:
            return (
                "CODEBASE UNDERSTANDING\n"
                "──────────────────────\n"
                "NexusCode has built the following semantic worldview of this repository. "
                "Use it to guide your retrieval strategy and planning decisions.\n\n"
                f"{wv}\n\n"
                "──────────────────────\n\n"
            )
    except Exception:
        logger.debug("Worldview fetch failed for planner (non-fatal)")
    return ""


def _build_system_prompt(
    repo_owner: str | None,
    repo_name: str | None,
    model: str | None = None,
    worldview_preamble: str = "",
) -> str:
    from src.agent.rules import load_rules
    from src.llm.client import is_ollama_model

    prompt = worldview_preamble + PLANNING_SYSTEM_PROMPT
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


def _build_minimal_system_prompt(model: str | None = None, worldview_preamble: str = "") -> str:
    """Fallback prompt when the richer prompt builder fails."""
    prompt = worldview_preamble + PLANNING_SYSTEM_PROMPT
    if model:
        try:
            from src.llm.client import is_ollama_model

            if is_ollama_model(model):
                prompt += _OLLAMA_TOOL_HINT
        except Exception:
            pass
    return prompt


def _build_minimal_initial_message(
    query: str,
    repo_owner: str | None,
    repo_name: str | None,
    web_research_notes: str = "",
) -> str:
    """Fallback initial message when prompt assembly fails."""
    scope = f" (repository: {repo_owner}/{repo_name})" if repo_owner and repo_name else ""
    message = f"{query}{scope}"
    if web_research_notes:
        message += f"\n\n## Web Research Notes\n{web_research_notes}"
    return message


async def _safe_fetch_worldview_context(repo_owner: str | None, repo_name: str | None) -> str:
    """Fetch worldview context without letting planner execution fail."""
    try:
        return await _fetch_worldview_context(repo_owner, repo_name)
    except Exception as exc:
        logger.warning("planning: worldview context fetch failed: %s", exc)
        return ""


def _safe_build_system_prompt(
    repo_owner: str | None,
    repo_name: str | None,
    model: str | None = None,
    worldview_preamble: str = "",
) -> str:
    """Build the system prompt with a minimal fallback on unexpected failure."""
    try:
        return _build_system_prompt(
            repo_owner,
            repo_name,
            model=model,
            worldview_preamble=worldview_preamble,
        )
    except Exception as exc:
        logger.warning("planning: system prompt build failed, using fallback: %s", exc)
        return _build_minimal_system_prompt(model=model, worldview_preamble=worldview_preamble)


def _safe_build_initial_message(
    query: str,
    repo_owner: str | None,
    repo_name: str | None,
    web_research_notes: str = "",
) -> str:
    """Build the user prompt with a minimal fallback on unexpected failure."""
    try:
        return _build_initial_message(
            query,
            repo_owner,
            repo_name,
            web_research_notes=web_research_notes,
        )
    except Exception as exc:
        logger.warning("planning: initial message build failed, using fallback: %s", exc)
        return _build_minimal_initial_message(
            query,
            repo_owner,
            repo_name,
            web_research_notes=web_research_notes,
        )


def _get_retrieval_tools() -> list[dict]:
    """Return the full retrieval toolset for planner runs."""
    from src.agent.mcp_bridge import get_external_tool_schemas
    from src.agent.tool_schemas import RETRIEVAL_TOOL_SCHEMAS

    return RETRIEVAL_TOOL_SCHEMAS + get_external_tool_schemas()


def _build_extra_context(allowed_repos: list[str] | None) -> dict[str, Any] | None:
    if allowed_repos is None:
        return None
    return {"allowed_repos": allowed_repos}


async def _run_relevance_gate(
    query: str,
    repo_owner: str | None,
    repo_name: str | None,
    model: str | None,
) -> tuple[bool, ImplementationPlan | None, Any]:
    """Return a pre-built out-of-scope response when strict relevance gating rejects."""
    mode = str(getattr(settings, "query_relevance_mode", "strict")).lower()
    if not settings.query_relevance_enabled or mode == "off":
        return False, None, None

    from src.retrieval.relevance import build_out_of_scope_message, check_query_relevance

    relevance = await check_query_relevance(query, repo_owner, repo_name)
    if relevance.is_relevant:
        return False, None, relevance

    logger.info(
        "planning: relevance gate flagged query (score=%.3f, reason=%s, mode=%s)",
        relevance.best_score,
        relevance.reason,
        mode,
    )
    if mode == "warn":
        return False, None, relevance

    msg = build_out_of_scope_message(query, relevance)
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
        retrieval_log=(
            f"Relevance gate: score={relevance.best_score:.3f} "
            f"< threshold={settings.query_relevance_threshold} ({relevance.reason})"
        ),
        elapsed_ms=0.0,
    )
    return True, plan, relevance


def _should_run_web_research(query: str, analysis: Any) -> bool:
    """Gate web research for tasks likely to benefit from external guidance."""
    if not getattr(settings, "web_research_selective_trigger", False):
        return True

    if getattr(analysis, "complexity", "") == "complex":
        return True

    lowered = query.lower()
    trigger_terms = (
        "best practice",
        "best practices",
        "migration",
        "library",
        "framework",
        "package",
        "dependency",
        "upgrade",
        "version",
        "integration",
        "pattern",
        "security",
        "performance",
    )
    return any(term in lowered for term in trigger_terms)


async def _maybe_run_web_research(
    query: str,
    repo_owner: str | None,
    repo_name: str | None,
    model: str,
    analysis: Any,
    enabled: bool,
) -> str:
    """Optionally fetch stack-aware web notes, with bounded latency and size."""
    if not enabled or not _should_run_web_research(query, analysis):
        return ""

    from src.planning.retriever import _extract_stack_fingerprint
    from src.planning.web_researcher import research_implementation

    timeout_s = getattr(settings, "web_research_timeout_s", 0)
    max_chars = getattr(settings, "web_research_max_chars", 0)

    try:
        stack_fp = await _extract_stack_fingerprint(repo_owner, repo_name)
        if timeout_s and timeout_s > 0:
            raw_notes = await asyncio.wait_for(
                research_implementation(query, stack_context=stack_fp, model=model),
                timeout=timeout_s,
            )
        else:
            raw_notes = await research_implementation(query, stack_context=stack_fp, model=model)
    except TimeoutError:
        logger.warning("planning: web research timed out after %ss", timeout_s)
        return ""
    except Exception as exc:
        logger.warning("planning: web research failed (non-fatal): %s", exc)
        return ""

    if not raw_notes:
        return ""

    web_notes = raw_notes[:max_chars] if max_chars and max_chars > 0 else raw_notes
    if web_notes:
        logger.info("planning: web research complete (%d chars)", len(web_notes))
    return web_notes


def _build_agent_loop_config(analysis: Any, thinking_budget: int):
    """Centralized planner AgentLoop configuration."""
    from src.agent.loop import AgentLoopConfig

    max_iterations = settings.plan_max_iterations
    token_budget = settings.agent_token_budget

    if getattr(analysis, "complexity", "") == "simple":
        max_iterations = getattr(settings, "plan_max_iterations_simple", max_iterations)
        token_budget = getattr(settings, "agent_token_budget_simple", token_budget)
    elif getattr(analysis, "complexity", "") == "moderate":
        max_iterations = getattr(settings, "plan_max_iterations_moderate", max_iterations)
        token_budget = getattr(settings, "agent_token_budget_moderate", token_budget)

    return AgentLoopConfig(
        max_iterations=max_iterations,
        cumulative_token_budget=token_budget,
        require_search_before_answer=True,
        thinking_budget=thinking_budget,
        planning_max_output_tokens=settings.planning_max_output_tokens,
    )


async def _build_planner_context(
    query: str,
    repo_owner: str | None,
    repo_name: str | None,
    web_research: bool,
    model: str | None,
    allowed_repos: list[str] | None,
    relevance: Any = None,
) -> PlannerExecutionContext:
    """Assemble the shared planner context for sync and streaming flows."""
    from src.planning.retriever import _analyze_query

    effective_model = model or settings.default_model
    analysis = _analyze_query(query)
    effective_thinking = (
        settings.planning_thinking_budget
        if getattr(analysis, "complexity", None) == "complex"
        else 0
    )

    web_task = asyncio.create_task(
        _maybe_run_web_research(
            query=query,
            repo_owner=repo_owner,
            repo_name=repo_name,
            model=effective_model,
            analysis=analysis,
            enabled=web_research,
        )
    )
    worldview_task = asyncio.create_task(_safe_fetch_worldview_context(repo_owner, repo_name))
    web_notes, worldview_preamble = await asyncio.gather(web_task, worldview_task)

    return PlannerExecutionContext(
        query=query,
        repo_owner=repo_owner,
        repo_name=repo_name,
        effective_model=effective_model,
        analysis=analysis,
        effective_thinking=effective_thinking,
        retrieval_tools=_get_retrieval_tools(),
        web_notes=web_notes,
        worldview_preamble=worldview_preamble,
        extra_context=_build_extra_context(allowed_repos),
        relevance=relevance,
    )


def _build_loop_inputs(ctx: PlannerExecutionContext) -> tuple[str, str, Any]:
    """Prepare prompts and config for an AgentLoop planner run."""
    return (
        _safe_build_system_prompt(
            ctx.repo_owner,
            ctx.repo_name,
            model=ctx.effective_model,
            worldview_preamble=ctx.worldview_preamble,
        ),
        _safe_build_initial_message(
            ctx.query,
            ctx.repo_owner,
            ctx.repo_name,
            web_research_notes=ctx.web_notes,
        ),
        _build_agent_loop_config(ctx.analysis, ctx.effective_thinking),
    )


async def _run_planning_loop(ctx: PlannerExecutionContext) -> tuple[dict, dict]:
    """Run the non-streaming planner agent loop."""
    from src.agent.loop import AgentLoop

    system_prompt, initial_message, config = _build_loop_inputs(ctx)
    return await AgentLoop().run(
        model=ctx.effective_model,
        system=system_prompt,
        initial_message=initial_message,
        retrieval_tools=ctx.retrieval_tools,
        final_answer_tools=_FINAL_ANSWER_TOOLS,
        config=config,
        repo_owner=ctx.repo_owner,
        repo_name=ctx.repo_name,
        extra_context=ctx.extra_context,
    )


async def _stream_planning_loop(ctx: PlannerExecutionContext) -> AsyncIterator[dict]:
    """Stream planner AgentLoop events."""
    from src.agent.loop import AgentLoop

    system_prompt, initial_message, config = _build_loop_inputs(ctx)
    async for event in AgentLoop().stream(
        model=ctx.effective_model,
        system=system_prompt,
        initial_message=initial_message,
        retrieval_tools=ctx.retrieval_tools,
        final_answer_tools=_FINAL_ANSWER_TOOLS,
        config=config,
        repo_owner=ctx.repo_owner,
        repo_name=ctx.repo_name,
        extra_context=ctx.extra_context,
    ):
        yield event


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


def _annotate_plan_metadata(plan: ImplementationPlan, ctx: PlannerExecutionContext) -> None:
    """Attach planner context metadata to the parsed plan."""
    if not plan.metadata:
        return

    plan.metadata.web_research_used = bool(ctx.web_notes)
    plan.metadata.query_complexity = getattr(ctx.analysis, "complexity", "")
    plan.metadata.sub_queries_count = len(getattr(ctx.analysis, "sub_queries", []))

    if ctx.relevance is not None:
        plan.metadata.retrieval_log += (
            f" | relevance={ctx.relevance.reason}:{ctx.relevance.best_score:.3f}"
        )
        if ctx.relevance.reason in {"ambiguous", "out_of_scope", "no_index"}:
            plan.metadata.grounding_warnings.append(
                f"Relevance gate flagged query as {ctx.relevance.reason} "
                f"({ctx.relevance.best_score:.3f})."
            )


def _log_planning_complete(
    ctx: PlannerExecutionContext,
    stats: dict,
    tool_name: str | None,
    mode: str,
) -> None:
    """Emit a single structured completion log for planner runs."""
    logger.info(
        "planning: %s complete (%s, model=%s, complexity=%s, elapsed_ms=%.0f, iter=%d, tool_calls=%d, tool=%s, web=%s)",
        mode,
        ctx.query[:80],
        ctx.effective_model,
        getattr(ctx.analysis, "complexity", ""),
        stats.get("elapsed_ms", 0),
        stats.get("iterations", 0),
        stats.get("tool_calls", 0),
        tool_name,
        bool(ctx.web_notes),
    )


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
    rejected, rejected_plan, relevance = await _run_relevance_gate(
        query,
        repo_owner,
        repo_name,
        model,
    )
    if rejected:
        return rejected_plan

    ctx = await _build_planner_context(
        query=query,
        repo_owner=repo_owner,
        repo_name=repo_name,
        web_research=web_research,
        model=model,
        allowed_repos=allowed_repos,
        relevance=relevance,
    )
    tool_block, stats = await _run_planning_loop(ctx)

    plan = _parse_tool_block(tool_block, query, stats, ctx.effective_model)
    _annotate_plan_metadata(plan, ctx)
    _log_planning_complete(ctx, stats, tool_block.get("name"), mode="sync")
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
    rejected, rejected_plan, relevance = await _run_relevance_gate(
        query,
        repo_owner,
        repo_name,
        model,
    )
    if rejected:
        yield {"type": "plan_complete", "plan": rejected_plan}
        return

    ctx = await _build_planner_context(
        query=query,
        repo_owner=repo_owner,
        repo_name=repo_name,
        web_research=web_research,
        model=model,
        allowed_repos=allowed_repos,
        relevance=relevance,
    )

    async for event in _stream_planning_loop(ctx):
        if event["type"] == "done":
            plan = _parse_tool_block(event["tool_block"], query, event["stats"], ctx.effective_model)
            _annotate_plan_metadata(plan, ctx)
            _log_planning_complete(ctx, event["stats"], event["tool_block"].get("name"), mode="stream")
            yield {"type": "plan_complete", "plan": plan}
        else:
            # Pass through: agent_tool_call, agent_tool_result, thinking, token
            yield event
