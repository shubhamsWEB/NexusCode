"""
Enterprise Integration OAuth Manager — PKCE OAuth 2.0 flows for external services.

Handles the OAuth dance for:
  - Atlassian (Jira/Confluence): OAuth 2.0 with PKCE, scopes per product
  - Slack:       OAuth 2.0, workspace-level bot token
  - GitHub:      GitHub App or OAuth App
  - Figma:       OAuth 2.0
  - Notion:      OAuth 2.0

Flow:
  1. POST /integrations/{service}/oauth/initiate → returns redirect_url
  2. User is redirected to the service OAuth consent screen
  3. Service redirects to GET /integrations/oauth/callback?code=...&state=...
  4. We exchange the code for tokens, store in credential_store
  5. Integration is ready for agent use

State is stored in Redis with a 10-minute TTL (same pattern as MCP OAuth).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from urllib.parse import urlencode
from typing import Any

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_STATE_TTL = 600  # 10 minutes


# ── Provider configs ──────────────────────────────────────────────────────────

_PROVIDER_CONFIGS: dict[str, dict[str, Any]] = {
    "jira": {
        "auth_url": "https://auth.atlassian.com/authorize",
        "token_url": "https://auth.atlassian.com/oauth/token",
        "scopes": "read:jira-work write:jira-work read:jira-user offline_access",
        "audience": "api.atlassian.com",
        "extra_params": {"prompt": "consent"},
        "client_id_setting": "jira_oauth_client_id",
        "client_secret_setting": "jira_oauth_client_secret",
    },
    "slack": {
        "auth_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "scopes": "channels:read chat:write users:read",
        "client_id_setting": "slack_oauth_client_id",
        "client_secret_setting": "slack_oauth_client_secret",
    },
    "github": {
        "auth_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "scopes": "repo read:org",
        "client_id_setting": "github_oauth_client_id",
        "client_secret_setting": "github_oauth_client_secret",
    },
    "figma": {
        "auth_url": "https://www.figma.com/oauth",
        "token_url": "https://www.figma.com/api/oauth/token",
        "scopes": "file_read",
        "client_id_setting": "figma_oauth_client_id",
        "client_secret_setting": "figma_oauth_client_secret",
    },
    "notion": {
        "auth_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "scopes": "read_content update_content insert_content",
        "client_id_setting": "notion_oauth_client_id",
        "client_secret_setting": "notion_oauth_client_secret",
    },
}


# ── PKCE helpers ──────────────────────────────────────────────────────────────


def _generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(96)
    digest = hashlib.sha256(verifier.encode()).digest()
    import base64
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── Redis state helpers ────────────────────────────────────────────────────────


async def _save_state(state: str, data: dict) -> None:
    import redis.asyncio as aioredis
    from src.config import settings

    r = aioredis.from_url(settings.redis_url)
    await r.setex(f"integration_oauth_state:{state}", _STATE_TTL, json.dumps(data))
    await r.aclose()


async def _load_state(state: str) -> dict | None:
    import redis.asyncio as aioredis
    from src.config import settings

    r = aioredis.from_url(settings.redis_url)
    raw = await r.get(f"integration_oauth_state:{state}")
    await r.delete(f"integration_oauth_state:{state}")  # one-time use
    await r.aclose()
    return json.loads(raw) if raw else None


# ── Public API ─────────────────────────────────────────────────────────────────


def get_supported_services() -> list[str]:
    return list(_PROVIDER_CONFIGS.keys())


async def initiate_oauth(
    service: str,
    org_id: str = "default",
    user_id: str | None = None,
) -> dict[str, str]:
    """
    Start the OAuth flow for a service.
    Returns {"redirect_url": "...", "state": "..."} for the caller to redirect to.
    """
    if service not in _PROVIDER_CONFIGS:
        raise ValueError(f"Unsupported OAuth service: {service!r}")

    cfg = _PROVIDER_CONFIGS[service]
    from src.config import settings

    client_id = getattr(settings, cfg["client_id_setting"], None)
    if not client_id:
        raise ValueError(
            f"OAuth client_id not configured for {service!r}. "
            f"Set {cfg['client_id_setting'].upper()} in your environment."
        )

    state = secrets.token_urlsafe(32)
    verifier, challenge = _generate_pkce()
    callback_url = f"{settings.integration_oauth_callback_base_url}/integrations/oauth/callback"

    await _save_state(state, {
        "service": service,
        "org_id": org_id,
        "user_id": user_id,
        "code_verifier": verifier,
        "callback_url": callback_url,
        "created_at": time.time(),
    })

    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": cfg["scopes"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if "audience" in cfg:
        params["audience"] = cfg["audience"]
    params.update(cfg.get("extra_params", {}))

    redirect_url = cfg["auth_url"] + "?" + urlencode(params)
    return {"redirect_url": redirect_url, "state": state}


async def handle_callback(code: str, state: str) -> dict[str, Any]:
    """
    Exchange an authorization code for tokens and store them.
    Called from the OAuth callback endpoint.
    Returns metadata about the completed auth.
    """
    import httpx

    state_data = await _load_state(state)
    if not state_data:
        raise ValueError("OAuth state not found or expired. Please initiate the flow again.")

    service = state_data["service"]
    cfg = _PROVIDER_CONFIGS[service]
    from src.config import settings

    client_id = getattr(settings, cfg["client_id_setting"], None)
    client_secret = getattr(settings, cfg["client_secret_setting"], None)
    callback_url = state_data["callback_url"]

    token_params: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": state_data["code_verifier"],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            cfg["token_url"],
            data=token_params,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        token_data = resp.json()

    access_token = token_data.get("access_token") or token_data.get("authed_user", {}).get("access_token", "")
    refresh_token = token_data.get("refresh_token")

    import datetime
    expires_at = None
    if token_data.get("expires_in"):
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=int(token_data["expires_in"]))

    # For Jira, we also need to fetch the cloudId
    metadata: dict = {}
    if service == "jira":
        metadata = await _fetch_jira_cloud_id(access_token)

    from src.integrations.auth.credential_store import store_credential
    cred_id = await store_credential(
        service=service,
        auth_type="oauth_user" if state_data.get("user_id") else "service_account",
        access_token=access_token,
        refresh_token=refresh_token,
        token_expires_at=expires_at,
        metadata=metadata,
        scopes=cfg["scopes"].split(),
        org_id=state_data["org_id"],
        user_id=state_data.get("user_id"),
    )

    logger.info("oauth_manager: stored credential for %s (cred_id=%s)", service, cred_id)
    return {"service": service, "credential_id": cred_id, "org_id": state_data["org_id"]}


async def refresh_token(service: str, org_id: str = "default", user_id: str | None = None) -> bool:
    """
    Refresh an expiring OAuth token.
    Returns True if refresh succeeded.
    """
    import httpx
    from src.integrations.auth.credential_store import get_credential, store_credential

    cred = await get_credential(service, org_id, user_id)
    if not cred or not cred.get("refresh_token"):
        return False

    cfg = _PROVIDER_CONFIGS.get(service)
    if not cfg:
        return False

    from src.config import settings
    client_id = getattr(settings, cfg["client_id_setting"], None)
    client_secret = getattr(settings, cfg["client_secret_setting"], None)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                cfg["token_url"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": cred["refresh_token"],
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            token_data = resp.json()

        import datetime
        expires_at = None
        if token_data.get("expires_in"):
            expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=int(token_data["expires_in"]))

        await store_credential(
            service=service,
            auth_type=cred["auth_type"],
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", cred["refresh_token"]),
            token_expires_at=expires_at,
            metadata=cred["metadata"],
            scopes=cred["scopes"],
            org_id=org_id,
            user_id=user_id,
        )
        return True
    except Exception as exc:
        logger.error("oauth_manager: token refresh failed for %s: %s", service, exc)
        return False


async def _fetch_jira_cloud_id(access_token: str) -> dict:
    """Fetch Atlassian cloud ID needed for Jira API calls."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            resp.raise_for_status()
            resources = resp.json()
        if resources:
            return {
                "cloud_id": resources[0]["id"],
                "cloud_name": resources[0]["name"],
                "base_url": resources[0]["url"],
            }
    except Exception as exc:
        logger.warning("oauth_manager: could not fetch Jira cloud ID: %s", exc)
    return {}
