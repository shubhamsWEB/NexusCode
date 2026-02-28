# MCP Server Access & Authentication

NexusCode exposes all 8 codebase intelligence tools via the **Model Context Protocol (MCP)** using SSE transport. This page explains how to generate auth tokens, connect Claude Desktop, and use each tool.

---

## MCP Endpoint

```
http://localhost:8000/mcp
```

The MCP server uses **SSE (Server-Sent Events)** transport — the same protocol used by Claude Desktop and most MCP clients.

- SSE stream: `GET  http://localhost:8000/mcp/sse`
- Message posting: `POST http://localhost:8000/mcp/messages/`

---

## Getting an MCP Token

NexusCode uses **JWT Bearer tokens** to authenticate MCP connections.

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

NexusCode follows the MCP SSE transport spec. Any compliant client can connect:

```
Transport:    SSE
Base URL:     http://localhost:8000/mcp
SSE stream:   GET /mcp/sse        (Authorization: Bearer <token>)
Messages:     POST /mcp/messages/ (Authorization: Bearer <token>)
```

---

## Available MCP Tools (8 total)

### 1. `search_codebase`
Hybrid semantic + keyword search across indexed repos.

```
search_codebase(
  query:    "authentication middleware"
  repo:     "owner/name"    # optional
  language: "python"        # optional filter
  top_k:    5               # 1-20, default 5
  mode:     "hybrid"        # "semantic" | "keyword" | "hybrid"
)
```

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

### 6. `plan_implementation`
Full grounded implementation plan: web research + codebase context → exact file paths, steps, pseudocode, risks.

```
plan_implementation(
  query:        "Add Redis caching to the embedding step"
  repo:         "owner/name"  # optional
  web_research: true          # default true
  model:        "claude-sonnet-4-6"  # optional
)
```

### 7. `ask_codebase`
Answer natural-language questions in mentor tone with inline code citations.

```
ask_codebase(
  question: "How does the webhook processing pipeline work?"
  repo:     "owner/name"  # optional
  model:    "claude-sonnet-4-6"  # optional
)
```

### 8. `list_skills`
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
| `401 Unauthorized` from MCP | Token expired or wrong — regenerate with `POST /auth/token` |
| Tools not appearing in Claude Desktop | Restart Claude Desktop after editing `claude_desktop_config.json` |
| `Connection refused` | Server not running — `uvicorn src.api.app:app --port 8000` |
| SSE connection drops immediately | Check the `Authorization` header is present in the config |
| Tools return empty results | No repos indexed yet — see [connecting-github.md](./connecting-github.md) |
