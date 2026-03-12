# 🧠 Codebase Intelligence — MCP Knowledge Server

<img width="2816" height="1536" alt="Gemini_Generated_Image_7kxlab7kxlab7kxl" src="https://github.com/user-attachments/assets/858bfb69-5152-47d4-9619-7c0bbfc94b42" />


[![CI](https://github.com/shubhamsWEB/nexusCode_server/actions/workflows/ci.yml/badge.svg)](https://github.com/shubhamsWEB/nexusCode_server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

NexusCode is a self-hosted codebase intelligence server for AI agents.

It indexes your GitHub repositories, keeps them fresh from push webhooks, and exposes that knowledge through a REST API and MCP so any agent or internal tool can query the same shared source of truth.

Detailed documentation lives at [usenexuscode.vercel.app](https://usenexuscode.vercel.app/). This README is intentionally short and focused on what the project is and how to get it running.

## What It Does

- Indexes GitHub repositories into a searchable code knowledge layer
- Keeps indexes fresh automatically when code changes
- Supports semantic search, keyword search, symbol lookup, and caller tracing
- Answers codebase questions with citations
- Generates grounded implementation plans against the indexed repo
- Exposes everything through REST endpoints and MCP tools

## How Developers Use It

| Need | NexusCode interface |
|---|---|
| Search a repo by intent or keywords | `/search` or `search_codebase` |
| Jump to a symbol or see who calls it | MCP symbol and caller tools |
| Ask "how does this work?" | `/ask` or `ask_codebase` |
| Plan a feature or refactor | `/plan` or `plan_implementation` |
| Register and index repos | `/repos` and the dashboard |

## How It Works

```text
GitHub push
  -> webhook
  -> background indexing worker
  -> parse + chunk + embed + store
  -> PostgreSQL/pgvector
  -> REST API and MCP tools
  -> AI agents, editors, and internal workflows
```

## Quick Start

### Prerequisites

- Python 3.11+
- Docker Desktop or local PostgreSQL + Redis
- `VOYAGE_API_KEY`
- `GITHUB_TOKEN`
- `GITHUB_WEBHOOK_SECRET`
- `JWT_SECRET`
- At least one LLM API key for Ask/Plan mode, such as `ANTHROPIC_API_KEY`

### 1. Install and configure

```bash
git clone https://github.com/shubhamsWEB/nexusCode_server.git
cd nexusCode_server

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Fill in `.env` using the values in `.env.example`.

### 2. Start infrastructure

```bash
docker compose up postgres redis -d
PYTHONPATH=. python scripts/init_db.py
```

### 3. Run the services

```bash
make dev
```

In another terminal:

```bash
make worker
```

Optional dashboard in a third terminal:

```bash
make dashboard
```

### 4. Verify it is up

```bash
curl http://localhost:8000/health
```

Useful local URLs:

- API: `http://localhost:8000`
- API docs: `http://localhost:8000/docs`
- Dashboard: `http://localhost:8501`

### 5. Index your first repository

```bash
curl -X POST http://localhost:8000/repos \
  -H "Content-Type: application/json" \
  -d '{"owner":"your-org","name":"your-repo","index_now":true}'
```

## Main Interfaces

### REST API

Core endpoints:

- `GET /health`
- `GET /repos`
- `POST /repos`
- `POST /search`
- `POST /ask`
- `POST /plan`
- `POST /webhook`

### MCP

NexusCode exposes MCP tools for search, symbol lookup, caller tracing, task context, ask mode, and planning mode at:

```text
http://localhost:8000/mcp
```

## Documentation

- Full docs: [usenexuscode.vercel.app](https://usenexuscode.vercel.app/)
- Getting started: [doc/getting-started.md](doc/getting-started.md)
- API reference: [doc/api-reference.md](doc/api-reference.md)
- GitHub and webhooks: [doc/connecting-github.md](doc/connecting-github.md)
- MCP access: [doc/mcp-access.md](doc/mcp-access.md)
- Search, Ask, and Planning: [doc/search-and-ask.md](doc/search-and-ask.md)
- Deployment: [doc/deployment.md](doc/deployment.md)

## Development

```bash
make test
make lint
make format
make typecheck
```

## License

[MIT](LICENSE)
