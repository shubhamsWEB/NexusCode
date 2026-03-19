"""
Ask Mode LLM agent — agentic codebase Q&A.

Instead of pre-fetching context before the LLM call, Claude is given the
retrieval tools directly and decides what to search for. Claude iterates —
searching the vector DB, looking up symbols, tracing callers — then calls
answer_question when it has enough real context.

Flow:
  query → AgentLoop(tools=[search, get_symbol, find_callers, get_file_context])
        → Claude searches iteratively (up to ask_max_iterations turns)
        → Claude calls answer_question(answer, cited_files, follow_up_hints)
        → parse → AskResult

Tone: friendly senior-engineer mentor, conversational markdown, inline citations.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.config import settings
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────────

ASK_SYSTEM_PROMPT = """\
You are a senior engineer mentoring a developer who has questions about a live codebase.
Your job is to EXPLAIN — not to build plans.

PROCESS
───────
1. Use search_codebase to find relevant code. Be specific with queries.
2. Use get_symbol to look up specific functions or classes by name.
3. Use find_callers to trace how something is used across the codebase.
4. Use get_file_context to understand a file's structure and dependencies.
5. Use get_semantic_context with the key symbols you found — it surfaces
   architectural relationships the call graph cannot show: "AuthService
   validates JWTToken", "PaymentFlow coordinates StripeClient". Call it
   whenever you want to explain WHY two components are coupled.
6. Call multiple tools if needed — follow the code where it leads.
7. Once you have real code to reference, call answer_question.

You MUST call at least one search tool before calling answer_question.
For architecture or design questions, also call get_semantic_context on the
key symbols found — it provides pre-extracted relationship facts that improve
the quality of your explanation significantly.

ANSWER STYLE
────────────
• Friendly, direct, authoritative. Think Slack message from a senior teammate.
• Open with a clear 1-2 sentence answer. No preamble.
• Walk through the code citing real file paths and line numbers.
• Use fenced code blocks for key snippets.
• Close with 2-3 concrete follow-up questions grounded in what you found.

