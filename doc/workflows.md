# Workflow Automation

NexusCode's Workflow Engine lets you define multi-step agentic automations as YAML files.
Workflows can automatically analyze PRs, triage alerts, generate RCA reports, run security
audits, and more — all with AI agents collaborating in a structured DAG.

---

## Creating a Workflow

### Via Dashboard

1. Open `http://localhost:8501` → **⚡ Workflows** tab
2. Click **➕ New Workflow**
3. Choose **Guided Builder** (form-based) or **YAML Editor** (direct YAML)
4. Save the workflow

### Via API

```bash
curl -X POST http://localhost:8000/workflows \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-workflow",
    "description": "What this workflow does",
    "yaml_definition": "name: my-workflow\n..."
  }'
```

---

## Workflow YAML Structure

```yaml
name: workflow-name          # Unique name (used in API calls)
description: "Summary"

trigger:
  type: manual | webhook | schedule | event
  webhook_path: /webhooks/my-path   # for webhook trigger
  cron_expr: "0 9 * * MON"          # for schedule trigger
  event_topic: "deploy.completed"   # for event trigger

context:                     # Global variables available in all steps
  team: "platform"

steps:
  - id: step-id
    type: agent | action | human_checkpoint
    role: searcher | planner | reviewer | coder | tester | supervisor
    depends_on: [other-step-id]
    tools: [search_codebase, generate_pdf]   # optional allowlist
    max_retries: 2
    task: |
      Task prompt here.
      {{ trigger.field }} — trigger payload
      {{ steps.prev_step.output }} — previous step result
      {{ context.team }} — workflow context
```

---

## Step Types

### `agent` — LLM Agent

An AI agent with a specialized role runs to produce text output.

```yaml
- id: review_code
  type: agent
  role: reviewer
  depends_on: [fetch_diff]
  task: |
    Review this PR diff for bugs, security issues, and code quality:
    {{ steps.fetch_diff.output }}
```

**Available roles:**
| Role | Best for |
|------|---------|
| `searcher` | Finding relevant code, tracing call paths |
| `planner` | Breaking down implementation tasks into steps |
| `reviewer` | Code review, security analysis |
| `coder` | Writing new code in the right style |
| `tester` | Test generation and coverage analysis |
| `supervisor` | Synthesizing outputs, writing final documents |

### `action` — Built-in Integration

Runs a deterministic action without an LLM. Fast and predictable.

```yaml
- id: post_result
  type: action
  action: github.post_pr_comment
  depends_on: [review_code]
  params:
    repo_owner: "{{ trigger.repo_owner }}"
    repo_name:  "{{ trigger.repo_name }}"
    pr_number:  "{{ trigger.pr_number }}"
    body:       "{{ steps.review_code.output }}"
```

**Available actions:**
| Action | Purpose |
|--------|---------|
| `github.get_pr_diff` | Fetch PR diff via GitHub API |
| `github.post_pr_comment` | Post a comment on a PR |
| `slack.send_message` | Send a Slack message |
| `webhook.call_url` | POST JSON to any URL |

### `human_checkpoint` — Pause for Approval

Pauses execution until a human responds via the UI or API.

```yaml
- id: approve_deploy
  type: human_checkpoint
  depends_on: [security_review]
  prompt: "Security review complete. Approve deployment?"
  options:
    - "Approve"
    - "Reject"
    - "Request more analysis"
  timeout_hours: 24
  on_timeout: continue    # or: fail
```

The human responds in the **Run History** UI (an interactive card appears) or via:
```bash
POST /workflows/checkpoints/{cp_id}/respond
{"response": "Approve"}
```

After responding, the workflow automatically resumes.

---

## Template Variables

Inside any `task`, `prompt`, or `params` value, use Jinja2 templates:

| Variable | Value |
|----------|-------|
| `{{ trigger.FIELD }}` | Any field from the trigger payload |
| `{{ steps.STEP_ID.output }}` | Text output of a completed step |
| `{{ context.KEY }}` | Value from the workflow's `context` section |
| `{{ trigger.FIELD \| default("N/A") }}` | With Jinja2 default filter |

---

## Running a Workflow

### Via Dashboard

1. **⚡ Workflows** → click your workflow → **▶ Run**
2. Paste a JSON trigger payload (required for webhook workflows)
3. Click **🚀 Trigger run**
4. Switch to **🕐 Run History** to watch progress

### Via API

