# Workflow Engine

NexusCode's Workflow Engine lets you define multi-step agentic automations as YAML files.
Each workflow is a DAG of steps that execute agents, call integrations, or pause for human input.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Workflow Lifecycle                     │
│                                                          │
│  DEFINE ──► STORE ──► TRIGGER ──► EXECUTE ──► STREAM    │
│  (YAML)   (DB)      (API/webhook) (DAG)    (SSE)        │
└─────────────────────────────────────────────────────────┘

DEFINE:   Write YAML DSL → POST /workflows
STORE:    workflow_definitions table in PostgreSQL
TRIGGER:  POST /workflows/{id}/run  (with JSON payload)
EXECUTE:  WorkflowExecutor.stream() → topological wave execution
STREAM:   SSE events via GET /workflows/runs/{run_id}/stream
```

---

## YAML DSL

```yaml
name: my-workflow               # Unique name (used in API calls)
description: "What this does"   # Human-readable summary

trigger:
  type: webhook | schedule | manual | event
  webhook_path: /webhooks/alerts   # for webhook triggers
  cron_expr: "0 9 * * MON"         # for schedule triggers
  event_topic: "deploy.completed"  # for event triggers
  filter:                          # optional payload filter
    environment: production

context:                          # Global key-value context
  repo: "my-org/my-service"
  team: "platform"

steps:
  - id: step-name                 # Unique within workflow
    type: agent | action | human_checkpoint
    role: searcher | planner | reviewer | coder | tester | supervisor
    depends_on: [other-step-id]   # DAG dependency
    parallel_with: [other-step]   # hint for parallel execution
    tools: [tool1, tool2]         # allowlist (optional)
    max_retries: 2                # default 2
    retry_delay_seconds: 30       # default 30
    task: |
      Multi-line task prompt.
      Use {{ trigger.field }} for trigger data.
      Use {{ steps.step_id.output }} for prior step results.
      Use {{ context.key }} for workflow context.
```

---

## Step Types

### `agent` — LLM Agent Step

Runs an `AgentLoop` with the specified role. Produces text output.

```yaml
- id: analyze_code
  type: agent
  role: reviewer
  depends_on: [fetch_diff]
  tools: [search_codebase, get_symbol, find_callers]
  task: |
    Review the following diff for bugs and security issues:
    {{ steps.fetch_diff.output }}
```

**Role selection guide:**
| Role | Best for |
|------|----------|
| `searcher` | Finding relevant code, tracing call paths |
| `planner` | Breaking down implementation tasks |
| `reviewer` | Code review, security analysis |
| `coder` | Writing new code |
| `tester` | Test generation and coverage analysis |
| `supervisor` | Synthesizing outputs, generating final documents |

### `action` — Integration Step

Calls a built-in action without an LLM. Fast and deterministic.

```yaml
- id: post_comment
  type: action
  action: github.post_pr_comment
  depends_on: [review]
  params:
    repo_owner: "{{ trigger.repo_owner }}"
    repo_name:  "{{ trigger.repo_name }}"
    pr_number:  "{{ trigger.pr_number }}"
    body:       "{{ steps.review.output }}"
```

**Available actions:**
| Action | Purpose |
|--------|---------|
| `github.get_pr_diff` | Fetch PR diff via GitHub API |
| `github.post_pr_comment` | Post a comment on a PR |
| `slack.send_message` | Send a Slack message (configurable) |
| `webhook.call_url` | POST to an arbitrary URL |

### `human_checkpoint` — Pause for Human Input

Pauses workflow execution until a human responds via the UI or API.

```yaml
- id: approve_deploy
  type: human_checkpoint
  depends_on: [review]
  prompt: "Review complete. Approve deployment to production?"
  options:
    - "Approve"
    - "Reject"
    - "Request changes"
  timeout_hours: 24
  on_timeout: continue | fail
```

The checkpoint appears in the Run History UI as an interactive card. When the human submits
their response, the workflow resumes from the next step. The response is available as
`{{ steps.approve_deploy.output }}`.

---

## DAG Execution Model

### Dependency Resolution

```python
# Kahn's algorithm for topological sort + cycle detection
def topological_order(workflow: WorkflowDef) -> list[list[StepDef]]:
    # Returns waves: steps in the same wave can run in parallel
    # Wave 0: steps with no dependencies
    # Wave 1: steps whose deps are all in Wave 0
    # etc.
```

### Parallel Execution

Steps within the same wave execute concurrently:

```python
for wave in waves:
    if len(wave) == 1:
        # Single step — run directly
        async for evt in self._execute_step(wave[0]):
            yield evt
    else:
        # Multiple independent steps — run in parallel
        tasks = [self._collect_step(s) for s in wave]
        results = await asyncio.gather(*tasks, return_exceptions=True)
