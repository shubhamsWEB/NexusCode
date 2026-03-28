"""GitHub integration client — PR lifecycle, issue management, repo operations."""

from __future__ import annotations

from typing import Any

import httpx

from src.integrations.auth.credential_store import get_fresh_credential
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_GH_API = "https://api.github.com"


async def _get_headers(org_id: str = "default") -> dict[str, str]:
    cred = await get_fresh_credential("github", org_id)
    if not cred:
        # Fall back to the NexusCode indexing token
        from src.config import settings
        token = settings.github_token
        if not token:
            raise RuntimeError("GitHub credentials not configured.")
    else:
        token = cred["access_token"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def create_pr(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str = "main",
    draft: bool = False,
    org_id: str = "default",
) -> dict[str, Any]:
    """Create a GitHub pull request."""
    headers = await _get_headers(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_GH_API}/repos/{owner}/{repo}/pulls",
            headers=headers,
            json={"title": title, "body": body, "head": head, "base": base, "draft": draft},
        )
        resp.raise_for_status()
        data = resp.json()

    return {
        "number": data["number"],
        "html_url": data["html_url"],
        "state": data["state"],
        "title": data["title"],
        "draft": data.get("draft", False),
    }


async def get_pr(
    owner: str,
    repo: str,
    pr_number: int,
    org_id: str = "default",
) -> dict[str, Any]:
    """Get a pull request by number."""
    headers = await _get_headers(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    return {
        "number": data["number"],
        "html_url": data["html_url"],
        "state": data["state"],
        "title": data["title"],
        "body": data.get("body", ""),
        "merged": data.get("merged", False),
        "draft": data.get("draft", False),
        "reviews_count": data.get("review_comments", 0),
    }


async def add_pr_comment(
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    org_id: str = "default",
) -> dict[str, Any]:
    """Add a review comment or issue comment to a PR."""
    headers = await _get_headers(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_GH_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            headers=headers,
            json={"body": body},
        )
        resp.raise_for_status()
        data = resp.json()

    return {"id": data["id"], "html_url": data["html_url"]}


async def request_reviewers(
    owner: str,
    repo: str,
    pr_number: int,
    reviewers: list[str],
    org_id: str = "default",
) -> dict[str, Any]:
    """Request reviewers on a pull request."""
    headers = await _get_headers(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
            headers=headers,
            json={"reviewers": reviewers},
        )
        resp.raise_for_status()

    return {"requested": reviewers}


async def get_pr_diff(
    owner: str,
    repo: str,
    pr_number: int,
    org_id: str = "default",
) -> str:
    """Get the unified diff of a pull request."""
    headers = await _get_headers(org_id)
    diff_headers = {**headers, "Accept": "application/vnd.github.v3.diff"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=diff_headers,
        )
        resp.raise_for_status()

    return resp.text


async def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    org_id: str = "default",
) -> dict[str, Any]:
    """Create a GitHub issue."""
    headers = await _get_headers(org_id)
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_GH_API}/repos/{owner}/{repo}/issues",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    return {"number": data["number"], "html_url": data["html_url"], "state": data["state"]}
