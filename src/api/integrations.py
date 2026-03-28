"""
Enterprise Integration Gateway API.

Endpoints:
  GET  /integrations                      — list configured integrations + status
  GET  /integrations/{service}/status     — test connection to a service
  POST /integrations/{service}/credentials — store service_account credentials
  DELETE /integrations/{service}/credentials — remove credentials
  POST /integrations/{service}/oauth/initiate — start OAuth flow
  GET  /integrations/oauth/callback        — OAuth callback (all services)
  POST /integrations/{service}/oauth/refresh — refresh expiring token
  GET  /integrations/tools                 — list all available integration tools
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)
router = APIRouter(prefix="/integrations", tags=["integrations"])


# ── Request models ─────────────────────────────────────────────────────────────


class StoreCredentialRequest(BaseModel):
    access_token: str
    refresh_token: str | None = None
    metadata: dict[str, Any] = {}
    scopes: list[str] = []
    org_id: str = "default"


# ── List configured integrations ──────────────────────────────────────────────


@router.get("")
async def list_integrations(org_id: str = Query("default")) -> JSONResponse:
    """List all configured integrations and their status."""
    from src.integrations.auth.credential_store import list_credentials

    creds = await list_credentials(org_id=org_id)
    services = ["jira", "slack", "github", "figma", "notion"]

    configured = {c["service"] for c in creds}
    result = []
    for svc in services:
        cred = next((c for c in creds if c["service"] == svc), None)
        result.append({
            "service": svc,
            "configured": svc in configured,
            "auth_type": cred["auth_type"] if cred else None,
            "token_expires_at": cred["token_expires_at"] if cred else None,
            "scopes": cred["scopes"] if cred else [],
        })
    return JSONResponse(result)


# ── Test connection ────────────────────────────────────────────────────────────


@router.get("/{service}/status")
async def test_connection(service: str, org_id: str = Query("default")) -> JSONResponse:
    """Test connectivity to an integration service."""
    try:
        if service == "jira":
            from src.integrations.jira.client import search_issues
            issues = await search_issues("project is not EMPTY ORDER BY created DESC", max_results=1, org_id=org_id)
            return JSONResponse({"service": "jira", "status": "connected", "sample_issues": len(issues)})

        elif service == "slack":
            from src.integrations.slack.client import list_channels
            channels = await list_channels(org_id=org_id)
            return JSONResponse({"service": "slack", "status": "connected", "channels": len(channels)})

        elif service == "github":
            from src.integrations.auth.credential_store import get_credential
            cred = await get_credential("github", org_id)
            if not cred:
                from src.config import settings
                if settings.github_token:
                    return JSONResponse({"service": "github", "status": "connected", "auth": "github_token"})
                raise RuntimeError("No GitHub credentials")
            return JSONResponse({"service": "github", "status": "connected", "auth": cred["auth_type"]})

        elif service == "figma":
            from src.integrations.auth.credential_store import get_credential
            cred = await get_credential("figma", org_id)
            if not cred:
                raise RuntimeError("No Figma credentials")
            return JSONResponse({"service": "figma", "status": "connected"})

        elif service == "notion":
            from src.integrations.notion.client import search
            results = await search("", org_id=org_id)
            return JSONResponse({"service": "notion", "status": "connected", "pages_accessible": len(results)})

        else:
            raise HTTPException(status_code=404, detail=f"Unknown service: {service!r}")

    except RuntimeError as exc:
        return JSONResponse({"service": service, "status": "not_configured", "error": str(exc)})
    except Exception as exc:
        return JSONResponse({"service": service, "status": "error", "error": str(exc)}, status_code=502)


# ── Store service_account credentials ─────────────────────────────────────────


@router.post("/{service}/credentials")
async def store_credentials(service: str, req: StoreCredentialRequest) -> JSONResponse:
    """
    Store a service_account credential (API token, bot token, etc.).
    The token is encrypted before storage — it is never logged or returned in plaintext.
    """
    from src.integrations.auth.credential_store import store_credential

    valid_services = ["jira", "slack", "github", "figma", "notion"]
    if service not in valid_services:
        raise HTTPException(status_code=400, detail=f"Service must be one of: {valid_services}")

    try:
        cred_id = await store_credential(
            service=service,
            auth_type="service_account",
            access_token=req.access_token,
            refresh_token=req.refresh_token,
            metadata=req.metadata,
            scopes=req.scopes,
            org_id=req.org_id,
        )
        return JSONResponse({"service": service, "credential_id": cred_id, "stored": True})
    except Exception as exc:
        logger.error("integrations: store_credentials failed for %s: %s", service, exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Delete credentials ────────────────────────────────────────────────────────


@router.delete("/{service}/credentials")
async def delete_credentials(service: str, org_id: str = Query("default")) -> JSONResponse:
    """Remove stored credentials for a service."""
    from src.integrations.auth.credential_store import delete_credential

    deleted = await delete_credential(service=service, org_id=org_id)
    return JSONResponse({"service": service, "deleted": deleted})


# ── OAuth flow ─────────────────────────────────────────────────────────────────


@router.post("/{service}/oauth/initiate")
async def initiate_oauth(
    service: str,
    org_id: str = Query("default"),
    user_id: str | None = Query(None),
) -> JSONResponse:
    """Start an OAuth 2.0 PKCE flow. Returns the redirect URL."""
    from src.integrations.auth.oauth_manager import initiate_oauth as _initiate

    try:
        result = await _initiate(service=service, org_id=org_id, user_id=user_id)
        return JSONResponse(result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    """
    OAuth callback — exchanges the code for tokens and stores them.
    Redirects to the dashboard integrations page on completion.
    """
    from src.integrations.auth.oauth_manager import handle_callback

    try:
        result = await handle_callback(code=code, state=state)
        service = result.get("service", "")
        return RedirectResponse(
            url=f"http://localhost:8501?integration_connected={service}",
            status_code=302,
        )
    except Exception as exc:
        logger.error("integrations: OAuth callback failed: %s", exc)
        return RedirectResponse(
            url=f"http://localhost:8501?integration_error={str(exc)[:100]}",
            status_code=302,
        )


@router.post("/{service}/oauth/refresh")
async def refresh_oauth_token(
    service: str,
    org_id: str = Query("default"),
    user_id: str | None = Query(None),
) -> JSONResponse:
    """Manually trigger a token refresh."""
    from src.integrations.auth.oauth_manager import refresh_token

    success = await refresh_token(service=service, org_id=org_id, user_id=user_id)
    return JSONResponse({"service": service, "refreshed": success})


# ── Tool schema listing ────────────────────────────────────────────────────────


@router.get("/tools")
async def list_integration_tools(role: str | None = Query(None)) -> JSONResponse:
    """
    List available integration tool schemas.
    Optionally filter by agent role to see which tools that role can use.
    """
    from src.integrations.registry import ALL_INTEGRATION_TOOL_SCHEMAS, get_tools_for_role

    if role:
        tools = get_tools_for_role(role)
    else:
        tools = ALL_INTEGRATION_TOOL_SCHEMAS

    return JSONResponse([
        {"name": t["name"], "description": t["description"][:200]}
        for t in tools
    ])
