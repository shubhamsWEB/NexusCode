---
name: search-codebase
description: Search indexed GitHub repositories for code, symbols, callers, and file structure. Use for natural-language queries ("where is auth handled?"), identifier lookups (find a function by name), call-site discovery (who calls this method?), file structure overviews, and pre-assembled coding-task context. Requires a running NexusCode server with at least one indexed repository.
metadata:
  author: nexuscode
  version: "1.0"
compatibility: Requires NexusCode API at http://localhost:8000 with VOYAGE_API_KEY configured and at least one indexed repository.
---

# Search Codebase Skill

## Available tools

NexusCode exposes 5 search/retrieval MCP tools (plus the planning tool):

| Tool | Best for |
|---|---|
| `search_codebase` | Natural-language or identifier queries — finds relevant chunks |
| `get_symbol` | "Go to Definition" — find a function/class by name |
| `find_callers` | "Find All References" — who calls this symbol? |
| `get_file_context` | Structural map of a file — all symbols, imports, imported-by |
| `get_agent_context` | Pre-assembled context before starting a task |

## search_codebase

```
search_codebase(
  query="where is Shopify session authentication handled?",
  repo="owner/name",   # optional
  language="typescript",  # optional filter
  top_k=5,             # 1-20, default 5
  mode="hybrid"        # "hybrid" | "semantic" | "keyword"
)
```

Returns ranked code chunks with file path, line numbers, symbol name, and source preview.

**When to use each mode:**
- `hybrid` — default, best quality (RRF of vector + keyword + cross-encoder rerank)
- `semantic` — when the query is conceptual ("error handling logic")
- `keyword` — when you know the exact identifier ("processWebhook")

## get_symbol

```
get_symbol(
  name="authenticate",   # exact, qualified, or fuzzy
  repo="owner/name"
)
```

Returns: file path, line range, full signature, docstring, is_exported.
Supports fuzzy matching — `"auth"` finds `"authenticate"`, `"Authorization"`, etc.

## find_callers

```
find_callers(
  symbol="PaymentService.charge",
  repo="owner/name",
  depth=1   # 1-3 call hops
)
```

Returns: all files/functions that call this symbol, with call-site code snippets.
**Use this** before changing a function signature — know what breaks.

## get_file_context

```
get_file_context(
  path="app/services/auth.ts",
  repo="owner/name",
  include_deps=True   # also show files that import this file
)
```

Returns: language, last commit, all symbols (with signatures), imports list, imported-by list.

## get_agent_context

```
get_agent_context(
  task="Add OAuth2 PKCE flow to the auth service",
  focal_files=["src/auth/service.py", "src/auth/middleware.py"],
  token_budget=8000,
  repo="owner/name"
)
```

Call this at the START of a coding task. Returns a single, deduplicated, token-budget-aware
context string combining focal files + semantic search results. Ready to inject into prompts.

## REST API equivalent

```bash
POST http://localhost:8000/search
{
  "query": "where is auth handled?",
  "repo": "owner/name",
  "language": "python",
  "top_k": 5,
  "mode": "hybrid",
  "rerank": true,
  "token_budget": 8000
}
```

See [references/mcp-tools.md](references/mcp-tools.md) for complete response schemas.

## Common patterns

**1. Start a coding task:**
```
get_agent_context(task="refactor the payment flow", focal_files=["src/payments/"])
```

**2. Understand a file before editing:**
```
get_file_context(path="src/api/webhook.py")
```

**3. Find where a feature is implemented:**
```
search_codebase(query="rate limiting middleware")
```

**4. Check impact of a change:**
```
find_callers(symbol="upsert_chunks")
```

**5. Look up a function:**
```
get_symbol(name="embed_query")
```

## If search returns no results

- Verify at least one repo is indexed: `GET /repos`
- Check VOYAGE_API_KEY is set in `.env`
- Try `mode="keyword"` to test if the issue is embedding-related
- Re-index: `POST /repos/{owner}/{name}/index`
