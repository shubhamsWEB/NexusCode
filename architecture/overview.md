# NexusCode — System Overview

NexusCode is a **centralized, always-on Codebase Intelligence Server**. It continuously indexes
GitHub repositories and exposes the resulting knowledge via:

- **MCP (Model Context Protocol)** — for Claude, Cursor, and other AI tools
- **REST API** — for custom integrations, CI/CD pipelines, and automation
- **Streamlit Dashboard** — browser-based admin UI for humans

---

## Core Philosophy

| Principle | What it means |
|-----------|---------------|
| **Async-first** | Every I/O operation is async (asyncio throughout) |
| **MCP-native** | All capabilities exposed as MCP tools, not custom JSON-RPC |
| **Provider-agnostic** | Same codebase works with Claude, GPT-4o, Grok, Ollama |
| **Token-budgeted** | Every retrieval step accounts for token cost explicitly |
| **Streaming-first** | `/plan`, `/ask`, and workflow runs support real-time SSE |
| **Soft deletes** | Code chunks are marked `is_deleted`, never dropped from DB |
| **Best-effort persistence** | Chat/plan history never blocks the API response path |

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| **Language** | Python 3.11+ |
| **Web Framework** | FastAPI + Starlette + Uvicorn |
| **Admin UI** | Streamlit |
| **MCP SDK** | Anthropic Python MCP SDK (FastMCP) |
| **Database** | PostgreSQL 15+ with **pgvector** extension |
| **Vector Index** | HNSW (via pgvector, migrated from ivfflat) |
| **Lexical Search** | tsvector + pg_trgm (same PostgreSQL) |
| **Job Queue** | Redis + RQ |
| **Embedding Model** | Voyage AI — `voyage-code-2` (1536 dimensions) |
| **Reranker** | `cross-encoder/ms-marco-MiniLM-L-6-v2` (local CPU) |
| **AST Parsing** | Tree-sitter (12+ languages) |
| **LLM Providers** | Anthropic (Claude), OpenAI (GPT-4o/o3), xAI (Grok), Ollama |
| **Auth** | OAuth 2.1 + PKCE, PyJWT (HS256) |
| **HTTP Client** | httpx (async) |
| **Deployment** | Docker Compose / Railway / bare-metal |

---

## Full Component Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                           EXTERNAL SOURCES                           │
│                                                                      │
│   GitHub Repositories                External MCP Servers           │
│   (push webhooks, REST API)           (Context7, Browserbase, etc.) │
└───────────────┬──────────────────────────────────┬──────────────────┘
                │ push events                       │ SSE / HTTP
┌───────────────▼──────────────────────────────────▼──────────────────┐
│                          NexusCode Server                            │
│                                                                      │
│  ┌─────────────────────┐    ┌─────────────────────────────────────┐ │
│  │     FastAPI App      │    │         MCP Server (FastMCP)        │ │
│  │  (http :8000)        │    │   SSE: /mcp/sse                     │ │
│  │                      │    │   8 tools exposed to AI clients     │ │
│  │  Routers:            │    └─────────────────────────────────────┘ │
│  │  /repos              │                                            │
│  │  /search             │    ┌─────────────────────────────────────┐ │
│  │  /ask                │    │      Streamlit Dashboard             │ │
│  │  /plan               │    │   (http :8501)                      │ │
│  │  /workflows          │    │   16 pages for human operators      │ │
│  │  /mcp-servers        │    └─────────────────────────────────────┘ │
│  │  /agent-roles        │                                            │
│  │  /graph              │    ┌─────────────────────────────────────┐ │
│  │  /documents          │    │        Agent System                 │ │
│  │  /history            │    │  AgentLoop → execute_tool()         │ │
│  │  /skills             │    │  Roles: searcher, planner,          │ │
│  │  /auth               │    │    reviewer, coder, tester,         │ │
│  └──────────┬───────────┘    │    supervisor                       │ │
│             │                └─────────────────────────────────────┘ │
│  ┌──────────▼───────────┐    ┌─────────────────────────────────────┐ │
│  │   Indexing Pipeline  │    │       Workflow Engine               │ │
│  │  Parser → Chunker →  │    │  YAML DSL → DAG Executor            │ │
│  │  Enricher → Embedder │    │  Parallel step waves                │ │
│  │  → PostgreSQL        │    │  Human checkpoints                  │ │
│  └──────────────────────┘    │  SSE streaming                      │ │
│                              └─────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │                     Retrieval Pipeline                           │ │
│  │  HNSW semantic  +  tsvector keyword  →  RRF merge  →  Rerank    │ │
│  │  Assembler (token-budget context formatting)                     │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │
              ┌────────────────────┼─────────────────────┐
              │                    │                      │
  ┌───────────▼──────┐   ┌─────────▼───────┐   ┌────────▼────────┐
  │   PostgreSQL     │   │      Redis       │   │  LLM Providers  │
  │  + pgvector      │   │  ● RQ job queue  │   │  ● Anthropic    │
  │  ● chunks        │   │  ● pub/sub bus   │   │  ● OpenAI       │
  │  ● symbols       │   │  ● embed cache   │   │  ● xAI/Grok     │
  │  ● repos         │   └─────────────────┘   │  ● Ollama       │
  │  ● workflows     │                          └─────────────────┘
  │  ● chat history  │
  │  ● plan history  │
  │  ● knowledge     │
  │    graph edges   │
  │  ● documents     │
  └──────────────────┘
