"""
External MCP Server management endpoints.

Endpoints
---------
GET    /mcp-servers              — list all servers (with live tool_count from bridge cache)
POST   /mcp-servers              — register a new server; 201 Created
PATCH  /mcp-servers/{id}         — update fields; 200
DELETE /mcp-servers/{id}         — remove server + evict from bridge cache; 200
POST   /mcp-servers/{id}/test    — test live connection for a saved server
POST   /mcp-servers/test-url     — test an unsaved server by URL or stdio command
POST   /mcp-servers/reload       — reload bridge from DB; returns new tool count

OAuth endpoints
---------------
POST /mcp-servers/oauth/discover          — probe URL for OAuth metadata
POST /mcp-servers/{id}/oauth/initiate     — generate PKCE + state, return auth_url
GET  /mcp-servers/oauth/callback          — exchange code, store token
POST /mcp-servers/{id}/oauth/refresh      — refresh expired token
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json as _json_stdlib
import logging
import os
import secrets
import socket
import ssl
import urllib.parse

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


# ── Request models ────────────────────────────────────────────────────────────


class AddMCPServerRequest(BaseModel):
    name: str
    server_type: str = "remote"        # remote | stdio
    # remote
    url: str | None = None
    transport: str = "auto"            # auto | streamable_http | sse
    auth_type: str = "header"          # none | header | bearer | basic | oauth
    auth_header: str | None = None     # token / "email:token" / verbatim header
    # stdio
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    # common
    description: str | None = None
    enabled: bool = True


class UpdateMCPServerRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    auth_header: str | None = None
    auth_type: str | None = None
    description: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None


class TestUrlRequest(BaseModel):
    url: str | None = None
    auth_header: str | None = None
    auth_type: str = "header"
    transport: str = "auto"
    server_type: str = "remote"
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}


class OAuthDiscoverRequest(BaseModel):
    url: str


# ── DB helpers ────────────────────────────────────────────────────────────────

_SERVER_COLS = """
    id, name, url, enabled, auth_header, description,
    COALESCE(transport, 'auto') AS transport,
    COALESCE(server_type, 'remote') AS server_type,
    command, args, env,
    COALESCE(auth_type, 'header') AS auth_type,
    oauth_client_id, oauth_token, oauth_expires_at, oauth_token_endpoint,
    tool_count, last_seen_at, last_error, created_at
