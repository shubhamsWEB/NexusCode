# Cross-Repository Search & Scoped API Keys

NexusCode can intelligently search **across multiple repositories in a single query**, routing each request only to the repos that are likely relevant — and enforcing per-team access controls via scoped API keys.

---

## The Problem

An organisation with 100+ indexed repos faces two compounding issues:

| Problem | Impact |
|---------|--------|
| Searching all repos blindly | Exhausts the LLM token budget with irrelevant noise |
| Team A accidentally sees Team B's private repos | Security/compliance violation |
| Frontend developer's query returns platform infra code | Low-quality answers |

---

## The Solution — Two Complementary Layers

```
Request
  │
  ▼
┌──────────────────────────────────────────────────────┐
│  LAYER 1: REPO SCOPE GATE (API key)                  │
│  "Which repos is this key allowed to see?"           │
│  Enforced before any scoring or search happens.      │
│  Repos outside the allowed set are never touched.    │
└─────────────────────────┬────────────────────────────┘
                          │  (only allowed repos pass through)
                          ▼
┌──────────────────────────────────────────────────────┐
│  LAYER 2: REPO ROUTER (query-time ranking)           │
│  "Which of my allowed repos are relevant to THIS     │
│   specific query?"                                   │
│  Scores repos by (semantic centroid cosine +         │
│  keyword Jaccard), searches only the top-N.          │
└─────────────────────────┬────────────────────────────┘
                          │  (top-N relevant repos)
                          ▼
┌──────────────────────────────────────────────────────┐
│  PARALLEL SEARCH + RERANK                            │
│  Search each repo in parallel with per-repo          │
│  token budgets, then assemble multi-repo context.    │
└──────────────────────────────────────────────────────┘
```

Together: **only allowed, relevant repos are searched** — with proportional token budgets and clear multi-repo context grouping.

---

## How Cross-Repo Routing Works

### Repo Summaries

After each indexing job completes, NexusCode computes a **repo summary** and stores it in the `repo_summaries` table:

| Field | What it stores |
|-------|----------------|
| `centroid_embedding` | Average of all chunk embeddings — a 1536-dim vector representing the repo's "semantic centre" |
| `tech_stack_keywords` | Top-50 most frequent tokens from enriched content (e.g. `["fastapi", "sqlalchemy", "redis"]`) |
| `language_distribution` | `{"python": 0.72, "typescript": 0.28}` |
| `chunk_count` | Total indexed chunks |

Summaries are cached in Redis (`repo_router:summaries`, TTL 120s) and invalidated after every indexing job.

### Routing Algorithm

When a query arrives with no specific repo pinned:

1. **Load repo summaries** (from Redis cache → PostgreSQL)
2. **Scope gate** — filter to `allowed_repos` from the API key (skips scoring entirely for out-of-scope repos)
3. **Score each repo** — combined score:
   ```
   score = (0.75 × cosine_similarity(query_vector, centroid))
         + (0.25 × jaccard_similarity(query_tokens, tech_stack_keywords))
   ```
4. **Filter** repos below `cross_repo_min_score` (default 0.20)
5. **Sort** descending; take top `cross_repo_max_repos` (default 5)
6. If `current_repo` hint is provided, that repo is always placed first

### Budget Allocation

Each included repo gets a proportional share of the total token budget, with a floor guarantee:

```
floor = max(500 tokens, total_budget × 10%)
remaining = total_budget − (floor × num_repos)
repo_budget = floor + (remaining × repo_score / sum_of_scores)
```

This ensures even a low-scoring repo gets 500 tokens of context — it's never completely silenced.

### Multi-Repo Context Format

Results from different repos are visually separated:

```
╔════════════════════════════════════════════════════════╗
║  REPO: myorg/auth-service  [python]                    ║
╚════════════════════════════════════════════════════════╝
══ File: src/auth/routes.py ══
[lines 10-45] POST /users/login
…

╔════════════════════════════════════════════════════════╗
║  REPO: myorg/frontend  [typescript]                    ║
╚════════════════════════════════════════════════════════╝
══ File: src/api/auth.ts ══
…
```

Repos are ordered by their highest-scored chunk (most relevant first).

---

## Scoped API Keys

### What They Are

A scoped API key is a long-lived credential that restricts which repos an agent, team, or client can access. It's the primary mechanism for multi-team deployments.

