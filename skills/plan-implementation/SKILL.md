---
name: plan-implementation
description: Generate a complete, grounded implementation plan for a bug fix, new feature, or refactoring task. Use this BEFORE writing any code when the task is non-trivial. Returns exact file paths, symbol names, ordered steps with dependencies, pseudocode for complex logic, risk assessment, and a test plan — all grounded in the live codebase index.
metadata:
  author: nexuscode
  version: "1.0"
compatibility: Requires a running NexusCode API server at http://localhost:8000 with ANTHROPIC_API_KEY and VOYAGE_API_KEY configured.
---

# Plan Implementation Skill

## When to use this skill

Use `plan_implementation` **before writing code** when:
- Fixing a non-trivial bug (> 3 files likely affected)
- Adding a new feature that touches existing logic
- Refactoring a module, class, or API contract
- Unsure which files or functions need to change

Skip for: typo fixes, single-line changes, trivial config updates.

## How to use — MCP tool (recommended)

The `plan_implementation` MCP tool is the fastest path. Call it with a plain-English description:

```
plan_implementation(
  query="Add rate limiting to the /search endpoint — 100 req/min per IP",
  repo="owner/name"   # optional: scope to one repo
)
```

The tool returns formatted markdown with:
- **Summary** — the overall approach
- **Files to change** — exact paths + per-symbol changes with pseudocode
- **Execution steps** — ordered, with step dependencies
- **Risks** — severity-tagged with mitigation
- **Test plan** — specific assertions

## How to use — REST API

```bash
curl -X POST http://localhost:8000/plan \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Add rate limiting to the /search endpoint — 100 req/min per IP",
    "repo_owner": "owner",
    "repo_name": "myrepo",
    "stream": false
  }'
```

### Streaming mode (SSE)

Set `"stream": true` to receive a server-sent-events stream:

```bash
curl -N -X POST http://localhost:8000/plan \
  -H "Content-Type: application/json" \
  -d '{"query": "...", "stream": true}'
```

SSE event types: `status` → `retrieval_complete` → `plan_complete` / `error`

## Reading the response

See [references/REFERENCE.md](references/REFERENCE.md) for the complete JSON schema.

Key fields to act on:

| Field | What to do |
|---|---|
| `files[].path` + `files[].changes[]` | Edit these files in this order |
| `steps[]` ordered by `step_number` | Follow steps; check `depends_on_steps` before each |
| `risks[]` with `severity: "high"` | Address these explicitly before marking done |
| `test_plan` | Run or write these tests after each step |

## Step-by-step workflow

1. **Call the tool** with your query and optional repo scope
2. **Read the summary** — verify it matches your intent; if not, refine the query
3. **Check assumptions** — the plan may list clarifying assumptions; confirm they are correct
4. **Follow the steps in order** — do not skip or reorder; dependencies are computed
5. **After each step**: run the listed verification (test, linter, type-check)
6. **Address all high-severity risks** before considering the task done

## Refining a poor plan

If the plan is generic or references wrong files:
- The repo may not be indexed → run indexing first (`POST /repos/{owner}/{name}/index`)
- Add more detail to the query: include function names, file paths, error messages
- Scope to a specific repo with `repo_owner` / `repo_name`

## Example queries that work well

```
"Fix the bug where webhook_events stay in 'queued' status after processing"
"Add cursor-based pagination to GET /repos — return next_cursor in response"
"Refactor the chunker to use tree-sitter's new incremental parsing API"
"Add TypeScript support to the Tree-sitter parser — it currently only does JS"
"The reranker model loads on every request instead of being cached — fix this"
```

## Dashboard equivalent

Open **🧩 Planning Mode** in the Streamlit dashboard at `http://localhost:8501`.
Same capability with a visual plan renderer.
