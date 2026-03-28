"""Notion integration client — read and write Notion pages and databases."""

from __future__ import annotations

from typing import Any

import httpx

from src.integrations.auth.credential_store import get_fresh_credential
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


async def _get_headers(org_id: str = "default") -> dict[str, str]:
    cred = await get_fresh_credential("notion", org_id)
    if not cred:
        from src.config import settings
        token = settings.notion_api_key
        if not token:
            raise RuntimeError("Notion credentials not configured. Set NOTION_API_KEY or complete OAuth.")
    else:
        token = cred["access_token"]

    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def get_page(page_id: str, org_id: str = "default") -> dict[str, Any]:
    """Get a Notion page by ID."""
    headers = await _get_headers(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        # Get page metadata
        resp = await client.get(f"{_NOTION_API}/pages/{page_id}", headers=headers)
        resp.raise_for_status()
        page = resp.json()

        # Get page content (blocks)
        blocks_resp = await client.get(f"{_NOTION_API}/blocks/{page_id}/children", headers=headers)
        blocks_resp.raise_for_status()
        blocks_data = blocks_resp.json()

    title = ""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            title_items = prop.get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_items)
            break

    content = _blocks_to_text(blocks_data.get("results", []))
    return {
        "id": page["id"],
        "title": title,
        "url": page.get("url", ""),
        "created_time": page.get("created_time", ""),
        "last_edited_time": page.get("last_edited_time", ""),
        "content": content,
    }


async def create_page(
    parent_id: str,
    title: str,
    content: str = "",
    parent_type: str = "page",
    org_id: str = "default",
) -> dict[str, Any]:
    """Create a new Notion page under a parent page or database."""
    headers = await _get_headers(org_id)

    parent: dict = {}
    if parent_type == "database":
        parent = {"database_id": parent_id}
    else:
        parent = {"page_id": parent_id}

    blocks = _text_to_blocks(content) if content else []

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_NOTION_API}/pages",
            headers=headers,
            json={
                "parent": parent,
                "properties": {
                    "title": {"title": [{"type": "text", "text": {"content": title}}]}
                },
                "children": blocks,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return {"id": data["id"], "url": data.get("url", ""), "created": True}


async def update_page(
    page_id: str,
    title: str | None = None,
    content: str | None = None,
    org_id: str = "default",
) -> dict[str, Any]:
    """Update a Notion page's title and/or append content blocks."""
    headers = await _get_headers(org_id)

    async with httpx.AsyncClient(timeout=15) as client:
        if title:
            resp = await client.patch(
                f"{_NOTION_API}/pages/{page_id}",
                headers=headers,
                json={"properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}}},
            )
            resp.raise_for_status()

        if content:
            blocks = _text_to_blocks(content)
            resp = await client.patch(
                f"{_NOTION_API}/blocks/{page_id}/children",
                headers=headers,
                json={"children": blocks},
            )
            resp.raise_for_status()

    return {"id": page_id, "updated": True}


async def search(query: str, org_id: str = "default") -> list[dict[str, Any]]:
    """Search Notion pages and databases."""
    headers = await _get_headers(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_NOTION_API}/search",
            headers=headers,
            json={"query": query, "page_size": 10},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for obj in data.get("results", []):
        title = ""
        if obj.get("object") == "page":
            for prop in obj.get("properties", {}).values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                    break
        results.append({
            "id": obj["id"],
            "type": obj.get("object", ""),
            "title": title,
            "url": obj.get("url", ""),
            "last_edited": obj.get("last_edited_time", ""),
        })
    return results


def _blocks_to_text(blocks: list) -> str:
    """Convert Notion block objects to plain text."""
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich_text = content.get("rich_text", [])
        text = "".join(rt.get("plain_text", "") for rt in rich_text)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _text_to_blocks(text: str) -> list:
    """Convert plain text to Notion paragraph blocks (split on newlines)."""
    blocks = []
    for line in text.split("\n"):
        if line.strip():
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": line}}]
                },
            })
    return blocks[:100]  # Notion API limit: 100 blocks per request
