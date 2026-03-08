# NexusCode Documentation

**NexusCode** is a centralized, always-on Codebase Intelligence Server. It continuously indexes
GitHub repositories and exposes the resulting knowledge via MCP, REST API, and a browser dashboard.

**Default ports:** API `http://localhost:8000` · Dashboard `http://localhost:8501`

---

## Guides

| Document | Description |
|----------|-------------|
| [Getting Started](./getting-started.md) | Install, configure, and run your first query |
| [Connecting GitHub](./connecting-github.md) | Personal Access Token, GitHub App, webhooks |
| [Configuration](./configuration.md) | All environment variables and config options |
| [Deployment](./deployment.md) | Docker Compose, Railway, bare-metal, Nginx |

## Feature Guides

| Document | Description |
|----------|-------------|
| [Search, Ask & Planning](./search-and-ask.md) | Search modes, Ask Mode, Planning Mode, SPARC |
| [Workflows](./workflows.md) | Workflow automation, YAML DSL, human checkpoints |
| [Agent Roles](./agent-roles.md) | Built-in and custom agent roles |
| [Knowledge Graph](./knowledge-graph.md) | Codebase knowledge graph visualization |
| [PDF Generation](./pdf-generation.md) | Generating downloadable PDF reports |
| [MCP Access](./mcp-access.md) | Tokens, Claude Code/Desktop/Cursor setup |
| [External MCP Servers](./external-mcp-servers.md) | Connect Context7, Browserbase, etc. |
| [Custom Skills](./custom-skills.md) | Build and deploy custom agent skills |

## API Reference

| Document | Description |
|----------|-------------|
| [API Reference](./api-reference.md) | Complete REST API — all 40+ endpoints |

## Deep-Dive Architecture

See the [`/architecture`](../architecture/README.md) directory:

| Document | Description |
|----------|-------------|
| [Overview](../architecture/overview.md) | System components, tech stack, data flow |
| [Indexing Pipeline](../architecture/indexing-pipeline.md) | GitHub → Parser → Embedder → PostgreSQL |
| [Retrieval Pipeline](../architecture/retrieval-pipeline.md) | HNSW + RRF + Cross-encoder + Assembly |
| [Agent System](../architecture/agent-system.md) | AgentLoop, gates, tools, roles |
| [Workflow Engine](../architecture/workflow-engine.md) | DAG execution, parallel waves, checkpoints |
| [Database Schema](../architecture/database-schema.md) | All tables, indexes, migrations |
| [LLM Providers](../architecture/llm-providers.md) | Anthropic, OpenAI, Grok, Ollama |
| [MCP Integration](../architecture/mcp-integration.md) | MCP protocol, tool registration, bridge |
| [How to Use](../architecture/how-to-use.md) | End-to-end step-by-step usage guide |

---

## 60-Second Quick Start

```bash
# 1. Configure
cp .env.example .env   # fill in DATABASE_URL, VOYAGE_API_KEY, ANTHROPIC_API_KEY, GITHUB_TOKEN

# 2. Initialize DB
PYTHONPATH=. python scripts/init_db.py

# 3. Start services (3 terminals)
PYTHONPATH=. uvicorn src.api.app:app --port 8000
PYTHONPATH=. rq worker indexing
PYTHONPATH=. streamlit run src/ui/dashboard.py --server.port 8501

# 4. Index a repo
curl -X POST http://localhost:8000/repos \
  -H "Content-Type: application/json" \
  -d '{"owner":"your-org","name":"your-repo"}'
curl -X POST http://localhost:8000/repos/your-org/your-repo/index

# 5. Ask a question
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"query":"How does authentication work?"}'
```

---

## Architecture in One Diagram

```
GitHub Push
    │ POST /webhook/github (HMAC verified)
    ▼
RQ Worker ──► Parser → Chunker → Enricher → Embedder ──► PostgreSQL + pgvector

Query Path:
  POST /search  → embed → HNSW + tsvector → RRF → cross-encoder → context
  POST /ask     → AgentLoop → tools → retrieval → LLM → markdown answer + citations
  POST /plan    → AgentLoop → retrieval + web research → LLM → ImplementationPlan
  POST /workflows/{id}/run  → DAG executor → multi-agent steps → SSE stream
  GET  /mcp/sse → 8 MCP tools for Claude Code / Cursor / Claude Desktop

PDF reports, knowledge graphs, workflow automation, and external MCP tools all
run through the same shared infrastructure.
```

---

## What's New (Since Initial Release)

| Feature | Guide |
|---------|-------|
| Workflow Automation Engine | [workflows.md](./workflows.md) |
| Multi-agent orchestration with 6 roles | [agent-roles.md](./agent-roles.md) |
| Human checkpoint support | [workflows.md](./workflows.md) |
| PDF report generation | [pdf-generation.md](./pdf-generation.md) |
| Knowledge Graph visualization | [knowledge-graph.md](./knowledge-graph.md) |
| External MCP server bridge | [external-mcp-servers.md](./external-mcp-servers.md) |
| Custom agent role overrides | [agent-roles.md](./agent-roles.md) |
| Multi-LLM provider support (Claude / GPT-4o / Grok / Ollama) | [configuration.md](./configuration.md) |
| SPARC-structured implementation plans | [search-and-ask.md](./search-and-ask.md) |
| Chat history persistence | [search-and-ask.md](./search-and-ask.md) |
| Search quality presets | [search-and-ask.md](./search-and-ask.md) |
