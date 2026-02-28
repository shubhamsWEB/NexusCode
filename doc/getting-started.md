# Getting Started with NexusCode

NexusCode is a self-hosted Codebase Intelligence server. It indexes your GitHub repositories, keeps them fresh via push webhooks, and exposes the knowledge through a REST API and MCP (Model Context Protocol) server that AI coding assistants can connect to.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | 3.12 or 3.13 recommended |
| PostgreSQL 15+ with pgvector | `CREATE EXTENSION vector;` required |
| Redis 6+ | Job queue for background indexing |
| Voyage AI API key | Embeddings — [voyageai.com](https://www.voyageai.com) |
| At least one LLM key | Anthropic, OpenAI, or xAI (for Planning + Ask modes) |
| GitHub PAT or GitHub App | For fetching repository files |

---

## 1. Clone and Install

```bash
git clone https://github.com/your-org/nexuscode-server
cd nexuscode-server

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

---

## 2. Configure Environment

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

Minimum required variables:

```bash
# .env

# ── Database ──────────────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/codebase_intel

# ── Redis ─────────────────────────────────────────────────────
REDIS_URL=redis://localhost:6379

# ── Embeddings (required) ─────────────────────────────────────
VOYAGE_API_KEY=pa-...

# ── LLM — at least one required for /plan and /ask ───────────
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# GROK_API_KEY=xai-...

# ── GitHub ────────────────────────────────────────────────────
GITHUB_TOKEN=ghp_...           # Personal Access Token
GITHUB_WEBHOOK_SECRET=some-random-secret-string

# ── Auth ──────────────────────────────────────────────────────
JWT_SECRET=another-random-secret-string-min-32-chars
```

See [configuration.md](./configuration.md) for the full list of settings.

---

## 3. Initialize the Database

```bash
# Create extensions and tables
PYTHONPATH=. python scripts/init_db.py

# Optional: run all migrations (idempotent)
psql $DATABASE_URL -f src/storage/migrations/001_init.sql
psql $DATABASE_URL -f src/storage/migrations/003_chat_history.sql
psql $DATABASE_URL -f src/storage/migrations/004_indices.sql
psql $DATABASE_URL -f src/storage/migrations/005_parent_chunks.sql
psql $DATABASE_URL -f src/storage/migrations/006_enriched_fts.sql
psql $DATABASE_URL -f src/storage/migrations/007_hnsw.sql
```

---

## 4. Start All Services

Open three terminal windows:

**Terminal 1 — API server**
```bash
PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
  uvicorn src.api.app:app --port 8000 --reload
```

**Terminal 2 — Indexing worker**
```bash
PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
  rq worker indexing --url redis://localhost:6379
```

**Terminal 3 — Dashboard (optional)**
```bash
PYTHONPATH=. API_URL=http://localhost:8000 \
  streamlit run src/ui/dashboard.py --server.port 8501
```

> **macOS note:** `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` is required for the RQ worker due to macOS fork safety restrictions.

---

## 5. Verify It's Running

```bash
# Health check
curl http://localhost:8000/health
# → {"status":"ok","repos":0,"chunks":0,...}

# Available LLM models
curl http://localhost:8000/models
# → {"available":["claude-sonnet-4-6","gpt-4o",...]}

# Interactive API docs
open http://localhost:8000/docs

# Admin dashboard
open http://localhost:8501
```

---

## 6. Index Your First Repository

```bash
curl -X POST http://localhost:8000/repos \
  -H "Content-Type: application/json" \
  -d '{"owner": "your-org", "name": "your-repo", "index_now": true}'
```

Monitor progress in the dashboard or watch the worker terminal. Indexing a 500-file repository takes about 30–60 seconds.

Once indexed, try a search:

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "authentication", "top_k": 5}'
```

---

## 7. Run Tests

```bash
PYTHONPATH=. pytest tests/ -v
# 117 tests, no network or database required
```

---

## Next Steps

- [Connect GitHub repos and configure webhooks](./connecting-github.md)
- [Access the MCP server from Claude Desktop](./mcp-access.md)
- [Use the Planning and Ask modes](./search-and-ask.md)
- [Add custom skills for your team](./custom-skills.md)
- [Deploy to production](./deployment.md)
