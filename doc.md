# NexusCode — Technical Documentation

A centralized, always-fresh knowledge service that indexes GitHub repositories and exposes your entire codebase to AI agents via the **Model Context Protocol (MCP)**. This document explains how every component works and how to connect any GitHub repository.

---

## Table of Contents

1. [How It Works — The Big Picture](#1-how-it-works--the-big-picture)
2. [System Architecture](#2-system-architecture)
3. [Component Deep-Dives](#3-component-deep-dives)
   - [3.1 GitHub Integration](#31-github-integration)
   - [3.2 Indexing Pipeline](#32-indexing-pipeline)
   - [3.3 Storage Layer](#33-storage-layer)
   - [3.4 Retrieval Layer](#34-retrieval-layer)
   - [3.5 MCP Server](#35-mcp-server)
   - [3.6 Admin Dashboard](#36-admin-dashboard)
4. [Configuring a New GitHub Repository](#4-configuring-a-new-github-repository)
5. [Configuration Reference](#5-configuration-reference)
6. [Search Query Guide](#6-search-query-guide)
7. [MCP Tools Reference](#7-mcp-tools-reference)
8. [Performance & Tuning](#8-performance--tuning)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. How It Works — The Big Picture

NexusCode continuously maintains a semantic index of your codebase. Here is the full lifecycle of a code change:

```
Developer pushes commit to GitHub
         │
         │  POST /webhook  (HMAC-verified)
         ▼
┌─────────────────────┐
│  FastAPI API Server │  — receives push event
│  (webhook.py)       │  — verifies GitHub signature
└────────┬────────────┘  — enqueues indexing job
         │
         │  Redis queue ("indexing")
         ▼
┌─────────────────────┐
│  RQ Worker          │  1. Fetch only changed files from GitHub API
│  (pipeline.py)      │  2. Merkle check: skip unchanged files
│                     │  3. AST parse: extract symbols (functions, classes)
│                     │  4. Chunk: split into 512-token pieces on AST boundaries
│                     │  5. Enrich: inject filename, scope, imports into each chunk
│                     │  6. Embed: send to Voyage AI (voyage-code-2, 1536 dims)
│                     │  7. Store: upsert chunks + symbols into PostgreSQL
└────────┬────────────┘
         │
         ▼
┌─────────────────────────────────────────┐
│  PostgreSQL + pgvector                  │
│  ├─ chunks      (vector + full-text)    │
│  ├─ symbols     (fuzzy search)          │
│  ├─ merkle_nodes (change detection)     │
│  ├─ repos       (registration)         │
│  └─ webhook_events (audit log)          │
└────────┬────────────────────────────────┘
         │
         │  5 MCP tools exposed via SSE
         ▼
  AI Agents (Claude Desktop, LangGraph, CI bots…)
```

**Key design principles:**

| Principle | How it's implemented |
|-----------|---------------------|
| Always fresh | Webhook fires on every push; new code is queryable in 2–4 seconds |
| Only re-index what changed | Merkle nodes store GitHub blob SHAs; unchanged files are skipped in <1ms |
| Semantic understanding | Voyage AI `voyage-code-2` embeds enriched code chunks (not raw text) |
| Symbol awareness | Tree-sitter AST extracts functions, classes, methods for 12+ languages |
| High precision search | Hybrid semantic + keyword + cross-encoder reranking |
| AI-native interface | MCP protocol — any agent can call tools without custom API code |

---

## 2. System Architecture

### Services

| Service | Technology | Purpose |
|---------|-----------|---------|
| **API Server** | FastAPI + uvicorn | Webhook receiver, search endpoint, MCP mount |
| **Worker** | RQ (Redis Queue) | Background indexing jobs |
| **Database** | PostgreSQL 16 + pgvector | Vector store, full-text search, metadata |
| **Queue** | Redis 7 | Job queue between API and worker |
| **Dashboard** | Streamlit | Admin UI for health, search, event log |

### Port Map

| Service | Default Port |
|---------|-------------|
| API Server | 8000 |
| Streamlit Dashboard | 8501 |
| PostgreSQL | 5432 |
| Redis | 6379 |

### Process Map

```
Terminal 1: uvicorn src.api.app:app --port 8000
Terminal 2: rq worker indexing --url redis://localhost:6379
Terminal 3: streamlit run src/ui/dashboard.py          (optional)
```

---

## 3. Component Deep-Dives

### 3.1 GitHub Integration

#### Webhook Handler (`src/github/webhook.py`)

Every GitHub push event arrives here first.

**Signature verification** — NexusCode uses HMAC-SHA256 to verify every webhook payload. GitHub signs the raw request body with your `GITHUB_WEBHOOK_SECRET` and sends the signature in the `X-Hub-Signature-256` header. The server compares with `hmac.compare_digest` (constant-time, prevents timing attacks). Any mismatch returns HTTP 401.

```
Request headers required:
  X-GitHub-Event      → "push" (or "ping")
  X-Hub-Signature-256 → "sha256=<hex>"
  X-GitHub-Delivery   → "<uuid>"
```

**Processing flow:**

1. Parse and verify HMAC signature
2. Respond to `ping` events immediately (used during webhook setup)
3. Ignore non-`push` events
4. Parse the JSON body into a `PushEvent` dataclass
5. Filter to the configured branch (default: `main`)
6. Filter files to indexable extensions only
7. Log the event to `webhook_events` table (non-blocking)
8. Enqueue a Redis job: `src.pipeline.pipeline.run_incremental_index`
9. Return **HTTP 202 Accepted** with the delivery ID

The response is always fast (< 50ms) because all indexing happens asynchronously in the worker.

#### GitHub API Client (`src/github/fetcher.py`)

The worker fetches file content directly from the GitHub REST API — no local `git clone` required.

- **Authentication:** Bearer token via `GITHUB_TOKEN` (5,000 req/hr) or GitHub App credentials (15,000 req/hr)
- **`fetch_file(owner, repo, path, ref)`** — Downloads a single file at a specific commit SHA. Returns `(content_str, blob_sha)` or `None` if the file doesn't exist.
- **`fetch_full_tree(owner, repo, ref)`** — Single recursive API call to get all file paths in a repo (used during initial full index).
- **`filter_indexable_paths(paths)`** — Applies extension and ignore-pattern filters from config.

#### Push Event Parsing (`src/github/events.py`)

The `PushEvent` dataclass parses the GitHub webhook payload and provides clean properties:

```python
event.branch          # → "main"
event.files_to_upsert # → ["src/auth.py", "src/models.py"]  (added + modified, deduplicated)
event.files_to_delete # → ["src/old_file.py"]               (removed, not re-added)
event.head_commit_author   # → "developer@example.com"
event.head_commit_message  # → "Add OAuth flow"
```

Files that are removed then re-added in the same push are correctly handled: they appear only in `files_to_upsert`, not in `files_to_delete`.

---

### 3.2 Indexing Pipeline

The pipeline (`src/pipeline/pipeline.py`) is the RQ worker entry point. It orchestrates all steps for one push event.

#### Step 1: Deletion

For each deleted file path:
1. Soft-delete all chunks (`is_deleted = TRUE`)
2. Hard-delete all symbol entries
3. Remove the merkle node (so re-adds trigger a fresh index)

#### Step 2: Merkle Change Detection

For each file to upsert, before doing any work:

```python
stored_sha  = await get_merkle_hash(path, owner, repo)  # from our DB
content, blob_sha = await fetch_file(owner, repo, path, ref=commit_sha)

if stored_sha == blob_sha:
    skip this file  # content hasn't changed, already indexed
```

This is the most important optimization. GitHub's blob SHA is a content hash — if it matches what we stored, the file is identical to what we already indexed. This skips 100% of unchanged files in <1ms each.

#### Step 3: AST Parsing (`src/pipeline/parser.py`)

Tree-sitter parses source code into an Abstract Syntax Tree. NexusCode extracts:

| Extracted | Description |
|-----------|-------------|
| `ParsedSymbol.name` | Bare function/class name |
| `ParsedSymbol.qualified_name` | `ClassName.method_name` for methods |
| `ParsedSymbol.kind` | `"function"`, `"class"`, or `"method"` |
| `ParsedSymbol.start_line` / `end_line` | Source line range |
| `ParsedSymbol.signature` | Function signature (params + return type) |
| `ParsedSymbol.docstring` | Extracted docstring/JSDoc comment |
| `ParsedSymbol.is_exported` | Whether the symbol is exported/public |

**Supported languages:** Python, TypeScript, TSX, JavaScript, Java, Go, Rust, C++, C, C#, Ruby, Swift, Kotlin.

Files with unsupported extensions fall back to plain-text chunking.

#### Step 4: Chunking (`src/pipeline/chunker.py`)

Chunking converts a parsed file into a list of `RawChunk` objects. The algorithm:

1. **Symbol-aware splitting** — Each top-level function or class becomes its own chunk if it fits within the token target (default: 512 tokens). This means a chunk almost always represents a complete logical unit.

2. **Large symbol handling** — If a class is too large (e.g., 2,000 tokens):
   - Emit the class header as one chunk
   - Emit each method as separate chunks

3. **Sliding window fallback** — For non-code files or symbols with no AST structure, a sliding window with 128-token overlap is used.

4. **Greedy merge** — After splitting, adjacent small chunks (< 50 tokens each) are merged up to the target size. This prevents many tiny "orphan" chunks.

The result: every chunk maps to a meaningful code unit (function body, class definition, etc.), making search results self-contained and readable.

#### Step 5: Enrichment (`src/pipeline/enricher.py`)

Before embedding, each chunk gets a metadata header prepended:

```
File: app/routes/auth.server.ts
Scope: AuthService > authenticate
Language: typescript
Key imports:
import { shopify } from "../shopify.server";
import type { LoaderFunctionArgs } from "@remix-run/node";

Code:
export const loader = async ({ request }: LoaderFunctionArgs) => {
  await shopify.authenticate.admin(request);
  ...
};
```

This enrichment is critical for retrieval quality. When a user asks "what handles Shopify authentication?", the model can match on the file path, scope chain, and import names — not just the raw code. Studies show 30–40% improvement in retrieval precision with this technique.

The `chunk_id` (primary key in the database) is a **SHA-256 hash of the enriched content**, making it a stable, deterministic identifier. If a chunk's content doesn't change, its ID stays the same across re-indexes — enabling efficient caching.

#### Step 6: Embedding (`src/pipeline/embedder.py`)

Enriched chunks are batched and sent to Voyage AI's `voyage-code-2` model:

- **Model:** `voyage-code-2` (optimized for code; 1536 dimensions)
- **Input type:** `"document"` for chunks, `"query"` for search queries
- **Batch size:** Up to 128 chunks per API call (120,000 tokens max per batch)
- **Cache hit detection:** Before calling the API, the embedder checks the DB for existing chunk IDs. If a chunk hasn't changed (same SHA-256 ID), its embedding is reused — no API call needed.
- **Rate limit handling:** Exponential backoff with up to 5 retries. Free-tier accounts (detected by error message) get automatic throttling.

#### Step 7: Storage (`src/storage/db.py`)

Results are written to PostgreSQL using upsert operations:

- **`upsert_chunks`** — Uses `ON CONFLICT DO UPDATE` so re-indexed chunks restore `is_deleted=False` and update commit metadata/embeddings.
- **`upsert_symbols`** — Replaces symbol records for each file.
- **`upsert_merkle_node`** — Stores the new blob SHA to enable future skipping.
- **`update_webhook_status`** — Marks the webhook event as `done` (or `error`) with a `processed_at` timestamp.

---

### 3.3 Storage Layer

#### Database Schema

**`chunks`** — The primary search table.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | SHA-256 of enriched content |
| `file_path` | TEXT | Relative path in repo |
| `repo_owner` / `repo_name` | TEXT | Repository identification |
| `commit_sha` | TEXT | Commit where this version was indexed |
| `language` | TEXT | Detected language |
| `symbol_name` | TEXT | Function/class name (nullable) |
| `symbol_kind` | TEXT | `function`, `class`, `method` |
| `scope_chain` | TEXT | `ClassName > method_name` |
| `start_line` / `end_line` | INT | Line range in source file |
| `raw_content` | TEXT | Original source code |
| `enriched_content` | TEXT | Metadata header + source (what was embedded) |
| `embedding` | vector(1536) | Voyage AI embedding |
| `is_deleted` | BOOLEAN | Soft-delete flag |

**`symbols`** — Lightweight symbol index for fast name lookups.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | `{file_path}:{qualified_name}` |
| `name` | TEXT | Bare name |
| `qualified_name` | TEXT | `ClassName.method_name` |
| `kind` | TEXT | `function`, `class`, `method` |
| `signature` | TEXT | Full signature |
| `docstring` | TEXT | Documentation comment |
| `is_exported` | BOOLEAN | Public/exported flag |

**`merkle_nodes`** — Change detection table.

| Column | Type | Description |
|--------|------|-------------|
| `file_path` | TEXT | File path (composite PK) |
| `repo_owner` / `repo_name` | TEXT | Repo (composite PK) |
| `blob_sha` | TEXT | GitHub's content hash |
| `last_indexed` | TIMESTAMP | When this version was indexed |

**`webhook_events`** — Audit log for all incoming GitHub events.

| Column | Type | Description |
|--------|------|-------------|
| `delivery_id` | TEXT UNIQUE | GitHub's unique delivery UUID |
| `status` | TEXT | `queued` → `done` / `error` |
| `files_changed` | INT | Number of files in the push |
| `received_at` | TIMESTAMP | When webhook arrived |
| `processed_at` | TIMESTAMP | When worker finished |

#### Indexes

The schema creates several indexes for performance:

| Index | Type | Supports |
|-------|------|---------|
| `chunks_embedding_idx` | IVFFlat (cosine, lists=100) | Semantic similarity search |
| `chunks_fts_idx` | GIN tsvector | Full-text keyword search |
| `chunks_symbol_trgm_idx` | GIN pg_trgm | Fuzzy symbol name search |
| `symbols_name_trgm_idx` | GIN pg_trgm | Fuzzy symbol lookup |
| `chunks_repo_idx` | B-tree | Per-repo filtering |

---

### 3.4 Retrieval Layer

#### Hybrid Search (`src/retrieval/searcher.py`)

Three search modes are available:

**Semantic mode** — pgvector cosine similarity:
```sql
SELECT *, 1 - (embedding <=> $query_vector) AS score
FROM chunks
WHERE is_deleted = FALSE
ORDER BY embedding <=> $query_vector
LIMIT $top_k
```

**Keyword mode** — PostgreSQL full-text + trigram:
```sql
SELECT *,
  ts_rank(to_tsvector('english', raw_content), plainto_tsquery($query)) * 0.7
  + similarity(symbol_name, $query) * 0.3 AS score
FROM chunks
WHERE is_deleted = FALSE
  AND (raw_content @@ plainto_tsquery($query)
    OR similarity(symbol_name, $query) > 0.15)
ORDER BY score DESC
```

**Hybrid mode** (default) — Both lists merged with Reciprocal Rank Fusion (RRF):
```
RRF score = Σ  1 / (60 + rank_i)
```

RRF combines two ranked lists into one without requiring score normalization. A result ranked #1 in both semantic and keyword search scores higher than one ranked #1 in only one list. The constant 60 is the standard RRF constant from the original paper.

#### Cross-Encoder Reranking (`src/retrieval/reranker.py`)

After initial retrieval, results are passed through a local cross-encoder:
- **Model:** `cross-encoder/ms-marco-MiniLM-L-6-v2` (~60MB, CPU inference)
- **Input:** `(query, raw_content[:1500])` pairs
- **Output:** Re-scored and re-sorted results
- **Lazy loading:** Model downloads on first use; subsequent calls use cached model

The cross-encoder reads the query and each result together in full context, unlike the bi-encoder embedding approach (which scores query and document independently). This gives a 30–40% precision improvement at the cost of ~100ms latency.

#### Context Assembler (`src/retrieval/assembler.py`)

The assembler converts ranked results into a ready-to-inject context string:

1. Iterates results by score (highest first)
2. Skips duplicates by chunk_id
3. Greedily fills the token budget (default: 8,000 tokens)
4. Formats each chunk with a separator, file metadata, and source code

Output format:
```
────────────────────────────────────────────────────────────
File: app/shopify.server.ts  [lines 1-35]  (typescript)
Score: 4.409
Last changed: dev@example.com @ 4e86a85

import "@shopify/shopify-app-react-router/adapters/node";
const shopify = shopifyApp({
  apiVersion: ApiVersion.October25,
  ...
});
```

---

### 3.5 MCP Server

The MCP server (`src/mcp/server.py`) exposes five tools over Server-Sent Events (SSE). Any MCP-compatible AI agent can connect to `http://your-server:8000/mcp/sse` and call these tools.

#### Tool: `search_codebase`

```
search_codebase(
  query:    "what handles Shopify auth?",
  repo:     "owner/name",     # optional — scope to one repo
  language: "typescript",     # optional — filter by language
  top_k:    5,                # number of results (1–20)
  mode:     "hybrid",         # "hybrid" | "semantic" | "keyword"
)
```

Runs the full retrieval pipeline: embed → search → rerank → assemble. Returns a JSON string with results and the assembled context.

#### Tool: `get_symbol`

```
get_symbol(
  name: "authenticate",
  repo: "owner/name",   # optional
)
```

Fuzzy symbol lookup — equivalent to "Go to Definition" in an IDE. Uses pg_trgm similarity matching so partial names work (e.g., `"auth"` finds `"authenticate"`, `"AuthService"`, etc.). Returns up to 10 matches with file paths and line numbers.

#### Tool: `find_callers`

```
find_callers(
  symbol: "shopify.authenticate.admin",
  repo:   "owner/name",   # optional
  depth:  1,              # call depth (1–3)
)
```

Finds all code that calls or references a given symbol. Uses keyword search filtered to call sites (lines that don't start with definition keywords like `function`, `class`, `const`, etc.). Returns call sites with file paths, line numbers, and code previews.

#### Tool: `get_file_context`

```
get_file_context(
  path:         "app/shopify.server.ts",
  repo:         "owner/name",   # optional
  include_deps: True,           # include files that import this one
)
```

Returns the complete structural map of a file: all symbols with signatures, all imports, commit metadata, and optionally the list of files that import this file (reverse dependency tracking).

#### Tool: `get_agent_context`

```
get_agent_context(
  task:        "refactor the authentication flow to use sessions",
  focal_files: ["app/shopify.server.ts", "app/routes/auth.$.tsx"],
  token_budget: 8000,
  repo:         "owner/name",   # optional
)
```

The most powerful tool — designed as your first call for any coding task. It:
1. Fetches all chunks from `focal_files` (given priority score of 10.0)
2. Runs semantic search for additional related context
3. Deduplicates and reranks everything together
4. Returns the best context within the token budget

This gives the AI agent a curated, relevant, token-efficient context for the task — no manual file selection required.

#### Authentication

MCP endpoints are protected by JWT bearer tokens. For development, issue a token:

```bash
curl -s -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"sub": "my-agent", "repos": ["owner/repo"]}'
```

Tokens are HS256-signed, expire in 8 hours, and can be scoped to specific repos.

---

### 3.6 Admin Dashboard

The Streamlit dashboard (`src/ui/dashboard.py`) at `http://localhost:8501` has three pages:

**Health** — Index status at a glance:
- Active chunks, symbols, files, repos, last-indexed timestamp
- Per-repository breakdown table (active chunks, soft-deleted, files)
- Recently indexed files with language, token count, commit
- Chunk size distribution bar chart (token count buckets)

**Query Tester** — Interactive search UI:
- 6 pre-loaded showcase queries
- Mode selector (hybrid / semantic / keyword), top-K slider, rerank toggle
- Results table with scores, file paths, line ranges, symbols
- Code preview panels (expandable, syntax-highlighted)
- Assembled context text area (copy directly to your LLM prompt)
- Retrieval log showing which chunks were included and why

**Activity Feed** — Webhook event log:
- Last 20 push events received
- Status icons: 🟡 queued, 🔵 processing, ✅ done, ❌ error
- Files changed, processing duration, error messages

---

## 4. Configuring a New GitHub Repository

### Step 1: Register the repository

```bash
curl -X POST http://localhost:8000/repos \
  -H "Content-Type: application/json" \
  -d '{
    "owner": "your-org",
    "name":  "your-repo",
    "branch": "main"
  }'
```

This inserts a row into the `repos` table so the server knows about the repository.

### Step 2: Run the initial full index

The initial index fetches every file in the repository:

```bash
# Set environment variables for the script
REPO_OWNER=your-org REPO_NAME=your-repo python scripts/full_index.py
```

`full_index.py` calls `fetch_full_tree` to get all file paths, filters to indexable extensions, then enqueues one indexing job per file. Watch progress in the worker terminal or via the health endpoint:

```bash
watch -n 3 'curl -s http://localhost:8000/health | python3 -m json.tool'
```

For a typical 100-file repository, the full index takes 2–5 minutes (dominated by Voyage AI API calls).

### Step 3: Configure the GitHub webhook

In your GitHub repository settings, go to **Settings → Webhooks → Add webhook**:

| Field | Value |
|-------|-------|
| **Payload URL** | `https://your-server-url/webhook` |
| **Content type** | `application/json` |
| **Secret** | Your `GITHUB_WEBHOOK_SECRET` value |
| **SSL verification** | Enable (required for production) |
| **Events** | Select "Just the push event" |

For local development, use a tunnel:
```bash
# ngrok (free tier)
ngrok http 8000
# Use the https:// ngrok URL as your webhook Payload URL
```

### Step 4: Verify the webhook

GitHub sends a `ping` event when a webhook is first created. You should see in your API server logs:

```
INFO: POST /webhook → 200 (ping → pong)
```

Push a commit to the repository. Within 5 seconds, you should see:

```
INFO: POST /webhook → 202 (delivery: abc-123, files_to_upsert: 2)
# worker logs:
pipeline.start   upsert=2 delete=0
pipeline.done    elapsed_s=3.1 files_processed=2 chunks_upserted=6
```

### Step 5: Verify in the dashboard

Open `http://localhost:8501`, go to the **Health** page, and confirm your repository appears in the per-repo breakdown table.

Go to the **Activity Feed** page to see the webhook event with status ✅ done.

### Step 6: Connect your AI agent

#### Claude Desktop

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

#### Python (MCP SDK)

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client("http://localhost:8000/mcp/sse") as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()

        # Search the codebase
        result = await session.call_tool(
            "search_codebase",
            {"query": "what handles authentication?", "repo": "your-org/your-repo"}
        )
        print(result.content[0].text)

        # Get context for a coding task
        result = await session.call_tool(
            "get_agent_context",
            {
                "task": "add rate limiting to the API",
                "focal_files": ["src/api/app.py"],
                "repo": "your-org/your-repo"
            }
        )
        print(result.content[0].text)
```

#### REST API (no MCP client needed)

```bash
# Search
curl -s -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "what handles authentication?",
    "repo": "your-org/your-repo",
    "top_k": 5,
    "mode": "hybrid",
    "rerank": true
  }'
```

---

## 5. Configuration Reference

All configuration is loaded from environment variables (and a `.env` file via `python-dotenv`).

### Required Variables

| Variable | Example | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | `ghp_xxx` | Personal Access Token with `repo` read scope |
| `GITHUB_WEBHOOK_SECRET` | `random-32-char-string` | HMAC secret — must match GitHub webhook config |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@localhost:5432/db` | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `VOYAGE_API_KEY` | `pa-xxx` | Voyage AI key for `voyage-code-2` embeddings |
| `JWT_SECRET` | `random-32-char-string` | Signs MCP bearer tokens |

### Optional Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_APP_ID` | — | GitHub App ID (higher rate limits than PAT) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | — | Path to `.pem` private key for GitHub App |
| `GITHUB_DEFAULT_BRANCH` | `main` | Branch to track for webhook updates |
| `ANTHROPIC_API_KEY` | — | Reserved for future Claude-powered tools |
| `EMBEDDING_DIMENSIONS` | `1536` | Must match the model (voyage-code-2 = 1536) |
| `EMBEDDING_BATCH_SIZE` | `128` | Chunks per Voyage API call |
| `CHUNK_TARGET_TOKENS` | `512` | Target chunk size in tokens |
| `CHUNK_OVERLAP_TOKENS` | `128` | Overlap between adjacent chunks |
| `CHUNK_MIN_TOKENS` | `50` | Chunks smaller than this are merged |
| `CONTEXT_TOKEN_BUDGET` | `8000` | Max tokens in assembled context |
| `RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Local reranker model |
| `RERANKER_TOP_N` | `20` | Candidate pool size for reranker |
| `DB_POOL_SIZE` | `10` | SQLAlchemy connection pool size |
| `DB_MAX_OVERFLOW` | `20` | Additional connections allowed above pool size |
| `JWT_EXPIRY_HOURS` | `8` | MCP token TTL in hours |

### Supported File Extensions (default)

```
.py .js .ts .tsx .jsx .java .go .rs .cpp .c .h .hpp .cs .rb .swift .kt
.sql .sh .bash .yaml .yml .json .toml .md .txt
```

Override with `SUPPORTED_EXTENSIONS=.py,.ts,.go` (comma-separated).

### Ignored Paths (default)

```
node_modules .git __pycache__ .venv venv dist build .next .nuxt
coverage .pytest_cache *.min.js *.min.css *.lock
```

Override with `IGNORE_PATTERNS=node_modules,.git,dist` (comma-separated).

---

## 6. Search Query Guide

### Natural Language Queries

NexusCode understands natural language — you don't need to know the exact function name:

```
"what handles Shopify authentication and session management?"
"where is the payment processing logic?"
"how does the app configure database connections?"
"error handling for failed API requests"
```

### Symbol Lookups

Use `get_symbol` for precise name-based lookups:

```
"authenticate"         → finds authenticate, shopify.authenticate, AuthService.authenticate
"PaymentService"       → finds the class and all methods
"handleWebhook"        → finds the handler function
```

### File-Based Context

Use `get_file_context` when you know the file:

```
get_file_context("src/api/routes/auth.ts")
→ Returns all symbols, imports, and what imports this file
```

### Task-Oriented Context

Use `get_agent_context` when starting a coding task:

```
get_agent_context(
  task="refactor database queries to use connection pooling",
  focal_files=["src/db/client.ts", "src/db/queries.ts"]
)
→ Returns all relevant context within your token budget, prioritizing focal files
```

---

## 7. MCP Tools Reference

### `search_codebase`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Natural language or identifier query |
| `repo` | string | null | Scope to `owner/name` |
| `language` | string | null | Filter by language (python, typescript, etc.) |
| `top_k` | int | 5 | Number of results (1–20) |
| `mode` | string | `"hybrid"` | `"hybrid"` \| `"semantic"` \| `"keyword"` |

Returns: JSON with `results` array, `context` string, `tokens_used`, `retrieval_log`.

### `get_symbol`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | required | Symbol name (partial match works) |
| `repo` | string | null | Scope to `owner/name` |

Returns: JSON with `symbols` array (name, qualified_name, kind, file_path, start_line, signature).

### `find_callers`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `symbol` | string | required | Function or variable name |
| `repo` | string | null | Scope to `owner/name` |
| `depth` | int | 1 | Call depth to traverse (1–3) |

Returns: JSON with `call_sites` array (file, lines, preview, symbol).

### `get_file_context`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | required | File path (relative to repo root) |
| `repo` | string | null | Scope to `owner/name` |
| `include_deps` | bool | true | Include files that import this file |

Returns: JSON with file symbols, imports, commit info, and importing files.

### `get_agent_context`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `task` | string | required | Natural language description of the task |
| `focal_files` | list[string] | null | Files to prioritize (fetched in full) |
| `token_budget` | int | 8000 | Max tokens in assembled context |
| `repo` | string | null | Scope to `owner/name` |

Returns: Formatted context string ready to inject into an LLM prompt.

---

## 8. Performance & Tuning

### Observed Performance

| Metric | Target | Actual |
|--------|--------|--------|
| Initial index (19 files) | < 2 min | ~13 seconds |
| Incremental update (1 file) | < 30s | **2–4 seconds** |
| Merkle skip (unchanged files) | > 99% | 100% |
| Search latency p95 (warm) | < 500ms | ~100–400ms |
| Reranker cold start | — | ~15s (model load) |
| Reranker warm | — | ~100ms |

### Tuning Chunk Size

Smaller chunks (`CHUNK_TARGET_TOKENS=256`) → more precise results, more API cost, larger index.
Larger chunks (`CHUNK_TARGET_TOKENS=1024`) → more context per result, better for reading full functions.

The default 512 tokens balances precision and context.

### Tuning the Token Budget

`CONTEXT_TOKEN_BUDGET` controls how much context is assembled per search. For GPT-4/Claude with 200K context windows, you can safely increase this to 32,000 or higher for richer results.

### Scaling Embedding Throughput

The default batch size (`EMBEDDING_BATCH_SIZE=128`) fits Voyage AI's free tier. On paid plans, this is already at the maximum per-call limit. To speed up initial indexing, run multiple workers:

```bash
# Terminal 2
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker indexing --url redis://localhost:6379

# Terminal 3
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker indexing --url redis://localhost:6379
```

### GitHub App vs Personal Access Token

| | PAT | GitHub App |
|--|-----|-----------|
| Rate limit | 5,000 req/hr | 15,000 req/hr |
| Setup | Simple | Requires app registration |
| Security | User credentials | App credentials (per-installation) |
| Large repos | May hit limits | Recommended |

For repositories with thousands of files, use GitHub App credentials.

---

## 9. Troubleshooting

### Webhook returns 401

Your `GITHUB_WEBHOOK_SECRET` doesn't match the secret configured in GitHub. They must be byte-for-byte identical. Regenerate the secret in GitHub settings and update your `.env` file.

### Worker processes 0 files

Check that the commit SHA in the webhook payload exists on GitHub. If using `simulate_webhook.py`, use a real commit SHA from your repository — not a fabricated hash.

Check also that the files have supported extensions. Review `SUPPORTED_EXTENSIONS` in your config.

### Chunk count drops after re-index

This was a known bug (now fixed). The old behavior used `ON CONFLICT DO NOTHING` in `upsert_chunks`, which silently skipped re-inserting soft-deleted chunks. The fix uses `ON CONFLICT DO UPDATE SET is_deleted = FALSE`. If you're on an older version, pull the latest code.

### Reranker times out on first search

The cross-encoder model (`ms-marco-MiniLM-L-6-v2`) is downloaded on first use (~60MB). The first request will take 10–20 seconds. Subsequent requests use the cached model at ~100ms.

To pre-warm the reranker on startup, add a startup event to `app.py`:

```python
@app.on_event("startup")
async def warm_reranker():
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import SearchResult
    # Dummy call to trigger model load
    rerank("warmup", [], top_n=0)
```

### RQ worker crashes on macOS

This is a known macOS issue with Python's `fork()` and Objective-C runtime. Always prefix the worker command with:

```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker indexing --url redis://localhost:6379
```

### `No module named 'src'` errors

Always run scripts from the project root with `PYTHONPATH=.`:

```bash
PYTHONPATH=. python scripts/deploy_check.py
PYTHONPATH=. python scripts/full_index.py
```

### `pgvector` extension not found

The migration SQL runs `CREATE EXTENSION IF NOT EXISTS vector`. This requires pgvector to be installed in your PostgreSQL instance. Use the `pgvector/pgvector:pg16` Docker image (already configured in `docker-compose.yml`) — it includes pgvector pre-installed.

### Webhook events stay in "queued" status

The pipeline updates the webhook event to `done` at the end of processing via `update_webhook_status`. If events stay `queued`, check that:
1. The RQ worker is running and processing jobs (check worker logs)
2. The `delivery_id` in the job payload matches the `delivery_id` in `webhook_events`
3. No uncaught exception is preventing the status update at the end of the pipeline

### Activity Feed shows no events

Push events are only logged if the webhook passes HMAC verification and the target branch matches `GITHUB_DEFAULT_BRANCH`. Check that the branch name matches exactly (e.g., `main` vs `master`).

---

*Last updated: Day 8 — all 8 phases complete. 53/53 tests passing.*
