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
5. Call multiple tools if needed — follow the code where it leads.
6. Once you have real code to reference, call answer_question.

You MUST call at least one search tool before calling answer_question.

ANSWER STYLE
────────────
• Friendly, direct, authoritative. Think Slack message from a senior teammate.
• Open with a clear 1–2 sentence answer. No preamble.
• Walk through the code citing real file paths and line numbers.
• Use fenced code blocks for key snippets.
• Close with 2–3 concrete follow-up questions grounded in what you found.

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
        "elapsed_ms",
        "follow_up_hints",
        "quality_score",
        "context_tokens",
        "tool_calls_count",
        "iterations",
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


def _build_system_prompt(repo_owner: str | None, repo_name: str | None) -> str:
    from src.agent.rules import load_rules

    rules = load_rules(repo_owner, repo_name)
    if not rules:
        return ASK_SYSTEM_PROMPT
    return ASK_SYSTEM_PROMPT + f"\n\n---\n\n## Codebase-Specific Rules\n\n{rules}"


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
) -> AskResult:
    """
    Run the Ask Mode agent loop (non-streaming).
    Claude searches the codebase iteratively then calls answer_question.
    """
    from src.agent.loop import AgentLoop, AgentLoopConfig
    from src.agent.mcp_bridge import get_external_tool_schemas
    from src.agent.tool_schemas import ASK_RETRIEVAL_TOOL_SCHEMAS

    effective_model = model or settings.default_model
    all_retrieval = ASK_RETRIEVAL_TOOL_SCHEMAS + get_external_tool_schemas()

    tool_block, stats = await AgentLoop().run(
        model=effective_model,
        system=_build_system_prompt(repo_owner, repo_name),
        initial_message=_build_initial_message(query, repo_owner, repo_name),
        retrieval_tools=all_retrieval,
        final_answer_tools=[_ASK_ANSWER_TOOL],
        config=AgentLoopConfig(
            max_iterations=settings.ask_max_iterations,
            cumulative_token_budget=settings.agent_token_budget,
            require_search_before_answer=True,
            thinking_budget=0,
        ),
        repo_owner=repo_owner,
        repo_name=repo_name,
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

    effective_model = model or settings.default_model
    all_retrieval = ASK_RETRIEVAL_TOOL_SCHEMAS + get_external_tool_schemas()

    async for event in AgentLoop().stream(
        model=effective_model,
        system=_build_system_prompt(repo_owner, repo_name),
        initial_message=_build_initial_message(query, repo_owner, repo_name),
        retrieval_tools=all_retrieval,
        final_answer_tools=[_ASK_ANSWER_TOOL],
        config=AgentLoopConfig(
            max_iterations=settings.ask_max_iterations,
            cumulative_token_budget=settings.agent_token_budget,
            require_search_before_answer=True,
            thinking_budget=0,
        ),
        repo_owner=repo_owner,
        repo_name=repo_name,
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