| Property | Details |
|----------|---------|
| Storage | SHA-256 hash only — the raw key is shown once at creation and never stored |
| Scope | A list of `"owner/name"` strings the key can access; empty list = all repos (admin key) |
| Enforcement | All 7 internal retrieval tools check scope before touching any data |
| Auth methods | `X-Api-Key` header **or** `?api_key=` query param (for SSE URLs) |

### Creating a Scoped Key

```bash
# Admin key — access to all repos
curl -X POST http://localhost:8000/api-keys \
  -H "Content-Type: application/json" \
  -d '{"name": "admin-key", "description": "Full access admin key"}'

# Scoped key — frontend team sees only their 3 repos
curl -X POST http://localhost:8000/api-keys \
  -H "Content-Type: application/json" \
  -d '{
    "name": "frontend-team",
    "description": "Frontend squad — web-app, design-system, api-gateway",
    "allowed_repos": ["myorg/web-app", "myorg/design-system", "myorg/api-gateway"]
  }'
```

**Response** (raw key shown **once only**):
```json
{
  "id": 3,
  "raw_key": "abc123xyz...",
  "name": "frontend-team",
  "allowed_repos": ["myorg/web-app", "myorg/design-system", "myorg/api-gateway"],
  "created_at": "2026-03-10T09:00:00Z"
}
```

> **Copy the `raw_key` now.** It is never stored and cannot be retrieved again. If lost, delete and recreate the key.

### Listing Keys

```bash
curl http://localhost:8000/api-keys
```

```json
[
  {
    "id": 1,
    "name": "frontend-team",
    "description": "Frontend squad",
    "allowed_repos": ["myorg/web-app", "myorg/design-system"],
    "created_at": "2026-03-10T09:00:00Z",
    "last_used_at": "2026-03-10T11:42:00Z"
  }
]
```

Key hashes are never returned. `last_used_at` is updated on every authenticated request.

### Deleting a Key

```bash
curl -X DELETE http://localhost:8000/api-keys/3
```

### Using a Key — MCP Client

Embed the key in the MCP URL (works with Streamable HTTP clients such as Cursor and Claude Code):

```json
{
  "mcpServers": {
    "nexuscode": {
      "type": "streamable-http",
      "url": "http://nexuscode-server:8000/mcp?api_key=abc123xyz..."
    }
  }
}
```

Or as an HTTP header (for REST API calls):

```bash
curl -X POST http://localhost:8000/ask \
  -H "X-Api-Key: abc123xyz..." \
  -H "Content-Type: application/json" \
  -d '{"query": "How does the login flow work?"}'
```

### Scope Enforcement

Scope is enforced at every retrieval layer — not just at the HTTP boundary:

| Tool | Enforcement method |
|------|--------------------|
| `search_codebase` | Cross-repo router only considers allowed repos; single-repo search filtered by SQL |
| `get_symbol` | SQL `WHERE (repo_owner \|\| '/' \|\| repo_name) = ANY(:allowed)` |
| `find_callers` | Post-filters `_keyword_search` results by allowed set |
| `get_file_context` | SQL filter on both symbol and chunk queries |
| `get_agent_context` | SQL filter on focal-file chunks; in-memory filter on semantic results |
| `plan_implementation` | Passes `allowed_repos` down to the planner's agent loop |
| `ask_codebase` | Passes `allowed_repos` down to the ask agent's tool calls |

**Priority:** pinned repo (`repo=` parameter) > API key scope > unrestricted (all repos)

---

## Dashboard — API Key Scopes Page

The **🗝️ API Key Scopes** page in the Streamlit dashboard (`http://localhost:8501`) provides:

1. **Existing Keys table** — name, allowed repos, created / last-used timestamps, delete button
2. **Create New Key form** — name, description, multiselect of indexed repos from `/repos`
3. **Key reveal banner** — after creation, the raw key is displayed until you dismiss it
4. **Usage instructions** — copy-paste MCP config snippet with your API URL

---

## Dashboard — Routing Status

The **📦 Repos** page has a collapsible **Cross-Repo Routing Status** section that shows:

- `centroid_embedding` status (computed / pending) for each repo
- `chunk_count` and top tech-stack keywords
- Language distribution
- Per-repo **Refresh** button to trigger an on-demand summary recomputation

---

## Team Workflow Example