```

---

## Data Flow — Push to Query

```
1. Developer pushes to GitHub
   │
2. GitHub sends webhook POST /webhook/github
   │  (HMAC-SHA256 verified)
   │
3. Webhook handler queues an RQ job
   │
4. RQ worker runs incremental_index(repo, commits)
   │
   ├── 4a. Fetch changed file list (GitHub API)
   ├── 4b. Merkle diff: identify added/modified/deleted files
   ├── 4c. Fetch file blobs (parallel, up to 10 concurrent)
   ├── 4d. Tree-sitter AST parse → extract symbols + imports
   ├── 4e. Chunk each file (512-token target, 128 overlap)
   ├── 4f. Enrich chunks (prepend scope chain + imports)
   ├── 4g. Embed with voyage-code-2 (batched, 1536-dim vectors)
   └── 4h. Upsert chunks + symbols into PostgreSQL
       (ON CONFLICT DO UPDATE — restores soft-deleted rows)
   │
5. Total latency: ~2–4 seconds from push to queryable
   │
6. AI client calls search_codebase(query)
   │
   ├── 6a. Embed query with voyage-code-2
   ├── 6b. HNSW cosine similarity search (semantic)
   ├── 6c. tsvector + pg_trgm keyword search (in parallel)
   ├── 6d. RRF merge of both result sets
   ├── 6e. Cross-encoder reranking (local model)
   └── 6f. Token-budget context assembly → return to client
```

---

## Services and Ports

| Service | Port | Command | Purpose |
|---------|------|---------|---------|
| API Server | 8000 | `uvicorn src.api.app:app` | FastAPI + MCP |
| Dashboard | 8501 | `streamlit run src/ui/dashboard.py` | Streamlit admin |
| PostgreSQL | 5432 | Docker or system | Primary database |
| Redis | 6379 | Docker or system | Queue + cache |
| RQ Worker | — | `rq worker indexing` | Background jobs |

---

## Supported Languages

Tree-sitter parsers are installed for:
Python · TypeScript · JavaScript · Java · Go · Rust · C++ · C · C# · Ruby · Swift · Kotlin · PHP · Scala · JSON · YAML · HTML · CSS · Shell · SQL · TOML · Markdown

---

## Key Design Decisions

### Why PostgreSQL instead of a dedicated vector DB?
Single-service simplicity. pgvector HNSW delivers strong performance for codebases up to
millions of chunks, and keeps vector search co-located with metadata queries (filtering by
repo, language, file path). No Pinecone/Weaviate to operate separately.

### Why RQ instead of Celery?
Lightweight, Redis-native, zero configuration. Celery's broker/backend split adds operational
overhead that isn't needed for a single indexing queue.

### Why MCP + REST (not just one)?
MCP is the native protocol for AI tool use. REST allows integration with non-AI systems,
CI/CD pipelines, and custom dashboards. Both share the same underlying query functions.

### Why voyage-code-2?
Voyage AI's code embedding model outperforms OpenAI `text-embedding-3-large` on code retrieval
benchmarks while being significantly cheaper per token. The 1536-dimensional space maps well
to HNSW indexing.
