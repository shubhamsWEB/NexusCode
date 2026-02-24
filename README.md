# 🧠 Codebase Intelligence — MCP Knowledge Server

[![CI](https://github.com/shubhamsWEB/nexusCode_server/actions/workflows/ci.yml/badge.svg)](https://github.com/shubhamsWEB/nexusCode_server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

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

## Running Locally — Complete Step-by-Step Guide

This guide assumes **no prior Python knowledge**. Follow each step in order. The whole setup takes about 15–20 minutes.

You will need **four terminal windows** open by the end. On macOS open Terminal (or iTerm2). On Windows use PowerShell or Windows Terminal.

---

### Part 1 — Install the Required Software

#### 1.1 Install Git

Git lets you download the project code.

- **macOS:** Open Terminal and run `git --version`. If it's not installed, macOS will prompt you to install Xcode Command Line Tools — click Install.
- **Windows:** Download and install from [git-scm.com](https://git-scm.com/download/win). Use all default options.
- **Linux (Ubuntu/Debian):** `sudo apt install git`

Verify it works:
```bash
git --version
# Expected output: git version 2.x.x
```

---

#### 1.2 Install Docker Desktop

Docker runs the PostgreSQL database and Redis in isolated containers — no manual database installation needed.

1. Download Docker Desktop from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
2. Install it and **start the Docker Desktop app** (the whale icon must appear in your menu bar / system tray)

Verify it works:
```bash
docker --version
# Expected output: Docker version 26.x.x
```

> ⚠️ **Docker Desktop must be running** every time you work with this project. If you see "Cannot connect to Docker daemon", open Docker Desktop first.

---

#### 1.3 Install Python 3.11 or newer

Python is the programming language this project is written in.

- **macOS:** Download the installer from [python.org/downloads](https://www.python.org/downloads/). Choose the latest Python 3.11 or 3.12 release. Run the `.pkg` installer.
- **Windows:** Download from [python.org/downloads](https://www.python.org/downloads/). **Important:** On the first installer screen, tick the box that says **"Add Python to PATH"** before clicking Install.
- **Linux (Ubuntu/Debian):** `sudo apt install python3.11 python3.11-venv python3-pip`

Verify it works:
```bash
python3 --version
# Expected output: Python 3.11.x  (or 3.12.x)
```

> On Windows the command may be `python` instead of `python3`.

---

### Part 2 — Get Your API Keys

NexusCode needs three external services. Set these up before configuring the project.

#### 2.1 GitHub Personal Access Token (required)

This lets NexusCode read file contents from your GitHub repositories.

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Click **"Generate new token" → "Generate new token (classic)"**
3. Give it a name like `nexuscode-local`
4. Set expiration to **90 days** (or "No expiration" for local dev)
5. Under **Scopes**, tick **`repo`** (the top-level checkbox — this includes read access)
6. Scroll down and click **"Generate token"**
7. **Copy the token now** — it starts with `ghp_`. You won't see it again.

---

#### 2.2 Voyage AI API Key (required — for code embeddings)

This powers the AI semantic search ("find code by meaning").

1. Go to [voyageai.com](https://www.voyageai.com) and create a free account
2. In your dashboard, click **"API Keys"** → **"Create new key"**
3. Copy the key — it starts with `pa-`

> The free tier includes enough credits to index several repos and run hundreds of searches.

---

#### 2.3 Anthropic API Key (required for Planning Mode)

This powers the implementation plan generator.

1. Go to [console.anthropic.com](https://console.anthropic.com) and create an account
2. Click **"API Keys"** → **"Create Key"**
3. Copy the key — it starts with `sk-ant-`

---

#### 2.4 Make Up Two Secrets

These are random strings you create yourself — they are passwords used internally.

**JWT Secret** — used to sign authentication tokens. Pick any long random string:
```
my-super-secret-jwt-key-change-this-in-production-123
```

**Webhook Secret** — used to verify GitHub sends the webhooks (not someone else). Pick any string:
```
my-webhook-secret-abc123
```

Write these down — you'll paste them into the config file in the next step.

---

### Part 3 — Download and Configure the Project

#### 3.1 Clone the repository

Open a terminal and run:
```bash
git clone https://github.com/shubhamsWEB/nexusCode_server.git
cd nexusCode_server
```

All following commands in this guide must be run from inside this `nexusCode_server` folder.

---

#### 3.2 Create your configuration file

Copy the example config:
```bash
# macOS / Linux
cp .env.example .env

# Windows (PowerShell)
Copy-Item .env.example .env
```

Now open `.env` in any text editor (TextEdit on Mac, Notepad on Windows, or VS Code). Fill in each value using the keys you collected above:

```bash
# ── Required ──────────────────────────────────────────────────────────────────

# Your GitHub Personal Access Token (from step 2.1)
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# The webhook secret you made up (from step 2.4)
GITHUB_WEBHOOK_SECRET=my-webhook-secret-abc123

# Leave these exactly as-is — they match the Docker setup below
DATABASE_URL=postgresql+asyncpg://codebase:secret@localhost:5432/codebase_intel
REDIS_URL=redis://localhost:6379

# Your Voyage AI key (from step 2.2)
VOYAGE_API_KEY=pa-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Leave as-is
EMBEDDING_MODEL=voyage-code-2
EMBEDDING_DIMENSIONS=1024

# Your Anthropic key (from step 2.3)
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# The JWT secret you made up (from step 2.4)
JWT_SECRET=my-super-secret-jwt-key-change-this-in-production-123

# ── Optional (leave blank if you don't have them) ──────────────────────────────
GITHUB_OAUTH_CLIENT_ID=
GITHUB_OAUTH_CLIENT_SECRET=
```

Save the file. The `.env` file is **never committed to Git** — your secrets stay on your machine.

---

### Part 4 — Start the Databases

NexusCode uses two databases: **PostgreSQL** (stores code chunks and search indexes) and **Redis** (job queue for background indexing). Docker runs both with a single command.

Open **Terminal 1** and run:
```bash
docker compose up postgres redis -d
```

The `-d` flag means "run in the background". The first time this runs it will download the database images — takes 1–2 minutes.

Verify both databases are healthy:
```bash
docker compose ps
```

You should see both `postgres` and `redis` with status **`healthy`**:
```
NAME                    STATUS
nexuscode-postgres-1    Up (healthy)
nexuscode-redis-1       Up (healthy)
```

> If you see `starting` instead of `healthy`, wait 10 seconds and run `docker compose ps` again.

---

### Part 5 — Set Up Python

#### 5.1 Create a virtual environment

A virtual environment is an isolated Python installation just for this project. It keeps this project's dependencies separate from anything else on your system.

```bash
# macOS / Linux
python3 -m venv .venv

# Windows
python -m venv .venv
```

---

#### 5.2 Activate the virtual environment

You must activate the virtual environment **every time you open a new terminal window**.

```bash
# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Windows (Command Prompt)
.venv\Scripts\activate.bat
```

When activated, your terminal prompt will show `(.venv)` at the start. This tells you the virtual environment is active.

---

#### 5.3 Install dependencies

This installs all the Python packages the project needs (FastAPI, SQLAlchemy, Voyage AI client, etc.):

```bash
pip install -r requirements.txt
```

This takes 3–5 minutes the first time. You'll see packages installing line by line. Wait until you see `Successfully installed ...` at the end.

---

### Part 6 — Initialize the Database Schema

This creates all the database tables that NexusCode needs. Run it once during initial setup.

```bash
PYTHONPATH=. python scripts/init_db.py
```

> **What is `PYTHONPATH=.`?** It tells Python to look for code in the current folder (`nexusCode_server/`). NexusCode's modules reference each other as `from src.xxx import ...` and Python needs to know where `src/` lives. This prefix is required for all scripts in this project.

> **Windows:** PowerShell doesn't support the `VAR=value command` syntax. Run this instead:
> ```powershell
> $env:PYTHONPATH="."; python scripts/init_db.py
> ```

You should see output like:
```
INFO  Connected to PostgreSQL
INFO  Created tables: chunks, symbols, merkle_nodes, repos, webhook_events
INFO  Database initialized successfully
```

---

### Part 7 — Start the API Server

**Keep Terminal 1 free for Docker.** Open **Terminal 2**, navigate to the project folder, and activate the virtual environment first:

```bash
cd nexusCode_server
source .venv/bin/activate   # (or the Windows equivalent)
```

Start the API server:
```bash
# macOS / Linux
PYTHONPATH=. uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

# Windows (PowerShell)
$env:PYTHONPATH="."; uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload
```

You should see:
```
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

**Leave this terminal running.** The API server must stay running for everything else to work.

Verify it's working — open a new terminal tab and run:
```bash
curl http://localhost:8000/health
```

Or just open [http://localhost:8000/health](http://localhost:8000/health) in your browser. You should see JSON with `"status": "ok"`.

---

### Part 8 — Start the Background Worker

The worker is a separate process that picks up indexing jobs from the queue and processes them (fetching files from GitHub, parsing code, creating embeddings, storing in the database).

Open **Terminal 3**, navigate to the project folder, and activate the virtual environment:
```bash
cd nexusCode_server
source .venv/bin/activate   # (or Windows equivalent)
```

Start the worker:
```bash
# macOS
PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker indexing --url redis://localhost:6379

# Linux
PYTHONPATH=. rq worker indexing --url redis://localhost:6379

# Windows (PowerShell)
$env:PYTHONPATH="."; rq worker indexing --url redis://localhost:6379
```

> **What is `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`?** This is a macOS-only setting. macOS has a restriction on "forking" processes that causes the worker to crash without it. Linux and Windows don't need it.

You should see:
```
Worker rq:worker:xxx started, version 2.x.x
Subscribing to channel rq:pubsub:xxx
*** Listening on indexing...
```

**Leave this terminal running.** Without the worker, repos can be registered but files will never be indexed.

---

### Part 9 — Index Your First Repository

Open **Terminal 4**, navigate to the project folder, and activate the virtual environment:
```bash
cd nexusCode_server
source .venv/bin/activate   # (or Windows equivalent)
```

Run the indexing script, replacing `OWNER` and `REPO` with a real GitHub repository:
```bash
PYTHONPATH=. python scripts/full_index.py OWNER REPO --branch main
```

**Examples:**
```bash
# Index a public repo (no special permissions needed)
PYTHONPATH=. python scripts/full_index.py octocat Hello-World --branch master

# Index your own private repo
PYTHONPATH=. python scripts/full_index.py myorg my-backend-api --branch main
```

You will see output like:
```
Starting full index: myorg/my-backend-api@main

  Total files in repo : 143
  Indexable files     : 87
  Skipped             : 56

  HEAD commit: a3f91bc  (feat: add payment retry logic)
  Enqueueing 87 indexing jobs...

  Job ID : 550e8400-e29b-41d4-a716-446655440000
  Queue  : indexing (87 jobs pending)
```

Now watch **Terminal 3** (the worker). You'll see it processing each file:
```
INFO  Processing: src/payments/service.py
INFO  Chunks upserted: 4
INFO  Processing: src/payments/models.py
...
```

Indexing speed: roughly **5–15 files per second** depending on file size and API response times. A typical 100-file repo takes 30–90 seconds.

Check when it's done:
```bash
curl http://localhost:8000/health
```

When `chunks` is greater than 0, indexing is complete.

---

### Part 10 — Start the Admin Dashboard

The dashboard gives you a visual interface to manage repos, run searches, and monitor the system.

In Terminal 4 (or open a new one):
```bash
# macOS / Linux
PYTHONPATH=. API_URL=http://localhost:8000 streamlit run src/ui/dashboard.py --server.port 8501

# Windows (PowerShell)
$env:PYTHONPATH="."; $env:API_URL="http://localhost:8000"; streamlit run src/ui/dashboard.py --server.port 8501
```

Streamlit will print:
```
  You can now view your Streamlit app in your browser.
  Local URL: http://localhost:8501
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

---

### Part 11 — Verify Everything Works

Run the built-in deployment check:
```bash
PYTHONPATH=. python scripts/deploy_check.py
```

Expected output:
```
=======================================================
  Codebase Intelligence — Deployment Check
=======================================================

Required environment variables:
  ✓ GITHUB_TOKEN
  ✓ GITHUB_WEBHOOK_SECRET
  ✓ DATABASE_URL
  ✓ REDIS_URL
  ✓ VOYAGE_API_KEY
  ✓ JWT_SECRET

Database connectivity:
  ✓ PostgreSQL    connected — X active chunks

Redis connectivity:
  ✓ Redis         connected — v7.x.x

=======================================================
✓ All checks passed — system is ready!
=======================================================
```

Try your first search:
```bash
curl -s -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "authentication", "top_k": 3}' | python3 -m json.tool
```

---

### Part 12 — What to Do Next Session

When you come back to work on the project, you need to restart the services. Docker databases persist their data automatically.

```bash
# 1. Start Docker databases (if Docker Desktop is running, this is instant)
docker compose up postgres redis -d

# 2. Terminal 2 — API server
cd nexusCode_server && source .venv/bin/activate
PYTHONPATH=. uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

# 3. Terminal 3 — Worker
cd nexusCode_server && source .venv/bin/activate
PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker indexing --url redis://localhost:6379

# 4. Dashboard (optional)
cd nexusCode_server && source .venv/bin/activate
PYTHONPATH=. API_URL=http://localhost:8000 streamlit run src/ui/dashboard.py --server.port 8501
```

Your indexed repos and all data are **persisted in Docker volumes** — you don't need to re-index after restarting.

---

### Troubleshooting

**`ModuleNotFoundError: No module named 'src'`**
You forgot the `PYTHONPATH=.` prefix. Add it before every `python` or `uvicorn` command.

**`Address already in use` on port 8000 or 8501**
Something is already using that port. Find and stop it:
```bash
# macOS / Linux — find what's on port 8000
lsof -i :8000
# Kill it
kill -9 <PID>
```

**`Cannot connect to Docker daemon`**
Docker Desktop is not running. Open the Docker Desktop app and wait for it to fully start (the whale icon stops animating).

**Worker does nothing / jobs stay queued**
Make sure the worker is running in Terminal 3 and shows `Listening on indexing...`. The worker must be running **at the same time** as the API server.

**`GITHUB_WEBHOOK_SECRET must be set` error**
Your `.env` file is not being loaded. Make sure you're running commands from inside the `nexusCode_server/` directory and that `.env` exists (not `.env.example`).

**`psycopg2.OperationalError: could not connect to server`**
The PostgreSQL container is not running or not yet healthy. Run `docker compose ps` and wait for `healthy` status.

**The worker exits immediately on macOS**
You're missing `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`. Add it to the front of the worker command.

**`pip: command not found`**
Your virtual environment is not activated. Run `source .venv/bin/activate` first.

---

## Quick Start (for experienced developers)

If you're already comfortable with Python and Docker:

```bash
git clone https://github.com/shubhamsWEB/nexusCode_server.git
cd nexusCode_server
cp .env.example .env       # fill in GITHUB_TOKEN, VOYAGE_API_KEY, ANTHROPIC_API_KEY, JWT_SECRET

docker compose up postgres redis -d

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. python scripts/init_db.py

# Terminal 1 — API
PYTHONPATH=. uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 — Worker (macOS: prepend OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES)
PYTHONPATH=. rq worker indexing --url redis://localhost:6379

# Index a repo
PYTHONPATH=. python scripts/full_index.py <owner> <repo> --branch main

# Dashboard (optional)
PYTHONPATH=. API_URL=http://localhost:8000 streamlit run src/ui/dashboard.py --server.port 8501
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

---

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and the PR process.

```bash
# Quick start for contributors
git clone https://github.com/shubhamsWEB/nexusCode_server.git
cd nexusCode_server
make install-dev   # venv + deps + pre-commit hooks
cp .env.example .env  # fill in your keys
make docker-infra  # start postgres + redis
make db-init       # initialise schema
make dev           # start API server with hot-reload
make test          # run test suite
```

**Good first issues** are labelled [`good first issue`](https://github.com/shubhamsWEB/nexusCode_server/issues?q=label%3A%22good+first+issue%22) in the issue tracker.

---

## License

[MIT](LICENSE) © 2026 Shubham Agrawal
