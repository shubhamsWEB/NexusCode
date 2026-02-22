# 🧠 Codebase Intelligence — MCP Knowledge Server

A centralized, always-fresh knowledge service that indexes your GitHub repositories and exposes the entire codebase to any AI agent via Anthropic's **Model Context Protocol (MCP)**.

> Push a commit → GitHub fires a webhook → files re-indexed in <5 seconds → every AI agent you have sees the change instantly.

---

## What It Does

| Without this | With this |
|---|---|
| Each agent reads files ad-hoc | One server indexes everything once |
| Stale context from hours-old snapshots | Merkle-aware: re-indexes only changed files |
| No symbol awareness | Tree-sitter AST: functions, classes, methods |
| Raw grep for search | Hybrid semantic + keyword + cross-encoder rerank |
| Each agent needs GitHub access | One server, all agents use MCP |

---

## Architecture

```
GitHub Repository
       │
       │  webhook (push event)
       ▼
┌─────────────────┐     Redis Queue     ┌──────────────────────┐
│  FastAPI app    │ ──────────────────► │   RQ Worker          │
│  POST /webhook  │                     │   pipeline.py        │
│  POST /search   │                     │   ├─ fetch (GitHub)  │
│  GET  /health   │                     │   ├─ merkle check    │
│  /mcp  (SSE)    │                     │   ├─ Tree-sitter AST │
└─────────────────┘                     │   ├─ chunk + enrich  │
       │                                │   ├─ voyage-code-2   │
       │                                │   └─ store in PG     │
       ▼                                └──────────────────────┘
┌─────────────────────────────────────────┐
│  PostgreSQL + pgvector                  │
│  chunks (vector(1536))                  │
│  symbols  │  merkle_nodes  │  repos     │
└─────────────────────────────────────────┘
       │
       │  MCP tools
       ▼
  AI Agents (Claude Desktop, LangGraph, CI bots…)
```

---

## Quick Start

