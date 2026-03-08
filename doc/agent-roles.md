# Agent Roles

Agent roles define the persona, tool access, and behavioral constraints for AI agents
in NexusCode workflows and direct agent calls.

---

## Built-in Roles

NexusCode ships with 6 hardcoded roles:

### `searcher`
Deep codebase navigator. Searches broadly, then narrows. Follows call chains 2+ hops.

- **Tools:** `search_codebase`, `get_symbol`, `find_callers`, `get_file_context`
- **Require search:** Yes (must call search tools before answering)
- **Best for:** Finding relevant files, tracing execution paths, dependency analysis

### `planner`
Implementation planner grounded in real code. Never invents patterns.

- **Tools:** `plan_implementation`, `search_codebase`, `get_symbol`, `get_file_context`
- **Require search:** Yes
- **Best for:** Decomposing features, estimating scope, creating ordered step plans

### `reviewer`
Critical code reviewer focused on correctness, security, and performance.

- **Tools:** `search_codebase`, `get_symbol`, `find_callers`, `get_file_context`
- **Require search:** Yes
- **Best for:** PR review, security audits, performance analysis, blast-radius assessment

### `coder`
Code generator that matches existing style and patterns.

- **Tools:** `search_codebase`, `get_agent_context`, `get_symbol`, `get_file_context`
- **Require search:** Yes
- **Best for:** Writing new features, implementing fixes, generating boilerplate

### `tester`
Test strategy agent that writes comprehensive, runnable tests.

- **Tools:** `search_codebase`, `find_callers`, `get_symbol`, `get_file_context`
- **Require search:** Yes
- **Best for:** Unit tests, integration tests, test coverage analysis

### `supervisor`
Orchestrator that synthesizes outputs and writes final documents.

- **Tools:** `search_codebase`, `get_symbol`, `ask_codebase`, `generate_pdf`
- **Require search:** No (synthesizes, doesn't always search)
- **Best for:** Final document compilation, multi-agent output synthesis, PDF generation

---

## Customizing Roles via Dashboard

1. Open `http://localhost:8501` → **🤖 Agent Roles**
2. Click an existing role to edit it, or **+ New Role** to create a custom one
3. Edit the system prompt, tool list, and parameters
4. Click **Save**

Changes take effect immediately for new workflow runs (no restart needed).

---

## Customizing Roles via API

### View a Role

```bash
GET /agent-roles/supervisor
```

```json
{
  "name": "supervisor",
  "system_prompt": "You are a Supervisor agent...",
  "default_tools": ["search_codebase", "get_symbol", "ask_codebase", "generate_pdf"],
  "require_search": false,
  "max_iterations": 5,
  "token_budget": 80000,
  "is_builtin": true,
  "source": "hardcoded"
}
```

### Override a Built-in Role

```bash
PUT /agent-roles/supervisor
Content-Type: application/json

{
  "system_prompt": "You are a Supervisor agent...",
  "instructions": "Always include a TLDR at the top. Use tables for structured comparisons.",
  "default_tools": ["search_codebase", "get_symbol", "ask_codebase", "generate_pdf"],
  "require_search": false,
  "max_iterations": 7,
  "token_budget": 120000
}
```

The `instructions` field is appended to the system prompt under `## Additional Instructions`
without replacing the base prompt — useful for incremental customization.

### Create a Custom Role

```bash
PUT /agent-roles/security-auditor
Content-Type: application/json

{
  "system_prompt": "You are a Security Auditor specialized in OWASP Top 10 vulnerabilities. You:\n- Search for all authentication flows\n- Check for injection vulnerabilities\n- Verify authorization checks\n- Flag any sensitive data exposure\n\nAlways cite the exact file path and line number for every finding.",
  "default_tools": ["search_codebase", "get_symbol", "find_callers", "get_file_context"],
  "require_search": true,
  "max_iterations": 8,
  "token_budget": 100000
}
```

Use in a workflow:
```yaml
- id: security_audit
  type: agent
  role: security-auditor
  task: "Audit the authentication module for OWASP vulnerabilities"
```

### Reset a Built-in to Defaults

```bash
POST /agent-roles/supervisor/reset
```

### Delete a Custom Role

```bash
DELETE /agent-roles/my-custom-role
```

---

## Role Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `system_prompt` | string | (varies) | The core agent persona and instructions |
| `instructions` | string | "" | Additional instructions appended to system_prompt |
| `default_tools` | list | (varies) | Tools available to this role by default |
| `require_search` | bool | true | Must call a search tool before answering |
| `max_iterations` | int | 5 | Max AgentLoop iterations before forcing answer |
| `token_budget` | int | 80000 | Max cumulative tool-result tokens |

### Choosing `max_iterations`

| Value | Use case |
|-------|---------|
| 3–4 | Simple lookups, quick answers |
| 5 (default) | Standard tasks |
| 6–8 | Complex multi-file analysis |
| 10+ | Deep security audits, comprehensive planning |

### Choosing `token_budget`

| Value | Use case |
|-------|---------|
| 35,000 | Ask Mode (fast, focused) |
| 80,000 | Standard workflow steps |
| 120,000+ | Comprehensive audits, large codebases |

---

## Available Tools per Role

Each role can use any of the 8 internal tools plus any registered external MCP tools:

| Tool | Purpose |
|------|---------|
| `search_codebase` | Hybrid semantic+keyword search |
| `get_symbol` | Fuzzy symbol lookup |
| `find_callers` | BFS call graph traversal |
| `get_file_context` | Structural file map |
| `get_agent_context` | Pre-assembled task context |
| `plan_implementation` | Generate structured plan |
| `ask_codebase` | Answer questions about the codebase |
| `generate_pdf` | Convert markdown to PDF (supervisor only by default) |

**List all available tools:**
```bash
GET /agent-roles/tools
```

```json
{
  "internal": ["search_codebase", "get_symbol", "find_callers", "get_file_context",
               "get_agent_context", "plan_implementation", "ask_codebase", "generate_pdf"],
  "external": ["web_search", "browse_url"]   // registered external MCP tools
}
```

---

## Per-Step Tool Override

In a workflow, you can restrict a step to specific tools regardless of the role's default:

```yaml
- id: quick_search
  type: agent
  role: searcher
  tools: [search_codebase]   # only search, no symbol lookup
  task: "Find all API endpoint handlers"
```

This is useful for:
- Keeping steps focused (no irrelevant tools polluting the LLM context)
- Security (preventing a step from calling `generate_pdf` unintentionally)
- Performance (fewer tools = smaller prompts = faster responses)

---

## Role Precedence

1. **Step-level `tools:` override** — overrides everything (most specific)
2. **DB role override** — if an override exists in `agent_role_overrides` table
3. **Hardcoded `_ROLES` dict** — fallback default

The DB lookup happens at step execution time, so role changes apply to new runs immediately.