"""


async def _get_all_servers() -> list[dict]:
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    sql = text(f"SELECT {_SERVER_COLS} FROM external_mcp_servers ORDER BY created_at ASC")
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql)).mappings().all()

    result = []
    for r in rows:
        d = dict(r)
        for k in ("last_seen_at", "created_at", "oauth_expires_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        result.append(d)
    return result


async def _get_server_by_id(server_id: int) -> dict | None:
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    sql = text(f"SELECT {_SERVER_COLS} FROM external_mcp_servers WHERE id = :id")
    async with AsyncSessionLocal() as session:
        row = (await session.execute(sql, {"id": server_id})).mappings().first()

    if not row:
        return None

    d = dict(row)
    for k in ("last_seen_at", "created_at", "oauth_expires_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


def _effective_auth_header(server: dict) -> str | None:
    """Compute the Authorization header value from a server dict."""
    from src.agent.mcp_bridge import _build_auth_header
    auth_type = server.get("auth_type") or "header"
    if auth_type == "oauth":
        raw = server.get("oauth_token")
    else:
        raw = server.get("auth_header")
    return _build_auth_header(auth_type, raw)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("")
async def list_servers() -> JSONResponse:
    """List all registered external MCP servers with live tool counts from bridge cache."""
    from src.agent.mcp_bridge import _tool_registry

    servers = await _get_all_servers()

    # Augment with live tool count per server (keyed by url or command)
    identifier_to_live: dict[str, int] = {}
    for entry in _tool_registry.values():
        key = entry.get("server_url") or entry.get("command") or ""
        if key:
            identifier_to_live[key] = identifier_to_live.get(key, 0) + 1

    for s in servers:
        key = s.get("url") or s.get("command") or ""
        live = identifier_to_live.get(key)
        if live is not None:
            s["tool_count"] = live

    return JSONResponse(servers)


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_server(req: AddMCPServerRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """Register a new external MCP server."""
    import json as _json

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from src.storage.db import AsyncSessionLocal

    if req.server_type == "remote" and not req.url:
        raise HTTPException(status_code=422, detail="Remote server requires a URL.")
    if req.server_type == "stdio" and not req.command:
        raise HTTPException(status_code=422, detail="Stdio server requires a command.")

    sql = text(
        """
        INSERT INTO external_mcp_servers
          (name, url, enabled, auth_header, description, transport,
           server_type, command, args, env, auth_type)
        VALUES
          (:name, :url, :enabled, :auth_header, :description, :transport,
           :server_type, :command, :args, :env, :auth_type)
        RETURNING id, name, url, enabled, auth_header, description,
                  COALESCE(transport, 'auto') AS transport,
                  COALESCE(server_type, 'remote') AS server_type,
                  command, args, env,
                  COALESCE(auth_type, 'header') AS auth_type,
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
        "server_type": req.server_type,
        "command": req.command,
        "args": _json.dumps(req.args),
        "env": _json.dumps(req.env),
        "auth_type": req.auth_type,
    }

    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(sql, params)).mappings().first()
            await session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A server with URL '{req.url}' already exists.",
        ) from exc

    if not row:
        raise HTTPException(status_code=500, detail="Insert failed.")

    d = dict(row)
    for k in ("last_seen_at", "created_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()

    # Load tools into the bridge cache in background
    if req.enabled:
        from src.agent.mcp_bridge import _load_single_server
        server_spec = {
            "id": d["id"],
            "url": d.get("url"),
            "auth_header": d.get("auth_header"),
            "transport": d.get("transport") or "auto",
            "server_type": d.get("server_type") or "remote",
            "command": d.get("command"),
            "args": req.args,
            "env": req.env,
            "auth_type": d.get("auth_type") or "header",
            "oauth_token": None,
        }
        background_tasks.add_task(_load_single_server, server_spec)

    return JSONResponse(d, status_code=status.HTTP_201_CREATED)


@router.patch("/{server_id}")
async def update_server(server_id: int, req: UpdateMCPServerRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    """Update fields on an existing server."""
    import json as _json

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
    if req.auth_type is not None:
        updates.append("auth_type = :auth_type")
        params["auth_type"] = req.auth_type
    if req.description is not None:
        updates.append("description = :description")
        params["description"] = req.description
    if req.transport is not None:
        updates.append("transport = :transport")
        params["transport"] = req.transport
    if req.command is not None:
        updates.append("command = :command")
        params["command"] = req.command
    if req.args is not None:
        updates.append("args = :args")
        params["args"] = _json.dumps(req.args)
    if req.env is not None:
        updates.append("env = :env")
        params["env"] = _json.dumps(req.env)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update.")

    sql = text(f"UPDATE external_mcp_servers SET {', '.join(updates)} WHERE id = :id")

    async with AsyncSessionLocal() as session:
        result = await session.execute(sql, params)
        await session.commit()
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            raise HTTPException(status_code=404, detail=f"Server {server_id} not found.")

    server = await _get_server_by_id(server_id)

    # Sync bridge cache when connectivity-relevant fields change.
    from src.agent.mcp_bridge import _load_single_server, _tool_registry
    connectivity_changed = any(v is not None for v in [
        req.enabled, req.auth_header, req.auth_type, req.transport,
        req.command, req.args, req.env,
    ])
    if connectivity_changed:
        # Evict stale entries synchronously
        server_url = server.get("url")
        server_cmd = server.get("command")
        stale = [
            k for k, v in list(_tool_registry.items())
            if (server_url and v.get("server_url") == server_url)
            or (server_cmd and v.get("command") == server_cmd)
        ]
        for k in stale:
            del _tool_registry[k]

        if server.get("enabled"):
            server_spec = {
                "id": server["id"],
                "url": server.get("url"),
                "auth_header": server.get("auth_header"),
                "transport": server.get("transport") or "auto",
                "server_type": server.get("server_type") or "remote",
                "command": server.get("command"),
                "args": server.get("args") or [],
                "env": server.get("env") or {},
                "auth_type": server.get("auth_type") or "header",
                "oauth_token": server.get("oauth_token"),
            }
            background_tasks.add_task(_load_single_server, server_spec)

    return JSONResponse(server)


@router.delete("/{server_id}")
async def delete_server(server_id: int) -> JSONResponse:
    """Remove a server from the DB and evict its tools from the bridge cache."""
    from sqlalchemy import text

    from src.agent.mcp_bridge import _tool_registry
    from src.storage.db import AsyncSessionLocal

    server = await _get_server_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server {server_id} not found.")

    server_url = server.get("url")
    server_cmd = server.get("command")

    sql = text("DELETE FROM external_mcp_servers WHERE id = :id")
    async with AsyncSessionLocal() as session:
        await session.execute(sql, {"id": server_id})
        await session.commit()

    evicted = [
        k for k, v in list(_tool_registry.items())
        if (server_url and v.get("server_url") == server_url)
        or (server_cmd and v.get("command") == server_cmd)
    ]
    for k in evicted:
        del _tool_registry[k]
    if evicted:
        logger.info("mcp_bridge: evicted %d tool(s) from deleted server %s", len(evicted), server_url or server_cmd)

    return JSONResponse({"deleted": server_id, "tools_evicted": len(evicted)})


@router.post("/{server_id}/test")
async def test_saved_server(server_id: int) -> JSONResponse:
    """Test live connection for an already-saved server."""
    from src.agent.mcp_bridge import test_server

    server = await _get_server_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server {server_id} not found.")

    result = await test_server(
        server.get("url"),
        _effective_auth_header(server),
        transport=server.get("transport", "auto"),
        server_type=server.get("server_type", "remote"),
        command=server.get("command"),
        args=server.get("args") or [],
        env=server.get("env") or {},
    )
    return JSONResponse(result)


@router.post("/test-url")
async def test_url(req: TestUrlRequest) -> JSONResponse:
    """Test an unsaved MCP server (ad-hoc test before saving)."""
    from src.agent.mcp_bridge import _build_auth_header, test_server

    auth_header = _build_auth_header(req.auth_type, req.auth_header)
    result = await test_server(
        req.url,
        auth_header,
        transport=req.transport,
        server_type=req.server_type,
        command=req.command,
        args=req.args,
        env=req.env,
    )
    return JSONResponse(result)


@router.post("/reload")
async def reload_bridge_endpoint() -> JSONResponse:
    """Reload the MCP bridge from DB. Returns new total tool count."""
    from src.agent.mcp_bridge import reload_bridge

    tool_count = await reload_bridge()
    return JSONResponse({"tool_count": tool_count, "message": f"Bridge reloaded — {tool_count} tool(s) active"})


# ── SSRF guard ─────────────────────────────────────────────────────────────────
#
# Full SSRF mitigation strategy (CWE-918):
#
#   Step 1 — _resolve_safe_ip(hostname, port): resolve the hostname once, validate
#            every returned IP is public, and return the first validated IP string.
#
#   Step 2 — _ssrf_safe_https_get / _ssrf_safe_https_post: open the TCP connection
#            directly to the *pre-validated* IP (no second DNS lookup = no DNS-rebinding
#            window), while setting `server_hostname` so Python's ssl module still
#            performs correct SNI + certificate hostname verification against the
#            original hostname.
#
# This approach satisfies the CodeQL py/full-ssrf rule because the URL that flows
# to the network layer is constructed from `validated_ip` (our DNS result), not
# from the raw user-supplied string.

_PRIVATE_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / AWS metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


async def _resolve_safe_ip(hostname: str, port: int) -> str:
    """
    Resolve *hostname* and return the first public IP as a string.

    Raises HTTPException(400) if:
    - the hostname cannot be resolved
    - ANY resolved address is private, loopback, link-local, or unspecified
    """
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.run_in_executor(
            None, socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"Cannot resolve hostname '{hostname}': {exc}") from exc

    if not infos:
        raise HTTPException(status_code=400, detail=f"No addresses found for '{hostname}'.")

    validated_ip: str | None = None
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_unspecified
            or any(addr in net for net in _PRIVATE_NETWORKS)
        ):
            raise HTTPException(
                status_code=400,
                detail="Requests to private, loopback, or internal addresses are not allowed.",
            )
        if validated_ip is None:
            validated_ip = ip_str

    if validated_ip is None:
        raise HTTPException(status_code=400, detail=f"No routable address found for '{hostname}'.")
    return validated_ip


async def _ssrf_safe_https_get(url: str, *, timeout: float = 10.0) -> tuple[int, dict]:
    """
    SSRF-safe HTTPS GET.

    1. Validates the URL scheme is https://.
    2. Resolves the hostname → validates all IPs are public (_resolve_safe_ip).
    3. Opens a TCP connection **directly to the validated IP** — no second DNS
       lookup, so DNS-rebinding between check and connect is impossible.
    4. Passes `server_hostname=hostname` to asyncio.open_connection so that
       Python's ssl module performs SNI and certificate verification against the
       original hostname (not the IP), keeping TLS security intact.

    Returns (http_status_code, parsed_json_body).
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="URL must use the https:// scheme.")
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="URL must include a hostname.")
    port = parsed.port or 443
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")

    # Resolve once; connection below uses this IP directly — no re-resolution.
    validated_ip = await _resolve_safe_ip(hostname, port)

    ssl_ctx = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                validated_ip, port,
                ssl=ssl_ctx,
                server_hostname=hostname,   # SNI + cert check against hostname
            ),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Connection failed: {exc}") from exc

    try:
        writer.write(
            f"GET {path} HTTP/1.0\r\nHost: {hostname}\r\nAccept: application/json\r\n\r\n"
            .encode()
        )
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(131_072), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Request timed out.") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        return 0, {}
    try:
        status = int(raw[: raw.index(b"\r\n")].decode().split()[1])
    except (ValueError, IndexError):
        return 0, {}
    body = raw[header_end + 4:].strip()
    try:
        return status, _json_stdlib.loads(body) if body else {}
    except _json_stdlib.JSONDecodeError:
        return status, {}


async def _ssrf_safe_https_post_form(
    url: str, data: dict, *, timeout: float = 15.0
) -> tuple[int, dict]:
    """
    SSRF-safe HTTPS POST with application/x-www-form-urlencoded body.

    Uses the same IP-pinning strategy as _ssrf_safe_https_get.
    Returns (http_status_code, parsed_json_body).
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Token endpoint must use the https:// scheme.")
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Token endpoint must include a hostname.")
    port = parsed.port or 443
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")

    validated_ip = await _resolve_safe_ip(hostname, port)

    body_bytes = urllib.parse.urlencode(data).encode()
    ssl_ctx = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                validated_ip, port,
                ssl=ssl_ctx,
                server_hostname=hostname,
            ),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Connection to token endpoint failed: {exc}") from exc

    try:
        writer.write(
            (
                f"POST {path} HTTP/1.0\r\n"
                f"Host: {hostname}\r\n"
                "Accept: application/json\r\n"
                "Content-Type: application/x-www-form-urlencoded\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                "\r\n"
            ).encode() + body_bytes
        )
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(131_072), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Token request timed out.") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        return 0, {}
    try:
        status = int(raw[: raw.index(b"\r\n")].decode().split()[1])
    except (ValueError, IndexError):
        return 0, {}
    body = raw[header_end + 4:].strip()
    try:
        return status, _json_stdlib.loads(body) if body else {}
    except _json_stdlib.JSONDecodeError:
        return status, {}


