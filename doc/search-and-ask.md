# Search, Ask, and Planning Modes

NexusCode has three ways to interact with your indexed codebase. Each is optimized for a different workflow.

---

## Overview

| Mode | Best for | Endpoint | Output |
|---|---|---|---|
| **Search** | Finding relevant code, symbols, call sites | `POST /search` | Ranked chunks + assembled context |
| **Ask** | Understanding — "how does X work?" | `POST /ask` | Markdown answer with citations |
| **Plan** | Before writing code — "how should I implement X?" | `POST /plan` | Structured implementation plan |

---

## Search Mode

The search endpoint returns ranked code chunks ready to inject into any LLM prompt.

### Three search modes

| Mode | Uses | Best for |
|---|---|---|
| `semantic` | Vector cosine similarity (voyage-code-2) | Conceptual queries: "authentication flow", "error handling patterns" |
| `keyword` | tsvector full-text + trigram symbol matching | Exact identifier lookups: "UserService", "validate_token" |
| `hybrid` | Both, merged via Reciprocal Rank Fusion + cross-encoder rerank | Best overall — default |

### Request

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "query":        "authentication middleware",
    "repo":         "owner/name",
    "language":     "python",
    "top_k":        5,
    "mode":         "hybrid",
    "rerank":       true,
    "token_budget": 8000
  }'
```

### Response

```json
{
  "query": "authentication middleware",
  "mode": "hybrid",
  "results": [
    {
      "file":         "src/mcp/auth.py",
      "repo":         "owner/name",
      "symbol":       "require_auth",
      "kind":         "function",
      "scope":        "require_auth",
      "lines":        "65-82",
      "language":     "python",
      "score":        0.8931,
      "rerank_score": 4.2156,
      "quality_score": 0.9851,
      "commit":       "a1b2c3d",
      "preview":      "async def require_auth(credentials: ...)..."
    }
  ],
  "context":       "═══ File: src/mcp/auth.py ...",
  "tokens_used":   1240,
  "retrieval_log": "Query: 'authentication middleware'\nChunks: 5, tokens: 1240/8000"
}
```

**`quality_score`** is the sigmoid-normalized cross-encoder confidence (0.0–1.0). Values above 0.8 indicate high relevance.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `query` | required | Natural language or identifier |
| `repo` | all repos | Scope to `"owner/name"` |
| `language` | all | Filter: `"python"`, `"typescript"`, etc. |
| `top_k` | 5 | Results to return (1–20) |
| `mode` | `"hybrid"` | `"semantic"`, `"keyword"`, or `"hybrid"` |
| `rerank` | `true` | Apply cross-encoder reranking |
| `token_budget` | 8000 | Max tokens in assembled context string |
| `preset` | `"balanced"` | Quality preset (see below) |

### Search Quality Presets

Presets let you choose between speed and thoroughness in a single parameter:

| Preset | `top_k` | Reranking | Best for |
|--------|---------|-----------|----------|
| `fast` | 5 | Off | IDE autocomplete, quick symbol lookups |
| `balanced` | 10 | On | Standard queries — default |
| `thorough` | 20 | On | Deep analysis, comprehensive context gathering |

```bash
# Thorough search for a comprehensive answer
curl -X POST http://localhost:8000/search \
  -d '{"query": "all database connection handling", "preset": "thorough"}'
```

Presets set `top_k` and `rerank` automatically. Explicitly passing `top_k` or `rerank` overrides the preset values.

---

## Ask Mode

Ask Mode answers natural-language questions in a mentor tone — like a senior engineer on Slack explaining how something works. It always cites real file paths and line numbers.

### When to use Ask vs. Search

- **Search** → you need the raw code chunks (to feed to another LLM, build a tool, etc.)
- **Ask** → you want an explanation (for a developer reading the answer)

### Request (streaming)

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{
    "query":      "How does the webhook processing pipeline work?",
    "repo_owner": "your-org",
    "repo_name":  "your-repo",
    "stream":     true,
    "model":      "claude-sonnet-4-6"
  }'
```

