# MCP Server Access & Authentication

NexusCode exposes a **core codebase-context MCP profile** by default at `/mcp`, and a **full MCP profile** at `/mcp/full`. This page explains how to generate auth tokens, connect Claude Desktop or Cursor-compatible clients, and use each profile.

---

## MCP Endpoint

Core profile:
```
http://localhost:8000/mcp
```

Full profile:
```
http://localhost:8000/mcp/full
```

The MCP server uses **Streamable HTTP** (MCP 2025-03-26 spec).

- Primary core endpoint: `POST http://localhost:8000/mcp`
- Full endpoint: `POST http://localhost:8000/mcp/full`
- Legacy SSE docs elsewhere in the repo are outdated for local client setup.

---

## Authentication Options

NexusCode supports two authentication mechanisms for MCP connections:

| Method | Best for | Scope enforcement |
|--------|----------|-------------------|
| **Scoped API key** (recommended) | Multi-team deployments, long-lived agent configs | Per-team allowed repos — persisted in DB |
| **JWT Bearer token** | Dev/CI access, short-lived sessions | Per-request repo claim |

---

## Scoped API Keys (Recommended)

Scoped API keys permanently bind a credential to a set of allowed repos. All retrieval tools
enforce the scope automatically — out-of-scope repos are never searched.

### Create a key

```bash
# Admin key — access to all repos
curl -X POST http://localhost:8000/api-keys \
  -H "Content-Type: application/json" \
  -d '{"name": "admin", "description": "Full access"}'

# Scoped key — frontend team sees only their repos
curl -X POST http://localhost:8000/api-keys \
  -H "Content-Type: application/json" \
  -d '{
    "name": "frontend-team",
    "description": "Frontend squad",
    "allowed_repos": ["myorg/frontend", "myorg/auth-service", "myorg/user-service"]
  }'
```

**Response** — raw key shown **once only**:
```json
{
  "id": 3,
  "raw_key": "abc123xyz...",
  "name": "frontend-team",
  "allowed_repos": ["myorg/frontend", "myorg/auth-service", "myorg/user-service"]
}
```

> **Copy `raw_key` immediately.** It is never stored and cannot be retrieved again.

### Connect MCP client with a scoped key

Append `?api_key=<key>` to the MCP URL:

```json
{
  "mcpServers": {
    "nexuscode": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp?api_key=abc123xyz..."
    }
  }
}
```

For REST calls, use the `X-Api-Key` header:

```bash
curl -X POST http://localhost:8000/ask \
  -H "X-Api-Key: abc123xyz..." \
  -H "Content-Type: application/json" \
  -d '{"query": "How does the login flow work?"}'
```

### Manage keys

```bash
# List keys (hashes never returned)
curl http://localhost:8000/api-keys

# Delete a key by ID
curl -X DELETE http://localhost:8000/api-keys/3
```

The **🗝️ API Key Scopes** page in the Streamlit dashboard provides a full UI for key management,
including the one-time key reveal banner and copy-paste MCP config snippets.

See [cross-repo-search.md](./cross-repo-search.md) for the complete cross-repo routing and scoped
key guide.

---

## Getting an MCP Token (JWT)

NexusCode also supports **JWT Bearer tokens** for authenticated MCP connections.

### Generate a token (dev/local)

```bash
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"sub": "my-agent", "repos": []}'
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 28800
}
```

**Parameters:**

| Field | Description |
|---|---|
| `sub` | Subject identifier — any string (e.g. `"dev"`, `"claude-desktop"`, `"ci-bot"`) |
| `repos` | List of `"owner/name"` strings the token can access. Empty list `[]` = access all repos |

Tokens expire after **8 hours** (configurable via `JWT_EXPIRY_HOURS`).

### Token with restricted repo access

```bash
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{
    "sub": "frontend-team",
    "repos": ["myorg/web-app", "myorg/design-system"]
  }'
```

This token can only search/plan/ask against those two repos.

### Verify a token

```bash
curl http://localhost:8000/auth/verify \
  -H "Authorization: Bearer <token>"
# → {"sub":"my-agent","repos":[],"exp":...,"valid":true}
```

## Connecting Any MCP Client

NexusCode follows the MCP Streamable HTTP transport spec. Any compliant client can connect:

```
Transport:    Streamable HTTP
Endpoint:     POST http://localhost:8000/mcp
Auth:         Authorization: Bearer <token>
              or ?api_key=<key>
```

---

## MCP Profiles

### Core profile (`/mcp`)

This is the default endpoint intended for Cursor, Claude Desktop, and other external agents that mainly need grounded codebase context.

Available tools:
- `search_codebase`
- `get_symbol`
- `find_callers`
- `get_file_context`
- `get_agent_context`
- `get_semantic_context`

### Full profile (`/mcp/full`)

This preserves the broader NexusCode MCP surface for advanced/internal clients.

