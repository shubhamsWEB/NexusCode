"""
Jira integration client — async REST wrapper for the Atlassian Jira Cloud API.

Credentials are fetched transparently from the credential store.
The LLM only ever sees tool results (issue data), never raw tokens.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.integrations.auth.credential_store import get_fresh_credential
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)


async def _get_headers(org_id: str = "default") -> tuple[dict[str, str], str]:
    """Return (auth_headers, base_url) for Jira API calls."""
    cred = await get_fresh_credential("jira", org_id)
    if not cred:
        raise RuntimeError(
            "Jira credentials not configured. "
            "Set JIRA_API_TOKEN + JIRA_EMAIL + JIRA_BASE_URL or complete OAuth setup."
        )

    meta = cred.get("metadata", {})

    # OAuth token
    if cred["auth_type"] == "oauth_user" or meta.get("cloud_id"):
        cloud_id = meta.get("cloud_id", "")
        base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}"
        headers = {
            "Authorization": f"Bearer {cred['access_token']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    else:
        # API token (Basic Auth with email:token)
        import base64
        from src.config import settings
        email = meta.get("email") or settings.jira_email or ""
        token = cred["access_token"]
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
        base_url = (meta.get("base_url") or "").rstrip("/")
        headers = {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    return headers, base_url


async def get_issue(issue_key: str, org_id: str = "default") -> dict[str, Any]:
    """Fetch a Jira issue by key (e.g. 'PROJ-123')."""
    headers, base_url = await _get_headers(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{base_url}/rest/api/3/issue/{issue_key}",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    fields = data.get("fields", {})
    return {
        "key": data["key"],
        "id": data["id"],
        "summary": fields.get("summary", ""),
        "description": _extract_text(fields.get("description")),
        "status": fields.get("status", {}).get("name", ""),
        "assignee": (fields.get("assignee") or {}).get("displayName", "Unassigned"),
        "reporter": (fields.get("reporter") or {}).get("displayName", ""),
        "priority": (fields.get("priority") or {}).get("name", ""),
        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        "labels": fields.get("labels", []),
        "created": fields.get("created", ""),
        "updated": fields.get("updated", ""),
        "url": f"{base_url}/browse/{data['key']}",
    }


async def search_issues(
    jql: str,
    max_results: int = 20,
    org_id: str = "default",
) -> list[dict[str, Any]]:
    """Search Jira issues using JQL."""
    headers, base_url = await _get_headers(org_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{base_url}/rest/api/3/search",
            headers=headers,
            json={"jql": jql, "maxResults": max_results, "fields": ["summary", "status", "assignee", "priority", "issuetype"]},
        )
        resp.raise_for_status()
        data = resp.json()

    issues = []
    for item in data.get("issues", []):
        fields = item.get("fields", {})
        issues.append({
            "key": item["key"],
            "summary": fields.get("summary", ""),
            "status": fields.get("status", {}).get("name", ""),
            "assignee": (fields.get("assignee") or {}).get("displayName", "Unassigned"),
            "priority": (fields.get("priority") or {}).get("name", ""),
            "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        })
    return issues


async def create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Story",
    priority: str | None = None,
    labels: list[str] | None = None,
    assignee_account_id: str | None = None,
    org_id: str = "default",
) -> dict[str, Any]:
    """Create a new Jira issue."""
    headers, base_url = await _get_headers(org_id)

    fields: dict[str, Any] = {
        "project": {"key": project_key},
        "summary": summary,
        "issuetype": {"name": issue_type},
        "description": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
        },
    }
    if priority:
        fields["priority"] = {"name": priority}
    if labels:
        fields["labels"] = labels
    if assignee_account_id:
        fields["assignee"] = {"accountId": assignee_account_id}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{base_url}/rest/api/3/issue",
            headers=headers,
            json={"fields": fields},
        )
        resp.raise_for_status()
        data = resp.json()

    return {"key": data["key"], "id": data["id"], "url": f"{base_url}/browse/{data['key']}"}


async def update_issue(
    issue_key: str,
    summary: str | None = None,
    description: str | None = None,
    status: str | None = None,
    comment: str | None = None,
    org_id: str = "default",
) -> dict[str, Any]:
    """Update an existing Jira issue (summary, description, status, or add a comment)."""
    headers, base_url = await _get_headers(org_id)

    async with httpx.AsyncClient(timeout=15) as client:
        if summary or description:
            fields: dict[str, Any] = {}
            if summary:
                fields["summary"] = summary
            if description:
                fields["description"] = {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
                }
            resp = await client.put(
                f"{base_url}/rest/api/3/issue/{issue_key}",
                headers=headers,
                json={"fields": fields},
            )
            resp.raise_for_status()

        if status:
            # Find the transition ID for the desired status
            trans_resp = await client.get(
                f"{base_url}/rest/api/3/issue/{issue_key}/transitions",
                headers=headers,
            )
            trans_resp.raise_for_status()
            transitions = trans_resp.json().get("transitions", [])
            trans_id = next(
                (t["id"] for t in transitions if t["to"]["name"].lower() == status.lower()),
                None,
            )
            if trans_id:
                await client.post(
                    f"{base_url}/rest/api/3/issue/{issue_key}/transitions",
                    headers=headers,
                    json={"transition": {"id": trans_id}},
                )

        if comment:
            await client.post(
                f"{base_url}/rest/api/3/issue/{issue_key}/comment",
                headers=headers,
                json={
                    "body": {
                        "type": "doc", "version": 1,
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}],
                    }
                },
            )

    return {"key": issue_key, "status": "updated"}


def _extract_text(adf_node: Any) -> str:
    """Extract plain text from Atlassian Document Format (ADF) node."""
    if not adf_node:
        return ""
    if isinstance(adf_node, str):
        return adf_node
    text_parts = []
    if isinstance(adf_node, dict):
        if adf_node.get("type") == "text":
            return adf_node.get("text", "")
        for child in adf_node.get("content", []):
            text_parts.append(_extract_text(child))
    return " ".join(filter(None, text_parts))
