"""
Integration dispatcher — routes tool calls and integration step actions to the
correct client method.

Two dispatch paths:
  1. dispatch_tool(tool_name, params) — called by tool_executor.py when an agent
     calls a tool like "jira_get_issue" or "slack_send_message".
  2. dispatch_integration(operation, params) — called by graph_engine.py for
     StepType.integration steps in YAML (e.g. integration: "jira.create_issue").

Both paths are transparent to the LLM — it never sees credentials.
"""

from __future__ import annotations

from typing import Any

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)


# ── Tool dispatch (agent tool calls) ──────────────────────────────────────────


async def dispatch_tool(tool_name: str, params: dict[str, Any]) -> Any:
    """
    Route an agent tool call to the correct integration client method.
    Called by tool_executor.py when tool_name matches an integration tool.

    Implements a single 401-retry: if the external API returns 401 (token
    expired between the proactive refresh check and the actual HTTP call),
    we force-refresh the credential and retry once. This handles the rare
    race condition that get_fresh_credential() cannot prevent.

    Returns the result dict (or raises on error).
    """
    import httpx

    org_id = params.pop("org_id", "default")
    params_copy = dict(params)  # preserve for retry

    try:
        return await _route_tool(tool_name, params_copy, org_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
        service = tool_name.split("_")[0]
        logger.warning(
            "dispatcher: 401 for %s/%s — forcing token refresh and retrying",
            service, org_id,
        )
        try:
            from src.integrations.auth.oauth_manager import refresh_token as _do_refresh
            refreshed = await _do_refresh(service=service, org_id=org_id)
            if refreshed:
                return await _route_tool(tool_name, dict(params_copy), org_id)
        except Exception as refresh_exc:
            logger.error("dispatcher: retry refresh failed for %s: %s", service, refresh_exc)
        raise  # re-raise original 401 if refresh also failed


try:
    from langsmith import traceable as _traceable
    _integration_traceable = _traceable(name="integration_tool", run_type="tool")
except ImportError:
    def _integration_traceable(fn):  # type: ignore[misc]
        return fn


@_integration_traceable
async def _route_tool(tool_name: str, params: dict[str, Any], org_id: str) -> Any:
    """Inner routing — separated so dispatch_tool can retry with fresh tokens."""

    # ── Jira ──────────────────────────────────────────────────────────────
    if tool_name == "jira_get_issue":
        from src.integrations.jira.client import get_issue
        return await get_issue(params["issue_key"], org_id=org_id)

    if tool_name == "jira_search_issues":
        from src.integrations.jira.client import search_issues
        return await search_issues(params["jql"], params.get("max_results", 20), org_id=org_id)

    if tool_name == "jira_create_issue":
        from src.integrations.jira.client import create_issue
        return await create_issue(
            project_key=params["project_key"],
            summary=params["summary"],
            description=params.get("description", ""),
            issue_type=params.get("issue_type", "Story"),
            priority=params.get("priority"),
            labels=params.get("labels"),
            org_id=org_id,
        )

    if tool_name == "jira_update_issue":
        from src.integrations.jira.client import update_issue
        return await update_issue(
            issue_key=params["issue_key"],
            summary=params.get("summary"),
            description=params.get("description"),
            status=params.get("status"),
            comment=params.get("comment"),
            org_id=org_id,
        )

    # ── Slack ─────────────────────────────────────────────────────────────
    if tool_name == "slack_send_message":
        from src.integrations.slack.client import send_message
        return await send_message(
            channel=params["channel"],
            text=params["text"],
            thread_ts=params.get("thread_ts"),
            org_id=org_id,
        )

    if tool_name == "slack_get_channel_history":
        from src.integrations.slack.client import get_channel_history
        return await get_channel_history(
            channel=params["channel"],
            limit=params.get("limit", 20),
            org_id=org_id,
        )

    if tool_name == "slack_list_channels":
        from src.integrations.slack.client import list_channels
        return await list_channels(org_id=org_id)

    # ── GitHub ────────────────────────────────────────────────────────────
    if tool_name == "github_create_pr":
        from src.integrations.github.client import create_pr
        return await create_pr(
            owner=params["owner"],
            repo=params["repo"],
            title=params["title"],
            body=params["body"],
            head=params["head"],
            base=params.get("base", "main"),
            draft=params.get("draft", False),
            org_id=org_id,
        )

    if tool_name == "github_get_pr":
        from src.integrations.github.client import get_pr
        return await get_pr(params["owner"], params["repo"], params["pr_number"], org_id=org_id)

    if tool_name == "github_add_pr_comment":
        from src.integrations.github.client import add_pr_comment
        return await add_pr_comment(params["owner"], params["repo"], params["pr_number"], params["body"], org_id=org_id)

    if tool_name == "github_get_pr_diff":
        from src.integrations.github.client import get_pr_diff
        diff = await get_pr_diff(params["owner"], params["repo"], params["pr_number"], org_id=org_id)
        return {"diff": diff[:10000]}  # truncate for context window safety

    if tool_name == "github_create_issue":
        from src.integrations.github.client import create_issue
        return await create_issue(
            owner=params["owner"],
            repo=params["repo"],
            title=params["title"],
            body=params.get("body", ""),
            labels=params.get("labels"),
            assignees=params.get("assignees"),
            org_id=org_id,
        )

    # ── Figma ─────────────────────────────────────────────────────────────
    if tool_name == "figma_get_file":
        from src.integrations.figma.client import get_file
        return await get_file(params["file_key_or_url"], depth=params.get("depth", 2), org_id=org_id)

    if tool_name == "figma_get_component":
        from src.integrations.figma.client import get_node
        return await get_node(params["file_key_or_url"], params["node_id"], org_id=org_id)

    if tool_name == "figma_get_components":
        from src.integrations.figma.client import get_components
        return await get_components(params["file_key_or_url"], org_id=org_id)

    if tool_name == "figma_get_styles":
        from src.integrations.figma.client import get_styles
        return await get_styles(params["file_key_or_url"], org_id=org_id)

    # ── Notion ────────────────────────────────────────────────────────────
    if tool_name == "notion_get_page":
        from src.integrations.notion.client import get_page
        return await get_page(params["page_id"], org_id=org_id)

    if tool_name == "notion_create_page":
        from src.integrations.notion.client import create_page
        return await create_page(
            parent_id=params["parent_id"],
            title=params["title"],
            content=params.get("content", ""),
            parent_type=params.get("parent_type", "page"),
            org_id=org_id,
        )

    if tool_name == "notion_update_page":
        from src.integrations.notion.client import update_page
        return await update_page(
            page_id=params["page_id"],
            title=params.get("title"),
            content=params.get("content"),
            org_id=org_id,
        )

    if tool_name == "notion_search":
        from src.integrations.notion.client import search
        return await search(params["query"], org_id=org_id)

    raise ValueError(f"Unknown integration tool: {tool_name!r}")


def is_integration_tool(tool_name: str) -> bool:
    """Return True if this tool name belongs to the integration layer."""
    return tool_name.startswith((
        "jira_", "slack_", "github_", "figma_", "notion_",
    ))


# ── Integration step dispatch (YAML StepType.integration) ────────────────────


async def dispatch_integration(operation: str, params: dict[str, Any]) -> Any:
    """
    Route a YAML integration step (e.g. 'jira.create_issue') to the correct client.
    operation format: 'service.method_name'
    """
    if "." not in operation:
        raise ValueError(f"Invalid integration operation {operation!r}. Expected 'service.method'")

    service, method = operation.split(".", 1)
    tool_name = f"{service}_{method}"

    if is_integration_tool(tool_name):
        return await dispatch_tool(tool_name, params)

    raise ValueError(f"Unknown integration operation: {operation!r}")