```
Admin (DevOps):
  POST /api-keys
  {"name": "frontend-team",
   "allowed_repos": ["myorg/frontend", "myorg/auth-service", "myorg/user-service"]}
  → raw_key: "abc123..."

Frontend developer configures Claude Code:
  ~/.claude/settings.json:
  {
    "mcpServers": {
      "nexuscode": {
        "type": "streamable-http",
        "url": "http://nexuscode:8000/mcp?api_key=abc123..."
      }
    }
  }

Developer queries:
  search_codebase("how do I call the user login endpoint",
                   current_repo="myorg/frontend")

  → Router considers only: frontend, auth-service, user-service
  → Scores: auth-service=0.72 (high), user-service=0.41 (medium), frontend=0.31
  → current_repo hint → frontend placed first regardless
  → Parallel search across all 3 with proportional budgets
  → Returns: frontend context + auth-service login route + user-service user model
```

---

## Configuration Settings

All settings are in `src/config.py` and can be overridden via environment variables:

| Setting | Default | Description |
|---------|---------|-------------|
| `CROSS_REPO_ENABLED` | `true` | Master toggle for cross-repo routing |
| `CROSS_REPO_MAX_REPOS` | `5` | Max repos searched per query within scope |
| `CROSS_REPO_MIN_SCORE` | `0.20` | Minimum combined score to include a repo |
| `CROSS_REPO_KEYWORD_WEIGHT` | `0.25` | Weight of keyword Jaccard score |
| `CROSS_REPO_SEMANTIC_WEIGHT` | `0.75` | Weight of centroid cosine score |
| `CROSS_REPO_ROUTER_CACHE_TTL` | `120` | Redis TTL for router summaries cache (seconds) |
| `CROSS_REPO_SUMMARY_UPDATE_MIN_CHUNKS` | `10` | Min chunks before centroid is computed |
| `API_KEY_HEADER` | `X-Api-Key` | HTTP header name for API key auth |
| `API_KEY_QUERY_PARAM` | `api_key` | URL query param fallback (for SSE URLs) |
| `API_KEY_CACHE_TTL` | `300` | Redis TTL for key→scope cache (seconds) |

---

## Graceful Degradation

| Scenario | Behaviour |
|----------|-----------|
| `repo_summaries` table is empty (fresh install) | Falls back to naive all-repo search |
| Repo has fewer than 10 chunks | Skipped by router (centroid not reliable) |
| Redis unavailable | Router falls back to DB; scope middleware falls back to DB |
| `cross_repo_enabled = false` | Single-repo path only; cross-repo tool params ignored |
| Invalid or missing API key | Returns HTTP 401; Redis cache checked first to minimise DB load |

---

## Database Tables

### `repo_summaries`

```sql
CREATE TABLE repo_summaries (
    repo_owner            TEXT NOT NULL,
    repo_name             TEXT NOT NULL,
    centroid_embedding    vector(1536),         -- AVG of all chunk embeddings
    tech_stack_keywords   TEXT[]  DEFAULT '{}', -- top-50 frequent tokens
    language_distribution JSONB   DEFAULT '{}', -- {"python": 0.72, "ts": 0.28}
    chunk_count           INTEGER DEFAULT 0,
    updated_at            TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (repo_owner, repo_name)
);
```

Updated automatically after every successful indexing job (non-blocking `asyncio.create_task`). Invalidates the `repo_router:summaries` Redis cache on each update.

### `api_key_scopes`

```sql
CREATE TABLE api_key_scopes (
    id            SERIAL PRIMARY KEY,
    key_hash      TEXT NOT NULL UNIQUE,   -- SHA-256(raw_key), raw never stored
    name          TEXT NOT NULL,
    description   TEXT,
    allowed_repos TEXT[] DEFAULT '{}',    -- empty = admin (all repos)
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_used_at  TIMESTAMP WITH TIME ZONE
);
```

`last_used_at` is updated on every authenticated request (best-effort, non-blocking).

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api-keys` | Create a new scoped key — returns `raw_key` once |
| `GET` | `/api-keys` | List all keys (no hashes, no raw keys) |
| `DELETE` | `/api-keys/{id}` | Delete a key by ID |
| `GET` | `/repo-summaries` | List routing summaries for all repos |
| `POST` | `/repos/{owner}/{name}/refresh-summary` | Trigger on-demand centroid recomputation |

See [api-reference.md](./api-reference.md) for full request/response schemas.
