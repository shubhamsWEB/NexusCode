# REST API Reference

Base URL: `http://localhost:8000`
Interactive docs: `http://localhost:8000/docs`

All endpoints return JSON unless noted. Authentication is only required for the MCP server (see [mcp-access.md](./mcp-access.md)).

---

## Health & Status

### `GET /health`
Index statistics and server status.

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "repos": 3,
  "chunks": 12450,
  "symbols": 890,
  "files": 234
}
```

### `GET /models`
List LLM models available (based on configured API keys).

```bash
curl http://localhost:8000/models
```

```json
{"available": ["claude-sonnet-4-6", "gpt-4o", "gpt-4o-mini"]}
```

---

## Repository Management

### `GET /repos`
List all registered repositories with per-repo statistics.

```bash
curl http://localhost:8000/repos
```

### `POST /repos`
Register a new repository.

**Body:**
```json
{
  "owner":     "your-org",
  "name":      "your-repo",
  "branch":    "main",
  "index_now": true
}
```

- `branch` — default `"main"`
- `index_now` — if `true`, triggers a full index job immediately

**Response:**
```json
{"repo": "your-org/your-repo", "status": "queued", "job_id": "uuid"}
```

### `DELETE /repos/{owner}/{name}`
Hard-delete a repository and all its indexed data.

```bash
curl -X DELETE http://localhost:8000/repos/your-org/your-repo
```

### `POST /repos/{owner}/{name}/index`
Trigger a full re-index job for an existing repo.

```bash
curl -X POST http://localhost:8000/repos/your-org/your-repo/index
```

---

## Search

### `POST /search`

Hybrid semantic + keyword search with optional cross-encoder reranking.

**Body:**
```json
{
  "query":        "authentication middleware",
  "repo":         "owner/name",
  "language":     "python",
  "top_k":        5,
  "mode":         "hybrid",
  "rerank":       true,
  "token_budget": 8000
}
```

**Response:**
```json
{
  "query":   "authentication middleware",
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
      "preview":       "async def require_auth..."
    }
  ],
  "context":       "formatted code context string",
  "tokens_used":   1240,
  "retrieval_log": "..."
}
```

---

## Ask Mode

### `POST /ask`

Answer a natural-language question about the codebase.

**Body:**
```json
{
  "query":      "How does the webhook processing pipeline work?",
  "repo_owner": "your-org",
  "repo_name":  "your-repo",
  "stream":     false,
  "session_id": "uuid-optional",
  "model":      "claude-sonnet-4-6"
}
```

**Response (sync, `stream: false`):**
```json
{
  "answer":          "The webhook pipeline starts when...",
  "cited_files":     ["src/github/webhook.py:42-80"],
  "follow_up_hints": ["What is the Merkle diff?", "..."],
  "quality_score":   0.87,
  "elapsed_ms":      1840,
  "session_id":      "3f2a1b9c-..."
}
```

**Response (streaming, `stream: true`):**
```
data: {"type": "token",           "text": "The webhook..."}
data: {"type": "answer_complete", "result": {...}, "session_id": "uuid"}
```

---

## Planning Mode

### `POST /plan`

Generate a structured implementation plan.

**Body:**
```json
{
  "query":        "Add rate limiting to POST /search",
  "repo_owner":   "your-org",
  "repo_name":    "your-repo",
  "stream":       false,
  "web_research": true,
  "model":        "claude-sonnet-4-6"
}
```

**Response (sync):** Full `ImplementationPlan` object (see [search-and-ask.md](./search-and-ask.md) for schema).

**Streaming events:**
```
data: {"type": "thinking",      "text": "..."}
data: {"type": "token",         "text": "..."}
data: {"type": "plan_complete", "plan": {...}}
```

---

## Skills

### `GET /skills`
List all discovered skills (builtin + custom).

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
    }
  ],
  "total": 4
}
```

### `GET /skills/{name}`
Get full SKILL.md content for a named skill.

```bash
curl http://localhost:8000/skills/plan-implementation
```

```json
{
  "name":        "plan-implementation",
  "description": "...",
  "content":     "---\nname: plan-implementation\n...",
  "source":      "builtin",
  "metadata":    {"author": "nexuscode", "version": "1.2"}
}
```

### `POST /skills/reload`
Reload skill cache from disk without restarting.

```bash
curl -X POST http://localhost:8000/skills/reload
# → {"message": "Reloaded 6 skills"}
```

---

## History

### `GET /history/ask`
List recent Ask Mode sessions.

```bash
curl http://localhost:8000/history/ask
```

### `GET /history/ask/{session_id}`
Get a full Ask Mode session with all turns.

```bash
curl http://localhost:8000/history/ask/3f2a1b9c-...
```

### `GET /history/plan`
List recent plan generation history.

### `GET /history/plan/{plan_id}`
Get a specific plan by ID.

---

## Webhooks

### `POST /webhook`
Receives GitHub push events. Must be registered as a GitHub webhook. Verifies HMAC-SHA256 signature.

```bash
# Test with simulation script
PYTHONPATH=. python scripts/simulate_webhook.py --owner org --repo repo --file src/main.py
```

### `GET /events`
List recent webhook events.

```bash
curl "http://localhost:8000/events?limit=20"
curl "http://localhost:8000/events?repo_owner=your-org&repo_name=your-repo"
```

---

## Auth

### `POST /auth/token`
Generate a JWT token for MCP or API access.

```bash
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"sub": "my-agent", "repos": []}'
```

**Body:**
```json
{
  "sub":   "identifier",
  "repos": ["owner/name"]
}
```

`repos: []` = access all repos. Specify repo strings to restrict access.

**Response:**
```json
{
  "access_token": "eyJ...",
  "token_type":   "bearer",
  "expires_in":   28800
}
```

### `GET /auth/verify`
Verify a token and return its claims.

```bash
curl http://localhost:8000/auth/verify \
  -H "Authorization: Bearer <token>"
```

---

## Statistics

### `GET /stats/repos`
Per-repository chunk and file breakdown.

### `GET /stats/recent-files`
Recently indexed files ordered by `indexed_at DESC`.

```bash
curl "http://localhost:8000/stats/recent-files?limit=20"
```

### `GET /stats/chunk-distribution`
Token-count bucket distribution for active chunks.

---

## Error Responses

All errors follow:
```json
{"detail": "Human-readable error message"}
```

Common HTTP status codes:

| Code | Meaning |
|---|---|
| `400` | Bad request — invalid parameters |
| `401` | Unauthorized — missing or invalid Bearer token |
| `404` | Resource not found (repo, skill, session) |
| `422` | Validation error — request body doesn't match schema |
| `500` | Internal server error — check server logs |
| `503` | Service unavailable — database or worker not reachable |
