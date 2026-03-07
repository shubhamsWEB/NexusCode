# NexusCode — Agentic Orchestration Design Brief
_Architecture deep-dive · March 2026_

## tl;dr

NexusCode already has a world-class single-agent loop (AgentLoop + ToolExecutor + MCP Bridge).
The gap is **multi-step, multi-agent, persistent workflows** that can run autonomously — triggered by
events, schedules, or other agents — and produce durable, auditable results.

The right answer is NOT to bolt on n8n.
The right answer is a **native Codebase Automation Engine (CAE)** that turns NexusCode into
a _programmable codebase brain_ — one that other tools (including n8n) can call as a step.

---

## Why Not n8n?

n8n is a generic Node.js workflow runner. It would:

| n8n strength | NexusCode reality |
|---|---|
| 400+ pre-built integrations | We need 3: GitHub, Slack, Jira |
| Visual low-code builder | Devs prefer YAML + API |
| Runs as separate service | Adds Node.js infra, cross-service latency |
| Workflow steps are HTTP calls | Every step needs LIVE codebase context |
| State is n8n-internal | We need state in our own Postgres for querying |

The fatal flaw: n8n steps are HTTP black boxes. They cannot embed codebase vectors,
stream agent thinking, or share token-budget-aware context across steps.

**n8n's right role:** An optional *external trigger source* — it posts to
`POST /workflows/{id}/run` from the "other world" (Slack, Jira, calendar).
NexusCode does the actual intelligence work.

---

## The Unique Bet: Codebase-Aware Workflow Orchestration

Generic orchestrators (n8n, Temporal, Prefect, Airflow) treat every step as a black box.
NexusCode's moat is that **every step in a workflow has semantic codebase context** —
vector search, symbol graphs, AST structure, git history — automatically injected into
the agent that executes it.

No other orchestrator can do this. This is the unique solution.

---

## Proposed Architecture: Codebase Automation Engine (CAE)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    TRIGGER LAYER                                        │
│  GitHub Webhook  │  APScheduler  │  REST API  │  n8n/Zapier Webhook    │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────────┐
│                    WORKFLOW REGISTRY                                    │
│  YAML/JSON workflow definitions · PostgreSQL storage · versioned        │
│  Triggers: webhook_filter, cron, manual, event_bus topic               │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────────┐
│                    DAG EXECUTOR  (extends RQ)                           │
│  Topological sort · Parallel branches · Per-step retry + backoff       │
│  State machine: PENDING → RUNNING → (WAITING_HUMAN) → DONE/FAILED     │
└──────────┬────────────────────────────────────────────────┬─────────────┘
           │                                                │
┌──────────▼──────────────────┐             ┌──────────────▼──────────────┐
│  MULTI-AGENT ROUTER         │             │  ACTION EXECUTOR            │
│  Supervisor spawns agents   │             │  Non-LLM steps:             │
│  with specialized roles:    │             │  • github.post_comment      │
│  • Searcher                 │             │  • slack.send_message       │
│  • Planner                  │             │  • jira.create_issue        │
│  • Reviewer                 │             │  • git.create_pr            │
│  • Coder                    │             │  • webhook.call_url         │
│  • Tester                   │             └─────────────────────────────┘
│  All share workflow context │
└──────────┬──────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────────────────┐
│  EXISTING AgentLoop + ToolExecutor + MCP Bridge (UNCHANGED)            │
│  Every agent step gets: codebase search + symbols + planning context   │
└──────────┬──────────────────────────────────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────────────────┐
│  EXECUTION CONTEXT STORE  (new Postgres tables)                        │
│  workflow_definitions · workflow_runs · step_executions                │
│  agent_messages · token_usage_ledger · human_checkpoints               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## New Database Tables (4 additions)

### `workflow_definitions`
```sql
id UUID PK, name TEXT, description TEXT, yaml_definition TEXT,
trigger_type ENUM(webhook, schedule, manual, event),
trigger_config JSONB,   -- {event_filter, cron_expr, webhook_path}
version INT, is_active BOOL,
created_at, updated_at
```

### `workflow_runs`
```sql
id UUID PK, workflow_id UUID FK,
status ENUM(pending, running, waiting_human, completed, failed, cancelled),
trigger_payload JSONB,   -- raw webhook/event that started this
started_at, completed_at,
total_tokens_used INT, total_cost_usd DECIMAL,
error_message TEXT, result JSONB
```

