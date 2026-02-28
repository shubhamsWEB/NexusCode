# Custom Skills

Skills are Markdown documents that describe capabilities, workflows, and domain knowledge. AI agents (Claude Desktop, Claude Code, custom automation) can discover them via `GET /skills` or the `list_skills` MCP tool and use them to understand how to work with NexusCode and your codebase.

---

## What a Skill Is

A skill is a `SKILL.md` file with a YAML frontmatter block. The rest of the file is freeform Markdown — documentation, instructions, API references, domain knowledge, whatever your team needs.

```
skills/
  your-skill-name/
    SKILL.md          ← required
    references/       ← optional supporting files
    examples/         ← optional examples
```

---

## The SKILL.md Format

```markdown
---
name: your-skill-name
description: One sentence describing what this skill does and when to use it.
metadata:
  author: your-team
  version: "1.0"
  tags: ["payments", "api"]
compatibility: Any constraints (e.g. requires /plan endpoint)
---

# Your Skill Title

## What it does
Explain what this skill enables an agent to do.

## When to use
- Scenario A
- Scenario B

## How to use
Specific instructions for invoking this skill.

## API Reference
Document relevant API calls, parameters, and response shapes.

## Examples
Show concrete examples.
```

**Required frontmatter fields:**

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Identifier used in `GET /skills/{name}` — use kebab-case |
| `description` | Yes | One sentence; shown in skill listings and to agents |
| `metadata` | No | Any additional key-value data |
| `compatibility` | No | Notes about required server config or environment |

---

## Built-in Skills

NexusCode ships with 4 built-in skills in the `skills/` directory:

| Skill | Description |
|---|---|
| `plan-implementation` | Generate grounded implementation plans before writing code |
| `search-codebase` | Search code, look up symbols, get pre-assembled context |
| `manage-repos` | Register repos, trigger indexing, inspect stats |
| `ask-codebase` | Answer natural-language questions about the codebase |

---

## Adding Custom Skills

### Step 1: Create the skill directory and SKILL.md

```bash
mkdir -p /path/to/your-team-skills/payments-domain

cat > /path/to/your-team-skills/payments-domain/SKILL.md << 'EOF'
---
name: payments-domain
description: Domain knowledge for the payments service — billing cycles, webhook events, Stripe integration patterns, and common failure modes.
metadata:
  author: payments-team
  version: "1.0"
  tags: ["payments", "stripe", "billing"]
---

# Payments Domain Knowledge

## Billing Cycle
Our billing cycle runs on the 1st of each month. `BillingService.run_cycle()` in
`src/billing/service.py` is the entry point. It is triggered by a cron job in
`infra/cron/billing.yaml`.

## Stripe Webhooks
All Stripe events land at `POST /stripe/webhook`. The handler is in
`src/payments/stripe_handler.py`. Critical events:
- `invoice.payment_succeeded` — marks subscription active
- `invoice.payment_failed` — triggers dunning flow (3 retries over 7 days)
- `customer.subscription.deleted` — hard-cancels the account

## Common Failure Modes
1. **Duplicate webhook delivery** — Stripe may send the same event twice. We deduplicate
   on `stripe_event_id` in the `stripe_events` table.
2. **Clock skew** — Use `stripe_event.created` (Stripe's timestamp), not `datetime.now()`.
3. **Idempotency keys** — Always pass `idempotency_key` for charge operations.

## Testing
Use Stripe's test mode: `STRIPE_SECRET_KEY=sk_test_...`
Trigger test events: `stripe trigger invoice.payment_succeeded`
EOF
```

### Step 2: Point NexusCode to your custom skills directory

Set `CUSTOM_SKILLS_DIRS` in `.env`:

```bash
CUSTOM_SKILLS_DIRS=/path/to/your-team-skills
```

Multiple directories are supported (comma-separated):

```bash
CUSTOM_SKILLS_DIRS=/path/to/team-skills,/path/to/shared-skills
```

