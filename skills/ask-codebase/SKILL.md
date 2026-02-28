---
name: ask-codebase
description: Answer natural-language questions about the codebase in a mentor tone. Retrieves relevant code context, then explains how it works with inline citations to real file paths and line numbers. Use for "how does X work?", "where is Y handled?", "why does Z do this?", and similar exploratory questions. Returns answer, cited files, and follow-up hints.
metadata:
  author: nexuscode
  version: "1.0"
compatibility: Requires a running NexusCode API server at http://localhost:8000 with at least one LLM API key configured.
---

# Ask Codebase Skill

## What it does

Ask Mode answers natural-language questions about indexed codebases in a mentor tone — clear, direct, grounded in real code with concrete citations. It's designed for developer Q&A, not for generating code changes.

## When to use

- **Exploratory questions**: "How does authentication work?", "Where is the webhook handler?"
- **Architecture questions**: "What's the data flow from search to response?", "How are chunks stored?"
- **Debugging context**: "Why does the reranker return scores < 0?", "What calls embed_query?"
- **Learning**: "Walk me through how the pipeline indexes a file"

Use `plan-implementation` instead when you need to make code changes.

## API Reference

### POST /ask

```http
POST http://localhost:8000/ask
Content-Type: application/json

{
  "query": "How does hybrid search work?",
  "repo_owner": "myorg",       // optional: scope to repo
  "repo_name": "myrepo",       // optional: scope to repo
  "stream": true,              // SSE stream (default: true)
  "session_id": "uuid",        // optional: continue a session
  "model": "claude-sonnet-4-6" // optional: override LLM
}
```

**Streaming response (SSE)**:
```
data: {"type": "token", "text": "Hybrid search combines..."}
data: {"type": "answer_complete", "result": {...}, "session_id": "uuid"}
```

**Sync response** (`stream: false`):
```json
{
  "answer": "Hybrid search combines...",
  "cited_files": ["src/retrieval/searcher.py:85-90"],
  "follow_up_hints": ["How does RRF work?", "What is the reranker?"],
  "quality_score": 0.87,
  "elapsed_ms": 1240,
  "session_id": "uuid"
}
```

### MCP Tool: ask_codebase

```python
result = await ask_codebase(
    question="How does the reranker work?",
    repo="owner/name",  # optional
    model="claude-sonnet-4-6",  # optional
)
```

## Response fields

| Field | Description |
|---|---|
| `answer` | Full markdown answer with inline code citations |
| `cited_files` | List of `path:line_range` strings for every cited file |
| `follow_up_hints` | 2-3 concrete follow-up questions grounded in the codebase |
| `quality_score` | Context retrieval confidence (0.0–1.0) |
| `session_id` | Session UUID for continuing the conversation |
| `elapsed_ms` | Total response time in milliseconds |

## Session continuity

Each response returns a `session_id`. Pass it back in subsequent requests to continue a conversation thread. Sessions are stored in the `chat_sessions` / `chat_turns` tables and visible in the History tab of the dashboard.

## Supported models

Any model configured via API key in `.env`. Use `GET /models` to see available models. Default is `settings.default_model` (usually `claude-sonnet-4-6`).