Available tools include the full planning, Q&A, and admin/evolution surface in addition to the core tools.

## Core MCP Tools

### 1. `search_codebase`
Hybrid semantic + keyword search with intelligent cross-repo routing.

```
search_codebase(
  query:        "authentication middleware"
  repo:         "owner/name"    # optional — omit for automatic cross-repo routing
  current_repo: "owner/name"    # optional — always included first when cross-repo routing
  language:     "python"        # optional filter
  top_k:        5               # 1-20, default 5 per repo
  mode:         "hybrid"        # "semantic" | "keyword" | "hybrid"
  cross_repo:   true            # default true — enable intelligent multi-repo routing
)
```

When `repo` is omitted and `cross_repo=true`, the router scores all allowed repos by semantic
similarity to the query (centroid cosine + keyword Jaccard) and searches only the top-N. Results
from different repos are assembled into a single context with clear repo headers.

### 2. `get_symbol`
Fuzzy symbol lookup — like IDE "Go to Definition".

```
get_symbol(
  name: "UserService.authenticate"
  repo: "owner/name"  # optional
)
```

### 3. `find_callers`
Who calls a function? Multi-hop call graph traversal.

```
find_callers(
  symbol: "authenticate"
  repo:   "owner/name"  # optional
  depth:  1             # 1-3 hops
)
```

### 4. `get_file_context`
Complete structural map of a file: symbols, imports, what imports it.

```
get_file_context(
  path:         "src/auth/service.py"
  repo:         "owner/name"  # optional
  include_deps: true
)
```

### 5. `get_agent_context`
Pre-assembled, token-budget-aware context for a task. Call this before starting implementation.

```
get_agent_context(
  task:         "Add rate limiting to the search endpoint"
  focal_files:  ["src/api/app.py", "src/retrieval/searcher.py"]
  token_budget: 8000
  repo:         "owner/name"  # optional
)
```

## Full-only MCP Tools

### `plan_implementation`
Full grounded implementation plan: web research + codebase context → exact file paths, steps, pseudocode, risks.

```
plan_implementation(
  query:        "Add Redis caching to the embedding step"
  repo:         "owner/name"  # optional
  web_research: true          # default true
  model:        "claude-sonnet-4-6"  # optional
)
```

### `ask_codebase`
Answer natural-language questions in mentor tone with inline code citations.

```
ask_codebase(
  question: "How does the webhook processing pipeline work?"  # preferred
  query:    "How does the webhook processing pipeline work?"  # compatibility alias
  repo:     "owner/name"  # optional
  model:    "claude-sonnet-4-6"  # optional
)
```

### `list_skills`
Discover available skills by name and description.

```
list_skills(
  filter: "planning"  # optional keyword filter
)
```

---

## Example: Using Tools in Claude Desktop

Once connected, you can use the tools in natural language:

> "Search the nexuscode codebase for authentication middleware"

Claude will call `search_codebase` and return results.

> "Generate an implementation plan for adding Redis caching to the embedding step"

Claude will call `plan_implementation` and return a full structured plan with file paths, pseudocode, and steps.

---

## Using MCP Tools Directly via API

If you prefer REST over MCP, every tool has a direct HTTP equivalent:

| MCP Tool | REST Endpoint |
|---|---|
| `search_codebase` | `POST /search` |
| `plan_implementation` | `POST /plan` |
| `ask_codebase` | `POST /ask` |
| `get_symbol` | `GET /mcp` (via MCP) |
| `list_skills` | `GET /skills` |

See [api-reference.md](./api-reference.md) for the full REST API.

---

## Security Notes

- **Never commit tokens** to source control. Generate fresh tokens per client.
- For production, set `JWT_SECRET` to a strong random value (min 32 chars):
  ```bash
  JWT_SECRET=$(openssl rand -hex 32)
  ```
- Tokens are HS256-signed JWTs. They cannot be forged without `JWT_SECRET`.
- Restrict tokens to specific repos using the `repos` claim to limit blast radius.
- Tokens expire in 8 hours by default. Set `JWT_EXPIRY_HOURS` to change this.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `401 Unauthorized` from MCP | JWT expired — regenerate with `POST /auth/token`; or API key is wrong — check `?api_key=` value |
| Tools not appearing in Claude Desktop | Restart Claude Desktop after editing `claude_desktop_config.json` |
| `Connection refused` | Server not running — `uvicorn src.api.app:app --port 8000` |
| SSE connection drops immediately | Check the `Authorization` header or `?api_key=` param is present |
| Tools return empty results | No repos indexed yet — see [connecting-github.md](./connecting-github.md) |
| Search ignores scoped repos | Ensure `api_key` is in the SSE URL query param (not a header) for SSE connections |
| Cross-repo search returns one repo only | Repo summaries not yet computed — trigger with `POST /repos/{owner}/{name}/refresh-summary` |