Streaming SSE response:
```
data: {"type": "token", "text": "The webhook pipeline starts when "}
data: {"type": "token", "text": "GitHub sends a push event to `POST /webhook`..."}
data: {"type": "answer_complete", "result": {...}, "session_id": "uuid"}
```

### Request (synchronous)

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{
    "query":      "What calls the reranker?",
    "repo_owner": "your-org",
    "repo_name":  "your-repo",
    "stream":     false
  }'
```

### Response

```json
{
  "answer": "The reranker is called in two places...\n\n`src/planning/retriever.py` (line 446)...",
  "cited_files": [
    "src/planning/retriever.py:443-447",
    "src/mcp/server.py:97-99"
  ],
  "follow_up_hints": [
    "How does the cross-encoder model work?",
    "What is Reciprocal Rank Fusion?",
    "Where is the reranker model downloaded from?"
  ],
  "quality_score": 0.87,
  "elapsed_ms": 1840,
  "session_id": "3f2a1b9c-..."
}
```

### Session continuity

Pass `session_id` in subsequent requests to continue a conversation thread:

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{
    "query":      "And how does HyDE fit in?",
    "session_id": "3f2a1b9c-...",
    "stream":     false
  }'
```

Sessions are persisted to the database and visible in the **History** tab of the dashboard.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `query` | required | Natural-language question (min 5 chars) |
| `repo_owner` | all repos | Scope to a specific owner |
| `repo_name` | all repos | Scope to a specific repo |
| `stream` | `true` | `true` = SSE stream, `false` = sync JSON |
| `session_id` | auto-generated | Continue an existing chat session |
| `model` | server default | Override LLM (see `GET /models`) |

---

## Planning Mode

Planning Mode generates a complete, grounded implementation plan before you write a single line of code. It runs a 7-phase retrieval pipeline and produces a structured JSON plan with exact file paths, ordered steps, pseudocode, risks, and a test plan.

### What makes a good plan query

- **Specific**: "Add Redis caching to the `embed_query` function in `src/retrieval/searcher.py`"
- **Clear intent**: "Refactor the reranker to support batched inference"
- **Scoped**: include the repo if you want focused results

### Request (streaming)

```bash
curl -X POST http://localhost:8000/plan \
  -H "Content-Type: application/json" \
  -d '{
    "query":        "Add rate limiting to the POST /search endpoint",
    "repo_owner":   "your-org",
    "repo_name":    "your-repo",
    "stream":       true,
    "web_research": true,
    "model":        "claude-sonnet-4-6"
  }'
```

Streaming SSE events:
```
data: {"type": "thinking",      "text": "...extended thinking..."}
data: {"type": "token",         "text": "...partial tool output..."}
data: {"type": "plan_complete", "plan": {...full ImplementationPlan...}}
```

### Request (synchronous)

```bash
curl -X POST http://localhost:8000/plan \
  -H "Content-Type: application/json" \
  -d '{
    "query":  "Add rate limiting to the POST /search endpoint",
    "stream": false
  }'
```

### Response structure

```json
{
  "plan_id":    "uuid",
  "query":      "Add rate limiting to the POST /search endpoint",
  "response_type": "plan",
  "summary":    "Add per-IP rate limiting using slowapi...",
  "clarifying_assumptions": ["Using Redis as the rate limit store..."],
  "constraints": ["FastAPI middleware ordering..."],
  "design_alternatives": [
    {"approach": "slowapi middleware", "pros": [...], "cons": [...], "rejected_reason": ""},
    {"approach": "Redis INCR + TTL", "pros": [...], "cons": [...], "rejected_reason": "More complexity"}
  ],
  "failure_modes": [
    {"scenario": "Redis unavailable", "cause": "...", "mitigation": "..."}
  ],
  "files": [
    {
      "path": "src/api/app.py",
      "action": "modify",
      "reason": "Mount rate limiter middleware",
      "changes": [
        {
          "kind": "add",
          "symbol": null,
          "description": "Import slowapi and add Limiter(key_func=get_remote_address) at line 10",
          "pseudocode": null
        }
      ]
    }
  ],
  "steps": [
    {
      "step_number": 1,
      "title": "Install slowapi",
      "description": "Add `slowapi>=0.1.9` to requirements.txt",
      "files_involved": ["requirements.txt"],
      "depends_on_steps": [],
      "verification": "pip install slowapi && python -c 'import slowapi'"
    }
  ],
  "risks": [...],
  "test_plan": "...",
  "sparc": {
    "specification": "Add per-IP rate limiting...",
    "pseudocode":    "if requests[ip] > limit: raise 429",
    "architecture":  "Middleware in app.py wraps all routes...",
    "refinement":    "Redis unavailability must not break the endpoint...",
    "completion":    "pytest tests/test_rate_limit.py && curl -loop to verify 429"
  },
  "metadata": {
    "model":           "claude-sonnet-4-6",
    "context_tokens":  8420,
    "context_files":   12,
    "elapsed_ms":      4230,
    "quality_score":   0.91,
    "web_research_used": true,
    "query_complexity": "moderate"
  }
}
```

