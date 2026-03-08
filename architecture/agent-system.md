# Agent System

NexusCode's agent system is built around a single `AgentLoop` class that powers Planning Mode,
Ask Mode, and all workflow agent steps. Different behaviors come from different system prompts,
tool sets, and configurations — not different code paths.

---

## AgentLoop Design

```
┌─────────────────────────────────────────────────────────────┐
│                        AgentLoop                            │
│                                                             │
│  Input:                                                     │
│  ● model (str)                                              │
│  ● system prompt (str)                                      │
│  ● initial_message (str)                                    │
│  ● retrieval_tools (list[schema])                           │
│  ● final_answer_tools (list[schema])                        │
│  ● config (AgentLoopConfig)                                 │
│  ● repo_owner, repo_name (optional scope)                   │
│  ● extra_context (run_id, step_id for PDF storage)          │
│                                                             │
│  Loop (max_iterations + 2 safety headroom):                 │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  1. Check gates (force final answer?)                 │  │
│  │  2. Call LLM with tools + messages                    │  │
│  │  3. If final answer tool called → return result       │  │
│  │  4. If retrieval tools called → execute in parallel   │  │
│  │  5. Add tool results to messages → next iteration     │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  Output:                                                    │
│  ● final_tool_block (dict: name, input)                     │
│  ● stats (iterations, tool_calls, context_tokens, ms)       │
└─────────────────────────────────────────────────────────────┘
```

---

## Three Deterministic Gates

These gates are evaluated at the start of every iteration:

### Gate 1 — Iteration Limit
```python
if iteration >= config.max_iterations:
    # Force final answer this turn
    tools_this_turn = final_answer_tools_only
    tool_choice = {"type": "any"}   # Claude MUST call a tool
    inject_force_message()
```

### Gate 2 — Token Budget
```python
if cumulative_tool_result_tokens > config.cumulative_token_budget:
    # Force final answer regardless of iteration count
    force_final = True
```

**Why cumulative tokens, not context window?** Tool results accumulate fast. A search returning
6K tokens × 5 iterations = 30K tokens. Tracking cumulative result tokens gives a stable,
predictable budget that doesn't rely on estimating the full context window.

### Gate 3 — Grounding Check
```python
if config.require_search_before_answer and search_tools_called == 0:
    if claude_called_final_answer_without_searching:
        raise AgentGroundingError(
            "Claude answered without searching. Grounding violation."
        )
    else:
        # Nudge: inject user message asking Claude to search first
        inject_search_nudge()
```

This prevents Claude from hallucinating answers from training data when live code context
is available. The nudge gives Claude one chance to comply before the next gate triggers.

---

## Tool Execution

All retrieval tool calls in a single iteration are executed **in parallel** via `asyncio.gather`:

```python
async def _exec_tool_run(block):
    return block, await execute_tool(
        name=block.name,
        tool_input=block.input,
        repo_owner=repo_owner,
        repo_name=repo_name,
        extra_context=extra_context,   # passes run_id/step_id for PDF storage
    )

tool_pairs = await asyncio.gather(*[_exec_tool_run(b) for b in tool_use_blocks])
```

**Tool routing in `execute_tool()`:**
```
name == "search_codebase"     → _search_codebase()
name == "get_symbol"          → _get_symbol()
name == "find_callers"        → _find_callers()
name == "get_file_context"    → _get_file_context()
name == "get_agent_context"   → _get_agent_context()
name == "plan_implementation" → _plan_implementation()
name == "ask_codebase"        → _ask_codebase()
name == "generate_pdf"        → _generate_pdf()
else (unknown name)           → mcp_bridge.call_external_tool()
```

---

## Prompt Caching

For providers that support it (Anthropic), the last tool schema in the list gets
`cache_control: {type: "ephemeral"}`. This marks the system prompt + all tool schemas as a
cacheable prefix, saving ~10% of input token cost on turns 2+ of a multi-turn agent loop.

```python
def _add_cache_control_to_last(tools: list[dict]) -> list[dict]:
    result = list(tools)
    result[-1] = {**result[-1], "cache_control": {"type": "ephemeral"}}
    return result
```

---

## Prior Result Truncation

To prevent O(n²) token growth across iterations, earlier tool results are truncated:

```python
_MAX_PRIOR_RESULT_CHARS = 2_400  # ~600 tokens

def _truncate_prior_tool_results(messages):
    # Find all user messages that contain tool_results
    tr_indices = [...]
    # Leave the MOST RECENT batch untouched
    for idx in tr_indices[:-1]:
        # Truncate older results to 2400 chars each
        for result in messages[idx]["content"]:
            if len(result["content"]) > _MAX_PRIOR_RESULT_CHARS:
                result["content"] = result["content"][:2400] + "\n[...truncated]"
```

This ensures the agent always has full context of what it just retrieved, while older
context is summarized to stay within budget.

---

