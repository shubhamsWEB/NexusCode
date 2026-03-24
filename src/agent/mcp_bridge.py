"""
MCP Bridge — connects to external MCP servers and proxies tool calls.

Loads enabled server configs from the DB at startup, fetches their tool schemas,
and exposes them to the agent loop. Unknown tool names in tool_executor are
routed here.

In-memory state is rebuilt by init_bridge() at startup and reload_bridge() on demand.

Supports:
  - remote HTTP servers (streamable_http / SSE transport)
  - stdio subprocess servers (npx, python, docker, etc.)
  - auth_type: none | header | bearer | basic | oauth
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ── In-memory registry ────────────────────────────────────────────────────────
# tool_name → {schema, server_url, auth_header, server_type, command, args, env, transport}
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


# ── Auth helper ───────────────────────────────────────────────────────────────


def _build_auth_header(auth_type: str, auth_value: str | None) -> str | None:
    """Build the Authorization header value from auth_type + raw value.

    auth_type:
      'none'    — no header
      'header'  — verbatim (backwards-compat with old auth_header field)
      'bearer'  — prepends 'Bearer '
      'basic'   — expects 'email:token', base64-encodes to 'Basic ...'
      'oauth'   — auth_value is the oauth_token; prepends 'Bearer '
    """
    if not auth_value or auth_type == "none":
        return None
    if auth_type == "bearer":
        return f"Bearer {auth_value}"
    if auth_type == "basic":
        import base64
        return "Basic " + base64.b64encode(auth_value.encode()).decode()
    if auth_type == "oauth":
        return f"Bearer {auth_value}"
    # 'header': verbatim
    return auth_value


# ── DB helpers ────────────────────────────────────────────────────────────────


async def _load_enabled_servers() -> list[dict]:
    """Return all enabled servers from the DB."""
    try:
        from sqlalchemy import text

        from src.storage.db import AsyncSessionLocal

        sql = text(
            """
            SELECT id, name, url, auth_header,
                   COALESCE(transport, 'auto') AS transport,
                   COALESCE(server_type, 'remote') AS server_type,
                   command, args, env,
                   COALESCE(auth_type, 'header') AS auth_type,
                   oauth_token
            FROM external_mcp_servers WHERE enabled = TRUE
            """
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

        params: dict[str, Any]
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


# ── Transport helpers ─────────────────────────────────────────────────────────

_TRANSPORT_SSE = "sse"
_TRANSPORT_HTTP = "streamable_http"
_TRANSPORT_AUTO = "auto"


async def _list_tools_sse(url: str, headers: dict) -> list[dict]:
    """Connect via legacy SSE transport and return tool schemas."""
    from mcp.client.sse import sse_client

    from mcp import ClientSession

    tool_schemas: list[dict] = []
    async with sse_client(url, headers=headers) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools_result = await session.list_tools()
        for tool in tools_result.tools:
            tool_schemas.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}},
            })
    return tool_schemas


async def _list_tools_streamable_http(url: str, headers: dict) -> list[dict]:
    """Connect via Streamable HTTP transport and return tool schemas."""
    from mcp.client.streamable_http import streamablehttp_client

    from mcp import ClientSession

    tool_schemas: list[dict] = []
    try:
        async with streamablehttp_client(url, headers=headers) as (read, write, _), ClientSession(read, write) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                tool_schemas.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}},
                })
    except Exception as exc:
        if tool_schemas:
            # Tools fetched successfully; error is from SSE stream teardown (e.g. Atlassian 400 on reconnect)
            logger.debug("mcp_bridge: streamable_http teardown error ignored (tools fetched OK): %s", exc)
        else:
            raise
    return tool_schemas


async def _list_tools_stdio(command: str, args: list, env: dict) -> list[dict]:
    """Spawn a stdio MCP subprocess and return its tool schemas."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=command,
        args=args or [],
        env={**os.environ, **(env or {})},
    )
    tool_schemas: list[dict] = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools_result = await session.list_tools()
        for tool in tools_result.tools:
            tool_schemas.append({
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}},
            })
    return tool_schemas


async def _call_tool_sse(url: str, headers: dict, tool_name: str, tool_input: dict) -> str:
    """Call a tool via legacy SSE transport."""
    from mcp.client.sse import sse_client

    from mcp import ClientSession

    async with sse_client(url, headers=headers) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(tool_name, tool_input)
    return _extract_tool_result(result)


