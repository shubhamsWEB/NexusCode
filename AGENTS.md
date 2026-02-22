# NexusCode — Codebase Intelligence MCP Server

Centralized, always-fresh codebase knowledge service. Indexes GitHub repos via
push webhooks and exposes them through MCP tools and a REST API.

**Default endpoints:** API `http://localhost:8000` · Dashboard `http://localhost:8501`

---

## Capability Index

| Skill | When to use | Primary interface |
|---|---|---|
| `plan-implementation` | Before writing code for any bug/feature/refactor — get a grounded plan | `POST /plan` or MCP `plan_implementation` |
| `search-codebase` | Find relevant code, symbols, callers, or pre-assembled task context | `POST /search` or any of the 6 MCP tools |
| `manage-repos` | Register a new repo, trigger indexing, inspect stats, or delete a repo | `GET/POST /repos`, dashboard |

**Load the matching skill** (`skills/<name>/SKILL.md`) for full instructions.

---

## MCP Tools (6 total)

```
search_codebase(query, repo?, language?, top_k?, mode?)   — hybrid semantic+keyword search
get_symbol(name, repo?)                                    — fuzzy symbol lookup (Go to Definition)
find_callers(symbol, repo?, depth?)                        — who calls this function?
get_file_context(path, repo?, include_deps?)               — structural file map
get_agent_context(task, focal_files?, token_budget?, repo?) — pre-assembled context for a task
plan_implementation(query, repo?)                          — Cursor-style implementation plan
```

MCP endpoint: `http://localhost:8000/mcp` (SSE transport)

---

## REST API Quick Reference

```
GET  /health                      → index stats (repos, chunks, symbols, files)
GET  /repos                       → list repos with per-repo stats
POST /repos                       → register repo {owner, name, branch?, index_now?}
DELETE /repos/{owner}/{name}      → hard-delete all data for a repo
POST /repos/{owner}/{name}/index  → trigger full re-index job
GET  /config                      → masked env config viewer
POST /webhook                     → GitHub push webhook receiver
POST /plan                        → generate implementation plan {query, repo_owner?, repo_name?, stream?}
POST /search                      → search {query, repo?, language?, top_k?, mode?, rerank?, token_budget?}
GET  /jobs                        → recent RQ job history
```

Interactive docs: `http://localhost:8000/docs`

---

## Required Environment Variables

```bash
VOYAGE_API_KEY          # Voyage AI — embeddings (required for search)
ANTHROPIC_API_KEY       # Claude — planning mode (required for /plan)
GITHUB_TOKEN            # GitHub PAT or App — indexing
GITHUB_WEBHOOK_SECRET   # HMAC secret — webhook verification
DATABASE_URL            # postgresql+asyncpg://...
REDIS_URL               # redis://localhost:6379
JWT_SECRET              # MCP auth token signing
```

---

## Start All Services

```bash
# API server
PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
  uvicorn src.api.app:app --port 8000 --reload

# Indexing worker
PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
  rq worker indexing --url redis://localhost:6379

# Admin dashboard
PYTHONPATH=. API_URL=http://localhost:8000 \
  streamlit run src/ui/dashboard.py --server.port 8501
```

---

## Architecture (one-liner per layer)

```
GitHub push → webhook.py → HMAC verify → RQ queue
RQ worker  → fetcher.py → parser.py → chunker.py → enricher.py → embedder.py → DB
DB (PostgreSQL + pgvector + pg_trgm): chunks, symbols, merkle_nodes, repos, webhook_events
Retrieval: searcher.py (hybrid RRF) → reranker.py (cross-encoder) → assembler.py
MCP server: FastMCP → 6 tools → SSE transport at /mcp
Planning: retriever.py (5-phase) → claude_planner.py (tool_use) → ImplementationPlan JSON
```

---

## Skills

Full instructions for each capability live in `skills/`:

- `skills/plan-implementation/` — generate grounded implementation plans
- `skills/search-codebase/`     — search code, look up symbols, get context
- `skills/manage-repos/`        — register, index, and manage repositories
