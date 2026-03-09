"""
External MCP Server management endpoints.

Endpoints
---------
GET    /mcp-servers              — list all servers (with live tool_count from bridge cache)
POST   /mcp-servers              — register a new server; 201 Created
PATCH  /mcp-servers/{id}         — update name/enabled/auth_header/description; 200
DELETE /mcp-servers/{id}         — remove server + evict from bridge cache; 200
POST   /mcp-servers/{id}/test    — test live connection for a saved server
POST   /mcp-servers/test-url     — test an unsaved server by URL
POST   /mcp-servers/reload       — reload bridge from DB; returns new tool count
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


# ── Request models ────────────────────────────────────────────────────────────


class AddMCPServerRequest(BaseModel):
    name: str
    url: str
    auth_header: str | None = None
    description: str | None = None
    enabled: bool = True
    transport: str = "auto"  # "auto" | "streamable_http" | "sse"


class UpdateMCPServerRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    auth_header: str | None = None
    description: str | None = None
    transport: str | None = None


class TestUrlRequest(BaseModel):
    url: str
    auth_header: str | None = None
    transport: str = "auto"


# ── DB helpers ────────────────────────────────────────────────────────────────


async def _get_all_servers() -> list[dict]:
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    sql = text(
        """
        SELECT id, name, url, enabled, auth_header, description,
               COALESCE(transport, 'auto') AS transport,
               tool_count, last_seen_at, last_error, created_at
        FROM external_mcp_servers
        ORDER BY created_at ASC
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql)).mappings().all()

    result = []
    for r in rows:
        d = dict(r)
        for k in ("last_seen_at", "created_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        result.append(d)
    return result


async def _get_server_by_id(server_id: int) -> dict | None:
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    sql = text(
        """
        SELECT id, name, url, enabled, auth_header, description,
               COALESCE(transport, 'auto') AS transport,
               tool_count, last_seen_at, last_error, created_at
        FROM external_mcp_servers
        WHERE id = :id
        """
    )
    async with AsyncSessionLocal() as session:
        row = (await session.execute(sql, {"id": server_id})).mappings().first()

    if not row:
        return None

    d = dict(row)
    for k in ("last_seen_at", "created_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("")
async def list_servers() -> JSONResponse:
    """List all registered external MCP servers with live tool counts from bridge cache."""
    from src.agent.mcp_bridge import get_external_tool_schemas, _tool_registry

    servers = await _get_all_servers()

    # Augment with live tool count per server from in-memory registry
    url_to_live_count: dict[str, int] = {}
    for entry in _tool_registry.values():
        u = entry["server_url"]
        url_to_live_count[u] = url_to_live_count.get(u, 0) + 1

    for s in servers:
        live = url_to_live_count.get(s["url"])
        if live is not None:
            s["tool_count"] = live

    return JSONResponse(servers)


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_server(req: AddMCPServerRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """Register a new external MCP server."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from src.storage.db import AsyncSessionLocal

    sql = text(
        """
        INSERT INTO external_mcp_servers (name, url, enabled, auth_header, description, transport)
        VALUES (:name, :url, :enabled, :auth_header, :description, :transport)
        RETURNING id, name, url, enabled, auth_header, description,
                  COALESCE(transport, 'auto') AS transport,
                  tool_count, last_seen_at, last_error, created_at
        """
    )
    params = {
        "name": req.name,
        "url": req.url,
        "enabled": req.enabled,
        "auth_header": req.auth_header,
        "description": req.description,
        "transport": req.transport,
    }

    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(sql, params)).mappings().first()
            await session.commit()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A server with URL '{req.url}' already exists.",
        )

    if not row:
        raise HTTPException(status_code=500, detail="Insert failed.")

    d = dict(row)
    for k in ("last_seen_at", "created_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()

    # Load tools into the bridge cache in the background — returns 201 instantly
    # even if the MCP server is slow to respond.
    if req.enabled:
        from src.agent.mcp_bridge import _load_single_server
        server_spec = {
            "id": d["id"],
            "url": d["url"],
            "auth_header": d.get("auth_header"),
            "transport": d.get("transport") or "auto",
        }
        background_tasks.add_task(_load_single_server, server_spec)

    return JSONResponse(d, status_code=status.HTTP_201_CREATED)


@router.patch("/{server_id}")
async def update_server(server_id: int, req: UpdateMCPServerRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """Update fields on an existing server."""
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    updates: list[str] = []
    params: dict = {"id": server_id}

    if req.name is not None:
        updates.append("name = :name")
        params["name"] = req.name
    if req.enabled is not None:
        updates.append("enabled = :enabled")
        params["enabled"] = req.enabled
    if req.auth_header is not None:
        updates.append("auth_header = :auth_header")
        params["auth_header"] = req.auth_header
    if req.description is not None:
        updates.append("description = :description")
        params["description"] = req.description
    if req.transport is not None:
        updates.append("transport = :transport")
        params["transport"] = req.transport

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    sql = text(f"UPDATE external_mcp_servers SET {', '.join(updates)} WHERE id = :id")

    async with AsyncSessionLocal() as session:
        result = await session.execute(sql, params)
        await session.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Server {server_id} not found.")

    server = await _get_server_by_id(server_id)

    # Sync bridge cache when connectivity-relevant fields change.
    # Eviction is immediate (in-process dict mutation); re-load is backgrounded.
    from src.agent.mcp_bridge import _tool_registry, _load_single_server
    connectivity_changed = (
        req.enabled is not None
        or req.auth_header is not None
        or req.transport is not None
    )
    if connectivity_changed:
        server_url = server["url"]
        # Evict stale tool entries synchronously — fast, no I/O
        stale = [k for k, v in list(_tool_registry.items()) if v["server_url"] == server_url]
        for k in stale:
            del _tool_registry[k]

        # Re-load in background so PATCH returns instantly
        if server.get("enabled"):
            server_spec = {
                "id": server["id"],
                "url": server["url"],
                "auth_header": server.get("auth_header"),
                "transport": server.get("transport") or "auto",
            }
            background_tasks.add_task(_load_single_server, server_spec)

    return JSONResponse(server)


@router.delete("/{server_id}")
async def delete_server(server_id: int) -> JSONResponse:
    """Remove a server from the DB and evict its tools from the bridge cache."""
    from sqlalchemy import text

    from src.agent.mcp_bridge import _tool_registry
    from src.storage.db import AsyncSessionLocal

    # Get URL before deleting so we can evict from bridge cache
    server = await _get_server_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server {server_id} not found.")

    server_url = server["url"]

    sql = text("DELETE FROM external_mcp_servers WHERE id = :id")
    async with AsyncSessionLocal() as session:
        await session.execute(sql, {"id": server_id})
        await session.commit()

    # Evict all tools from this server from the in-memory registry
    evicted = [k for k, v in list(_tool_registry.items()) if v["server_url"] == server_url]
    for k in evicted:
        del _tool_registry[k]
    if evicted:
        logger.info("mcp_bridge: evicted %d tool(s) from deleted server %s", len(evicted), server_url)

    return JSONResponse({"deleted": server_id, "tools_evicted": len(evicted)})


@router.post("/{server_id}/test")
async def test_saved_server(server_id: int) -> JSONResponse:
    """Test live connection for an already-saved server."""
    from src.agent.mcp_bridge import test_server

    server = await _get_server_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server {server_id} not found.")

    result = await test_server(
        server["url"],
        server.get("auth_header"),
        transport=server.get("transport", "auto"),
    )
    return JSONResponse(result)


@router.post("/test-url")
async def test_url(req: TestUrlRequest) -> JSONResponse:
    """Test an unsaved MCP server by URL (ad-hoc test before saving)."""
    from src.agent.mcp_bridge import test_server

    result = await test_server(req.url, req.auth_header, transport=req.transport)
    return JSONResponse(result)


@router.post("/reload")
async def reload_bridge_endpoint() -> JSONResponse:
    """Reload the MCP bridge from DB. Returns new total tool count."""
    from src.agent.mcp_bridge import reload_bridge

    tool_count = await reload_bridge()
    return JSONResponse({"tool_count": tool_count, "message": f"Bridge reloaded — {tool_count} tool(s) active"})
