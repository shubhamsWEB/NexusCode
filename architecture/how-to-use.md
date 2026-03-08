# How to Use NexusCode — End-to-End Guide

This guide walks you through every major NexusCode feature from initial setup to advanced
workflow automation.

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | Required |
| PostgreSQL | 15+ | With `pgvector` extension |
| Redis | 6+ | For job queue and caching |
| Voyage AI API Key | — | For code embeddings |
| LLM API Key | — | Anthropic (recommended), OpenAI, or Grok |
| GitHub Token | — | For indexing repositories |

---

## Step 1: Installation

```bash
# Clone and set up environment
git clone https://github.com/your-org/nexuscode-server
cd nexuscode-server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Install weasyprint system deps (for PDF generation)
# macOS:
brew install pango cairo
# Linux (Debian/Ubuntu):
apt-get install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2 libffi-dev
```

---

## Step 2: Configure Environment

Copy `.env.example` to `.env` and fill in the required values:

```bash
# Minimum required
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/nexuscode
REDIS_URL=redis://localhost:6379
VOYAGE_API_KEY=your-voyage-api-key
ANTHROPIC_API_KEY=your-anthropic-api-key      # for Claude
GITHUB_TOKEN=ghp_your-personal-access-token
GITHUB_WEBHOOK_SECRET=your-webhook-secret
JWT_SECRET=your-256-bit-random-secret

# Optional: additional LLM providers
OPENAI_API_KEY=sk-...
GROK_API_KEY=xai-...
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODELS=llama3.3,qwen2.5-coder
```

---

## Step 3: Initialize Database

```bash
PYTHONPATH=. python scripts/init_db.py

# Or run migrations manually in order:
for f in src/storage/migrations/*.sql; do
    psql $DATABASE_URL -f $f
done
```

---

## Step 4: Start All Services

```bash
# Terminal 1: API server (port 8000)
PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
  uvicorn src.api.app:app --port 8000 --reload

# Terminal 2: RQ worker (background indexing jobs)
PYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
  rq worker indexing --url redis://localhost:6379

# Terminal 3: Streamlit dashboard (port 8501)
PYTHONPATH=. API_URL=http://localhost:8000 \
  streamlit run src/ui/dashboard.py --server.port 8501
```

Or with Docker Compose:
```bash
docker-compose up -d
```

Verify everything is running:
```bash
curl http://localhost:8000/health
# → {"status": "ok", "chunks": 0, "repos": 0, "symbols": 0}
```

---

## Step 5: Index Your First Repository

### Option A: Via Dashboard

1. Open `http://localhost:8501`
2. Navigate to **📦 Repositories**
3. Click **Register Repo**
4. Enter `owner/name` (e.g. `microsoft/vscode`)
5. Click **Index Now**
6. Watch progress in the **📡 Activity Feed**

### Option B: Via API

```bash
# Register repo
curl -X POST http://localhost:8000/repos \
  -H "Content-Type: application/json" \
  -d '{"owner": "myorg", "name": "myrepo", "branch": "main"}'

# Trigger full index
curl -X POST http://localhost:8000/repos/myorg/myrepo/index

# Check progress
curl http://localhost:8000/events?limit=5
```

### Option C: From CLAUDE.md (for Claude Code users)

Add to your project's `CLAUDE.md`:
```markdown
Use NexusCode for all codebase lookups.
MCP server: http://localhost:8000/mcp/sse (token: Bearer YOUR_TOKEN)
```

---

## Step 6: Set Up GitHub Webhooks (for Live Updates)

```bash
# Auto-register webhook (easiest — requires admin access)
POST /repos/{owner}/{name}/webhook

# Or copy the webhook URL from:
GET http://localhost:8000/config
# Look for "webhook_url"

# Then add manually in GitHub:
# → Settings → Webhooks → Add webhook
# URL: https://your-server.com/webhook/github
# Content-Type: application/json
# Secret: your GITHUB_WEBHOOK_SECRET
# Events: Just the push event
```

After webhook setup, every `git push` will automatically update the index within 2–4 seconds.

---

## Step 7: Search the Codebase

### Via MCP (Claude Code / Cursor)

After [MCP setup](./mcp-integration.md):
```
Use search_codebase to find the authentication flow
get_symbol AuthService.authenticate
find_callers validate_token
```

### Via REST API

```bash
# Hybrid search
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "JWT token validation",
    "mode": "hybrid",
    "top_k": 5,
    "repo": "myorg/myrepo"
  }'

# Response includes: results[], context (pre-assembled), tokens_used
```

### Via Dashboard

1. Open **🔍 Query Tester**
2. Type your query
3. Select mode (hybrid/semantic/keyword)
4. View results with syntax-highlighted previews

---

## Step 8: Use Ask Mode

Ask Mode answers natural-language questions about your codebase with citations.

```bash
# Ask a question
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How does the webhook HMAC verification work?",
    "repo_owner": "myorg",
    "repo_name": "myrepo"
  }'

# With streaming (real-time answer)
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "...", "stream": true}' \
  --no-buffer
```

**Response includes:**
- `answer`: mentor-tone markdown explanation with citations
- `cited_files`: list of referenced files
- `follow_up_hints`: 2-3 suggested follow-up questions
- `session_id`: for conversation continuity

**Continue a conversation:**
```bash
curl -X POST http://localhost:8000/ask \
  -d '{"query": "And how are tokens refreshed?", "session_id": "uuid-from-previous"}'
```

---

## Step 9: Use Planning Mode

Planning Mode generates structured implementation plans grounded in your codebase.

```bash
curl -X POST http://localhost:8000/plan \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Add Redis-based rate limiting to POST /auth/login — max 5 attempts per IP per minute",
    "repo_owner": "myorg",
    "repo_name": "myrepo",
    "web_research": true
  }'
```

