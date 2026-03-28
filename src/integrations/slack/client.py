"""Slack integration client — async wrapper for the Slack Web API."""

from __future__ import annotations

from typing import Any

import httpx

from src.integrations.auth.credential_store import get_fresh_credential
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_SLACK_API = "https://slack.com/api"


async def _get_token(org_id: str = "default") -> str:
    cred = await get_fresh_credential("slack", org_id)
    if not cred:
        raise RuntimeError(
            "Slack credentials not configured. Set SLACK_BOT_TOKEN or complete OAuth setup."
        )
    return cred["access_token"]


async def send_message(
    channel: str,
    text: str,
    blocks: list | None = None,
    thread_ts: str | None = None,
    org_id: str = "default",
) -> dict[str, Any]:
    """Post a message to a Slack channel."""
    token = await _get_token(org_id)
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_SLACK_API}/chat.postMessage",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")

    return {"ts": data.get("ts", ""), "channel": data.get("channel", ""), "ok": True}


async def get_channel_history(
    channel: str,
    limit: int = 20,
    org_id: str = "default",
) -> list[dict[str, Any]]:
    """Fetch recent messages from a Slack channel."""
    token = await _get_token(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_SLACK_API}/conversations.history",
            headers={"Authorization": f"Bearer {token}"},
            params={"channel": channel, "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()

    messages = []
    for msg in data.get("messages", []):
        messages.append({
            "ts": msg.get("ts", ""),
            "user": msg.get("user", ""),
            "text": msg.get("text", ""),
            "type": msg.get("type", "message"),
        })
    return messages


async def list_channels(org_id: str = "default") -> list[dict[str, Any]]:
    """List public Slack channels."""
    token = await _get_token(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_SLACK_API}/conversations.list",
            headers={"Authorization": f"Bearer {token}"},
            params={"types": "public_channel,private_channel", "limit": 100},
        )
        resp.raise_for_status()
        data = resp.json()

    return [
        {"id": ch["id"], "name": ch["name"], "is_private": ch.get("is_private", False)}
        for ch in data.get("channels", [])
    ]


async def update_message(
    channel: str,
    ts: str,
    text: str,
    org_id: str = "default",
) -> dict[str, Any]:
    """Update an existing Slack message."""
    token = await _get_token(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_SLACK_API}/chat.update",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"channel": channel, "ts": ts, "text": text},
        )
        resp.raise_for_status()
        data = resp.json()

    return {"ts": data.get("ts", ""), "ok": data.get("ok", False)}
