"""
Integration Credential Store — encrypted token storage and retrieval.

Credentials are AES-256-GCM encrypted at rest. The encryption key comes from
settings.integration_encryption_key (32-byte hex string).

The LLM NEVER sees raw tokens. Tool dispatch functions call get_credential()
transparently; the agent only sees the tool's result (e.g. Jira issue data).

Credential types:
  - service_account: a bot/service credential shared across all workflows
    (e.g. the Jira bot, Slack bot, GitHub App)
  - oauth_user: a user-delegated OAuth credential tied to a specific user
    (e.g. for actions that should be attributed to a real person)

For most enterprise workflows, service_account is the right choice.
"""

from __future__ import annotations

import json
import os
from typing import Any

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── Encryption helpers ────────────────────────────────────────────────────────


def _get_key() -> bytes:
    """Return the 32-byte AES key from settings, or a dev fallback."""
    from src.config import settings

    key_hex = settings.integration_encryption_key
    if not key_hex:
        # Dev fallback — logs a warning, acceptable for local dev
        logger.warning("integration_encryption_key not set — using insecure dev key")
        key_hex = "0" * 64
    return bytes.fromhex(key_hex[:64])


def _encrypt(plaintext: str) -> bytes:
    """AES-256-GCM encrypt a string, returning nonce + tag + ciphertext."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _get_key()
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return nonce + ciphertext  # first 12 bytes = nonce, rest = ciphertext + 16-byte tag


def _decrypt(data: bytes) -> str:
    """Decrypt AES-256-GCM data, returning the plaintext string."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _get_key()
    nonce, ciphertext = data[:12], data[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode()


# ── Credential CRUD ────────────────────────────────────────────────────────────


async def store_credential(
    service: str,
    auth_type: str,
    access_token: str,
    refresh_token: str | None = None,
    token_expires_at: Any = None,
    metadata: dict | None = None,
    scopes: list[str] | None = None,
    org_id: str = "default",
    user_id: str | None = None,
) -> str:
    """
    Encrypt and store a credential in the integration_credentials table.
    Returns the credential UUID.
    """
    from sqlalchemy import text
    from src.storage.db import AsyncSessionLocal

    access_enc = _encrypt(access_token)
    refresh_enc = _encrypt(refresh_token) if refresh_token else None

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("""
                INSERT INTO integration_credentials
                    (org_id, service, auth_type, user_id,
                     access_token_enc, refresh_token_enc, token_expires_at,
                     metadata, scopes)
                VALUES
                    (:org_id, :service, :auth_type, :user_id,
                     :access_enc, :refresh_enc, :expires_at,
                     :metadata, :scopes)
                ON CONFLICT (org_id, service, COALESCE(user_id, 'service'))
                DO UPDATE SET
                    access_token_enc  = EXCLUDED.access_token_enc,
                    refresh_token_enc = EXCLUDED.refresh_token_enc,
                    token_expires_at  = EXCLUDED.token_expires_at,
                    metadata          = EXCLUDED.metadata,
                    scopes            = EXCLUDED.scopes,
                    updated_at        = now()
                RETURNING id
            """),
            {
                "org_id": org_id,
                "service": service,
                "auth_type": auth_type,
                "user_id": user_id,
                "access_enc": access_enc,
                "refresh_enc": refresh_enc,
                "expires_at": token_expires_at,
                "metadata": json.dumps(metadata or {}),
                "scopes": scopes or [],
            },
        )).mappings().first()
        await session.commit()

    return str(row["id"])


async def get_credential(
    service: str,
    org_id: str = "default",
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Retrieve and decrypt a credential.
    Returns a dict with access_token, refresh_token, metadata, scopes.
    Returns None if not found.
    """
    from sqlalchemy import text
    from src.storage.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("""
                SELECT id, auth_type, access_token_enc, refresh_token_enc,
                       token_expires_at, metadata, scopes
                FROM integration_credentials
                WHERE org_id = :org_id
                  AND service = :service
                  AND (:user_id IS NULL OR user_id = :user_id)
                ORDER BY
                    CASE WHEN :user_id IS NOT NULL AND user_id = :user_id THEN 0 ELSE 1 END,
                    updated_at DESC
                LIMIT 1
            """),
            {"org_id": org_id, "service": service, "user_id": user_id},
        )).mappings().first()

    if not row:
        return None

    try:
        access_token = _decrypt(bytes(row["access_token_enc"]))
    except Exception as exc:
        logger.error("credential_store: decryption failed for %s/%s: %s", service, org_id, exc)
        return None

    refresh_token = None
    if row["refresh_token_enc"]:
        try:
            refresh_token = _decrypt(bytes(row["refresh_token_enc"]))
        except Exception:
            pass

    return {
        "id": str(row["id"]),
        "auth_type": row["auth_type"],
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expires_at": row["token_expires_at"],
        "metadata": row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"] or "{}"),
        "scopes": list(row["scopes"] or []),
    }


async def list_credentials(org_id: str = "default") -> list[dict[str, Any]]:
    """List all credential records (without tokens) for a given org."""
    from sqlalchemy import text
    from src.storage.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            text("""
                SELECT id, service, auth_type, user_id, token_expires_at,
                       scopes, metadata, created_at, updated_at
                FROM integration_credentials
                WHERE org_id = :org_id
                ORDER BY service, created_at
            """),
            {"org_id": org_id},
        )).mappings().all()

    result = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        for ts_field in ("token_expires_at", "created_at", "updated_at"):
            if d.get(ts_field):
                d[ts_field] = d[ts_field].isoformat()
        result.append(d)
    return result


async def delete_credential(
    service: str,
    org_id: str = "default",
    user_id: str | None = None,
) -> bool:
    """Delete a credential. Returns True if a row was deleted."""
    from sqlalchemy import text
    from src.storage.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                DELETE FROM integration_credentials
                WHERE org_id = :org_id
                  AND service = :service
                  AND (:user_id IS NULL OR user_id = :user_id)
            """),
            {"org_id": org_id, "service": service, "user_id": user_id},
        )
        await session.commit()
        return result.rowcount > 0