**Response includes:**
- `summary`: High-level approach
- `files`: Exact files to change with action (MODIFY/CREATE/DELETE)
- `steps`: Ordered implementation steps with dependencies
- `risks`: Severity-tagged risks with mitigations
- `test_plan`: Concrete test scenarios
- `sparc_summary`: SPARC-structured breakdown

---

## Step 10: Set Up Workflow Automation

### Create a Workflow

```bash
# Upload from file
curl -X POST http://localhost:8000/workflows \
  -H "Content-Type: application/json" \
  -d @rca_workflow.yaml | python -m json.tool

# Or paste YAML directly:
curl -X POST http://localhost:8000/workflows \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-workflow",
    "yaml_definition": "name: my-workflow\n..."
  }'
```

### Trigger a Workflow

```bash
# Simple run
curl -X POST http://localhost:8000/workflows/my-workflow/run \
  -H "Content-Type: application/json" \
  -d '{"payload": {"repo_owner": "myorg", "repo_name": "myrepo"}}'

# Returns immediately: {"run_id": "abc123", "status": "running"}
```

### Stream the Run Progress

```bash
curl -N http://localhost:8000/workflows/runs/abc123/stream
```

### Respond to a Human Checkpoint

```bash
curl -X POST http://localhost:8000/workflows/checkpoints/{cp_id}/respond \
  -H "Content-Type: application/json" \
  -d '{"response": "Approve and generate RCA doc"}'
```

### Download Generated PDFs

When a supervisor agent calls `generate_pdf`, a download button appears in the Run History UI.
Or fetch directly:

```bash
curl http://localhost:8000/documents/{doc_id}/download -o report.pdf
```

---

## Step 11: Connect to Claude via MCP

```bash
# Generate a token
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"expires_hours": 168}'

# Add to Claude Code settings (~/.claude/settings.json)
{
  "mcpServers": {
    "nexuscode": {
      "type": "sse",
      "url": "http://localhost:8000/mcp/sse",
      "headers": {"Authorization": "Bearer YOUR_TOKEN"}
    }
  }
}
```

---

## Step 12: Add External MCP Tools (Optional)

Connect external MCP servers to extend your workflow agents' capabilities:

```bash
# Add Context7 for library documentation
curl -X POST http://localhost:8000/mcp-servers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "context7",
    "url": "https://mcp.context7.com/sse",
    "description": "Library documentation lookup"
  }'

# Reload bridge
curl -X POST http://localhost:8000/mcp-servers/reload
```

Workflow agents can now use Context7 tools alongside NexusCode's built-in tools.

---

## Step 13: Customize Agent Roles

```bash
# Create a custom role
curl -X PUT http://localhost:8000/agent-roles/security-auditor \
  -H "Content-Type: application/json" \
  -d '{
    "system_prompt": "You are a security auditor specialized in finding vulnerabilities...",
    "default_tools": ["search_codebase", "get_symbol", "find_callers"],
    "require_search": true,
    "max_iterations": 8,
    "token_budget": 100000
  }'

# Use in a workflow step:
# - id: audit_auth
#   type: agent
#   role: security-auditor
#   task: "Find all authentication bypass vulnerabilities..."
```

---

## Step 14: View History

```bash
# List recent Ask sessions
GET /history/ask?limit=20

# Get full session with all turns
GET /history/ask/{session_id}

# List recent plans
GET /history/plan?limit=20
```

---

## Step 15: Explore the Knowledge Graph

```bash
# Build graph for a repo
curl -X POST http://localhost:8000/graph/myorg/myrepo/build

# Get graph data
curl "http://localhost:8000/graph/myorg/myrepo?view=all&max_nodes=200"

# Response: nodes[] + edges[] for D3/vis.js visualization
```

---

## Common CLI Commands Reference

```bash
# Start API server
PYTHONPATH=. uvicorn src.api.app:app --port 8000

# Start RQ worker (required for indexing)
PYTHONPATH=. rq worker indexing --url redis://localhost:6379

# Start dashboard
PYTHONPATH=. API_URL=http://localhost:8000 streamlit run src/ui/dashboard.py --server.port 8501

# Run all tests
PYTHONPATH=. pytest tests/ -v

# Check deployment readiness
PYTHONPATH=. python scripts/deploy_check.py

# Full-index a repo from CLI
PYTHONPATH=. python scripts/full_index.py --owner myorg --name myrepo

# Test a search query from CLI
PYTHONPATH=. python scripts/test_query.py "JWT token validation"

# Simulate a webhook push (for testing)
PYTHONPATH=. python scripts/simulate_webhook.py --file src/auth/service.py
```

---

## Troubleshooting

### "No results found"
- Confirm repo is indexed: `GET /repos`
- Check indexing logs: `GET /events?limit=10`
- Try keyword mode for exact identifiers: `"mode": "keyword"`

### "Embedding failed"
- Verify `VOYAGE_API_KEY` is set and valid
- Check Voyage AI API status at `status.voyageai.com`

### "LLM call failed"
- Check API key: `GET /models` (only configured providers appear)
- Verify you have at least one LLM API key in `.env`

### "RQ worker not processing jobs"
- Must run as `rq worker indexing` (not `python -m rq worker`)
- Requires `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` on macOS
- Verify Redis is running: `redis-cli ping`

### "MCP connection refused"
- Verify API server is running on port 8000
- Check token hasn't expired: `GET /auth/verify`
- Ensure `Authorization: Bearer TOKEN` header is correct

### Dashboard shows "Cannot connect to API"
- Ensure `API_URL=http://localhost:8000` env var is set when starting Streamlit
- Check API server health: `curl http://localhost:8000/health`
