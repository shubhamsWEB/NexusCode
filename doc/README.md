# NexusCode Documentation

Centralized, always-fresh Codebase Intelligence Server — indexes GitHub repos and exposes them via MCP + REST API.

---

## Documentation Index

| Page | What it covers |
|---|---|
| [Getting Started](./getting-started.md) | Installation, prerequisites, first run, running tests |
| [Connecting GitHub](./connecting-github.md) | PAT vs GitHub App, registering repos, webhook setup, live updates |
| [MCP Server & Auth](./mcp-access.md) | Getting tokens, connecting Claude Desktop/Code, all 8 MCP tools |
| [Search, Ask & Planning](./search-and-ask.md) | Search modes, Ask Mode Q&A, Planning Mode, quality scores, model selection |
| [Custom Skills](./custom-skills.md) | Creating skills, SKILL.md format, CUSTOM_SKILLS_DIRS, enterprise patterns |
| [API Reference](./api-reference.md) | Complete REST API — all endpoints, parameters, response schemas |
| [Configuration](./configuration.md) | Every environment variable with defaults and description |
| [Deployment](./deployment.md) | Docker Compose, Railway, bare-metal, Nginx, systemd, scaling |

---

## 60-Second Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure (minimum)
export DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/nexus
export VOYAGE_API_KEY=pa-...
export ANTHROPIC_API_KEY=sk-ant-...
export GITHUB_TOKEN=ghp_...
export GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)
export JWT_SECRET=$(openssl rand -hex 32)

# 3. Initialize DB
PYTHONPATH=. python scripts/init_db.py

# 4. Start (3 terminals)
PYTHONPATH=. uvicorn src.api.app:app --port 8000
PYTHONPATH=. rq worker indexing
PYTHONPATH=. streamlit run src/ui/dashboard.py --server.port 8501

# 5. Index a repo
curl -X POST http://localhost:8000/repos \
  -d '{"owner":"your-org","name":"your-repo","index_now":true}'

# 6. Ask a question
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"query":"How does authentication work?","stream":false}'
```

---

## Architecture Summary

```
GitHub push → POST /webhook → HMAC verify → RQ queue
RQ worker  → fetch files → parse AST → chunk → embed → PostgreSQL

Query path:
  POST /search  → embed query → pgvector ANN (HNSW) + tsvector → RRF → cross-encoder → context
  POST /ask     → retrieve context → LLM (mentor tone) → markdown answer + citations
  POST /plan    → retrieve context + web research → LLM (structured tool) → ImplementationPlan JSON
  MCP /mcp/sse  → 8 tools (search, symbol, callers, file, context, plan, ask, skills)
```

---

## Default Ports

| Service | Port |
|---|---|
| API server | `8000` |
| Admin dashboard | `8501` |
| MCP SSE endpoint | `8000/mcp` |
