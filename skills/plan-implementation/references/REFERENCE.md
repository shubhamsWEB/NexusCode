# Plan Implementation — API Reference

## POST /plan

### Request

```typescript
{
  query:       string   // required, min 5 chars — bug/feature/refactor description
  repo_owner?: string   // optional — scope to one repo
  repo_name?:  string   // optional — scope to one repo (requires repo_owner)
  stream?:     boolean  // default false — if true, returns SSE stream
}
```

### Response (stream=false)

```typescript
{
  plan_id:                 string      // UUID
  query:                   string
  summary:                 string      // 2-3 sentence approach summary
  clarifying_assumptions:  string[]    // assumptions made for ambiguous queries

  files: Array<{
    path:    string                    // file path relative to repo root
    action:  "create"|"modify"|"delete"|"rename"|"move"
    reason:  string                    // why this file needs to change
    changes: Array<{
      kind:       "add"|"modify"|"delete"|"move"
      symbol?:    string               // qualified symbol name e.g. "AuthService.login"
      description: string              // what changes and why
      pseudocode?: string              // for complex logic
      line_hint?:  string              // approximate location e.g. "42-55"
    }>
  }>

  steps: Array<{
    step_number:       number
    title:             string
    description:       string
    files_involved:    string[]         // file paths touched in this step
    depends_on_steps:  number[]         // step numbers that must complete first
    verification?:     string           // how to confirm success
  }>

  risks: Array<{
    severity:          "low"|"medium"|"high"
    description:       string
    affected_symbols:  string[]
    mitigation:        string
  }>

  test_plan:  string                   // specific assertions and test strategy

  metadata?: {
    model:           string            // Claude model used
    context_tokens:  number            // tokens in retrieval context
    context_files:   number            // code chunks in context
    retrieval_log:   string            // what was retrieved
    elapsed_ms:      number            // total latency
  }
}
```

### SSE Events (stream=true)

Each event is `data: <JSON>\n\n` with these shapes:

```typescript
{ type: "status",             message: string }
{ type: "retrieval_complete", log: string, chunks: number, tokens: number }
{ type: "plan_complete",      plan: <same as sync response> }
{ type: "error",              message: string }
```

### Error responses

| Status | Cause | Fix |
|---|---|---|
| 503 | `anthropic` package not installed | `pip install anthropic>=0.40.0` |
| 503 | `ANTHROPIC_API_KEY` not set | Add to `.env`, restart server |
| 500 | Retrieval failed (DB/embedding error) | Check Postgres and Voyage API key |
| 500 | Claude API error | Check API key, model availability |

---

## Retrieval Pipeline (5 phases)

Understanding this helps write better queries:

```
Phase 1 — Embed query with voyage-code-2 (input_type="query")
Phase 2 — Hybrid search: pgvector cosine + tsvector keyword + RRF merge → 15 candidates
Phase 3 — Cross-encoder rerank (ms-marco-MiniLM-L-6-v2) → top 10
Phase 4 — File structure maps for top-5 unique files (symbols table)
Phase 5 — Caller context for top-3 symbols + second semantic pass on symbol names
```

**Token budget:** 12,000 total — split 65 % primary chunks / 20 % caller context / 15 % expansion.

---

## MCP Tool Signature

```python
plan_implementation(
    query: str,    # min ~10 chars for good results
    repo:  str,    # optional, format: "owner/name"
) -> str           # formatted markdown
```

Returns markdown with all plan sections formatted for readability in Claude Desktop.

---

## Quality Factors

| Factor | Effect |
|---|---|
| Repo indexed recently | Best results — all symbols and chunks are current |
| Query includes symbol names | Retrieval finds more relevant context |
| Query scoped to one repo | Less noise, more precise file paths |
| Repo not indexed | Generic plan with no file paths — index first |
| ANTHROPIC_API_KEY missing | HTTP 503 error |