### `step_executions`
```sql
id UUID PK, run_id UUID FK, step_id TEXT,
status ENUM(pending, running, completed, failed, skipped),
agent_role TEXT,         -- searcher/planner/reviewer/coder/tester/action
input_context JSONB,     -- what was passed in
output JSONB,            -- what was produced
tokens_used INT, started_at, completed_at,
retry_count INT, error_message TEXT
```

### `human_checkpoints`
```sql
id UUID PK, run_id UUID FK, step_id TEXT,
prompt TEXT,             -- what the agent is asking the human
options JSONB,           -- suggested choices
response TEXT,           -- human's answer
status ENUM(waiting, answered, timed_out),
created_at, answered_at, timeout_at
```

---

## Workflow Definition Language (YAML DSL)

```yaml
name: automated-pr-review
description: Full AI code review on every PR opened
version: 1

trigger:
  type: webhook
  filter:
    event: github.pull_request.opened
    repos: ["*"]          # all indexed repos

context:
  repo: "{{ trigger.repo_owner }}/{{ trigger.repo_name }}"
  pr_number: "{{ trigger.pr_number }}"
  diff_url: "{{ trigger.diff_url }}"

steps:
  - id: fetch_diff
    type: action
    action: github.get_pr_diff
    params:
      pr_number: "{{ context.pr_number }}"
    output_as: diff

  - id: understand_changes
    type: agent
    role: searcher
    task: |
      Analyze this diff and identify:
      1. What files changed and why
      2. Related symbols and their callers
      3. Any patterns that suggest risk
    context_inject:
      - diff: "{{ steps.fetch_diff.output }}"
    tools: [search_codebase, get_symbol, find_callers]

  - id: security_review
    type: agent
    role: reviewer
    depends_on: [understand_changes]
    task: "Review for security vulnerabilities based on the change analysis"
    context_inject:
      - analysis: "{{ steps.understand_changes.output }}"
    parallel_with: [performance_review]

  - id: performance_review
    type: agent
    role: reviewer
    depends_on: [understand_changes]
    task: "Review for performance regressions or inefficiencies"
    context_inject:
      - analysis: "{{ steps.understand_changes.output }}"
    parallel_with: [security_review]

  - id: synthesize_review
    type: agent
    role: planner
    depends_on: [security_review, performance_review]
    task: "Synthesize all findings into a structured, actionable PR review comment"
    context_inject:
      - security: "{{ steps.security_review.output }}"
      - performance: "{{ steps.performance_review.output }}"

  - id: human_approval
    type: human_checkpoint
    depends_on: [synthesize_review]
    prompt: "Review the generated comment before posting. Approve or edit?"
    timeout_hours: 4
    on_timeout: skip     # post anyway if no response in 4h

  - id: post_comment
    type: action
    depends_on: [human_approval]
    action: github.post_pr_comment
    params:
      pr_number: "{{ context.pr_number }}"
      body: "{{ steps.synthesize_review.output }}"
```

---

## New API Endpoints (6 additions to src/api/)

```
POST /workflows              — Create/update workflow definition
GET  /workflows              — List all workflows
GET  /workflows/{id}         — Get definition + last 10 runs
POST /workflows/{id}/run     — Trigger manual run (or from n8n/Zapier)
GET  /workflows/runs/{run_id}         — Run status + step breakdown
GET  /workflows/runs/{run_id}/stream  — SSE live stream of agent thinking
POST /workflows/runs/{run_id}/checkpoint/{id}/respond — Human responds to checkpoint
```

---

## New MCP Tools (4 additions to src/mcp/server.py)

```python
@mcp_server.tool()
async def create_workflow(name: str, yaml_definition: str) -> dict:
    """Create or update a workflow automation."""

@mcp_server.tool()
async def run_workflow(workflow_id: str, payload: dict = {}) -> dict:
    """Trigger a workflow run with optional payload context."""

@mcp_server.tool()
async def get_workflow_status(run_id: str) -> dict:
    """Get detailed status of a workflow run including all step outputs."""

@mcp_server.tool()
async def list_workflows(active_only: bool = True) -> list:
    """List all workflow definitions with last run status."""
```

---

## Agent Roles (Specialized System Prompts)

Each role inherits the base AgentLoop but gets a specialized system prompt:

