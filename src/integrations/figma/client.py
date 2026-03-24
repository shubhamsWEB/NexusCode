"""Figma integration client — read design files, components, and styles."""

from __future__ import annotations

from typing import Any

import httpx

from src.integrations.auth.credential_store import get_fresh_credential
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_FIGMA_API = "https://api.figma.com/v1"


async def _get_headers(org_id: str = "default") -> dict[str, str]:
    cred = await get_fresh_credential("figma", org_id)
    if not cred:
        from src.config import settings
        token = settings.figma_access_token
        if not token:
            raise RuntimeError("Figma credentials not configured. Set FIGMA_ACCESS_TOKEN or complete OAuth.")
    else:
        token = cred["access_token"]

    # Figma uses X-Figma-Token for PAT, Authorization Bearer for OAuth
    cred_type = cred["auth_type"] if cred else "service_account"
    if cred_type == "oauth_user":
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return {"X-Figma-Token": token, "Content-Type": "application/json"}


def _extract_file_key(file_key_or_url: str) -> str:
    """Extract file key from a Figma URL or return as-is if already a key."""
    if "figma.com" in file_key_or_url:
        parts = file_key_or_url.rstrip("/").split("/")
        try:
            file_idx = parts.index("file")
            return parts[file_idx + 1]
        except (ValueError, IndexError):
            pass
    return file_key_or_url


async def get_file(
    file_key_or_url: str,
    depth: int = 2,
    org_id: str = "default",
) -> dict[str, Any]:
    """
    Get a Figma file's structure (pages, frames, components).
    depth controls how deep to traverse the node tree (default 2 = pages + top-level frames).
    """
    headers = await _get_headers(org_id)
    file_key = _extract_file_key(file_key_or_url)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_FIGMA_API}/files/{file_key}",
            headers=headers,
            params={"depth": depth},
        )
        resp.raise_for_status()
        data = resp.json()

    # Return a summarised view rather than the full JSON tree
    document = data.get("document", {})
    pages = []
    for page in document.get("children", []):
        frames = [
            {"id": child["id"], "name": child["name"], "type": child["type"]}
            for child in page.get("children", [])
            if child.get("type") in ("FRAME", "COMPONENT", "COMPONENT_SET")
        ]
        pages.append({"id": page["id"], "name": page["name"], "frames": frames})

    return {
        "file_key": file_key,
        "name": data.get("name", ""),
        "last_modified": data.get("lastModified", ""),
        "version": data.get("version", ""),
        "pages": pages,
        "components_count": len(data.get("components", {})),
        "styles_count": len(data.get("styles", {})),
    }


async def get_node(
    file_key_or_url: str,
    node_id: str,
    org_id: str = "default",
) -> dict[str, Any]:
    """Get a specific node (component, frame, etc.) by its ID."""
    headers = await _get_headers(org_id)
    file_key = _extract_file_key(file_key_or_url)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_FIGMA_API}/files/{file_key}/nodes",
            headers=headers,
            params={"ids": node_id},
        )
        resp.raise_for_status()
        data = resp.json()

    nodes = data.get("nodes", {})
    node = nodes.get(node_id, {}).get("document", {})
    return {
        "id": node.get("id", node_id),
        "name": node.get("name", ""),
        "type": node.get("type", ""),
        "description": node.get("description", ""),
        "children_count": len(node.get("children", [])),
    }


async def get_components(
    file_key_or_url: str,
    org_id: str = "default",
) -> list[dict[str, Any]]:
    """List all components in a Figma file."""
    headers = await _get_headers(org_id)
    file_key = _extract_file_key(file_key_or_url)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_FIGMA_API}/files/{file_key}/components",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    return [
        {
            "key": comp.get("key", ""),
            "name": comp.get("name", ""),
            "description": comp.get("description", ""),
            "node_id": comp.get("node_id", ""),
        }
        for comp in data.get("meta", {}).get("components", [])
    ]


async def get_styles(
    file_key_or_url: str,
    org_id: str = "default",
) -> list[dict[str, Any]]:
    """List all styles (colors, typography, effects) in a Figma file."""
    headers = await _get_headers(org_id)
    file_key = _extract_file_key(file_key_or_url)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_FIGMA_API}/files/{file_key}/styles",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    return [
        {
            "key": s.get("key", ""),
            "name": s.get("name", ""),
            "style_type": s.get("style_type", ""),
            "description": s.get("description", ""),
        }
        for s in data.get("meta", {}).get("styles", [])
    ]
