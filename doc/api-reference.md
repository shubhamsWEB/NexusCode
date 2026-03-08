# REST API Reference

**Base URL:** `http://localhost:8000`
**Interactive docs:** `http://localhost:8000/docs` (Swagger UI)
**OpenAPI schema:** `http://localhost:8000/openapi.json`

All endpoints return JSON unless noted otherwise. Authentication is only required for the MCP server SSE endpoint (see [mcp-access.md](./mcp-access.md)). All request bodies use `Content-Type: application/json`.

---

## Table of Contents

1. [Health & Status](#health--status)
2. [Repository Management](#repository-management)
3. [Search](#search)
4. [Ask Mode](#ask-mode)
5. [Planning Mode](#planning-mode)
6. [Workflows](#workflows)
7. [Documents (PDF)](#documents-pdf)
8. [Knowledge Graph](#knowledge-graph)
9. [Agent Roles](#agent-roles)
10. [External MCP Servers](#external-mcp-servers)
11. [Skills](#skills)
12. [History](#history)
13. [Webhooks & Events](#webhooks--events)
14. [Authentication](#authentication)
15. [Statistics](#statistics)
16. [Error Responses](#error-responses)
17. [SSE Streaming Guide](#sse-streaming-guide)

---

## Health & Status

### `GET /health`

Server health check with index statistics.

```bash
curl http://localhost:8000/health
```

**Response:**
```json
{
  "status": "ok",
  "repos": 3,
  "chunks": 12450,
  "symbols": 890,
  "files": 234
}
```

---

### `GET /models`

List LLM models available based on configured API keys.

```bash
curl http://localhost:8000/models
```

**Response:**
```json
{
  "available": ["claude-sonnet-4-6", "claude-opus-4-6", "gpt-4o", "gpt-4o-mini", "grok-3"]
}
```

Models only appear if the corresponding API key is set in the environment. See [configuration.md](./configuration.md) for key names.

---

## Repository Management

### `GET /repos`

List all registered repositories with per-repo statistics.

```bash
curl http://localhost:8000/repos
```

**Response:**
```json
[
  {
    "id": 1,
    "owner": "myorg",
    "name": "my-repo",
    "branch": "main",
    "status": "indexed",
    "last_indexed": "2026-03-08T14:30:00Z",
    "chunks": 4521,
    "symbols": 312,
    "files": 89
  }
]
```

---

### `POST /repos`

Register a new repository (and optionally trigger immediate indexing).

**Body:**
```json
{
  "owner":     "your-org",
  "name":      "your-repo",
  "branch":    "main",
  "index_now": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `owner` | string | yes | GitHub organization or username |
| `name` | string | yes | Repository name |
| `branch` | string | no | Branch to index. Default: `"main"` |
| `index_now` | bool | no | Trigger full index immediately. Default: `false` |

**Response `201 Created`:**
```json
{
  "repo":   "your-org/your-repo",
  "status": "queued",
  "job_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

### `GET /repos/{owner}/{name}`

Get a single repository's details and statistics.

```bash
curl http://localhost:8000/repos/myorg/my-repo
```

**Response:**
```json
{
  "owner": "myorg",
  "name": "my-repo",
  "branch": "main",
  "status": "indexed",
  "last_indexed": "2026-03-08T14:30:00Z",
  "chunks": 4521,
  "symbols": 312,
  "files": 89
}
```

**404** if not registered.

---

### `POST /repos/{owner}/{name}/index`

Trigger a full re-index job for an existing repository.

```bash
curl -X POST http://localhost:8000/repos/myorg/my-repo/index
```

**Response:**
```json
{"job_id": "uuid", "status": "queued"}
```

---

### `DELETE /repos/{owner}/{name}`

Hard-delete a repository and all its indexed chunks, symbols, and merkle nodes.

```bash
curl -X DELETE http://localhost:8000/repos/myorg/my-repo
```

**Response `200`:**
```json
{"deleted": "myorg/my-repo"}
```

---

### `GET /repos/{owner}/{name}/stats`

Detailed per-language and per-file statistics for a repository.

```bash
curl http://localhost:8000/repos/myorg/my-repo/stats
```

**Response:**
```json
{
  "chunks_by_language": {"python": 2100, "typescript": 1800, "go": 621},
  "top_files_by_chunks": [
    {"file": "src/api/app.py", "chunks": 45},
    {"file": "src/retrieval/searcher.py", "chunks": 38}
  ],
  "total_tokens": 1240000
}
```

---

## Search

### `POST /search`

Hybrid semantic + keyword search across indexed repositories, with optional cross-encoder reranking.

**Body:**
```json
{
  "query":        "authentication middleware verify token",
  "repo":         "owner/name",
  "language":     "python",
  "top_k":        5,
  "mode":         "hybrid",
  "rerank":       true,
  "token_budget": 8000,
  "preset":       "balanced"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | Natural-language or code query |
| `repo` | string | no | Filter to `"owner/name"`. Omit to search all repos |
| `language` | string | no | Filter by language (e.g. `"python"`, `"typescript"`) |
| `top_k` | int | no | Max results to return. Default: `5` |
| `mode` | string | no | `"hybrid"` (default), `"semantic"`, or `"keyword"` |
| `rerank` | bool | no | Run cross-encoder reranking. Default: `true` |
| `token_budget` | int | no | Max tokens for assembled context. Default: `8000` |
| `preset` | string | no | Quality preset: `"fast"`, `"balanced"` (default), `"thorough"` |

**Search Quality Presets:**

| Preset | top_k | Reranking | Use case |
|--------|-------|-----------|----------|
| `fast` | 5 | No | Quick lookups, low latency |
| `balanced` | 10 | Yes | Standard queries |
| `thorough` | 20 | Yes | Deep analysis, comprehensive coverage |

**Response:**
```json
{
  "query":   "authentication middleware verify token",
  "mode":    "hybrid",
  "results": [
    {
      "file":          "src/mcp/auth.py",
      "repo":          "owner/name",
      "symbol":        "require_auth",
      "kind":          "function",
      "scope":         "require_auth",
      "lines":         "65-82",
      "language":      "python",
      "score":         0.8931,
      "rerank_score":  4.2156,
      "quality_score": 0.9851,
      "commit":        "a1b2c3d",
      "preview":       "async def require_auth(request: Request) -> None:..."
    }
  ],
  "context":       "# src/mcp/auth.py:65-82\nasync def require_auth...",
  "tokens_used":   1240,
  "retrieval_log": "semantic:8 keyword:6 merged:10 reranked:5"
}
```

---

## Ask Mode

### `POST /ask`

Answer a natural-language question about the indexed codebase using an AI agent with tool access.

**Body:**
```json
{
  "query":      "How does the webhook processing pipeline work end-to-end?",
  "repo_owner": "your-org",
  "repo_name":  "your-repo",
  "stream":     false,
  "session_id": "3f2a1b9c-...",
  "model":      "claude-sonnet-4-6"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | Your question about the codebase |
| `repo_owner` | string | no | Restrict search to this repo owner |
| `repo_name` | string | no | Restrict search to this repo name |
| `stream` | bool | no | Stream response via SSE. Default: `false` |
| `session_id` | string | no | Resume an existing chat session for follow-up questions |
| `model` | string | no | LLM model to use. Default: highest-priority configured model |

**Response (sync, `stream: false`):**
```json
{
  "answer":          "The webhook pipeline starts when GitHub sends a POST to /webhook...",
  "cited_files":     ["src/github/webhook.py:42-80", "src/pipeline/pipeline.py:15-60"],
  "follow_up_hints": ["What is the Merkle diff algorithm?", "How does chunking work?"],
  "quality_score":   0.87,
  "elapsed_ms":      1840,
  "session_id":      "3f2a1b9c-uuid"
}
```

**Response (streaming, `stream: true`):**

Server-Sent Events stream:
```
data: {"type": "token",           "text": "The webhook pipeline"}
data: {"type": "token",           "text": " starts when"}
data: {"type": "answer_complete", "result": {...full sync response...}, "session_id": "uuid"}
```

---

## Planning Mode

### `POST /plan`

Generate a structured, grounded implementation plan for a development task.

**Body:**
```json
{
  "query":        "Add rate limiting to POST /search using a sliding window algorithm",
  "repo_owner":   "your-org",
  "repo_name":    "your-repo",
  "stream":       false,
  "web_research": true,
  "model":        "claude-sonnet-4-6"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | Feature or task description |
| `repo_owner` | string | no | Target repository owner |
| `repo_name` | string | no | Target repository name |
| `stream` | bool | no | Stream thinking + plan via SSE. Default: `false` |
| `web_research` | bool | no | Include web research on best practices. Default: `true` (Anthropic models only) |
| `model` | string | no | LLM model to use |

**Response (sync):**
```json
{
  "plan": {
    "title":   "Add Sliding-Window Rate Limiting to POST /search",
    "summary": "Implement a Redis-backed sliding window rate limiter...",
    "sparc": {
      "specification":  "Rate limit: 100 req/min per IP, 429 with Retry-After header...",
      "pseudocode":     "1. Extract client IP\\n2. Increment Redis counter...",
      "architecture":   "Middleware layer in FastAPI, Redis ZSET per key...",
      "refinements":    ["Consider X-Forwarded-For for proxied clients..."],
      "completion":     "Add to src/api/middleware.py, mount in app.py..."
    },
    "steps": [
      {
        "id":          1,
        "title":       "Create rate limiter middleware",
        "description": "Implement RedisRateLimiter class in src/api/middleware.py...",
        "files":       ["src/api/middleware.py", "src/api/app.py"],
        "effort":      "medium",
        "risk":        "low"
      }
    ],
    "affected_files":  ["src/api/middleware.py", "src/api/app.py", "src/config.py"],
    "risks":           ["Redis unavailability degrades to passthrough"],
    "estimated_steps": 4,
    "quality_score":   0.92
  },
  "elapsed_ms": 12400,
  "plan_id":    "uuid"
}
```

**Streaming events:**
```
data: {"type": "thinking",      "text": "...extended thinking text..."}
data: {"type": "token",         "text": "...plan token..."}
data: {"type": "plan_complete", "plan": {...full plan...}, "plan_id": "uuid"}
```

---

## Workflows

### `GET /workflows`

List all saved workflow definitions.

```bash
curl "http://localhost:8000/workflows?active_only=true"
```

| Param | Description |
|-------|-------------|
| `active_only` | If `true`, only return enabled workflows. Default: `false` |

**Response:**
```json
[
  {
    "id":          "uuid",
    "name":        "rca-automation",
    "description": "Automated root cause analysis for production incidents",
    "is_active":   true,
    "created_at":  "2026-03-08T10:00:00Z",
    "updated_at":  "2026-03-08T10:00:00Z",
    "run_count":   12
  }
]
```

---

### `POST /workflows`

Create or update a workflow. If a workflow with the same `name` already exists, it is updated.

**Body:**
```json
{
  "name":            "rca-automation",
  "description":     "Automated root cause analysis",
  "yaml_definition": "name: rca-automation\ndescription: ...\n..."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Unique workflow name (slug-style) |
| `description` | string | no | Human-readable description |
| `yaml_definition` | string | yes | Full YAML workflow definition |

**Response `201 Created`:**
```json
{
  "id":   "uuid",
  "name": "rca-automation"
}
```

**Response `200 OK`** (if name already exists — updated in place):
```json
{
  "id":      "uuid",
  "name":    "rca-automation",
  "updated": true
}
```

See [workflows.md](./workflows.md) for the full YAML DSL reference.

---

### `GET /workflows/{workflow_id}`

Get full workflow definition and its last 10 run summaries.

```bash
curl http://localhost:8000/workflows/uuid-or-name
```

**Response:**
```json
{
  "id":              "uuid",
  "name":            "rca-automation",
  "description":     "...",
  "yaml_definition": "name: rca-automation\n...",
  "is_active":       true,
  "recent_runs": [
    {
      "run_id":     "run-uuid",
      "status":     "completed",
      "started_at": "2026-03-08T14:30:00Z",
      "ended_at":   "2026-03-08T14:35:12Z",
      "tokens_used": 48920
    }
  ]
}
```

---

### `DELETE /workflows/{workflow_id}`

Delete a workflow and all its run history (including generated PDFs).

```bash
curl -X DELETE http://localhost:8000/workflows/uuid
```

**Response:**
```json
{"deleted": "rca-automation"}
```

---

### `POST /workflows/{workflow_id}/run`

Trigger a workflow run with an optional payload.

```bash
curl -X POST http://localhost:8000/workflows/rca-automation/run \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "service":            "payment-api",
      "environment":        "production",
      "severity":           "HIGH",
      "error_message":      "Connection pool exhausted",
      "stack_trace":        "at PaymentService.charge (payment.ts:45)",
      "affected_component": "checkout",
      "timestamp":          "2026-03-08T14:30:00Z"
    }
  }'
```

**Response (returns immediately):**
```json
{
  "run_id": "run-uuid",
  "status": "running"
}
```

Watch progress via SSE: `GET /workflows/runs/{run_id}/stream`

---

### `GET /workflows/runs`

List all workflow runs across all workflows.

```bash
curl "http://localhost:8000/workflows/runs?limit=20&workflow_id=uuid"
```

| Param | Description |
|-------|-------------|
| `limit` | Max runs to return. Default: `20` |
| `workflow_id` | Filter by workflow UUID |

**Response:**
```json
[
  {
    "run_id":      "run-uuid",
    "workflow_id": "wf-uuid",
    "workflow_name": "rca-automation",
    "status":      "completed",
    "started_at":  "2026-03-08T14:30:00Z",
    "ended_at":    "2026-03-08T14:35:12Z",
    "tokens_used": 48920,
    "trigger_payload": {"service": "payment-api", ...}
  }
]
```

---

### `GET /workflows/runs/{run_id}`

Get a specific run with all step outputs, timing, tokens, and generated documents.

```bash
curl http://localhost:8000/workflows/runs/run-uuid
```

**Response:**
```json
{
  "run_id":    "run-uuid",
  "status":    "completed",
  "started_at": "2026-03-08T14:30:00Z",
  "ended_at":   "2026-03-08T14:35:12Z",
  "tokens_used": 48920,
  "steps": [
    {
      "step_id":    "understand_error",
      "status":     "completed",
      "started_at": "2026-03-08T14:30:05Z",
      "ended_at":   "2026-03-08T14:31:20Z",
      "tokens_used": 12480,
      "output": {
        "text": "The error originates in PaymentService at...",
        "documents": [
          {
            "doc_id":     "doc-uuid",
            "filename":   "rca-payment-api-2026-03-08.pdf",
            "size_bytes": 45312
          }
        ]
      },
      "retries": 0,
      "error": null
    }
  ],
  "pending_checkpoints": [
    {
      "checkpoint_id": "cp-uuid",
      "step_id":       "approve_deploy",
      "prompt":        "Security review complete. Approve deployment?",
      "options":       ["Approve", "Reject", "Request more analysis"],
      "created_at":    "2026-03-08T14:33:00Z"
    }
  ]
}
```

---

### `GET /workflows/runs/{run_id}/stream`

Stream live progress events for a running (or recently completed) workflow via Server-Sent Events.

```bash
curl -N http://localhost:8000/workflows/runs/run-uuid/stream
```

**Events:**
```
data: {"type": "workflow_started",  "run_id": "run-uuid", "workflow": "rca-automation"}
data: {"type": "step_started",      "step_id": "understand_error", "role": "searcher"}
data: {"type": "step_token",        "step_id": "understand_error", "text": "..."}
data: {"type": "step_complete",     "step_id": "understand_error", "tokens": 12480, "output": "..."}
data: {"type": "checkpoint_created","checkpoint_id": "cp-uuid", "prompt": "Approve?"}
data: {"type": "checkpoint_resolved","checkpoint_id": "cp-uuid", "response": "Approve"}
data: {"type": "workflow_complete", "run_id": "run-uuid", "tokens_total": 48920}
data: {"type": "workflow_failed",   "run_id": "run-uuid", "error": "Step failed: ..."}
```

---

### `POST /workflows/checkpoints/{checkpoint_id}/respond`

Respond to a human checkpoint to resume a paused workflow.

```bash
curl -X POST http://localhost:8000/workflows/checkpoints/cp-uuid/respond \
  -H "Content-Type: application/json" \
  -d '{"response": "Approve"}'
```

**Body:**
```json
{"response": "Approve"}
```

`response` must be one of the options defined in the checkpoint step (`options` list), or any free text if no options were specified.

**Response:**
```json
{"status": "resumed", "run_id": "run-uuid"}
```

**404** if checkpoint not found or already resolved.

---

## Documents (PDF)

### `GET /documents/{doc_id}/download`

Download a generated PDF document by its ID.

```bash
curl http://localhost:8000/documents/doc-uuid/download -o report.pdf
```

**Response:**
- `Content-Type: application/pdf`
- `Content-Disposition: attachment; filename="rca-payment-api-2026-03-08.pdf"`
- Body: raw PDF bytes

**404** if the document doesn't exist or the parent workflow run was deleted:
```json
{"detail": "Document not found"}
```

`doc_id` values are returned in:
- Workflow run step output (`steps[].output.documents[].doc_id`)
- The agent's `generate_pdf` tool response (`download_url` field)

---

## Knowledge Graph

### `POST /graph/{owner}/{name}/build`

Build (or rebuild) the knowledge graph for a repository. Synchronous — waits up to 30 seconds.

```bash
curl -X POST http://localhost:8000/graph/myorg/my-repo/build
```

**Response:**
```json
{
  "nodes_created": 145,
  "edges_created": 412,
  "built_at":      "2026-03-08T14:30:00Z"
}
```

---

### `GET /graph/{owner}/{name}`

Retrieve the knowledge graph data for a repository.

```bash
curl "http://localhost:8000/graph/myorg/my-repo?view=all&max_nodes=200"
```

| Param | Description |
|-------|-------------|
| `view` | `"files"` (import edges only), `"symbols"` (symbol edges only), `"all"` (default) |
| `max_nodes` | Limit returned nodes for large graphs. Default: `200` |

**Response:**
```json
{
  "nodes": [
    {
      "id":    "src/auth/service.py",
      "label": "auth/service.py",
      "type":  "file",
      "color": "#4B8BBE",
      "size":  15
    },
    {
      "id":    "sym:AuthService.authenticate",
      "label": "authenticate",
      "type":  "symbol",
      "color": "#28A745",
      "size":  10
    }
  ],
  "edges": [
    {
      "source":     "src/auth/service.py",
      "target":     "src/models/user.py",
      "type":       "imports",
      "confidence": 1.0
    }
  ],
  "stats":    {"node_count": 45, "edge_count": 120},
  "built_at": "2026-03-08T14:30:00Z"
}
```

Node colors: Python=`#4B8BBE`, TypeScript/JS=`#F7DF1E`, Go=`#00ADD8`, Rust=`#FF4500`.
Edge types: `imports`, `defines`, `contains`, `calls`.

See [knowledge-graph.md](./knowledge-graph.md) for the full guide.

---

## Agent Roles

### `GET /agent-roles`

List all agent roles (built-in + custom overrides).

```bash
curl http://localhost:8000/agent-roles
```

**Response:**
```json
[
  {
    "name":          "supervisor",
    "system_prompt": "You are a Supervisor agent...",
    "default_tools": ["search_codebase", "get_symbol", "ask_codebase", "generate_pdf"],
    "require_search": false,
    "max_iterations": 5,
    "token_budget":   80000,
    "is_builtin":     true,
    "source":         "hardcoded"
  }
]
```

---

### `GET /agent-roles/tools`

List all available tools (internal + registered external MCP tools).

```bash
curl http://localhost:8000/agent-roles/tools
```

**Response:**
```json
{
  "internal": [
    "search_codebase", "get_symbol", "find_callers", "get_file_context",
    "get_agent_context", "plan_implementation", "ask_codebase", "generate_pdf"
  ],
  "external": ["web_search", "browse_url"]
}
```

---

### `GET /agent-roles/{name}`

Get a specific role's full configuration.

```bash
curl http://localhost:8000/agent-roles/supervisor
```

**Response:**
```json
{
  "name":          "supervisor",
  "system_prompt": "You are a Supervisor agent...",
  "default_tools": ["search_codebase", "get_symbol", "ask_codebase", "generate_pdf"],
  "require_search": false,
  "max_iterations": 5,
  "token_budget":   80000,
  "is_builtin":     true,
  "source":         "hardcoded"
}
```

If a DB override exists, `source` is `"database"` and the overridden fields are shown.

---

### `PUT /agent-roles/{name}`

Create or override an agent role. For built-in roles, this saves an override in the DB. For new names, creates a custom role.

**Body:**
```json
{
  "system_prompt": "You are a Security Auditor specialized in OWASP Top 10...",
  "instructions":  "Always cite the exact file path and line number for every finding.",
  "default_tools": ["search_codebase", "get_symbol", "find_callers", "get_file_context"],
  "require_search": true,
  "max_iterations": 8,
  "token_budget":   100000
}
```

| Field | Type | Description |
|-------|------|-------------|
| `system_prompt` | string | Full system prompt for the agent persona |
| `instructions` | string | Appended to system_prompt under `## Additional Instructions` |
| `default_tools` | list | Tool names available to this role |
| `require_search` | bool | Must call a search tool before answering |
| `max_iterations` | int | Max AgentLoop iterations. Range: 1–20 |
| `token_budget` | int | Max cumulative tool-result tokens |

**Response `200`:**
```json
{"name": "security-auditor", "saved": true}
```

---

### `DELETE /agent-roles/{name}`

Delete a custom role, or remove a DB override for a built-in role (restoring defaults).

```bash
curl -X DELETE http://localhost:8000/agent-roles/security-auditor
```

**Response:**
```json
{"deleted": "security-auditor"}
```

**400** if attempting to delete a built-in role with no override.

---

### `POST /agent-roles/{name}/reset`

Reset a built-in role to its hardcoded defaults by removing any DB override.

```bash
curl -X POST http://localhost:8000/agent-roles/supervisor/reset
```

**Response:**
```json
{"reset": "supervisor", "message": "Role reset to built-in defaults"}
```

---

## External MCP Servers

### `GET /mcp-servers`

List all registered external MCP servers.

```bash
curl http://localhost:8000/mcp-servers
```

**Response:**
```json
[
  {
    "id":          1,
    "name":        "Context7",
    "url":         "https://mcp.context7.com/sse",
    "description": "Library documentation lookup",
    "enabled":     true,
    "created_at":  "2026-03-01T10:00:00Z",
    "tool_count":  3
  }
]
```

---

### `POST /mcp-servers`

Register a new external MCP server.

**Body:**
```json
{
  "name":        "Context7",
  "url":         "https://mcp.context7.com/sse",
  "auth_header": "Bearer sk-...",
  "description": "Library documentation lookup",
  "enabled":     true
}
```

**Response `201 Created`:**
```json
{"id": 1, "name": "Context7", "tools_loaded": 3}
```

**409 Conflict** if the URL is already registered.

---

### `PATCH /mcp-servers/{id}`

Update one or more fields of an existing server record.

```bash
curl -X PATCH http://localhost:8000/mcp-servers/1 \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

Updatable fields: `name`, `enabled`, `auth_header`, `description`.

---

### `DELETE /mcp-servers/{id}`

Remove a server and evict its tools from the active bridge cache.

```bash
curl -X DELETE http://localhost:8000/mcp-servers/1
```

**Response:**
```json
{"deleted": 1, "tools_evicted": 8}
```

---

### `POST /mcp-servers/{id}/test`

Test the live connection for a saved server (does not modify DB).

```bash
curl -X POST http://localhost:8000/mcp-servers/1/test
```

**Response:**
```json
{"ok": true, "tools": ["get_package_info", "list_packages", "resolve_library"]}
```

---

### `POST /mcp-servers/test-url`

Test an unsaved server by URL before committing it to the registry.

```bash
curl -X POST http://localhost:8000/mcp-servers/test-url \
  -H "Content-Type: application/json" \
  -d '{"url": "http://localhost:3100/sse", "auth_header": "Bearer sk-..."}'
```

**Response:**
```json
{"ok": true, "tools": ["tool_a", "tool_b"]}
```

---

### `POST /mcp-servers/reload`

Reconnect all enabled servers and refresh all tool schemas in the active bridge cache.

```bash
curl -X POST http://localhost:8000/mcp-servers/reload
```

**Response:**
```json
{"tool_count": 11, "message": "Bridge reloaded — 11 tool(s) active"}
```

See [external-mcp-servers.md](./external-mcp-servers.md) for the full guide.

---

## Skills

### `GET /skills`

List all discovered skills (built-in + custom), with optional source filter.

```bash
curl http://localhost:8000/skills
curl "http://localhost:8000/skills?source=custom"
curl "http://localhost:8000/skills?source=builtin"
```

**Response:**
```json
{
  "skills": [
    {
      "name":         "plan-implementation",
      "description":  "Generate a complete grounded implementation plan...",
      "source":       "builtin",
      "source_label": "skills/"
    },
    {
      "name":         "security-audit",
      "description":  "Run an OWASP-aligned security audit...",
      "source":       "custom",
      "source_label": "custom_skills/"
    }
  ],
  "total": 5
}
```

---

### `GET /skills/{name}`

Get the full content of a skill's SKILL.md file.

```bash
curl http://localhost:8000/skills/plan-implementation
```

**Response:**
```json
{
  "name":        "plan-implementation",
  "description": "Generate a complete grounded implementation plan...",
  "content":     "---\nname: plan-implementation\n...",
  "source":      "builtin",
  "metadata":    {"author": "nexuscode", "version": "1.2"}
}
```

**404** if skill name not found.

---

### `POST /skills/reload`

Reload the skill cache from disk without restarting the server.

```bash
curl -X POST http://localhost:8000/skills/reload
```

**Response:**
```json
{"message": "Reloaded 6 skills"}
```

---

## History

### `GET /history/ask`

List recent Ask Mode chat sessions.

```bash
curl "http://localhost:8000/history/ask?limit=20"
```

**Response:**
```json
[
  {
    "session_id": "3f2a1b9c-uuid",
    "repo":       "myorg/my-repo",
    "created_at": "2026-03-08T14:20:00Z",
    "turn_count": 3,
    "first_query": "How does authentication work?"
  }
]
```

---

### `GET /history/ask/{session_id}`

Get a full Ask Mode session with all question-answer turns.

```bash
curl http://localhost:8000/history/ask/3f2a1b9c-uuid
```

**Response:**
```json
{
  "session_id": "3f2a1b9c-uuid",
  "repo":       "myorg/my-repo",
  "created_at": "2026-03-08T14:20:00Z",
  "turns": [
    {
      "role":        "user",
      "content":     "How does authentication work?",
      "created_at":  "2026-03-08T14:20:01Z"
    },
    {
      "role":        "assistant",
      "content":     "Authentication in NexusCode uses JWT...",
      "cited_files": ["src/mcp/auth.py:45-80"],
      "created_at":  "2026-03-08T14:20:03Z"
    }
  ]
}
```

---

### `GET /history/plan`

List recent implementation plan generation history.

```bash
curl "http://localhost:8000/history/plan?limit=20"
```

---

### `GET /history/plan/{plan_id}`

Get a specific saved implementation plan by ID.

```bash
curl http://localhost:8000/history/plan/plan-uuid
```

---

## Webhooks & Events

### `POST /webhook`

Receives GitHub push events. Must be registered as a GitHub webhook URL. Verifies `X-Hub-Signature-256` HMAC header.

```
POST /webhook
X-GitHub-Event: push
X-Hub-Signature-256: sha256=<hmac>
```

Responds `202 Accepted` on success. Queues a background indexing job via RQ.

**Test with simulation script:**
```bash
PYTHONPATH=. python scripts/simulate_webhook.py \
  --owner myorg --repo my-repo --file src/main.py
```

---

### `GET /events`

List recent webhook delivery events.

```bash
curl "http://localhost:8000/events?limit=20"
curl "http://localhost:8000/events?repo_owner=myorg&repo_name=my-repo"
```

**Response:**
```json
[
  {
    "id":           "uuid",
    "event_type":   "push",
    "repo":         "myorg/my-repo",
    "commit_sha":   "a1b2c3d",
    "files_changed": 3,
    "status":       "completed",
    "received_at":  "2026-03-08T14:00:00Z",
    "processed_at": "2026-03-08T14:00:05Z"
  }
]
```

**Event statuses:** `queued`, `processing`, `completed`, `error`

---

### `GET /events/{event_id}`

Get a single webhook event by ID.

```bash
curl http://localhost:8000/events/event-uuid
```

---

## Authentication

### `POST /auth/token`

Generate a JWT Bearer token for MCP or API access.

```bash
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"sub": "my-agent", "repos": []}'
```

**Body:**
```json
{
  "sub":   "my-agent",
  "repos": ["myorg/my-repo"]
}
```

| Field | Description |
|-------|-------------|
| `sub` | Identifier for this token (any string) |
| `repos` | List of `"owner/name"` to restrict access. Empty `[]` = all repos |

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type":   "bearer",
  "expires_in":   28800
}
```

Token expiry is controlled by `JWT_EXPIRY_HOURS` in `.env` (default: 8 hours).

---

### `GET /auth/verify`

Verify a Bearer token and return its decoded claims.

```bash
curl http://localhost:8000/auth/verify \
  -H "Authorization: Bearer eyJhbGci..."
```

**Response:**
```json
{
  "sub":   "my-agent",
  "repos": [],
  "exp":   1741450800,
  "iat":   1741422000
}
```

**401** if the token is missing, expired, or invalid.

---

## Statistics

### `GET /stats/repos`

Per-repository breakdown of chunks and files.

```bash
curl http://localhost:8000/stats/repos
```

**Response:**
```json
[
  {
    "repo":    "myorg/my-repo",
    "chunks":  4521,
    "symbols": 312,
    "files":   89
  }
]
```

---

### `GET /stats/recent-files`

Recently indexed files ordered by `indexed_at DESC`.

```bash
curl "http://localhost:8000/stats/recent-files?limit=20"
```

**Response:**
```json
[
  {
    "file_path":   "src/api/app.py",
    "repo":        "myorg/my-repo",
    "language":    "python",
    "chunks":      12,
    "indexed_at":  "2026-03-08T14:00:00Z"
  }
]
```

---

### `GET /stats/chunk-distribution`

Token-count bucket distribution for all active chunks — useful for understanding corpus density.

```bash
curl http://localhost:8000/stats/chunk-distribution
```

**Response:**
```json
{
  "buckets": [
    {"range": "0-128",   "count": 423},
    {"range": "128-256", "count": 1240},
    {"range": "256-512", "count": 2100},
    {"range": "512+",    "count": 758}
  ],
  "total_chunks": 4521
}
```

---

## Error Responses

All errors use standard HTTP status codes with a JSON body:

```json
{"detail": "Human-readable error message"}
```

| Code | Meaning |
|------|---------|
| `400` | Bad request — invalid parameters or body |
| `401` | Unauthorized — missing, expired, or invalid Bearer token |
| `404` | Resource not found (repo, document, skill, session, etc.) |
| `409` | Conflict — e.g. duplicate URL when registering an MCP server |
| `422` | Validation error — request body doesn't match schema (FastAPI auto-generated) |
| `500` | Internal server error — check server logs |
| `503` | Service unavailable — database or Redis not reachable |

**Validation errors (422)** include a detailed `detail` array from FastAPI:
```json
{
  "detail": [
    {
      "loc":  ["body", "query"],
      "msg":  "field required",
      "type": "value_error.missing"
    }
  ]
}
```

---

## SSE Streaming Guide

Several endpoints support Server-Sent Events (SSE) for streaming responses. Use `stream: true` in the request body or the dedicated stream endpoints.

### Client Setup

**curl (terminal):**
```bash
# -N disables curl's output buffering
curl -N -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "...", "stream": true}'
```

**JavaScript (browser/Node.js):**
```javascript
// Using fetch with ReadableStream
const res = await fetch('http://localhost:8000/ask', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({query: 'How does auth work?', stream: true})
});

const reader = res.body.getReader();
const decoder = new TextDecoder();

while (true) {
  const {done, value} = await reader.read();
  if (done) break;
  const text = decoder.decode(value);
  for (const line of text.split('\n')) {
    if (line.startsWith('data: ')) {
      const event = JSON.parse(line.slice(6));
      if (event.type === 'token') process.stdout.write(event.text);
      if (event.type === 'answer_complete') console.log('\nDone:', event.result);
    }
  }
}
```

**Python:**
```python
import httpx, json

with httpx.stream('POST', 'http://localhost:8000/ask',
                  json={'query': 'How does auth work?', 'stream': True}) as r:
    for line in r.iter_lines():
        if line.startswith('data: '):
            event = json.loads(line[6:])
            if event['type'] == 'token':
                print(event['text'], end='', flush=True)
```

### SSE Event Types by Endpoint

**`POST /ask` (stream: true):**
| Event type | Fields | Description |
|------------|--------|-------------|
| `token` | `text` | Incremental answer text |
| `answer_complete` | `result`, `session_id` | Final answer + metadata |
| `error` | `message` | Stream error |

**`POST /plan` (stream: true):**
| Event type | Fields | Description |
|------------|--------|-------------|
| `thinking` | `text` | Extended thinking text (Anthropic only) |
| `token` | `text` | Incremental plan text |
| `plan_complete` | `plan`, `plan_id` | Complete ImplementationPlan object |
| `error` | `message` | Stream error |

**`GET /workflows/runs/{run_id}/stream`:**
| Event type | Fields | Description |
|------------|--------|-------------|
| `workflow_started` | `run_id`, `workflow` | Workflow execution began |
| `step_started` | `step_id`, `role` | A step began executing |
| `step_token` | `step_id`, `text` | Token output from an agent step |
| `step_complete` | `step_id`, `tokens`, `output` | Step finished successfully |
| `step_failed` | `step_id`, `error`, `retry` | Step failed (may retry) |
| `checkpoint_created` | `checkpoint_id`, `prompt`, `options` | Waiting for human |
| `checkpoint_resolved` | `checkpoint_id`, `response` | Human responded |
| `workflow_complete` | `run_id`, `tokens_total` | All steps done |
| `workflow_failed` | `run_id`, `error` | Workflow stopped with error |

### Connection Handling

- SSE connections are long-lived HTTP/1.1 connections; configure reverse proxies to not buffer them (Nginx: `proxy_buffering off`)
- For workflow streams: the connection stays open until `workflow_complete` or `workflow_failed`
- If the client disconnects and reconnects, re-fetch `GET /workflows/runs/{run_id}` to get the current state, then re-subscribe to the stream
- Events are not replayed on reconnect
