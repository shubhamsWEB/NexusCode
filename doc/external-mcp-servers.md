# External MCP Servers

NexusCode can connect to **external MCP servers** — tools you or your team already run (e.g. a monorepo package-map server, a private API catalogue, a platform knowledge base) — and expose their tools to the Claude agent loop alongside NexusCode's own retrieval tools.

This means Claude can call your external tools during Ask Mode and Planning Mode exactly the same way it calls `search_codebase` or `get_symbol`. No duplication, no stale markdown files.

---

## How it works

```
Claude API call
  tools = [local: search_codebase, get_symbol, find_callers, get_file_context]
         + [external: whatever your MCP server exposes]

AgentLoop → tool_executor
  ├── known local tool  →  NexusCode DB functions (unchanged)
  └── unknown tool name →  mcp_bridge → your external MCP server (SSE/HTTP)
```

**Tool schemas** are fetched from each external server at API startup and cached in memory. They are merged into Claude's tool palette automatically — you don't change any prompts or code. Local tools always take priority: if an external server exports a tool with the same name as a built-in, the built-in wins and a warning is logged.

---

## Managing servers from the dashboard

Open the **🔌 MCP Servers** page in the dashboard.

### Adding a server

1. Fill in **Name**, **URL** (SSE endpoint, e.g. `http://localhost:3100/sse`), and optionally an **Auth header** (e.g. `Bearer sk-...`) and a **Description**.
2. Click **Test Connection** to verify the server is reachable and see its tool list before saving.
3. Click **Save** to register the server.

After saving, click **↺ Reload Bridge** to connect immediately — or restart the API server.

### Server status indicators

| Icon | Meaning |
|---|---|
| 🟢 | Connected — tools loaded successfully |
| ⬜ | Registered but never connected (new or first boot pending) |
| 🔴 | Last connection attempt failed — hover for error detail |

### Enable / Disable

Toggle a server on or off without deleting it. Disabled servers are skipped at startup and during bridge reloads.

### Auth header masking

Auth header values are masked in the UI — only the last 4 characters are shown (e.g. `...xyz9`). The full value is stored server-side and never returned in API responses.

---

## Managing servers via the API

All endpoints are under `/mcp-servers`.

### `GET /mcp-servers`
List all registered servers. The `tool_count` field reflects the live count from the in-memory bridge cache (may differ from the DB value if the bridge hasn't reloaded yet).

```bash
curl http://localhost:8000/mcp-servers
```

```json
[
  {
    "id":          1,
    "name":        "Package MCP",
    "url":         "http://localhost:3100/sse",
    "enabled":     true,
    "auth_header": null,
    "description": "Monorepo package intelligence",
    "tool_count":  8,
    "last_seen_at": "2026-03-02T20:00:00+00:00",
    "last_error":  null,
    "created_at":  "2026-03-02T19:00:00+00:00"
  }
]
```

### `POST /mcp-servers`
Register a new external server. Returns `201 Created`.

```bash
curl -X POST http://localhost:8000/mcp-servers \
  -H "Content-Type: application/json" \
  -d '{
    "name":        "Package MCP",
    "url":         "http://localhost:3100/sse",
    "auth_header": "Bearer sk-...",
    "description": "Monorepo package intelligence",
    "enabled":     true
  }'
```

### `PATCH /mcp-servers/{id}`
Update any field on an existing server.

```bash
curl -X PATCH http://localhost:8000/mcp-servers/1 \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

Fields: `name`, `enabled`, `auth_header`, `description`.

### `DELETE /mcp-servers/{id}`
Remove a server from the DB and immediately evict its tools from the bridge cache.

```bash
curl -X DELETE http://localhost:8000/mcp-servers/1
```

```json
{"deleted": 1, "tools_evicted": 8}
```

### `POST /mcp-servers/{id}/test`
Test the live connection for a saved server. Does **not** modify the DB or reload the bridge.

```bash
curl -X POST http://localhost:8000/mcp-servers/1/test
```

```json
{"ok": true, "tools": ["get_package_info", "list_packages", "get_service_deps"]}
```

### `POST /mcp-servers/test-url`
Test an unsaved server by URL before registering it.

```bash
curl -X POST http://localhost:8000/mcp-servers/test-url \
  -H "Content-Type: application/json" \
  -d '{"url": "http://localhost:3100/sse", "auth_header": "Bearer sk-..."}'
```

### `POST /mcp-servers/reload`
Reconnect to all enabled servers, refresh tool schemas in memory, and return the new total tool count. Call this after adding or updating servers without restarting the API.

```bash
curl -X POST http://localhost:8000/mcp-servers/reload
# → {"tool_count": 8, "message": "Bridge reloaded — 8 tool(s) active"}
```

---

## DB migration

Run this once before starting the API for the first time with this feature:

```bash
psql $DATABASE_URL -f src/storage/migrations/008_external_mcp_servers.sql
```

The migration is idempotent (`IF NOT EXISTS` throughout) — safe to re-run.

---

## Connection protocol

NexusCode connects to external servers using the **MCP SSE transport** (`mcp.client.sse.sse_client` + `mcp.ClientSession`). Your server must expose an SSE endpoint — the same transport used by NexusCode's own MCP server at `/mcp/sse`.

**Authentication:** if `auth_header` is set, it is sent as the `Authorization` HTTP header on every connection. This supports any scheme: `Bearer <token>`, `Basic <base64>`, or custom headers.

---

## Startup behaviour

At API startup, `init_bridge()` is called automatically:

1. Load all rows where `enabled = TRUE` from `external_mcp_servers`.
2. For each server: open an SSE connection, call `list_tools`, cache schemas in memory.
3. On success: update `tool_count` and `last_seen_at` in the DB.
4. On failure: record the error in `last_error`, leave `tool_count` unchanged. The API starts normally — bridge failures are non-fatal.

If no external servers are configured, startup continues silently in under 1 ms.

---

## Name collision policy

If an external server exports a tool whose name matches a built-in NexusCode tool (`search_codebase`, `get_symbol`, `find_callers`, `get_file_context`, or any final-answer tool), the **local tool wins** and the external one is skipped with a warning in the server log:

```
WARNING mcp_bridge: external tool 'search_codebase' from http://... conflicts with local tool — skipping
```

---

## Example: monorepo package server

If your team runs an MCP server that understands your monorepo's package graph, you can register it so Claude knows which package owns which service when planning changes:

```bash
# Register
curl -X POST http://localhost:8000/mcp-servers \
  -d '{"name": "Package Map", "url": "http://mcp.internal:3100/sse"}'

# Reload bridge
curl -X POST http://localhost:8000/mcp-servers/reload

# Now ask a cross-package question — Claude will call your tool automatically
curl -X POST http://localhost:8000/ask \
  -d '{"query": "Which packages depend on the auth service?"}'
```

Claude will call `get_service_deps` (or whatever your server exposes) as part of its reasoning, exactly as it calls `search_codebase`.