## Extended Thinking

Planning Mode can use Anthropic's extended thinking:

```python
# Only for Anthropic models, only when not forcing final answer
if config.thinking_budget > 0 and not force_final:
    params["thinking"] = {"type": "enabled", "budget_tokens": config.thinking_budget}
    # Use streaming internally (non-streaming API rejects large max_tokens)
    async with client.messages.stream(**params) as stream:
        response = await stream.get_final_message()
```

Extended thinking surfaces Claude's reasoning chain and is disabled during the forced-final-answer
turn (incompatible with `tool_choice: {type: any}`).

---

## Agent Roles

Each role wraps the same `AgentLoop` with a different configuration:

```python
ROLES = {
    "searcher": {
        "system_prompt": "You are a specialized Codebase Searcher...",
        "default_tools": ["search_codebase", "get_symbol", "find_callers", "get_file_context"],
        "require_search": True,
        "max_iterations": 5,
        "token_budget": 80_000,
    },
    "planner": {
        "system_prompt": "You are a specialized Implementation Planner...",
        "default_tools": ["plan_implementation", "search_codebase", "get_symbol", "get_file_context"],
        "require_search": True,
    },
    "reviewer": {
        "system_prompt": "You are a specialized Code Reviewer...",
        "default_tools": ["search_codebase", "get_symbol", "find_callers", "get_file_context"],
        "require_search": True,
    },
    "coder": {
        "system_prompt": "You are a specialized Code Generator...",
        "default_tools": ["search_codebase", "get_agent_context", "get_symbol", "get_file_context"],
        "require_search": True,
    },
    "tester": {
        "system_prompt": "You are a specialized Test Generation agent...",
        "default_tools": ["search_codebase", "find_callers", "get_symbol", "get_file_context"],
        "require_search": True,
    },
    "supervisor": {
        "system_prompt": "You are a Supervisor agent...",
        "default_tools": ["search_codebase", "get_symbol", "ask_codebase", "generate_pdf"],
        "require_search": False,   # supervisor synthesizes, doesn't always search
    },
}
```

### DB Overrides

Role configs can be overridden at runtime via the Agent Roles UI or API:

```sql
-- agent_role_overrides table
SELECT system_prompt, instructions, default_tools, require_search,
       max_iterations, token_budget
FROM agent_role_overrides
WHERE name = :role_name AND is_active = TRUE;
```

If no override exists, falls back to the hardcoded `_ROLES` dict.

The `instructions` field is appended to the system prompt under `## Additional Instructions`
without replacing it, allowing incremental customization.

---

## All 8 Internal Tools

| Tool | Schema set | Purpose |
|------|-----------|---------|
| `search_codebase` | RETRIEVAL (core 4) | Hybrid semantic+keyword search |
| `get_symbol` | RETRIEVAL (core 4) | Fuzzy symbol lookup |
| `find_callers` | RETRIEVAL (core 4) | BFS call graph traversal |
| `get_file_context` | RETRIEVAL (core 4) | Structural file map |
| `get_agent_context` | EXTENDED | Pre-assembled context for a task |
| `plan_implementation` | EXTENDED | Generate implementation plan |
| `ask_codebase` | EXTENDED | Answer natural-language question |
| `generate_pdf` | STANDALONE | Convert markdown → PDF + store |

---

## External Tool Integration (MCP Bridge)

The MCP Bridge (`src/agent/mcp_bridge.py`) extends the agent with tools from external MCP servers:

```
Startup:
  init_bridge()
    → Load all enabled servers from generated_documents table
    → For each server: open SSE connection, list_tools()
    → Cache tool schemas in _tool_registry (name → schema)

At tool execution time:
  execute_tool("some_external_tool", ...)
    → is_external_tool("some_external_tool") == True
    → call_external_tool("some_external_tool", params)
    → Route to the correct server's SSE connection
    → Return JSON result

In AgentLoop:
  all_available = ALL_INTERNAL_TOOL_SCHEMAS + get_external_tool_schemas()
  # Filtered by role's default_tools allowlist
```

**Collision policy:** If an external tool has the same name as an internal tool, the internal
tool wins. External tools are always optional extras.

---

## Streaming Mode

Both `run()` and `stream()` support the same agent loop. The streaming version yields SSE events:

```
{"type": "agent_tool_call",   "tool": "search_codebase", "input_summary": "JWT auth flow"}
{"type": "agent_tool_result", "tool": "search_codebase", "tokens": 1840, "cumulative_tokens": 1840}
{"type": "thinking",          "text": "...Claude's extended thinking..."}
{"type": "token",             "text": "partial answer text..."}  ← final answer only
{"type": "done",              "tool_block": {...}, "stats": {...}}
```

The streaming implementation uses `client.messages.stream()` for normal turns and
`client.messages.create()` for the forced-final-answer turn (avoids thinking/tool_choice
compatibility issues).
