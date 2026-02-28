# NexusCode — Codebase Intelligence MCP Server

> Claude Code workspace context. See AGENTS.md for the full capability index.

## Project Identity

A centralized, always-on **Codebase Intelligence MCP Server** written in Python.
Not an IDE tool — a team-wide service that indexes GitHub repos and exposes them
via the Model Context Protocol (MCP) + REST API.

**Default:** API `http://localhost:8000` · Dashboard `http://localhost:8501`

---

## Before You Start Any Task

1. **Read AGENTS.md** — capability index + API quick reference
2. **Use `plan_implementation`** via MCP (or `POST /plan`) for any non-trivial change
3. Run `PYTHONPATH=. pytest tests/ -v` to verify nothing is broken

---

## Key Source Files

```
src/config.py                       — Pydantic settings (single source of truth)
src/api/app.py                      — FastAPI app + router mounts + GET /models
src/api/plan.py                     — POST /plan (planning mode)
src/api/ask.py                      — POST /ask (Ask Mode)
src/api/repos.py                    — repo management endpoints
src/planning/schemas.py             — ImplementationPlan Pydantic models
src/planning/retriever.py           — 7-phase retrieval pipeline
src/planning/claude_planner.py      — LLM caller via provider abstraction
src/planning/web_researcher.py      — Stack-aware web research (Anthropic-only)
src/ask/ask_agent.py                — Ask Mode LLM agent (mentor tone + citations)
src/llm/__init__.py                 — get_provider, list_available_models
src/llm/types.py                    — LLMToolSchema, LLMStreamEvent, LLMResponse
src/llm/base.py                     — LLMProvider Protocol
src/llm/anthropic_provider.py       — Claude provider (retry, semaphore, thinking)
src/llm/openai_provider.py          — OpenAI provider (gpt-4o, o3, etc.)
src/llm/grok_provider.py            — xAI Grok provider (extends OpenAI)
src/llm/registry.py                 — Model→provider mapping + factory
src/llm/tool_converter.py           — Schema conversion (Anthropic↔OpenAI↔unified)
src/pipeline/pipeline.py            — indexing orchestrator
src/retrieval/searcher.py           — hybrid search (RRF)
src/retrieval/reranker.py           — cross-encoder rerank
src/retrieval/assembler.py          — token-budget context formatter
src/mcp/server.py                   — 7 MCP tools via FastMCP
src/storage/db.py                   — async SQLAlchemy queries
src/ui/dashboard.py                 — Streamlit admin dashboard router
src/ui/_pages/planning.py           — Planning Mode page (with model selector)
src/ui/_pages/ask.py                — Ask Mode chat UI page (with model selector)
```

---

## Development Rules

- `PYTHONPATH=.` is required for all local runs — never omit it
- `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` required for RQ on macOS
- Dashboard pages go in `src/ui/_pages/` (underscore prefix prevents Streamlit auto-discovery)
- All new API endpoints → `src/api/` as a separate router, mounted in `app.py`
- All new MCP tools → `src/mcp/server.py` with `@mcp_server.tool()` decorator
- Settings → add to `src/config.py` `Settings` class first, read via `settings.*`

---

## Tech Stack

Python 3.11 · FastAPI · PostgreSQL + pgvector · Redis + RQ · voyage-code-2 ·
cross-encoder/ms-marco-MiniLM-L-6-v2 · Claude (Anthropic SDK) · OpenAI SDK ·
xAI/Grok (OpenAI-compatible) · FastMCP · Streamlit

---

## Skills

Use these skill packages when performing the corresponding tasks:

- `skills/plan-implementation/` — generate an implementation plan before coding
- `skills/search-codebase/`     — search code and symbols in the indexed repos
- `skills/manage-repos/`        — register repos, trigger indexing, check stats
- `skills/ask-codebase/`        — answer natural-language questions about the codebase