### SPARC summary

Every implementation plan includes a `sparc` field with a concise walkthrough:

| Phase | Meaning | Maps to |
|---|---|---|
| **S** — Specification | What needs to be built and why | `constraints` + `clarifying_assumptions` |
| **P** — Pseudocode | Key algorithmic logic | `files[].changes[].pseudocode` |
| **A** — Architecture | How it flows through the system | `summary` |
| **R** — Refinement | Edge cases and trade-offs | `design_alternatives` + `failure_modes` |
| **C** — Completion | How to verify it's done | `test_plan` |

### Three response types

The LLM picks the right response type based on your query:

| `response_type` | When | Key fields |
|---|---|---|
| `plan` | Task requires code changes | `files`, `steps`, `risks`, `test_plan`, `sparc` |
| `answer` | Question or explanation | `answer`, `key_files` |
| `analysis` | "How to improve X?" / code review | `analysis`, `key_files` |

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `query` | required | Bug/feature/refactor description (min 5 chars) |
| `repo_owner` | all repos | Scope to a specific owner |
| `repo_name` | all repos | Scope to a specific repo |
| `stream` | `false` | `true` = SSE stream, `false` = sync JSON |
| `web_research` | `true` | Search web for best practices before planning |
| `model` | server default | Override LLM (see `GET /models`) |

### Web research

When `web_research: true` (the default), the planner fires a stack-aware web search in parallel with codebase retrieval. The web results are used to inform recommendations (e.g., which library to use) but are never echoed verbatim into the plan. Web research requires Anthropic API (Claude's `web_search_20250305` tool).

---

## Choosing a Model

```bash
# List available models (based on configured API keys)
curl http://localhost:8000/models
```

Response:
```json
{
  "available": ["claude-sonnet-4-6", "claude-opus-4-6", "gpt-4o", "gpt-4o-mini"]
}
```

Pass `model` in any request to override the default:

```bash
# Use o3 for a complex architectural plan
curl -X POST http://localhost:8000/plan \
  -d '{"query": "...", "model": "o3"}'
```

| Model | Best for |
|---|---|
| `claude-sonnet-4-6` | Default — fast, accurate, supports extended thinking |
| `claude-opus-4-6` | Complex architectural decisions |
| `gpt-4o` | Alternative if Anthropic quota is hit |
| `gpt-4o-mini` | Fast, cheap for simple questions |
| `o3` / `o4-mini` | Deep reasoning for hard planning problems |
| `grok-3` | Alternative LLM via xAI |

---

## Quality Score

Every search, ask, and plan response includes a `quality_score` (0.0–1.0) indicating the confidence of the retrieved context:

| Range | Meaning |
|---|---|
| 0.85–1.0 | Excellent — highly relevant context retrieved |
| 0.65–0.84 | Good — relevant context, minor gaps possible |
| 0.40–0.64 | Fair — partial context, check grounding warnings |
| 0.00–0.39 | Poor — insufficient indexed content for this query |

A low quality score usually means the repo isn't fully indexed yet, or the query is about code that hasn't been committed.