# ── OAuth endpoints ─────────────────────────────────────────────────────────────


@router.post("/oauth/discover")
async def oauth_discover(req: OAuthDiscoverRequest) -> JSONResponse:
    """
    Probe a remote MCP server URL for OAuth authorization server metadata.
    Returns {auth_endpoint, token_endpoint, registration_endpoint, scopes_supported}.
    """
    # Validate scheme + hostname; build well-known URL from parsed (not raw) input
    parsed_req = urllib.parse.urlparse(req.url)
    if parsed_req.scheme != "https":
        raise HTTPException(status_code=400, detail="URL must use the https:// scheme.")
    if not parsed_req.hostname:
        raise HTTPException(status_code=400, detail="URL must include a hostname.")
    port = parsed_req.port or 443
    base_netloc = f"{parsed_req.hostname}:{port}"
    # Try RFC 8414 well-known endpoint — path is a hardcoded constant, not user input
    well_known_url = f"https://{base_netloc}/.well-known/oauth-authorization-server"
    try:
        status_code, meta = await _ssrf_safe_https_get(well_known_url, timeout=10.0)
        if status_code == 200 and meta:
            return JSONResponse({
                "auth_endpoint": meta.get("authorization_endpoint"),
                "token_endpoint": meta.get("token_endpoint"),
                "registration_endpoint": meta.get("registration_endpoint"),
                "scopes_supported": meta.get("scopes_supported", []),
                "raw": meta,
            })
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("oauth_discover: well-known probe failed: %s", exc)

    return JSONResponse(
        {"error": "No OAuth metadata found at this URL. Try adding client_id manually."},
        status_code=404,
    )