### Prerequisites
- Docker Desktop running
- Python 3.11+
- Voyage AI account ([voyageai.com](https://www.voyageai.com)) — `voyage-code-2` model
- GitHub PAT with `repo:read` scope

### 1. Clone & configure

```bash
git clone <this-repo>
cd nexusCode_server
cp .env.example .env
# Edit .env — fill in GITHUB_TOKEN, VOYAGE_API_KEY, JWT_SECRET
```

### 2. Start infrastructure

```bash
docker compose up postgres redis -d
# Wait for healthy status:
docker compose ps
```

### 3. Start the API server + worker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1 — API server
uvicorn src.api.app:app --port 8000 --reload

# Terminal 2 — indexing worker (macOS needs this env var)
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker indexing --url redis://localhost:6379
```

### 4. Index your first repository

```bash
# Edit scripts/full_index.py — set REPO_OWNER and REPO_NAME
python scripts/full_index.py

# Watch progress
curl http://localhost:8000/health
```

### 5. Start the admin dashboard

```bash
streamlit run src/ui/dashboard.py
# Open http://localhost:8501
```

---

## Connecting AI Agents

### Claude Desktop (stdio MCP)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "codebase": {
      "command": "curl",
      "args": ["-N", "-H", "Accept: text/event-stream", "http://localhost:8000/mcp/sse"]
    }
  }
}
```

Or use the MCP Python SDK directly:

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client("http://localhost:8000/mcp/sse") as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(
            "search_codebase",
            {"query": "what handles authentication?"}
        )
        print(result.content[0].text)
```

### Get an API token

```bash
curl -s -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"sub": "my-agent", "repos": ["owner/repo"]}' \
  | jq .access_token
```

---

## MCP Tools Reference

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `search_codebase` | Hybrid semantic+keyword search | `query`, `mode` (hybrid/semantic/keyword), `top_k`, `repo` |
| `get_symbol` | Fuzzy symbol lookup (like "Go to Definition") | `name`, `repo` |
| `find_callers` | Who calls this function? | `symbol`, `depth`, `repo` |
| `get_file_context` | Full structural map of a file | `path`, `repo`, `include_deps` |
| `get_agent_context` | Pre-assembled task context (start here!) | `task`, `focal_files`, `token_budget`, `repo` |

---

## Showcase Demo Queries

Try these in the Query Tester dashboard or via `/search`:

```
1. "what handles Shopify authentication and session management?"
2. "where is the GraphQL product mutation defined?"
3. "webhook handler for uninstalled app event"
4. "chat widget toggle button and UI rendering"
5. "how does the app configure Shopify API version and scopes?"
6. "Prisma database session storage setup"
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | ✓ | PAT with `repo:read` (or use GitHub App) |
| `GITHUB_WEBHOOK_SECRET` | ✓ | HMAC secret — must match GitHub webhook config |
| `DATABASE_URL` | ✓ | `postgresql+asyncpg://user:pass@host:5432/db` |
| `REDIS_URL` | ✓ | `redis://host:6379` |
| `VOYAGE_API_KEY` | ✓ | Voyage AI key for `voyage-code-2` embeddings |
| `JWT_SECRET` | ✓ | Random secret for MCP auth tokens (32+ chars) |
| `EMBEDDING_DIMENSIONS` | — | Default `1536` (voyage-code-2) |
| `GITHUB_APP_ID` | — | GitHub App ID (alternative to PAT) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | — | Path to App `.pem` file |
| `ANTHROPIC_API_KEY` | — | For future Claude-powered tools |

---

## Deployment (Railway)

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login and deploy
railway login
railway init
railway up

# Set environment variables
railway variables set GITHUB_TOKEN=... VOYAGE_API_KEY=... JWT_SECRET=...

# Add PostgreSQL and Redis plugins in Railway dashboard
# Set DATABASE_URL and REDIS_URL from the plugin connection strings
```

The `railway.toml` is pre-configured for the web service. Deploy the worker as a separate Railway service using:
```
rq worker indexing --url $REDIS_URL
```

---

## Docker Compose (Self-Hosted)

```bash
# Full stack (API + worker + dashboard + postgres + redis)
docker compose up -d

# Check all services are healthy
docker compose ps

# Index a repository
docker compose exec app python scripts/full_index.py

# View logs
docker compose logs -f worker
docker compose logs -f app
```

---

## Adding a New Repository

```bash
# 1. Register the repo
curl -X POST http://localhost:8000/repos \
  -H "Content-Type: application/json" \
  -d '{"owner": "myorg", "name": "backend-api", "branch": "main"}'

# 2. Initial full index
REPO_OWNER=myorg REPO_NAME=backend-api python scripts/full_index.py

# 3. Set up GitHub webhook
#    URL: https://your-deployment.railway.app/webhook
#    Content-Type: application/json
#    Secret: your GITHUB_WEBHOOK_SECRET
#    Events: Just the push event
```

---

## Running Tests

```bash
# All tests (53 total, no network/DB required)
pytest tests/ -v

# Specific test modules
pytest tests/test_chunker.py tests/test_enricher.py -v
pytest tests/test_pipeline.py -v
pytest tests/test_webhook.py -v
```

---

## Performance Targets

| Metric | Target | Actual |
|--------|--------|--------|
| Initial index (19 files) | < 2 min | 13s ✓ |
| Incremental update (1 file) | < 30s | **2–4s** ✓ |
| Merkle skip (unchanged files) | > 99% | 100% ✓ |
| Search latency p95 | < 500ms | ~100ms (warm) ✓ |
| Chunk size (80th pct) | 200–512 tokens | ✓ |

---

## Project Structure

```
nexusCode_server/
├── src/
│   ├── config.py              # Pydantic settings
│   ├── api/app.py             # FastAPI: webhook, search, health, MCP mount
│   ├── github/
│   │   ├── webhook.py         # HMAC-verified webhook receiver
│   │   ├── fetcher.py         # GitHub REST API client
│   │   └── events.py          # PushEvent dataclasses
│   ├── pipeline/
│   │   ├── parser.py          # Tree-sitter AST (7 languages)
│   │   ├── chunker.py         # Recursive split-then-merge (512 tok target)
│   │   ├── enricher.py        # Scope chain + import injection
│   │   ├── embedder.py        # voyage-code-2 batched embedding
│   │   └── pipeline.py        # RQ worker orchestrator
│   ├── retrieval/
│   │   ├── searcher.py        # Hybrid vector+keyword + RRF merge
│   │   ├── reranker.py        # Cross-encoder (ms-marco-MiniLM-L-6-v2)
│   │   └── assembler.py       # Token-budget context assembly
│   ├── mcp/
│   │   ├── server.py          # 5 MCP tools (FastMCP)
│   │   └── auth.py            # JWT bearer token auth
│   ├── storage/
│   │   ├── db.py              # Async SQLAlchemy queries
│   │   ├── models.py          # ORM: Chunk, Symbol, MerkleNode, Repo
│   │   └── migrations/001_init.sql
│   └── ui/dashboard.py        # Streamlit admin dashboard
├── scripts/
│   ├── full_index.py          # Initial full repo index
│   ├── simulate_webhook.py    # Local end-to-end testing
│   ├── deploy_check.py        # Pre-deploy env verification
│   └── test_query.py          # CLI search tool
├── tests/                     # 53 unit tests
├── docker-compose.yml
├── Dockerfile
├── railway.toml
├── Procfile
└── requirements.txt
```