async def _call_tool_streamable_http(url: str, headers: dict, tool_name: str, tool_input: dict) -> str:
    """Call a tool via Streamable HTTP transport."""
    from mcp.client.streamable_http import streamablehttp_client

    from mcp import ClientSession

    result = None
    try:
        async with streamablehttp_client(url, headers=headers) as (read, write, _), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, tool_input)
    except Exception as exc:
        if result is not None:
            # Tool call succeeded; error is from SSE stream teardown (e.g. Atlassian 400 on reconnect)
            logger.debug("mcp_bridge: streamable_http teardown error ignored (tool call succeeded): %s", exc)
        else:
            raise
    return _extract_tool_result(result)


async def _call_tool_stdio(command: str, args: list, env: dict, tool_name: str, tool_input: dict) -> str:
    """Spawn a fresh stdio MCP subprocess, call one tool, return result."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=command,
        args=args or [],
        env={**os.environ, **(env or {})},
    )
    try:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, tool_input)
        return _extract_tool_result(result)
    except BaseException as exc:
        # Unwrap ExceptionGroup (Python 3.11+ anyio/TaskGroup) to surface the real error
        real_exc = exc
        while hasattr(real_exc, "exceptions") and real_exc.exceptions:
            real_exc = real_exc.exceptions[0]
        raise real_exc from None


def _extract_tool_result(result) -> str:
    parts: list[str] = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(block.text)
        else:
            parts.append(str(block))
    return "\n".join(parts) if parts else json.dumps({"result": "ok"})


# ── MCP connection helper ─────────────────────────────────────────────────────


async def _connect_and_list_tools(
    url: str, auth_header: str | None, transport: str = _TRANSPORT_AUTO
) -> list[dict]:
    """
    Connect to an MCP server and return its tool schemas.

    transport:
      'streamable_http' — new MCP transport (Context7 and most cloud servers)
      'sse'             — legacy SSE transport (older self-hosted servers)
      'auto'            — try streamable_http first, fall back to sse
    """
    headers: dict = {}
    if auth_header:
        headers["Authorization"] = auth_header

    if transport == _TRANSPORT_HTTP:
        return await _list_tools_streamable_http(url, headers)

    if transport == _TRANSPORT_SSE:
        return await _list_tools_sse(url, headers)

    # auto: try Streamable HTTP first, fall back to SSE
    try:
        return await _list_tools_streamable_http(url, headers)
    except Exception as http_exc:
        logger.debug("mcp_bridge: streamable_http failed for %s (%s), trying SSE", url, http_exc)
        try:
            return await _list_tools_sse(url, headers)
        except Exception as sse_exc:
            # Re-raise with both error messages for easier debugging
            raise RuntimeError(
                f"Both transports failed — streamable_http: {http_exc}; sse: {sse_exc}"
            ) from sse_exc


async def _call_tool_on_server(
    url: str, auth_header: str | None, tool_name: str, tool_input: dict,
    transport: str = _TRANSPORT_AUTO,
) -> str:
    """Call a single tool on a remote MCP server and return its result as a string."""
    headers: dict = {}
    if auth_header:
        headers["Authorization"] = auth_header

    if transport == _TRANSPORT_HTTP:
        return await _call_tool_streamable_http(url, headers, tool_name, tool_input)

    if transport == _TRANSPORT_SSE:
        return await _call_tool_sse(url, headers, tool_name, tool_input)

    # auto: try Streamable HTTP first, fall back to SSE
    try:
        return await _call_tool_streamable_http(url, headers, tool_name, tool_input)
    except Exception as http_exc:
        logger.debug("mcp_bridge: streamable_http call failed for %s (%s), trying SSE", url, http_exc)
        try:
            return await _call_tool_sse(url, headers, tool_name, tool_input)
        except Exception as sse_exc:
            raise RuntimeError(
                f"Both transports failed — streamable_http: {http_exc}; sse: {sse_exc}"
            ) from sse_exc


# ── Public API ────────────────────────────────────────────────────────────────


async def test_server(
    url: str | None,
    auth_header: str | None,
    transport: str = _TRANSPORT_AUTO,
    *,
    server_type: str = "remote",
    command: str | None = None,
    args: list | None = None,
    env: dict | None = None,
) -> dict:
    """
    Attempt to connect to a server and list its tools.
    Returns {ok: bool, tools: [name,...], transport_used?: str, error?: str}.
    Does NOT modify the DB or in-memory registry.
    """
    if server_type == "stdio":
        if not command:
            return {"ok": False, "tools": [], "error": "stdio server requires a command"}
        try:
            schemas = await _list_tools_stdio(command, args or [], env or {})
            return {"ok": True, "tools": [s["name"] for s in schemas], "transport_used": "stdio"}
        except Exception as exc:
            return {"ok": False, "tools": [], "error": str(exc)}

    if not url:
        return {"ok": False, "tools": [], "error": "remote server requires a URL"}

    # For auto mode, report which transport actually succeeded
    if transport == _TRANSPORT_AUTO:
        headers: dict = {}
        if auth_header:
            headers["Authorization"] = auth_header
        try:
            schemas = await _list_tools_streamable_http(url, headers)
            return {"ok": True, "tools": [s["name"] for s in schemas], "transport_used": "streamable_http"}
        except Exception:
            pass
        try:
            schemas = await _list_tools_sse(url, headers)
            return {"ok": True, "tools": [s["name"] for s in schemas], "transport_used": "sse"}
        except Exception as exc:
            return {"ok": False, "tools": [], "error": str(exc)}

    try:
        schemas = await _connect_and_list_tools(url, auth_header, transport)
        return {"ok": True, "tools": [s["name"] for s in schemas], "transport_used": transport}
    except Exception as exc:
        return {"ok": False, "tools": [], "error": str(exc)}


async def _load_single_server(server: dict) -> int:
    """
    Connect to one server, register its tools, update DB stats.
    Returns number of tools registered from this server.
    """
    server_id: int = server["id"]
    server_type: str = server.get("server_type") or "remote"
    transport: str = server.get("transport") or _TRANSPORT_AUTO
    count = 0

    # Build the effective auth header from auth_type
    auth_type: str = server.get("auth_type") or "header"
    if auth_type == "oauth":
        raw_auth = server.get("oauth_token")
    else:
        raw_auth = server.get("auth_header")
    auth_header = _build_auth_header(auth_type, raw_auth)

    identifier = server.get("url") or server.get("command") or str(server_id)

    try:
        if server_type == "stdio":
            command = server.get("command")
            args = server.get("args") or []
            env = server.get("env") or {}
            if not command:
                raise ValueError("stdio server is missing 'command'")
            schemas = await _list_tools_stdio(command, args, env)
        else:
            url = server["url"]
            schemas = await _connect_and_list_tools(url, auth_header, transport)

        for schema in schemas:
            name = schema["name"]
            if name in _LOCAL_TOOL_NAMES:
                logger.warning(
                    "mcp_bridge: external tool %r from %s conflicts with local tool — skipping",
                    name,
                    identifier,
                )
                continue
            entry: dict = {
                "schema": schema,
                "server_type": server_type,
                "transport": transport,
            }
            if server_type == "stdio":
                entry.update({
                    "command": server.get("command"),
                    "args": server.get("args") or [],
                    "env": server.get("env") or {},
                    "server_url": None,
                    "auth_header": None,
                })
            else:
                entry.update({
                    "server_url": server["url"],
                    "auth_header": auth_header,
                })
            _tool_registry[name] = entry
            count += 1

        await _update_server_stats(server_id, count, None)
        logger.info(
            "mcp_bridge: loaded %d tool(s) from %s (type=%s, transport=%s)",
            count, identifier, server_type, transport,
        )
    except Exception as exc:
        err_str = str(exc)
        logger.warning("mcp_bridge: failed to connect to %s: %s", identifier, err_str)
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


def get_external_tools_info() -> list[dict]:
    """Return tool metadata (name, description, server_url) for UI display."""
    return [
        {
            "name": entry["schema"]["name"],
            "description": entry["schema"].get("description", ""),
            "server_url": entry.get("server_url"),
            "server_type": entry.get("server_type", "remote"),
        }
        for entry in _tool_registry.values()
    ]


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
        server_type = entry.get("server_type", "remote")
        if server_type == "stdio":
            result = await _call_tool_stdio(
                entry["command"],
                entry.get("args") or [],
                entry.get("env") or {},
                name,
                tool_input,
            )
        else:
            result = await _call_tool_on_server(
                entry["server_url"],
                entry["auth_header"],
                name,
                tool_input,
                transport=entry.get("transport", _TRANSPORT_AUTO),
            )
        return result
    except Exception as exc:
        logger.error("mcp_bridge: call to external tool %r failed: %s", name, exc)
        return json.dumps({"error": f"External tool {name} failed: {exc}"})
