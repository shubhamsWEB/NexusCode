"""
AgentLoop — multi-turn Claude conversation with retrieval tool use.

Stripe-inspired design: instead of pre-fetching context before the LLM call,
we give Claude the retrieval tools directly and let it decide what to search for.
Claude iterates — searching, following leads, tracing call graphs — until confident,
then calls the final answer tool.

Deterministic gates (Stripe-inspired):
  Gate 1 — Iteration:     stop at max_iterations, force final answer
  Gate 2 — Token budget:  stop if cumulative tool result tokens > budget, force final answer
  Gate 3 — Grounding:     raise if Claude answers without calling any search tool first

Streaming yields SSE-compatible dicts:
  {"type": "agent_tool_call",   "tool": "search_codebase", "input_summary": "..."}
  {"type": "agent_tool_result", "tool": "search_codebase", "tokens": 1240}
  {"type": "thinking",          "text": "..."}
  {"type": "token",             "text": "..."}           ← final answer only
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from src.config import settings
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_FORCE_ANSWER_MSG = (
    "You have reached the context limit. "
    "Stop searching and call the answer tool now with what you know. "
    "If some details are uncertain, say so clearly in your answer."
)

_SOFT_ANSWER_NUDGE = (
    "You have gathered substantial codebase context. "
    "If you have enough information to answer comprehensively, please call the answer tool now. "
    "Only continue searching if critical details are still missing."
)

_DUPLICATE_QUERY_MSG = (
    "Note: Your last search query closely matches one you already ran. "
    "Repeating it will likely return the same results. "
    "Use the 'think' tool to evaluate whether you already have enough context, "
    "then either call the answer tool or try a distinctly different query."
)

# ── Query normalisation (for duplicate detection) ─────────────────────────────


def _normalize_query(query: str) -> str:
    """Produce a canonical form of a search query for duplicate detection.

    Lowercases, splits into words, removes stop words, sorts, and takes the
    top 8 significant tokens.  Two queries that differ only in word order or
    minor phrasing will map to the same key.
    """
    _STOP = {"the", "a", "an", "in", "of", "to", "how", "does", "do",
             "is", "are", "what", "where", "why", "which", "for", "and", "or"}
    tokens = [w for w in query.lower().split() if w not in _STOP and len(w) > 1]
    return " ".join(sorted(tokens)[:8])


# ── Budget status line (BATS paper pattern) ───────────────────────────────────


def _budget_line(iteration: int, max_iterations: int, tokens: int, budget: int) -> str:
    """One-line budget status injected into every tool-result batch.

    Mirrors the BATS paper (arxiv 2511.17006) finding that showing remaining
    budget produces calibrated stopping: agents answer sooner when confident
    and search longer only when genuinely uncertain, reducing cost ~31%.
    """
    pct = int(100 * tokens / budget) if budget else 0
    iters_left = max(0, max_iterations - iteration - 1)
    return (
        f"[Search budget: iteration {iteration + 1}/{max_iterations} | "
        f"context {tokens:,}/{budget:,} tokens ({pct}%) | "
        f"{iters_left} iteration{'s' if iters_left != 1 else ''} remaining before forced answer]"
    )


# ── Final-answer tool description enhancer ────────────────────────────────────


def _enhance_final_answer_tools(tools: list[dict]) -> list[dict]:
    """Append an early-calling invitation to every final-answer tool's description.

    Follows the Vercel AI SDK / pydantic-ai pattern: the tool schema itself
    explicitly invites Claude to call it early rather than only as a last resort.
    Applied once at the start of each agent loop.
    """
    _SUFFIX = (
        "\n\nIMPORTANT: Call this tool as soon as you have gathered enough context "
        "to answer the question well. You do NOT need to exhaust all available searches — "
        "a thorough answer based on good context is far better than over-searching. "
        "Use the 'think' tool first if you are unsure whether you have enough."
    )
    enhanced = []
    for t in tools:
        desc = t.get("description", "")
        if desc and _SUFFIX.strip()[:30] not in desc:
            t = {**t, "description": desc.rstrip() + _SUFFIX}
        enhanced.append(t)
    return enhanced


@dataclass
class AgentLoopConfig:
    max_iterations: int = 5
    cumulative_token_budget: int = 80_000
    require_search_before_answer: bool = True
    thinking_budget: int = 0  # 0 = disabled; >0 = extended thinking for plan mode
    planning_max_output_tokens: int = 8000  # base max output tokens per turn
    soft_answer_threshold: float = 0.60  # fraction of cumulative_token_budget at which to nudge toward answering


class AgentGroundingError(RuntimeError):
    """Claude attempted to answer without calling any search tool first."""


class AgentMaxIterationsError(RuntimeError):
    """Agent loop ended without Claude calling a final answer tool."""


def _estimate_tokens(text: str) -> int:
    """Rough 3.5-chars-per-token estimate (code is denser than prose)."""
    return max(1, int(len(text) / 3.5))


def _is_retryable(exc) -> bool:
    """Return True if an Anthropic APIStatusError should be retried.

    Handles both normal HTTP 429/529 errors and overload errors delivered as
    SSE error events inside a 200-status stream (status_code=200 on the exc).
    """
    if getattr(exc, "status_code", None) in (429, 529):
        return True
    # Overload delivered as SSE error event — response.status_code is 200
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return body.get("error", {}).get("type") == "overloaded_error"
    return False


def _tool_input_summary(tool_name: str, tool_input: dict) -> str:
    """Return a short human-readable summary of a tool call for SSE events."""
    # Known local tools
    if tool_name == "search_codebase":
        return tool_input.get("query", "")[:80]
    if tool_name == "get_symbol":
        return tool_input.get("name", "")[:80]
    if tool_name == "find_callers":
        return tool_input.get("symbol", "")[:80]
    if tool_name == "get_file_context":
        return tool_input.get("path", "")[:80]

    # External MCP tools — try common descriptive keys in priority order
    _DESCRIPTIVE_KEYS = (
        "query", "search", "text",            # search-like
        "libraryName", "library", "package",  # library docs (Context7, etc.)
        "topic", "subject",                   # context filters
        "context7CompatibleLibraryID",        # Context7 specific
        "name", "id", "identifier",           # lookup
        "path", "file", "url",                # resource
        "input", "message", "prompt",         # generic
    )
    for key in _DESCRIPTIVE_KEYS:
        val = tool_input.get(key)
        if val and isinstance(val, str) and val.strip():
            return f"{val.strip()[:70]}"

    # Fall back to first key: value pair
    if tool_input:
        k, v = next(iter(tool_input.items()))
        return f"{k}: {str(v)[:60]}"
    return ""


# ── Token-saving helpers ───────────────────────────────────────────────────────

_MAX_PRIOR_RESULT_CHARS = 2_400  # ≈600 tokens; older tool results are truncated to this


def _add_cache_control_to_last(tools: list[dict]) -> list[dict]:
    """Return tools with cache_control on the last entry.

    Marks system + all tool schemas as a cacheable prefix (Anthropic prompt
    caching). Saves re-processing ~2 000+ tokens on every turn after the first
    in a multi-turn agent loop at ~10% of normal input token cost.
    """
    if not tools:
        return tools
    result = list(tools)
    result[-1] = {**result[-1], "cache_control": {"type": "ephemeral"}}
    return result


def _truncate_prior_tool_results(messages: list[dict]) -> None:
    """Compact older tool-result batches in-place to cap O(n²) token growth.

    Leaves the most-recent user tool-result batch untouched (Claude needs the
    full text of what it just retrieved). Earlier batches are truncated to
    _MAX_PRIOR_RESULT_CHARS to bound accumulated context across iterations.
    """
    tr_indices = [
        i for i, m in enumerate(messages)
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and m["content"]
        and m["content"][0].get("type") == "tool_result"
    ]
    for idx in tr_indices[:-1]:  # leave the latest batch untouched
        for result in messages[idx]["content"]:
            text = result.get("content", "")
            if isinstance(text, str) and len(text) > _MAX_PRIOR_RESULT_CHARS:
                result["content"] = text[:_MAX_PRIOR_RESULT_CHARS] + "\n[...truncated]"


def _build_wm_preamble(wm: dict) -> str:
    """Build a working memory summary string to prepend to the system prompt.

    Returns an empty string if there is nothing meaningful to show.
    """
    lines = ["## Working Memory (accumulated so far)"]
    found_files = wm.get("found_files", [])
    if found_files:
        lines.append(f"Found files: {', '.join(found_files[:10])}")
    found_symbols = wm.get("found_symbols", [])
    if found_symbols:
        lines.append(f"Found symbols: {', '.join(found_symbols[:10])}")
    visited_paths = wm.get("visited_paths", [])
    if visited_paths:
        lines.append(f"Visited paths: {', '.join(visited_paths[:8])}")
    if len(lines) == 1:
        return ""  # nothing meaningful to add
    return "\n".join(lines) + "\n\n"


def _coerce_to_dict(raw: object) -> dict:
    """Ensure a tool block's input is always a plain dict.

    Some providers (Ollama/GLM) return tool arguments as a JSON *string*
    rather than a pre-parsed dict.  This normalizer handles all observed
    variants so downstream _parse_tool_block code can safely call .get().
    """
    import json as _json
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if raw:
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except _json.JSONDecodeError:
                pass
    return {}





class AgentLoop:
    """
    Runs a multi-turn Claude conversation where Claude uses retrieval tools
    to gather codebase context, then calls a final answer tool.

    Usage:
        loop = AgentLoop()
        tool_block, stats = await loop.run(
            model="claude-sonnet-4-6",
            system=SYSTEM_PROMPT,
            initial_message="How does authentication work?",
            retrieval_tools=RETRIEVAL_TOOL_SCHEMAS,
            final_answer_tools=[ANSWER_TOOL_SCHEMA],
            config=AgentLoopConfig(max_iterations=5),
            repo_owner="acme",
            repo_name="api",
        )
    """

    async def run(
        self,
        model: str,
        system: str,
        initial_message: str,
        retrieval_tools: list[dict],
        final_answer_tools: list[dict],
        config: AgentLoopConfig,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        extra_context: dict | None = None,
    ) -> tuple[dict, dict]:
        """
        Run the agent loop (non-streaming).

        Returns:
            (tool_block_dict, agent_stats)
            tool_block_dict: {"name": str, "input": dict} — the final answer tool call
            agent_stats: {iterations, tool_calls, context_tokens, elapsed_ms, session_id}
        """
        import anthropic

        from src.agent.tool_executor import execute_tool
        from src.agent.tool_schemas import LOAD_ARTIFACT_SCHEMA, THINK_TOOL_SCHEMA
        from src.llm.client import (
            MAX_RETRIES,
            RateLimitOrOverloadError,
            get_client_for_model,
            get_retry_after,
            is_ollama_model,
            semaphore,
        )

        client = get_client_for_model(model)
        _use_caching = not is_ollama_model(model)  # Ollama doesn't support prompt caching

        # ── Artifact store setup ───────────────────────────────────────────────
        artifact_store = None
        if settings.agent_session_enabled:
            from src.agent.artifact_store import ArtifactStore

            artifact_store = ArtifactStore(ttl=settings.artifact_ttl_seconds)
            extra_context = dict(extra_context or {})
            extra_context["_artifact_store"] = artifact_store

        # Enhance final-answer tool descriptions to invite early calling
        # (Vercel AI SDK / pydantic-ai pattern).
        final_answer_tools = _enhance_final_answer_tools(list(final_answer_tools))

        # Inject the "think" tool (and load_artifact when store is active) into retrieval tools.
        # think is side-effect-free so it must NOT count toward search_tools_called.
        _extra_tools = [THINK_TOOL_SCHEMA]
        if artifact_store is not None:
            _extra_tools.append(LOAD_ARTIFACT_SCHEMA)
        retrieval_tools_with_think = [*list(retrieval_tools), *_extra_tools]

        final_tool_names = {t["name"] for t in final_answer_tools}
        retrieval_tool_names = {t["name"] for t in retrieval_tools}  # excludes "think"/"load_artifact"
        all_tools = retrieval_tools_with_think + final_answer_tools

        messages: list[dict] = [{"role": "user", "content": initial_message}]

        search_tools_called = 0
        total_context_tokens = 0
        total_tool_calls = 0
        force_message_added = False
        soft_nudge_added = False
        seen_search_queries: set[str] = set()  # duplicate-query detection
        t0 = time.monotonic()

        # +2 safety headroom for the forced-final-answer turn
        for iteration in range(config.max_iterations + 2):
            # ── Gate 1 & 2: decide whether to force a final answer this turn ────
            # Gate 2 (token budget) is bypassed when token_budgeting_enabled=False,
            # allowing the LLM to consume the full retrieved context.
            _budget_gate = (
                settings.token_budgeting_enabled
                and total_context_tokens > config.cumulative_token_budget
            )
            force_final = iteration >= config.max_iterations or _budget_gate

            if force_final:
                tools_this_turn = final_answer_tools
                tool_choice: dict = {"type": "any"}
                if not force_message_added:
                    messages.append({"role": "user", "content": _FORCE_ANSWER_MSG})
                    force_message_added = True
                    logger.warning(
                        "agent_loop: forcing final answer (iter=%d tokens=%d)",
                        iteration,
                        total_context_tokens,
                    )
            else:
                tools_this_turn = all_tools
                tool_choice = {"type": "auto"}

            # ── Call the API with retry ────────────────────────────────────────
            # Anthropic forbids thinking + tool_choice:{type:any}, so disable
            # thinking when we force the final answer turn.
            use_thinking = config.thinking_budget > 0 and not force_final
            max_tokens = config.planning_max_output_tokens + config.thinking_budget if use_thinking else config.planning_max_output_tokens
            _truncate_prior_tool_results(messages)
            tools_for_turn = (
                _add_cache_control_to_last(tools_this_turn)
                if _use_caching
                else list(tools_this_turn)
            )

            # ── Working memory: inject accumulated context into system prompt ──
            effective_system = system
            if iteration > 0 and artifact_store is not None:
                try:
                    wm = await artifact_store.get_working_memory()
                    wm_prefix = _build_wm_preamble(wm)
                    if wm_prefix:
                        effective_system = wm_prefix + system
                except Exception:
                    pass  # non-fatal: fall back to original system prompt

            params: dict = {
                "model": model,
                "system": effective_system,
                "messages": messages,
                "tools": tools_for_turn,
                "tool_choice": tool_choice,
                "max_tokens": max_tokens,
            }
            if use_thinking:
                params["thinking"] = {"type": "enabled", "budget_tokens": config.thinking_budget}
            else:
                params["temperature"] = 0

            response = None
            last_exc = None
            async with semaphore:
                for attempt in range(MAX_RETRIES + 1):
                    try:
                        # Use streaming internally when thinking is enabled — the Anthropic
                        # SDK rejects non-streaming calls whose max_tokens may exceed the
                        # non-streaming timeout limit (e.g. 16k + thinking_budget).
                        if use_thinking:
                            async with client.messages.stream(**params) as _stream:
                                response = await _stream.get_final_message()
                        else:
                            response = await client.messages.create(**params)
                        break
                    except anthropic.APIStatusError as exc:
                        if _is_retryable(exc) and attempt < MAX_RETRIES:
                            wait = min(get_retry_after(exc) or (5 * 2**attempt), 120)
                            logger.warning(
                                "agent_loop: HTTP %s, retry %d in %.0fs",
                                exc.status_code,
                                attempt + 1,
                                wait,
                            )
                            last_exc = exc
                            await asyncio.sleep(wait)
                        else:
                            raise

            if response is None:
                raise RateLimitOrOverloadError(last_exc)

            # ── Process tool calls in the response ────────────────────────────
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            # No tool calls at all
            if not tool_use_blocks:
                text = " ".join(
                    b.text for b in response.content if hasattr(b, "text") and b.text
                )
                logger.warning(
                    "agent_loop: no tool calls at iter=%d stop_reason=%s",
                    iteration,
                    response.stop_reason,
                )
                # Nudge 1: Haven't searched yet — require search before answering.
                if config.require_search_before_answer and search_tools_called == 0 and not force_final:
                    messages.append({"role": "assistant", "content": [{"type": "text", "text": text or "(no response)"}]})
                    messages.append({
                        "role": "user",
                        "content": (
                            "You must use the search tools to look up relevant code in the codebase "
                            "before providing your answer. Please call search_codebase or get_symbol now."
                        ),
                    })
                    logger.warning("agent_loop: nudging Claude to search (iter=%d)", iteration)
                    continue
                # Nudge 2: Searched but Claude responded with plain text instead of calling the
                # answer tool. This often happens when Claude uses 'think' to draft an answer and
                # then confuses the echoed thought with having already called the answer tool.
                if search_tools_called > 0 and not force_final:
                    messages.append({"role": "assistant", "content": [{"type": "text", "text": text or "(no response)"}]})
                    messages.append({
                        "role": "user",
                        "content": (
                            "You did not call the answer tool. Plain text responses are not accepted — "
                            "you MUST call answer_question to submit your response. "
                            "Please call the answer tool now with your complete answer."
                        ),
                    })
                    logger.warning("agent_loop: nudging Claude to call answer tool (iter=%d)", iteration)
                    continue
                stats = _make_stats(iteration, total_tool_calls, total_context_tokens, t0, search_tools_called)
                if artifact_store is not None:
                    stats["session_id"] = artifact_store.session_id
                    await artifact_store.close()
                return {"name": "_text_fallback", "input": {"answer": text, "cited_files": [], "follow_up_hints": []}}, stats

            # Check if Claude called a final answer tool
            final_blocks = [b for b in tool_use_blocks if b.name in final_tool_names]
            if final_blocks:
                # ── Gate 3: grounding check ────────────────────────────────────
                if config.require_search_before_answer and search_tools_called == 0:
                    raise AgentGroundingError(
                        "Claude called the final answer tool without searching the codebase. "
                        "This is a grounding violation — the answer would be based on training data only."
                    )
                stats = _make_stats(iteration + 1, total_tool_calls, total_context_tokens, t0, search_tools_called)
                if artifact_store is not None:
                    stats["session_id"] = artifact_store.session_id
                    await artifact_store.close()
                final_b = final_blocks[0]
                return {"name": final_b.name, "input": _coerce_to_dict(final_b.input)}, stats

            if force_final and not final_blocks:
                # We forced but Claude still called retrieval tools — error
                raise AgentMaxIterationsError(
                    "Agent loop ended without Claude calling a final answer tool "
                    "even after forcing it."
                )

            # ── Execute retrieval tool calls and build tool_result messages ────
            # Add Claude's response to the conversation
            assistant_content = []
            for b in response.content:
                if b.type == "tool_use":
                    assistant_content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                elif b.type == "thinking":
                    assistant_content.append({
                        "type": "thinking",
                        "thinking": b.thinking,
                        "signature": b.signature,  # required by API when replaying thinking blocks
                    })
                else:
                    assistant_content.append({"type": "text", "text": getattr(b, "text", "")})
            messages.append({"role": "assistant", "content": assistant_content})

            # Execute all retrieval tools in parallel via asyncio.gather
            async def _exec_tool_run(block):
                return block, await execute_tool(
                    name=block.name,
                    tool_input=block.input,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    extra_context=extra_context,
                )

            tool_pairs = await asyncio.gather(*[_exec_tool_run(b) for b in tool_use_blocks])

            tool_results = []
            duplicate_detected = False
            for block, result_text in tool_pairs:
                total_tool_calls += 1
                if block.name in retrieval_tool_names:
                    search_tools_called += 1
                result_tokens = _estimate_tokens(result_text)
                total_context_tokens += result_tokens

                logger.debug(
                    "agent_loop: iter=%d tool=%s tokens=%d cumulative=%d",
                    iteration,
                    block.name,
                    result_tokens,
                    total_context_tokens,
                )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )

                # ── Duplicate query detection ──────────────────────────────────
                # If Claude repeats a semantically identical search, the results
                # will be the same — warn it and suggest answering instead.
                if block.name == "search_codebase" and not duplicate_detected:
                    raw_query = _coerce_to_dict(block.input).get("query", "")
                    if raw_query:
                        norm = _normalize_query(raw_query)
                        if norm in seen_search_queries:
                            tool_results.append({"type": "text", "text": _DUPLICATE_QUERY_MSG})
                            duplicate_detected = True
                            logger.debug(
                                "agent_loop: duplicate query detected at iter=%d: %r",
                                iteration, raw_query[:60],
                            )
                        else:
                            seen_search_queries.add(norm)

            # ── Budget status line (BATS paper pattern) ───────────────────────
            # Injected into every tool-result batch so Claude always knows its
            # remaining budget.  Produces calibrated stopping: answer sooner when
            # confident, search longer only when genuinely uncertain.
            if not force_final:
                tool_results.append({
                    "type": "text",
                    "text": _budget_line(iteration, config.max_iterations, total_context_tokens, config.cumulative_token_budget),
                })

            # ── Soft nudge: invite Claude to answer when context is rich enough ──
            # Triggers on the second-to-last normal iteration OR when token usage
            # crosses soft_answer_threshold of the budget. Token-threshold nudge is
            # skipped when token_budgeting_enabled=False so Claude can keep searching.
            if (
                not soft_nudge_added
                and not force_final
                and search_tools_called >= 1
                and (
                    (settings.token_budgeting_enabled and total_context_tokens > config.cumulative_token_budget * config.soft_answer_threshold)
                    or iteration >= config.max_iterations - 1
                )
            ):
                tool_results.append({"type": "text", "text": _SOFT_ANSWER_NUDGE})
                soft_nudge_added = True
                logger.debug(
                    "agent_loop: soft nudge at iter=%d tokens=%d",
                    iteration,
                    total_context_tokens,
                )

            messages.append({"role": "user", "content": tool_results})

        if artifact_store is not None:
            await artifact_store.close()
        raise AgentMaxIterationsError("Agent loop exhausted all iterations without a final answer.")

    async def stream(
        self,
        model: str,
        system: str,
        initial_message: str,
        retrieval_tools: list[dict],
        final_answer_tools: list[dict],
        config: AgentLoopConfig,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        extra_context: dict | None = None,
    ) -> AsyncIterator[dict]:
        """
        Stream the agent loop.

        Yields:
          {"type": "agent_tool_call",   "tool": str, "input_summary": str}
          {"type": "agent_tool_result", "tool": str, "tokens": int}
          {"type": "thinking",          "text": str}
          {"type": "token",             "text": str}   ← final answer tool only
          {"type": "done",              "tool_block": dict, "stats": dict}
        """
        import anthropic

        from src.agent.tool_executor import execute_tool
        from src.agent.tool_schemas import LOAD_ARTIFACT_SCHEMA, THINK_TOOL_SCHEMA
        from src.llm.client import (
            MAX_RETRIES,
            RateLimitOrOverloadError,
            get_client_for_model,
            get_retry_after,
            is_ollama_model,
            semaphore,
        )

        client = get_client_for_model(model)
        _use_caching = not is_ollama_model(model)

        # ── Artifact store setup ───────────────────────────────────────────────
        artifact_store = None
        if settings.agent_session_enabled:
            from src.agent.artifact_store import ArtifactStore

            artifact_store = ArtifactStore(ttl=settings.artifact_ttl_seconds)
            extra_context = dict(extra_context or {})
            extra_context["_artifact_store"] = artifact_store

        # Enhance final-answer tool descriptions to invite early calling.
        final_answer_tools = _enhance_final_answer_tools(list(final_answer_tools))

        # Inject the "think" tool (and load_artifact when store is active) alongside retrieval tools.
        _extra_tools = [THINK_TOOL_SCHEMA]
        if artifact_store is not None:
            _extra_tools.append(LOAD_ARTIFACT_SCHEMA)
        retrieval_tools_with_think = [*list(retrieval_tools), *_extra_tools]

        final_tool_names = {t["name"] for t in final_answer_tools}
        retrieval_tool_names = {t["name"] for t in retrieval_tools}  # excludes "think"/"load_artifact"
        all_tools = retrieval_tools_with_think + final_answer_tools

        messages: list[dict] = [{"role": "user", "content": initial_message}]

        search_tools_called = 0
        total_context_tokens = 0
        total_tool_calls = 0
        force_message_added = False
        soft_nudge_added = False
        seen_search_queries: set[str] = set()  # duplicate-query detection
        t0 = time.monotonic()

        for iteration in range(config.max_iterations + 2):
            _budget_gate = (
                settings.token_budgeting_enabled
                and total_context_tokens > config.cumulative_token_budget
            )
            force_final = iteration >= config.max_iterations or _budget_gate

            if force_final:
                tools_this_turn = final_answer_tools
                tool_choice: dict = {"type": "any"}
                if not force_message_added:
                    messages.append({"role": "user", "content": _FORCE_ANSWER_MSG})
                    force_message_added = True
            else:
                tools_this_turn = all_tools
                tool_choice = {"type": "auto"}

            # Anthropic forbids thinking + tool_choice:{type:any}, so disable
            # thinking when we force the final answer turn.
            use_thinking = config.thinking_budget > 0 and not force_final
            max_tokens = config.planning_max_output_tokens + config.thinking_budget if use_thinking else config.planning_max_output_tokens
            _truncate_prior_tool_results(messages)
            tools_for_turn = (
                _add_cache_control_to_last(tools_this_turn)
                if _use_caching
                else list(tools_this_turn)
            )

            # ── Working memory: inject accumulated context into system prompt ──
            effective_system = system
            if iteration > 0 and artifact_store is not None:
                try:
                    wm = await artifact_store.get_working_memory()
                    wm_prefix = _build_wm_preamble(wm)
                    if wm_prefix:
                        effective_system = wm_prefix + system
                except Exception:
                    pass  # non-fatal

            params: dict = {
                "model": model,
                "system": effective_system,
                "messages": messages,
                "tools": tools_for_turn,
                "tool_choice": tool_choice,
                "max_tokens": max_tokens,
            }
            if use_thinking:
                params["thinking"] = {"type": "enabled", "budget_tokens": config.thinking_budget}
            else:
                params["temperature"] = 0

            # ── Stream this turn ───────────────────────────────────────────────
            # Track which tool is currently being streamed to detect final answer tools
            current_tool_name: str | None = None
            is_streaming_final_tool = False

            last_exc = None
            streamed_ok = False

            async with semaphore:
                for attempt in range(MAX_RETRIES + 1):
                    try:
                        if force_final:
                            # Use create() for the forced-final-answer turn — mirrors the
                            # non-streaming run() path (which works) and avoids issues when
                            # history contains thinking blocks from prior turns but thinking
                            # must be disabled here (tool_choice:{type:any} forbids thinking).
                            final_message = await client.messages.create(**params)
                        else:
                            async with client.messages.stream(**params) as stream:
                                async for event in stream:
                                    event_type = getattr(event, "type", None)

                                    # Detect tool name from content_block_start
                                    if event_type == "content_block_start":
                                        cb = getattr(event, "content_block", None)
                                        if cb:
                                            cb_type = getattr(cb, "type", None)
                                            if cb_type == "tool_use":
                                                current_tool_name = getattr(cb, "name", None)
                                                is_streaming_final_tool = (
                                                    current_tool_name in final_tool_names
                                                )

                                    elif event_type == "content_block_stop":
                                        current_tool_name = None
                                        is_streaming_final_tool = False

                                    # Stream thinking (always)
                                    elif event_type == "thinking":
                                        thinking_text = getattr(event, "thinking", None)
                                        if thinking_text:
                                            yield {"type": "thinking", "text": thinking_text}

                                    # Stream text/input_json only for final answer tools
                                    elif event_type == "text":
                                        text = getattr(event, "text", None)
                                        if text and is_streaming_final_tool:
                                            yield {"type": "token", "text": text}

                                    elif event_type == "input_json":
                                        partial = getattr(event, "partial_json", None)
                                        if partial and is_streaming_final_tool:
                                            yield {"type": "token", "text": partial}

                                final_message = await stream.get_final_message()
                        streamed_ok = True
                        break

                    except anthropic.APIStatusError as exc:
                        if _is_retryable(exc) and attempt < MAX_RETRIES:
                            wait = min(get_retry_after(exc) or (5 * 2**attempt), 120)
                            logger.warning(
                                "agent_loop stream: HTTP %s, retry %d in %.0fs",
                                exc.status_code,
                                attempt + 1,
                                wait,
                            )
                            last_exc = exc
                            await asyncio.sleep(wait)
                        else:
                            raise

            if not streamed_ok:
                raise RateLimitOrOverloadError(last_exc)

            # ── Process the completed message ─────────────────────────────────
            tool_use_blocks = [b for b in final_message.content if b.type == "tool_use"]

            if not tool_use_blocks:
                text = " ".join(
                    b.text
                    for b in final_message.content
                    if hasattr(b, "text") and b.text
                )
                logger.warning(
                    "agent_loop stream: no tool calls at iter=%d stop_reason=%s",
                    iteration,
                    getattr(final_message, "stop_reason", "?"),
                )
                # Nudge 1: Haven't searched yet — require search before answering.
                if config.require_search_before_answer and search_tools_called == 0 and not force_final:
                    messages.append({"role": "assistant", "content": [{"type": "text", "text": text or "(no response)"}]})
                    messages.append({
                        "role": "user",
                        "content": (
                            "You must use the search tools to look up relevant code in the codebase "
                            "before providing your answer. Please call search_codebase or get_symbol now."
                        ),
                    })
                    logger.warning("agent_loop stream: nudging Claude to search (iter=%d)", iteration)
                    continue
                # Nudge 2: Searched but Claude responded with plain text instead of calling the
                # answer tool. This often happens when Claude uses 'think' to draft an answer and
                # then confuses the echoed thought with having already called the answer tool.
                if search_tools_called > 0 and not force_final:
                    messages.append({"role": "assistant", "content": [{"type": "text", "text": text or "(no response)"}]})
                    messages.append({
                        "role": "user",
                        "content": (
                            "You did not call the answer tool. Plain text responses are not accepted — "
                            "you MUST call answer_question to submit your response. "
                            "Please call the answer tool now with your complete answer."
                        ),
                    })
                    logger.warning("agent_loop stream: nudging Claude to call answer tool (iter=%d)", iteration)
                    continue
                stats = _make_stats(iteration, total_tool_calls, total_context_tokens, t0, search_tools_called)
                if artifact_store is not None:
                    stats["session_id"] = artifact_store.session_id
                    await artifact_store.close()
                yield {
                    "type": "done",
                    "tool_block": {"name": "_text_fallback", "input": {"answer": text, "cited_files": [], "follow_up_hints": []}},
                    "stats": stats,
                }
                return

            final_blocks = [b for b in tool_use_blocks if b.name in final_tool_names]
            if final_blocks:
                if config.require_search_before_answer and search_tools_called == 0:
                    raise AgentGroundingError(
                        "Claude answered without searching the codebase first."
                    )
                stats = _make_stats(iteration + 1, total_tool_calls, total_context_tokens, t0, search_tools_called)
                if artifact_store is not None:
                    stats["session_id"] = artifact_store.session_id
                    await artifact_store.close()
                final_b = final_blocks[0]
                yield {
                    "type": "done",
                    "tool_block": {"name": final_b.name, "input": _coerce_to_dict(final_b.input)},
                    "stats": stats,
                }
                return

            if force_final and not final_blocks:
                raise AgentMaxIterationsError(
                    "Agent loop ended without a final answer tool even after forcing it."
                )

            # ── Execute retrieval tools, emit tool events ─────────────────────
            assistant_content = []
            for b in final_message.content:
                if b.type == "tool_use":
                    assistant_content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                elif b.type == "thinking":
                    assistant_content.append({
                        "type": "thinking",
                        "thinking": b.thinking,
                        "signature": b.signature,  # required by API when replaying thinking blocks
                    })
                else:
                    assistant_content.append({"type": "text", "text": getattr(b, "text", "")})
            messages.append({"role": "assistant", "content": assistant_content})

            # Emit all tool_call events upfront (before parallel execution)
            for block in tool_use_blocks:
                yield {
                    "type": "agent_tool_call",
                    "tool": block.name,
                    "input_summary": _tool_input_summary(block.name, block.input),
                }

            # Execute all retrieval tools in parallel via asyncio.gather
            async def _exec_tool_stream(block):
                return block, await execute_tool(
                    name=block.name,
                    tool_input=block.input,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    extra_context=extra_context,
                )

            tool_pairs = await asyncio.gather(*[_exec_tool_stream(b) for b in tool_use_blocks])

            tool_results = []
            duplicate_detected = False
            for block, result_text in tool_pairs:
                total_tool_calls += 1
                if block.name in retrieval_tool_names:
                    search_tools_called += 1
                result_tokens = _estimate_tokens(result_text)
                total_context_tokens += result_tokens

                yield {
                    "type": "agent_tool_result",
                    "tool": block.name,
                    "tokens": result_tokens,
                    "cumulative_tokens": total_context_tokens,
                }

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )

                # ── Duplicate query detection ──────────────────────────────────
                if block.name == "search_codebase" and not duplicate_detected:
                    raw_query = _coerce_to_dict(block.input).get("query", "")
                    if raw_query:
                        norm = _normalize_query(raw_query)
                        if norm in seen_search_queries:
                            tool_results.append({"type": "text", "text": _DUPLICATE_QUERY_MSG})
                            duplicate_detected = True
                            logger.info(
                                "agent_loop stream: duplicate query at iter=%d: %r",
                                iteration, raw_query[:60],
                            )
                        else:
                            seen_search_queries.add(norm)

            # ── Budget status line (BATS paper pattern) ───────────────────────
            if not force_final:
                tool_results.append({
                    "type": "text",
                    "text": _budget_line(iteration, config.max_iterations, total_context_tokens, config.cumulative_token_budget),
                })

            # ── Soft nudge: invite Claude to answer when context is rich enough ──
            if (
                not soft_nudge_added
                and not force_final
                and search_tools_called >= 1
                and (
                    (settings.token_budgeting_enabled and total_context_tokens > config.cumulative_token_budget * config.soft_answer_threshold)
                    or iteration >= config.max_iterations - 1
                )
            ):
                tool_results.append({"type": "text", "text": _SOFT_ANSWER_NUDGE})
                soft_nudge_added = True
                logger.info(
                    "agent_loop stream: soft nudge at iter=%d tokens=%d",
                    iteration,
                    total_context_tokens,
                )

            messages.append({"role": "user", "content": tool_results})

        if artifact_store is not None:
            await artifact_store.close()
        raise AgentMaxIterationsError("Agent loop exhausted all iterations without a final answer.")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_stats(
    iterations: int,
    tool_calls: int,
    context_tokens: int,
    t0: float,
    search_tools_called: int,
) -> dict:
    return {
        "iterations": iterations,
        "tool_calls": tool_calls,
        "context_tokens": context_tokens,
        "elapsed_ms": (time.monotonic() - t0) * 1000,
        "search_tools_called": search_tools_called,
    }