# ── Fresh credential fetch (auto-refresh before expiry) ───────────────────────

_REFRESH_BUFFER_SECONDS = 300  # refresh if expiring within 5 minutes


async def get_fresh_credential(
    service: str,
    org_id: str = "default",
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Fetch a credential and proactively refresh it if it expires within
    _REFRESH_BUFFER_SECONDS. This is what all integration clients must use
    so tokens are always valid at the moment of the external API call.

    The token is fetched here, inside the tool execution path — it is never
    returned to the LLM context window. The agent only sees the tool result.

    Refresh is skipped for:
    - service_account tokens with no refresh_token (static API keys)
    - tokens with no expiry information (assumed non-expiring)
    - tokens that are still comfortably within their validity window
    """
    import datetime

    cred = await get_credential(service, org_id, user_id)
    if not cred:
        return None

    # Only OAuth tokens have refresh_token and token_expires_at
    if not cred.get("refresh_token") or not cred.get("token_expires_at"):
        return cred

    expires_at = cred["token_expires_at"]
    if isinstance(expires_at, str):
        # Handle both ISO format with and without timezone
        expires_at = expires_at.replace("Z", "+00:00")
        try:
            expires_at = datetime.datetime.fromisoformat(expires_at)
        except ValueError:
            return cred  # unparseable — skip refresh, return as-is

    # Normalise to naive UTC for comparison
    if expires_at.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=None) - expires_at.utcoffset()

    now = datetime.datetime.utcnow()
    seconds_remaining = (expires_at - now).total_seconds()

    if seconds_remaining < _REFRESH_BUFFER_SECONDS:
        logger.info(
            "credential_store: proactive refresh for %s/%s (expires in %.0fs)",
            service, org_id, max(0.0, seconds_remaining),
        )
        # Lazy import avoids circular dependency (oauth_manager imports credential_store)
        try:
            from src.integrations.auth.oauth_manager import refresh_token as _do_refresh
            refreshed = await _do_refresh(service=service, org_id=org_id, user_id=user_id)
            if refreshed:
                cred = await get_credential(service, org_id, user_id) or cred
            else:
                logger.warning(
                    "credential_store: proactive refresh failed for %s/%s — proceeding with existing token",
                    service, org_id,
                )
        except Exception as exc:
            logger.warning("credential_store: refresh error for %s: %s", service, exc)

    return cred


# ── Config-based credential bootstrap ─────────────────────────────────────────


async def bootstrap_from_config() -> None:
    """
    On startup, if settings contain integration tokens (e.g. JIRA_API_TOKEN),
    upsert them as service_account credentials in the DB.
    This allows teams to start using integrations with env vars before setting
    up full OAuth flows.
    """
    from src.config import settings

    tokens_to_bootstrap = []

    if settings.jira_api_token and settings.jira_base_url:
        tokens_to_bootstrap.append({
            "service": "jira",
            "access_token": settings.jira_api_token,
            "metadata": {"base_url": settings.jira_base_url, "email": settings.jira_email},
        })

    if settings.slack_bot_token:
        tokens_to_bootstrap.append({
            "service": "slack",
            "access_token": settings.slack_bot_token,
            "metadata": {},
        })

    if settings.figma_access_token:
        tokens_to_bootstrap.append({
            "service": "figma",
            "access_token": settings.figma_access_token,
            "metadata": {},
        })

    if settings.notion_api_key:
        tokens_to_bootstrap.append({
            "service": "notion",
            "access_token": settings.notion_api_key,
            "metadata": {},
        })

    for item in tokens_to_bootstrap:
        try:
            await store_credential(
                service=item["service"],
                auth_type="service_account",
                access_token=item["access_token"],
                metadata=item.get("metadata", {}),
            )
            logger.info("credential_store: bootstrapped service_account for %s", item["service"])
        except Exception as exc:
            logger.warning("credential_store: bootstrap failed for %s: %s", item["service"], exc)