def _pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) using S256."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


@router.post("/{server_id}/oauth/initiate")
async def oauth_initiate(server_id: int) -> JSONResponse:
    """
    Generate PKCE state, store in Redis with 10-min TTL,
    and return the authorization URL for the user to visit.
    """
    import json as _json

    from src.config import settings
    from src.storage.db import AsyncSessionLocal
    from sqlalchemy import text

    server = await _get_server_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server {server_id} not found.")
    if server.get("auth_type") != "oauth":
        raise HTTPException(status_code=400, detail="Server auth_type is not 'oauth'.")

    # Probe for OAuth metadata if we don't have token_endpoint yet
    token_endpoint = server.get("oauth_token_endpoint")
    auth_endpoint: str | None = None
    if server.get("url"):
        srv_parsed = urllib.parse.urlparse(server["url"])
        if srv_parsed.scheme != "https" or not srv_parsed.hostname:
            raise HTTPException(status_code=400, detail="Server URL must be a public HTTPS address.")
        srv_port = srv_parsed.port or 443
        well_known_url = f"https://{srv_parsed.hostname}:{srv_port}/.well-known/oauth-authorization-server"
        try:
            status_code, meta = await _ssrf_safe_https_get(well_known_url, timeout=10.0)
            if status_code == 200 and meta:
                auth_endpoint = meta.get("authorization_endpoint")
                if not token_endpoint:
                    token_endpoint = meta.get("token_endpoint")
        except Exception:
            pass

    if not auth_endpoint:
        raise HTTPException(status_code=400, detail="Could not discover authorization_endpoint for this server.")

    client_id = server.get("oauth_client_id")
    if not client_id:
        raise HTTPException(status_code=400, detail="oauth_client_id not set on this server.")

    code_verifier, code_challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    callback_url = f"{settings.mcp_oauth_callback_base_url.rstrip('/')}/mcp-servers/oauth/callback"

    # Store in Redis with 10-min TTL
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        await r.setex(
            f"mcp_oauth_state:{state}",
            600,
            _json.dumps({"server_id": server_id, "code_verifier": code_verifier}),
        )
        await r.aclose()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Redis unavailable: {exc}") from exc

    # Persist code_verifier + token_endpoint to DB (needed at callback time)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE external_mcp_servers SET oauth_code_verifier = :cv, oauth_token_endpoint = :te WHERE id = :id"
            ),
            {"cv": code_verifier, "te": token_endpoint, "id": server_id},
        )
        await session.commit()

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback_url,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = auth_endpoint + "?" + urllib.parse.urlencode(params)
    return JSONResponse({"auth_url": auth_url, "state": state})


