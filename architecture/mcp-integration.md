# MCP Integration

NexusCode exposes all its intelligence capabilities via the **Model Context Protocol (MCP)** —
the standard protocol for giving AI tools access to external knowledge and actions.

---

## What is MCP?

MCP (Model Context Protocol) is an open standard by Anthropic that allows AI assistants
(Claude, Cursor, etc.) to call external tools over SSE. NexusCode acts as an MCP server,
making its 8 tools available to any MCP-compatible client.

**Connection endpoints:**
```
SSE endpoint:        GET  http://localhost:8000/mcp/sse
Messages endpoint:   POST http://localhost:8000/mcp/messages/
```

---

## Exposed MCP Tools

All 8 tools are registered in `src/mcp/server.py` via `@mcp_server.tool()`:

| # | Tool | Description |
|---|------|-------------|
| 1 | `search_codebase` | Hybrid semantic+keyword search with reranking |
| 2 | `get_symbol` | Fuzzy symbol lookup (IDE "Go to Definition") |
| 3 | `find_callers` | Multi-hop BFS call graph traversal |
| 4 | `get_file_context` | Structural file map (symbols + imports + deps) |
| 5 | `get_agent_context` | Pre-assembled task context within token budget |
| 6 | `plan_implementation` | Full implementation plan with web research |
| 7 | `ask_codebase` | Answer natural-language questions in mentor tone |
| 8 | `list_skills` | Discover available skills (builtin + custom) |

Note: `generate_pdf` is an internal tool available to workflow agents only, not exposed via MCP.

---

## Authentication

All MCP connections require a **JWT Bearer token**:

```bash
# Generate a token
POST /auth/token
Content-Type: application/json
{"expires_hours": 8}

# Response
{"access_token": "eyJhbGciOiJIUzI1NiJ9...", "token_type": "bearer", "expires_in": 28800}

# Use in MCP connection
Authorization: Bearer eyJhbGciOiJIUzI1NiJ9...
```

**Token scopes:**
- Default: access to all repositories
- Restricted: `{"repo_owner": "myorg", "repo_name": "myrepo"}` in payload → tool calls scoped to that repo

**Token configuration:**
```bash
JWT_SECRET=your-256-bit-secret   # required
JWT_EXPIRY_HOURS=8               # default
```

---

## Connecting Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "nexuscode": {
      "type": "sse",
      "url": "http://localhost:8000/mcp/sse",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

Then in Claude Code:
```
/mcp nexuscode search_codebase query="authentication flow"
```

---

## Connecting Claude Desktop

Add to Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "nexuscode": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8000/mcp/sse"],
      "env": {
        "MCP_REMOTE_HEADER_Authorization": "Bearer YOUR_TOKEN"
      }
    }
  }
}
```

---

## Connecting Cursor

In Cursor settings → MCP Servers:

```json
{
  "nexuscode": {
    "url": "http://localhost:8000/mcp/sse",
    "headers": {"Authorization": "Bearer YOUR_TOKEN"}
  }
}
```

---

## MCP Server Implementation

NexusCode uses **FastMCP** (`mcp[cli]`) to implement the server:

```python
# src/mcp/server.py
from mcp.server.fastmcp import FastMCP

mcp_server = FastMCP("NexusCode")

@mcp_server.tool()
async def search_codebase(
    query: str,
    repo: str | None = None,
    language: str | None = None,
    top_k: int = 5,
    mode: str = "hybrid",
) -> str:
    """
    [Rich description for the LLM]
    Hybrid semantic + keyword search over all indexed code...
    """
    # Implementation calls same DB functions as REST API
    ...

# Mount to FastAPI
app.mount("/mcp", mcp_server.sse_app())
```

All MCP tools share the same underlying query functions as the REST API — no code duplication.

---

## External MCP Bridge

NexusCode can also **consume** external MCP servers (Context7, Browserbase, etc.) and expose
their tools to workflow agents.

### How it works

```
1. Register external server:
   POST /mcp-servers {"name": "context7", "url": "https://mcp.context7.com/sse"}

2. At startup (init_bridge()):
   ● For each enabled server in DB:
     - Open SSE connection
     - Call list_tools()
     - Cache schemas in _tool_registry dict

3. In AgentLoop:
   all_tools = INTERNAL_TOOLS + get_external_tool_schemas()
   # Filtered by role's default_tools allowlist

4. When agent calls an external tool:
   call_external_tool("web_search", {"query": "..."})
   → Route to the registered server
   → Return JSON result
```

### MCP Bridge API

```python
# src/agent/mcp_bridge.py
await init_bridge()                     # called at app startup
await reload_bridge()                   # called after server CRUD
get_external_tool_schemas() -> list     # returns cached schemas
is_external_tool(name: str) -> bool     # name lookup in registry
await call_external_tool(name, params)  # execute on remote server
await test_server(url, auth_header)     # connectivity test
```

### Managing External Servers

```bash
# Add a server
POST /mcp-servers
{"name": "context7", "url": "https://mcp.context7.com/sse", "description": "Library docs"}

# List servers
GET /mcp-servers

# Test connectivity
POST /mcp-servers/{id}/test

# Reload after changes
POST /mcp-servers/reload
```

**Tool collision policy:** If an external tool has the same name as an internal NexusCode tool,
the internal tool wins. External tools are always optional extras.

---

## Tool Schema Format

MCP tool schemas follow the Anthropic tool_use format:

```json
{
  "name": "search_codebase",
  "description": "Hybrid semantic + keyword search...",
  "input_schema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "..."},
      "top_k": {"type": "integer", "default": 5}
    },
    "required": ["query"]
  }
}
```

These schemas are passed directly to Claude as tools — the rich descriptions are what Claude
reads to decide when and how to call each tool.

---

## Verifying MCP Connection

```bash
# Check MCP server health via REST
curl http://localhost:8000/health

# Verify token
GET /auth/verify
Authorization: Bearer YOUR_TOKEN

# List available tools (via REST, not MCP protocol)
GET /skills
```

---

## SSE Protocol Details

NexusCode uses the MCP SSE transport, which works as follows:

```
Client                          Server (NexusCode)
  │                                     │
  │  GET /mcp/sse                        │
  │ ──────────────────────────────────► │
  │                                     │
  │  ← text/event-stream                │
  │  data: {"type":"endpoint","url":"/mcp/messages/"}
  │                                     │
  │  POST /mcp/messages/                │
  │  {"method":"tools/list",...}        │
  │ ──────────────────────────────────► │
  │  ← {"result":{"tools":[...]}}       │
  │                                     │
  │  POST /mcp/messages/                │
  │  {"method":"tools/call",            │
  │   "params":{"name":"search_codebase",
  │             "arguments":{"query":"auth"}}}
  │ ──────────────────────────────────► │
  │  ← streaming result chunks          │
```

The `sse_app()` from FastMCP handles all protocol details automatically.
