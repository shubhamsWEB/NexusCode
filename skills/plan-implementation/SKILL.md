---
name: plan-implementation
description: Generate a complete, grounded implementation plan for a bug fix, new feature, or refactoring task. Use this BEFORE writing any code when the task is non-trivial. Extracts the codebase stack (installed packages, language, framework), then searches the web for stack-specific integration gaps, then combines both with live codebase context to return exact file paths, symbol names, ordered steps, pseudocode, risk assessment, and a test plan. The plan explicitly states which packages it reuses vs which are new additions.
metadata:
  author: nexuscode
  version: "1.2"
compatibility: Requires a running NexusCode API server at http://localhost:8000 with ANTHROPIC_API_KEY and VOYAGE_API_KEY configured.
---

# Plan Implementation Skill

## How it works — Three-Tier Architecture

Planning runs a **three-tier pipeline** that combines into one coherent, stack-grounded plan:

**Tier 1 — Codebase Context** (ground truth, always)
Live retrieval from the indexed repo: real file paths, function names, call-sites, file structure.
The plan can only reference what actually exists here.

**Tier 2 — Stack Fingerprint** (what is already installed)
A fast DB query extracts the exact packages in `requirements.txt`/`package.json` and the most-used imports across all indexed files. Fires first so everything downstream knows what the codebase already has.

**Tier 3 — Stack-Aware Gap Analysis** (what is missing)
Web search fires *after* the stack fingerprint, so it knows the real installed packages. Instead of a generic tutorial, it answers:
- What in the existing stack already handles this task?
- What packages are genuinely missing and why?
- What are the version-specific gotchas for this stack + task combo?

The final plan **always states**: `Reuses: [existing packages] | Adds: [new packages or none]`
This prevents hallucinated dependency suggestions when the stack already covers the need.

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
  repo="owner/name",    # optional: scope to one repo
  web_research=True     # default True — search web for best approach
)
```

The tool returns formatted markdown with:
- **Problem Statement** — what needs to be solved and why
- **Current Architecture** — flow structure, key files, current state, infrastructure
- **Proposed Solutions** — ≥2 viable approaches with pros/cons, one recommended
- **Recommendation** — which option and why (architectural reasoning)
- **Implementation Plan** — prerequisites (coordination tasks) + ordered dev tasks
- **Files to change** — exact paths + per-symbol changes with pseudocode
- **Risks** — severity-tagged with mitigation
- **Open Questions** — decisions needing team input (with suggested owners)
- **References** — key files referenced throughout
- **Test plan** — specific assertions

## How to use — REST API

```bash
curl -X POST http://localhost:8000/plan \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Add rate limiting to the /search endpoint — 100 req/min per IP",
    "repo_owner": "owner",
    "repo_name": "myrepo",
    "stream": false,
    "web_research": true
  }'
```

Set `web_research: false` to skip the web search phase (useful when offline or testing).

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
| `problem_statement` | Verify this matches your intent; if not, refine the query |
| `proposed_solutions[]` | Review all options — the recommended one is marked |
| `recommendation` | Confirm you agree with the architectural reasoning |
| `prerequisites[]` | Complete coordination tasks before starting dev work |
| `files[].path` + `files[].changes[]` | Edit these files in this order |
| `steps[]` ordered by `step_number` | Follow steps; check `depends_on_steps` before each |
| `risks[]` with `severity: "high"` | Address these explicitly before marking done |
| `open_questions` | Resolve these with the relevant teams |
| `references[]` | Key files for quick navigation |
| `test_plan` | Run or write these tests after each step |

## Step-by-step workflow

1. **Call the tool** with your query and optional repo scope
2. **Read the problem statement** — verify it matches your intent; if not, refine the query
3. **Review proposed solutions** — understand all options, confirm the recommendation
4. **Check prerequisites** — complete coordination tasks first (team alignment, config, etc.)
5. **Follow the dev tasks in order** — do not skip or reorder; dependencies are computed
6. **After each step**: run the listed verification (test, linter, type-check)
7. **Address all high-severity risks** before considering the task done
8. **Resolve open questions** with the relevant teams

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