```bash
# Trigger with payload
curl -X POST http://localhost:8000/workflows/rca-automation/run \
  -H "Content-Type: application/json" \
  -d '{
    "payload": {
      "service": "payment-api",
      "environment": "production",
      "severity": "HIGH",
      "error_message": "Connection pool exhausted",
      "stack_trace": "at PaymentService.charge (payment.ts:45)",
      "affected_component": "checkout",
      "source": "DataDog",
      "timestamp": "2026-03-08T14:30:00Z"
    }
  }'

# Returns immediately:
{"run_id": "abc123", "status": "running"}
```

### Stream Live Progress

```bash
curl -N http://localhost:8000/workflows/runs/abc123/stream

# Events:
# data: {"type":"workflow_started","run_id":"abc123","workflow":"rca-automation"}
# data: {"type":"step_started","step_id":"understand_error","role":"searcher"}
# data: {"type":"step_complete","step_id":"understand_error","tokens":12480}
# data: {"type":"checkpoint_created","checkpoint_id":"cp-xyz","prompt":"Approve?"}
# data: {"type":"workflow_complete","tokens_total":48920}
```

---

## Run History

View all runs for all workflows:

```bash
GET /workflows/runs?limit=20&workflow_id=optional-id

# Get a specific run with all step details
GET /workflows/runs/{run_id}
```

**Response includes:**
- Run status, token usage, timing
- Each step: status, output, tokens, errors, retries
- Pending checkpoints with prompt text
- Generated documents (PDFs) attached to steps

---

## PDF Reports in Workflows

When a `supervisor` agent calls the `generate_pdf` tool, a downloadable PDF is automatically
attached to the step in Run History.

**Enable in a step:**
```yaml
- id: compile_report
  type: agent
  role: supervisor
  tools: [search_codebase, ask_codebase, generate_pdf]
  task: |
    Write the complete analysis document as markdown, then call generate_pdf with:
      content: the full markdown
      title: "Analysis: {{ trigger.service }}"
      filename: "analysis-{{ trigger.service }}"
      metadata: {"service": "{{ trigger.service }}", "severity": "{{ trigger.severity }}"}

    Include the download_url in your answer.
```

**Download the PDF:**
```bash
GET /documents/{doc_id}/download
→ Content-Type: application/pdf
→ Content-Disposition: attachment; filename="analysis-payment-api.pdf"
```

See [pdf-generation.md](./pdf-generation.md) for full details.

---

## Built-in RCA Workflow

`rca_workflow.yaml` (included in the project) automates production incident root cause analysis:

```
Step 1: understand_error   (searcher)   — Find error origin in codebase
Step 2: identify_root_cause (reviewer)  — Pinpoint exact failure mechanism
Step 3: create_fix_plan    (planner)    — 3-option fix plan (hotfix + proper + long-term)
Step 4: review_checkpoint  (human)      — Manual approval before doc generation
Step 5: compile_rca_doc    (supervisor) — Generate full RCA document + PDF
```

Upload it:
```bash
curl -X POST http://localhost:8000/workflows \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"rca-automation\", \"yaml_definition\": $(cat rca_workflow.yaml | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")}"
```

---

## Managing Workflows

```bash
# List all workflows
GET /workflows?active_only=true

# Get workflow definition + last 10 runs
GET /workflows/{workflow_id}

# Delete workflow + all run history
DELETE /workflows/{workflow_id}

# Enable/disable (edit YAML, re-save)
POST /workflows   # same name = update
```

---

## Parallel Step Execution

Steps with the same dependencies automatically run in parallel:

```yaml
steps:
  - id: fetch_data    # Wave 0
    type: action
    ...

  - id: search_code   # Wave 1 (after fetch_data)
    type: agent
    depends_on: [fetch_data]
    ...

  - id: check_tests   # Wave 1 (after fetch_data, runs IN PARALLEL with search_code)
    type: agent
    depends_on: [fetch_data]
    ...

  - id: compile_report  # Wave 2 (after both Wave 1 steps complete)
    type: agent
    depends_on: [search_code, check_tests]
    ...
```

---

## Retry Configuration

Each agent step retries on failure with exponential backoff:

```yaml
- id: my_step
  type: agent
  max_retries: 3          # default: 2
  retry_delay_seconds: 60 # default: 30
  # Retry delays: 60s → 120s → 240s
```

---

## Custom Agent Tool Allowlists

By default, each role uses its configured `default_tools`. Override per-step:

```yaml
- id: analyze
  type: agent
  role: searcher
  tools: [search_codebase, get_symbol]   # only these tools, not the full role default
```

External MCP tools can also be included here by name (if registered via `/mcp-servers`).