@router.get("/oauth/callback")
async def oauth_callback(request: Request) -> RedirectResponse:
    """
    Exchange the authorization code for tokens and store in DB.
    Redirects back to the dashboard after completion.
    """
    import json as _json

    from sqlalchemy import text

    from src.config import settings
    from src.storage.db import AsyncSessionLocal

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    dashboard_url = "http://localhost:8501"  # fallback

    if error:
        logger.warning("oauth_callback: error from provider: %s", error)
        return RedirectResponse(url=dashboard_url + "?oauth_error=" + urllib.parse.quote(error))

    if not code or not state:
        return RedirectResponse(url=dashboard_url + "?oauth_error=missing_code_or_state")

    # Retrieve state from Redis
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        raw = await r.get(f"mcp_oauth_state:{state}")
        if raw:
            await r.delete(f"mcp_oauth_state:{state}")
        await r.aclose()
    except Exception as exc:
        logger.error("oauth_callback: Redis error: %s", exc)
        return RedirectResponse(url=dashboard_url + "?oauth_error=redis_error")

    if not raw:
        return RedirectResponse(url=dashboard_url + "?oauth_error=state_expired_or_invalid")

    state_data = _json.loads(raw)
    server_id = state_data["server_id"]
    code_verifier = state_data["code_verifier"]

    server = await _get_server_by_id(server_id)
    if not server:
        return RedirectResponse(url=dashboard_url + "?oauth_error=server_not_found")

    token_endpoint = server.get("oauth_token_endpoint")
    client_id = server.get("oauth_client_id")
    callback_url = f"{settings.mcp_oauth_callback_base_url.rstrip('/')}/mcp-servers/oauth/callback"

    if not token_endpoint or not client_id:
        return RedirectResponse(url=dashboard_url + "?oauth_error=missing_token_endpoint_or_client_id")

    # Exchange code for tokens (SSRF-safe: IP-pinned connection)
    try:
        status_code, tokens = await _ssrf_safe_https_post_form(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_url,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
            timeout=15.0,
        )
        if status_code < 200 or status_code >= 300:
            raise ValueError(f"Token endpoint returned HTTP {status_code}")
    except HTTPException:
        return RedirectResponse(url=dashboard_url + "?oauth_error=invalid_token_endpoint")
    except Exception as exc:
        logger.error("oauth_callback: token exchange failed: %s", exc)
        return RedirectResponse(url=dashboard_url + "?oauth_error=token_exchange_failed")

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in")

    expires_at_sql = "now() + make_interval(secs => :expires_in)" if expires_in else "NULL"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                f"""
                UPDATE external_mcp_servers
                SET oauth_token = :token,
                    oauth_refresh_token = :refresh,
                    oauth_expires_at = {expires_at_sql},
                    oauth_code_verifier = NULL
                WHERE id = :id
                """
            ),
            {"token": access_token, "refresh": refresh_token, "expires_in": expires_in, "id": server_id},
        )
        await session.commit()

    logger.info("oauth_callback: stored tokens for server %d", server_id)
    return RedirectResponse(url=dashboard_url + "?oauth_success=1&server_id=" + str(server_id))


