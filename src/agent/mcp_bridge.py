"""
MCP Bridge — connects to external MCP servers and proxies tool calls.

Loads enabled server configs from the DB at startup, fetches their tool schemas,
and exposes them to the agent loop. Unknown tool names in tool_executor are
routed here.

In-memory state is rebuilt by init_bridge() at startup and reload_bridge() on demand.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# ── In-memory registry ────────────────────────────────────────────────────────
# tool_name → {schema, server_url, auth_header}
_tool_registry: dict[str, dict] = {}

# Local tool names — populated by ask_agent / claude_planner at startup
# so the bridge can avoid overwriting them.
_LOCAL_TOOL_NAMES: frozenset[str] = frozenset(
    [
        "search_codebase",
        "get_symbol",
        "find_callers",
        "get_file_context",
        "answer_question",
        "output_implementation_plan",
        "answer_codebase_question",
        "analyze_and_improve",
    ]
)


# ── DB helpers ────────────────────────────────────────────────────────────────


async def _load_enabled_servers() -> list[dict]:
    """Return all enabled servers from the DB."""
    try:
        from sqlalchemy import text

        from src.storage.db import AsyncSessionLocal

        sql = text(
            "SELECT id, name, url, auth_header FROM external_mcp_servers WHERE enabled = TRUE"
        )
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(sql)).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("mcp_bridge: could not load servers from DB: %s", exc)
        return []


async def _update_server_stats(
    server_id: int,
    tool_count: int,
    last_error: str | None,
) -> None:
    """Update tool_count, last_seen_at, and last_error for a server row."""
    try:
        from sqlalchemy import text

        from src.storage.db import AsyncSessionLocal

        if last_error is None:
            sql = text(
                """
                UPDATE external_mcp_servers
                SET tool_count = :tc, last_seen_at = now(), last_error = NULL
                WHERE id = :sid
                """
            )
            params = {"tc": tool_count, "sid": server_id}
        else:
            sql = text(
                """
                UPDATE external_mcp_servers
                SET last_error = :err
                WHERE id = :sid
                """
            )
            params = {"err": last_error[:500], "sid": server_id}

        async with AsyncSessionLocal() as session:
            await session.execute(sql, params)
            await session.commit()
    except Exception as exc:
        logger.debug("mcp_bridge: could not update server stats: %s", exc)


# ── MCP connection helper ─────────────────────────────────────────────────────


async def _connect_and_list_tools(url: str, auth_header: str | None) -> list[dict]:
    """
    Open an SSE connection to an MCP server, list its tools, and return
    them as a list of Anthropic-format tool schema dicts.
    Raises on connection failure.
    """
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header

    tool_schemas: list[dict] = []

    async with sse_client(url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                schema: dict = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}},
                }
                tool_schemas.append(schema)

    return tool_schemas


async def _call_tool_on_server(
    url: str, auth_header: str | None, tool_name: str, tool_input: dict
) -> str:
    """Call a single tool on a remote MCP server and return its result as a string."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header

    async with sse_client(url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, tool_input)

    # MCP returns a list of content blocks
    parts: list[str] = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(block.text)
        else:
            parts.append(str(block))
    return "\n".join(parts) if parts else json.dumps({"result": "ok"})


# ── Public API ────────────────────────────────────────────────────────────────


async def test_server(url: str, auth_header: str | None) -> dict:
    """
    Attempt to connect to a server and list its tools.
    Returns {ok: bool, tools: [name,...], error?: str}.
    Does NOT modify the DB or in-memory registry.
    """
    try:
        schemas = await _connect_and_list_tools(url, auth_header)
        return {"ok": True, "tools": [s["name"] for s in schemas]}
    except Exception as exc:
        return {"ok": False, "tools": [], "error": str(exc)}


async def _load_single_server(server: dict) -> int:
    """
    Connect to one server, register its tools, update DB stats.
    Returns number of tools registered from this server.
    """
    server_id: int = server["id"]
    url: str = server["url"]
    auth_header: str | None = server.get("auth_header")
    count = 0

    try:
        schemas = await _connect_and_list_tools(url, auth_header)
        for schema in schemas:
            name = schema["name"]
            if name in _LOCAL_TOOL_NAMES:
                logger.warning(
                    "mcp_bridge: external tool %r from %s conflicts with local tool — skipping",
                    name,
                    url,
                )
                continue
            _tool_registry[name] = {
                "schema": schema,
                "server_url": url,
                "auth_header": auth_header,
            }
            count += 1
        await _update_server_stats(server_id, count, None)
        logger.info("mcp_bridge: loaded %d tool(s) from %s", count, url)
    except Exception as exc:
        err_str = str(exc)
        logger.warning("mcp_bridge: failed to connect to %s: %s", url, err_str)
        await _update_server_stats(server_id, 0, err_str)

    return count


async def init_bridge() -> None:
    """
    Load all enabled servers from DB, connect, and cache their tool schemas.
    Called at application startup. Failures are non-fatal.
    """
    global _tool_registry
    _tool_registry = {}

    servers = await _load_enabled_servers()
    if not servers:
        logger.info("mcp_bridge: no external MCP servers configured")
        return

    total = 0
    for server in servers:
        total += await _load_single_server(server)

    logger.info("mcp_bridge: bridge initialised — %d external tool(s) available", total)


async def reload_bridge() -> int:
    """
    Re-initialise from DB. Called by POST /mcp-servers/reload.
    Returns total number of tools now in registry.
    """
    await init_bridge()
    return len(_tool_registry)


def get_external_tool_schemas() -> list[dict]:
    """Return Anthropic-format tool schemas for all cached external tools."""
    return [entry["schema"] for entry in _tool_registry.values()]


def is_external_tool(name: str) -> bool:
    """Return True if the tool name is registered from an external server."""
    return name in _tool_registry


async def call_external_tool(name: str, tool_input: dict) -> str:
    """
    Forward a tool call to the appropriate external MCP server.
    Returns a JSON string result. Never raises.
    """
    entry = _tool_registry.get(name)
    if not entry:
        return json.dumps({"error": f"External tool not found: {name}"})

    try:
        result = await _call_tool_on_server(
            entry["server_url"],
            entry["auth_header"],
            name,
            tool_input,
        )
        return result
    except Exception as exc:
        logger.error("mcp_bridge: call to external tool %r failed: %s", name, exc)
        return json.dumps({"error": f"External tool {name} failed: {exc}"})