```

### Retry Logic

Each step retries with exponential backoff:

```python
for attempt in range(step.max_retries + 1):
    try:
        output = await run_step(step)
        break
    except Exception as exc:
        if attempt < step.max_retries:
            delay = step.retry_delay_seconds * (2 ** attempt)
            # 30s → 60s → 120s
            await asyncio.sleep(delay)
        else:
            # Mark step as failed, yield step_failed event, re-raise
            raise
```

---

## Template Engine

Workflow steps use **Jinja2** templating to inject dynamic values:

```
{{ trigger.field_name }}           → value from the trigger payload
{{ steps.step_id.output }}         → text output from a completed step
{{ context.key }}                  → global workflow context value
{{ trigger.timestamp | default("unknown") }}   → with Jinja2 filters
```

The `ExecutionContext` object maintains:
- `trigger_payload`: original trigger JSON
- `step_outputs`: dict of completed step outputs
- `workflow_context`: YAML context section values

---

## SSE Event Stream

Connect to the live event stream for a running workflow:

```
GET /workflows/runs/{run_id}/stream
Content-Type: text/event-stream

data: {"type": "workflow_started", "run_id": "abc123", "workflow": "rca-automation"}

data: {"type": "step_started", "run_id": "abc123", "step_id": "understand_error",
       "step_type": "agent", "role": "searcher"}

data: {"type": "step_complete", "run_id": "abc123", "step_id": "understand_error",
       "tokens": 12480}

data: {"type": "checkpoint_created", "run_id": "abc123", "step_id": "review_checkpoint",
       "checkpoint_id": "cp-xyz", "prompt": "Review and approve?"}

data: {"type": "step_complete", "run_id": "abc123", "step_id": "review_checkpoint",
       "checkpoint_response": "Approve and generate RCA doc"}

data: {"type": "workflow_complete", "run_id": "abc123", "tokens_total": 48920}
```

---

## Run Storage

Every run creates a record in `workflow_runs` with:
- `id`: UUID
- `workflow_id`: FK to workflow_definitions
- `status`: pending → running → completed | failed | waiting_human
- `trigger_payload`: original JSON
- `context_snapshot`: Jinja2 context after completion
- `total_tokens_used`: cumulative LLM token count
- `started_at`, `completed_at`

Each step creates a record in `workflow_step_executions` with:
- `status`, `output` (JSONB), `tokens_used`
- `error_message`, `retry_count`
- `started_at`, `completed_at`

**Agent steps with generated PDFs** have their `output` enriched:
```json
{
  "text": "RCA document markdown...",
  "documents": [
    {"doc_id": "uuid", "filename": "rca-payment.pdf", "size_bytes": 45312}
  ]
}
```

---

## Human Checkpoint Flow

```
1. Executor creates checkpoint record:
   INSERT INTO human_checkpoints (run_id, step_id, prompt, options, timeout_hours)

2. Run status → "waiting_human"

3. Executor polls (every 10s) until:
   ● Checkpoint status == "answered" → resume
   ● Deadline exceeded → timeout behavior (skip or fail)

4. Human responds via:
   ● Dashboard UI: Run History → step card → radio/text → Submit
   ● API: POST /workflows/checkpoints/{cp_id}/respond {"response": "Approve"}

5. Executor sets run status → "running", continues with next wave
```

---

## Real-World Example: RCA Automation

The `rca_workflow.yaml` demonstrates a full 5-step automated RCA:

```
Wave 0: understand_error (searcher) ──────────────────────────────────┐
                                                                      │
Wave 1: identify_root_cause (reviewer, depends_on: understand_error)  │
                                                                      │
Wave 2: create_fix_plan (planner, depends_on: identify_root_cause)    │
                                                                      │
Wave 3: review_checkpoint (human, depends_on: create_fix_plan)        │
        → Pauses for human approval                                   │
                                                                      │
Wave 4: compile_rca_doc (supervisor, depends_on: review_checkpoint)   │
        → Writes full RCA markdown document                           │
        → Calls generate_pdf tool                                     │
        → PDF stored in PostgreSQL                                    │
        → Download button appears in Run History UI                   │
```

Trigger the RCA workflow:
```bash
POST /workflows/rca-automation/run
{
  "payload": {
    "service": "payment-api",
    "environment": "production",
    "severity": "HIGH",
    "error_message": "Connection pool exhausted",
    "stack_trace": "...",
    "affected_component": "checkout",
    "source": "DataDog",
    "timestamp": "2026-03-08T14:30:00Z"
  }
}
```