@router.post("/{server_id}/oauth/refresh")
async def oauth_refresh(server_id: int) -> JSONResponse:
    """Refresh an expired OAuth token using the stored refresh_token."""
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    server = await _get_server_by_id(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server {server_id} not found.")

    refresh_token = server.get("oauth_refresh_token")
    token_endpoint = server.get("oauth_token_endpoint")
    client_id = server.get("oauth_client_id")

    if not refresh_token:
        raise HTTPException(status_code=400, detail="No refresh_token stored for this server.")
    if not token_endpoint:
        raise HTTPException(status_code=400, detail="No token_endpoint stored for this server.")

    # Refresh token (SSRF-safe: IP-pinned connection)
    try:
        status_code, tokens = await _ssrf_safe_https_post_form(
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id or "",
            },
            timeout=15.0,
        )
        if status_code < 200 or status_code >= 300:
            raise ValueError(f"Token endpoint returned HTTP {status_code}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Token refresh failed: {exc}") from exc

    access_token = tokens.get("access_token")
    new_refresh = tokens.get("refresh_token", refresh_token)
    expires_in = tokens.get("expires_in")

    expires_at_sql = "now() + make_interval(secs => :expires_in)" if expires_in else "NULL"

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                f"""
                UPDATE external_mcp_servers
                SET oauth_token = :token,
                    oauth_refresh_token = :refresh,
                    oauth_expires_at = {expires_at_sql}
                WHERE id = :id
                """
            ),
            {"token": access_token, "refresh": new_refresh, "expires_in": expires_in, "id": server_id},
        )
        await session.commit()

    return JSONResponse({"ok": True, "message": "Token refreshed successfully."})