HARD RULES
──────────
• ONLY cite files and symbols you actually found via tool calls.
• NEVER invent file paths, function names, or line numbers.
• If something is not in the index, say so and suggest indexing the repo.
• Do NOT fill gaps with training knowledge — say explicitly what is missing.
"""

# ── Tool schema ────────────────────────────────────────────────────────────────

_ASK_ANSWER_TOOL: dict = {
    "name": "answer_question",
    "description": (
        "Answer the developer's question with a conversational markdown response "
        "grounded in retrieved code. Call ONLY after searching the codebase."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": (
                    "Full markdown answer: direct answer first, inline citations "
                    "(src/foo/bar.py:12-30), code blocks, 2-3 follow-up questions."
                ),
            },
            "cited_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths cited in the answer. Format: 'src/pipeline/pipeline.py:42-80'. Only from tool results.",
            },
            "follow_up_hints": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-3 follow-up questions grounded in the code you found.",
            },
        },
        "required": ["answer", "cited_files", "follow_up_hints"],
    },
}

# ── Result dataclass ───────────────────────────────────────────────────────────


class AskResult:
    """Parsed result from the ask agent."""

    __slots__ = (
        "answer",
        "cited_files",
        "context_tokens",
        "elapsed_ms",
        "follow_up_hints",
        "iterations",
        "quality_score",
        "tool_calls_count",
    )

    def __init__(
        self,
        answer: str,
        cited_files: list[str],
        follow_up_hints: list[str],
        elapsed_ms: float,
        quality_score: float = 0.0,
        context_tokens: int = 0,
        tool_calls_count: int = 0,
        iterations: int = 0,
    ):
        self.answer = answer
        self.cited_files = cited_files
        self.follow_up_hints = follow_up_hints
        self.elapsed_ms = elapsed_ms
        self.quality_score = quality_score
        self.context_tokens = context_tokens
        self.tool_calls_count = tool_calls_count
        self.iterations = iterations

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "cited_files": self.cited_files,
            "follow_up_hints": self.follow_up_hints,
            "elapsed_ms": self.elapsed_ms,
            "quality_score": self.quality_score,
            "context_tokens": self.context_tokens,
            "tool_calls_count": self.tool_calls_count,
            "iterations": self.iterations,
        }


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
    """Return a worldview preamble to prepend to the system prompt, or '' on miss."""
    if not repo_owner or not repo_name:
        return ""
    try:
        from src.evolution.worldview_generator import get_latest_worldview_text

        wv = await get_latest_worldview_text(repo_owner, repo_name)
        if wv:
            return (
                "CODEBASE UNDERSTANDING\n"
                "──────────────────────\n"
                "NexusCode has built the following semantic worldview of this repository "
                "from prior interactions and code analysis. Use it to orient your searches "
                "and calibrate your answers.\n\n"
                f"{wv}\n\n"
                "──────────────────────\n\n"
            )
    except Exception:
        logger.debug("Worldview fetch failed (non-fatal)")
    return ""


def _build_system_prompt(
    repo_owner: str | None,
    repo_name: str | None,
    model: str | None = None,
    worldview_preamble: str = "",
) -> str:
    from src.agent.rules import load_rules
    from src.llm.client import is_ollama_model

    prompt = worldview_preamble + ASK_SYSTEM_PROMPT
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
) -> str:
    scope = f" (repository: {repo_owner}/{repo_name})" if repo_owner and repo_name else ""
    return f"{query}{scope}"


def _parse_tool_block(tool_block: dict, stats: dict) -> AskResult:
    data = tool_block.get("input", {})
    return AskResult(
        answer=data.get("answer", "_No answer generated._"),
        cited_files=data.get("cited_files", []),
        follow_up_hints=data.get("follow_up_hints", []),
        elapsed_ms=stats.get("elapsed_ms", 0.0),
        context_tokens=stats.get("context_tokens", 0),
        tool_calls_count=stats.get("tool_calls", 0),
        iterations=stats.get("iterations", 0),
    )


# ── Public generate (non-streaming) ───────────────────────────────────────────


async def generate_answer(
    query: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    model: str | None = None,
    allowed_repos: list[str] | None = None,
) -> AskResult:
    """
    Run the Ask Mode agent loop (non-streaming).
    Claude searches the codebase iteratively then calls answer_question.
    """
    from src.agent.loop import AgentLoop, AgentLoopConfig
    from src.agent.mcp_bridge import get_external_tool_schemas
    from src.agent.tool_schemas import ASK_RETRIEVAL_TOOL_SCHEMAS

    # ── Relevance gate ─────────────────────────────────────────────────────────
    if settings.query_relevance_enabled:
        from src.retrieval.relevance import build_out_of_scope_message, check_query_relevance

        relevance = await check_query_relevance(query, repo_owner, repo_name)
        if not relevance.is_relevant:
            msg = build_out_of_scope_message(query, relevance)
            logger.info(
                "ask: relevance gate rejected query (score=%.3f, reason=%s)",
                relevance.best_score,
                relevance.reason,
            )
            return AskResult(
                answer=msg,
                cited_files=[],
                follow_up_hints=[
                    "Try asking about a specific function or file in the codebase",
                    "Search for a symbol name or module you want to understand",
                ],
                elapsed_ms=0.0,
                quality_score=relevance.best_score,
            )

    effective_model = model or settings.default_model
    all_retrieval = ASK_RETRIEVAL_TOOL_SCHEMAS + get_external_tool_schemas()

    worldview_preamble = await _fetch_worldview_context(repo_owner, repo_name)

    # Complexity-adaptive iteration limit (Gap 9)
    # Heuristic: long queries, multiple sub-questions, or "explain/how/why/trace"
    # framing suggest cross-cutting analysis that benefits from more iterations.
    _complex_keywords = {"explain", "how", "why", "trace", "flow", "all", "every", "end-to-end", "across", "compare", "difference", "relationship"}
    _query_lower = query.lower()
    _is_complex_ask = (
        len(query) > 150
        or "?" in query[query.find("?") + 1:]  # multiple question marks
        or any(kw in _query_lower for kw in _complex_keywords)
    )
    _ask_iterations = (
        getattr(settings, "ask_max_iterations_complex", settings.ask_max_iterations)
        if _is_complex_ask
        else settings.ask_max_iterations
    )

    tool_block, stats = await AgentLoop().run(
        model=effective_model,
        system=_build_system_prompt(
            repo_owner, repo_name, model=effective_model, worldview_preamble=worldview_preamble
        ),
        initial_message=_build_initial_message(query, repo_owner, repo_name),
        retrieval_tools=all_retrieval,
        final_answer_tools=[_ASK_ANSWER_TOOL],
        config=AgentLoopConfig(
            max_iterations=_ask_iterations,
            cumulative_token_budget=settings.agent_token_budget,
            require_search_before_answer=True,
            thinking_budget=0,
        ),
        repo_owner=repo_owner,
        repo_name=repo_name,
        extra_context={"allowed_repos": allowed_repos} if allowed_repos is not None else None,
    )

    result = _parse_tool_block(tool_block, stats)
    logger.info(
        "ask: %s answered in %.0fms (iter=%d, tool_calls=%d, tokens=%d)",
        effective_model,
        result.elapsed_ms,
        result.iterations,
        result.tool_calls_count,
        result.context_tokens,
    )
    return result


# ── Public stream generator ────────────────────────────────────────────────────


async def stream_generate_answer(
    query: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    model: str | None = None,
    allowed_repos: list[str] | None = None,
) -> AsyncIterator[dict]:
    """
    Stream the Ask Mode agent loop.

    Yields:
      {"type": "agent_tool_call",   "tool": str, "input_summary": str}
      {"type": "agent_tool_result", "tool": str, "tokens": int, "cumulative_tokens": int}
      {"type": "thinking",          "text": str}
      {"type": "token",             "text": str}
      {"type": "answer_complete",   "result": AskResult}
    """
    from src.agent.loop import AgentLoop, AgentLoopConfig
    from src.agent.mcp_bridge import get_external_tool_schemas
    from src.agent.tool_schemas import ASK_RETRIEVAL_TOOL_SCHEMAS

    # ── Relevance gate ─────────────────────────────────────────────────────────
    if settings.query_relevance_enabled:
        from src.retrieval.relevance import build_out_of_scope_message, check_query_relevance

        relevance = await check_query_relevance(query, repo_owner, repo_name)
        if not relevance.is_relevant:
            msg = build_out_of_scope_message(query, relevance)
            logger.info(
                "ask: relevance gate rejected query (score=%.3f, reason=%s)",
                relevance.best_score,
                relevance.reason,
            )
            result = AskResult(
                answer=msg,
                cited_files=[],
                follow_up_hints=[
                    "Try asking about a specific function or file in the codebase",
                    "Search for a symbol name or module you want to understand",
                ],
                elapsed_ms=0.0,
                quality_score=relevance.best_score,
            )
            yield {"type": "answer_complete", "result": result}
            return

    effective_model = model or settings.default_model
    all_retrieval = ASK_RETRIEVAL_TOOL_SCHEMAS + get_external_tool_schemas()

    worldview_preamble = await _fetch_worldview_context(repo_owner, repo_name)

    # Complexity-adaptive iteration limit (Gap 9)
    _complex_keywords = {"explain", "how", "why", "trace", "flow", "all", "every", "end-to-end", "across", "compare", "difference", "relationship"}
    _query_lower = query.lower()
    _is_complex_ask = (
        len(query) > 150
        or "?" in query[query.find("?") + 1:]
        or any(kw in _query_lower for kw in _complex_keywords)
    )
    _ask_iterations = (
        getattr(settings, "ask_max_iterations_complex", settings.ask_max_iterations)
        if _is_complex_ask
        else settings.ask_max_iterations
    )

    async for event in AgentLoop().stream(
        model=effective_model,
        system=_build_system_prompt(
            repo_owner, repo_name, model=effective_model, worldview_preamble=worldview_preamble
        ),
        initial_message=_build_initial_message(query, repo_owner, repo_name),
        retrieval_tools=all_retrieval,
        final_answer_tools=[_ASK_ANSWER_TOOL],
        config=AgentLoopConfig(
            max_iterations=_ask_iterations,
            cumulative_token_budget=settings.agent_token_budget,
            require_search_before_answer=True,
            thinking_budget=0,
        ),
        repo_owner=repo_owner,
        repo_name=repo_name,
        extra_context={"allowed_repos": allowed_repos} if allowed_repos is not None else None,
    ):
        if event["type"] == "done":
            result = _parse_tool_block(event["tool_block"], event["stats"])
            logger.info(
                "ask: stream complete in %.0fms (iter=%d, tool_calls=%d)",
                result.elapsed_ms,
                result.iterations,
                result.tool_calls_count,
            )
            yield {"type": "answer_complete", "result": result}
        else:
            # Pass through: agent_tool_call, agent_tool_result, thinking, token
            yield event