| Role | Focus | Default Tools |
|------|-------|---------------|
| `searcher` | Deep codebase navigation, symbol resolution | search_codebase, get_symbol, find_callers, get_file_context |
| `planner` | Implementation planning, step decomposition | plan_implementation, search_codebase |
| `reviewer` | Bug finding, security, performance | search_codebase, get_symbol, get_file_context |
| `coder` | Code generation, applying changes | search_codebase, get_agent_context, plan_implementation |
| `tester` | Test generation, coverage analysis | search_codebase, find_callers, get_file_context |
| `supervisor` | Task decomposition, result synthesis | All tools + spawn sub-agents |

---

## Event Bus (Redis pub/sub)

Extend the existing Redis connection (already used by RQ):

```python
# Topics
"nexus:events:github"    — GitHub webhook events (already received)
"nexus:events:schedule"  — APScheduler ticks
"nexus:events:manual"    — API-triggered runs
"nexus:events:external"  — n8n/Zapier/webhook sources
"nexus:workflow:updates" — Run status changes (for SSE clients)
```

Any workflow can subscribe to any topic. The event bus decouples triggers from execution.

---

## APScheduler Integration

Add to `src/scheduler/` :

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

async def load_scheduled_workflows():
    """Load all schedule-triggered workflows from DB and register jobs."""
    workflows = await db.get_workflows_by_trigger_type("schedule")
    for wf in workflows:
        scheduler.add_job(
            run_workflow_job,
            CronTrigger.from_crontab(wf.trigger_config["cron_expr"]),
            args=[wf.id],
            id=f"wf_{wf.id}",
            replace_existing=True,
        )
```

Scheduler starts alongside FastAPI (lifespan event). No new infrastructure needed.

---

## Implementation Roadmap (4 sprints)

### Sprint 1 — Foundation (Week 1-2)
- [ ] New DB tables: workflow_definitions, workflow_runs, step_executions
- [ ] WorkflowRegistry: CRUD in `src/workflows/registry.py`
- [ ] YAML parser + validator: `src/workflows/parser.py`
- [ ] Basic DAG executor (sequential steps only): `src/workflows/executor.py`
- [ ] REST endpoints: POST/GET /workflows, POST /workflows/{id}/run
- [ ] Streamlit page: workflow list + manual trigger

### Sprint 2 — Multi-Agent (Week 3-4)
- [ ] Agent role definitions + specialized system prompts: `src/agent/roles.py`
- [ ] Multi-Agent Router (supervisor spawns sub-agents): `src/agent/router.py`
- [ ] Parallel step execution (asyncio.gather on independent branches)
- [ ] Shared execution context store (inter-step data passing)
- [ ] Token usage ledger per step + total cost tracking

### Sprint 3 — Triggers & Events (Week 5-6)
- [ ] Event Bus (Redis pub/sub): `src/events/bus.py`
- [ ] APScheduler integration: `src/scheduler/`
- [ ] GitHub webhook → event bus routing (extend existing webhook handler)
- [ ] Human checkpoint table + API endpoint + Streamlit notification
- [ ] SSE streaming endpoint for live workflow progress

### Sprint 4 — Polish & Tooling (Week 7-8)
- [ ] 4 new MCP tools (create/run/status/list workflows)
- [ ] Streamlit workflow designer (YAML editor + DAG visualization)
- [ ] Workflow execution dashboard (Gantt-style step timeline)
- [ ] n8n integration guide + sample n8n template that calls NexusCode
- [ ] Built-in workflow templates: PR review, nightly audit, security scan

---

## Day-1 Killer Use Cases

1. **Automated PR Review** — Any PR → full AI review with security + perf + style → GitHub comment
2. **Nightly Codebase Audit** — Scheduled → scan for tech debt + security patterns → Slack digest
3. **Issue-to-Plan Pipeline** — GitHub issue opened → implementation plan generated → posted to issue
4. **Breaking Change Detector** — Push to main → find callers of changed symbols → flag risky changes
5. **New Repo Onboarding** — Repo registered → auto-index → generate architecture doc → post to wiki

---

## What Makes This Unstoppable

Every step in a NexusCode workflow gets:
- **Semantic codebase search** (voyage-code-2 embeddings)
- **Symbol resolution** (AST-extracted call graphs)
- **Planning context** (7-phase retrieval pipeline)
- **Multi-provider LLM** (Claude, GPT-4o, Ollama)
- **Extended thinking** (Claude Opus for complex steps)

No generic orchestrator (n8n, Temporal, Airflow) can give agents this.
This is the defensible moat.