Relative paths are supported (relative to the working directory where the server starts).

### Step 3: Reload skills

Without restarting the server:

```bash
curl -X POST http://localhost:8000/skills/reload
# → {"message":"Reloaded 6 skills"}
```

Or restart the server — skills are loaded automatically at startup.

### Step 4: Verify

```bash
curl http://localhost:8000/skills
# → lists all skills including your new one

curl http://localhost:8000/skills/payments-domain
# → full SKILL.md content + metadata
```

---

## Skills API

### List all skills

```http
GET /skills
```

Optional filter by source:
```http
GET /skills?source=builtin
GET /skills?source=custom
```

**Response:**
```json
{
  "skills": [
    {
      "name": "payments-domain",
      "description": "Domain knowledge for the payments service...",
      "source": "custom",
      "source_label": "/path/to/your-team-skills"
    },
    {
      "name": "plan-implementation",
      "description": "Generate a complete, grounded implementation plan...",
      "source": "builtin",
      "source_label": "skills/"
    }
  ],
  "total": 5
}
```

### Get a skill by name

```http
GET /skills/{name}
```

Returns the full `SKILL.md` content:

```json
{
  "name": "payments-domain",
  "description": "...",
  "content": "---\nname: payments-domain\n...",
  "source": "custom",
  "metadata": {"author": "payments-team", "version": "1.0", "tags": [...]}
}
```

### Reload skill cache

```http
POST /skills/reload
```

Use this to pick up new or edited skills without restarting the server. Returns the new total count.

### Via MCP tool

```python
# In Claude Desktop or any MCP client
list_skills()
# → lists all skills

list_skills(filter="payments")
# → skills matching "payments" in name or description
```

---

## Enterprise Use Cases

### Monorepo domain glossaries

Create one skill per domain team so agents know the boundaries:

```
custom-skills/
  auth-domain/SKILL.md          # Auth service conventions, JWT patterns
  payments-domain/SKILL.md      # Billing, Stripe, subscription lifecycle
  data-platform/SKILL.md        # ETL pipelines, data warehouse schemas
  infra/SKILL.md                # Deployment, Terraform, runbooks
```

### Team onboarding

A skill can serve as a living onboarding guide:

```markdown
---
name: backend-onboarding
description: New backend engineer guide — local setup, architecture tour, first PR checklist.
---
# Backend Onboarding
...
```

### Workflow automation

Skills can describe multi-step workflows that agents execute:

```markdown
---
name: release-workflow
description: Steps to cut a release — version bump, changelog, tag, deploy.
---
# Release Workflow
1. Run `make bump-version VERSION=x.y.z`
2. Run `make changelog`
3. Open PR against `main`
...
```

### API integration guides

Document internal APIs that the LLM may not know about:

```markdown
---
name: internal-data-api
description: Internal data API at data.internal:8080 — query patterns, auth, rate limits.
---
# Internal Data API
Base URL: http://data.internal:8080
Auth: service-to-service JWT, see src/clients/data_client.py
...
```

---

## Skill Discovery by Agents

When an agent calls `list_skills()`, it receives a description of each skill. Well-written descriptions make skills more useful:

**Bad description:**
> "Team stuff"

**Good description:**
> "Domain knowledge for the payments service — billing cycles, webhook events, Stripe integration patterns, and common failure modes specific to our subscription model."

The `description` field is what agents use to decide whether to fetch the full skill content. Make it specific and include keywords your team would search for.

---

## Notes

- Skill names are matched **case-insensitively** in `GET /skills/{name}`
- The `SKILL.md` filename must be exact (uppercase, no extension other than `.md`)
- Skills in subdirectories are discovered recursively — you can nest them
- If frontmatter is missing or malformed, the skill is still loaded with the directory name as its name and an empty description
- `POST /skills/reload` is safe to call repeatedly — it always replaces the cache fully
