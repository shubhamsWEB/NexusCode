# MCP Tools — Full Reference

MCP server endpoint: `http://localhost:8000/mcp` (SSE transport)

---

## Tool 1: search_codebase

### Parameters

| Param | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Natural language or identifier query |
| `repo` | string? | all repos | Scope to `"owner/name"` |
| `language` | string? | all | Filter: `python`, `typescript`, `javascript`, `go`, `rust`, … |
| `top_k` | int | 5 | Results to return (1–20) |
| `mode` | string | `"hybrid"` | `"semantic"` / `"keyword"` / `"hybrid"` |

### Response (JSON string)

```json
{
  "query": "...",
  "mode": "hybrid",
  "results": [
    {
      "file": "src/auth/service.py",
      "repo": "owner/name",
      "symbol": "authenticate",
      "kind": "function",
      "scope": "AuthService.authenticate",
      "lines": "42-67",
      "language": "python",
      "score": 0.9234,
      "commit": "abc1234",
      "preview": "def authenticate(token: str) -> User:\n    ..."
    }
  ],
  "context": "<formatted context string ready to inject>",
  "tokens_used": 2847,
  "retrieval_log": "..."
}
```

---

## Tool 2: get_symbol

### Parameters

| Param | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Exact, qualified, or natural-language symbol name |
| `repo` | string? | all repos | Scope to `"owner/name"` |

### Response

```json
{
  "symbols": [
    {
      "name": "authenticate",
      "qualified_name": "AuthService.authenticate",
      "kind": "method",
      "file": "src/auth/service.py",
      "repo": "owner/name",
      "lines": "42-67",
      "signature": "def authenticate(self, token: str) -> User",
      "docstring": "Verify JWT token and return the associated User.",
      "is_exported": true,
      "match_score": 0.95
    }
  ],
  "count": 1
}
```

---

## Tool 3: find_callers

### Parameters

| Param | Type | Default | Description |
|---|---|---|---|
| `symbol` | string | required | Symbol to find callers of |
| `repo` | string? | all repos | Scope |
| `depth` | int | 1 | Call hops (1–3) |

### Response

```json
{
  "symbol": "authenticate",
  "depth": 1,
  "caller_count": 3,
  "callers": [
    {
      "file": "src/api/middleware.py",
      "repo": "owner/name",
      "symbol_context": "require_auth",
      "kind": "function",
      "lines": "12-25",
      "call_sites": [
        { "line_no": 18, "text": "user = authenticate(token)" }
      ],
      "preview": "def require_auth(request: Request):\n    ..."
    }
  ]
}
```

---

## Tool 4: get_file_context

### Parameters

| Param | Type | Default | Description |
|---|---|---|---|
| `path` | string | required | File path (exact or partial) |
| `repo` | string? | all repos | Scope |
| `include_deps` | bool | true | Also list files that import this file |

### Response

```json
{
  "file": "src/auth/service.py",
  "language": "python",
  "last_commit": "abc1234",
  "commit_author": "Alice",
  "chunk_count": 4,
  "imports": ["src.config", "src.storage.db", "jwt"],
  "symbols": [
    {
      "name": "AuthService",
      "qualified_name": "AuthService",
      "kind": "class",
      "lines": "10-120",
      "signature": "class AuthService:",
      "docstring": "Handles JWT auth and OAuth flows.",
      "is_exported": true
    }
  ],
  "imported_by": ["owner/name:src/api/middleware.py"]
}
```

---

## Tool 5: get_agent_context

### Parameters

| Param | Type | Default | Description |
|---|---|---|---|
| `task` | string | required | Task description (natural language) |
| `focal_files` | string[]? | none | Files you're actively editing (get top priority) |
| `token_budget` | int | 8000 | Max context tokens (1000–32000) |
| `repo` | string? | all repos | Scope |

### Response

```json
{
  "task": "Add rate limiting to /search endpoint",
  "focal_files": ["src/api/app.py"],
  "context_text": "<ready-to-inject formatted context string>",
  "chunks_used": [
    { "file": "src/api/app.py", "lines": "1-50", "symbol": null, "score": 10.0, "tokens": 380 }
  ],
  "tokens_used": 4210,
  "retrieval_log": "..."
}
```

---

## Tool 6: plan_implementation

See the `plan-implementation` skill for full documentation.

### Parameters

| Param | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Bug/feature/refactor description |
| `repo` | string? | all repos | Scope to `"owner/name"` |

Returns formatted markdown (not JSON).

---

## Hybrid Search Pipeline

```
query string
    │
    ▼
voyage-code-2 embed (input_type="query") → 1536-dim vector
    │
    ├─► pgvector cosine similarity → semantic candidates
    │
    └─► tsvector + pg_trgm         → keyword candidates
                │
                ▼
        Reciprocal Rank Fusion (k=60)
                │
                ▼
        cross-encoder/ms-marco-MiniLM-L-6-v2 rerank
                │
                ▼
        token-budget assembler → formatted context string
```
