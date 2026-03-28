"""
Integration tool registry — maps agent roles to their allowed integration tools.

This is how enterprise roles get access to integration tools in the AgentLoop.
The graph_engine.py calls get_tools_for_role() when building the tool list for
each agent step.
"""

from __future__ import annotations

from src.integrations.jira.tools import JIRA_TOOL_SCHEMAS
from src.integrations.slack.tools import SLACK_TOOL_SCHEMAS
from src.integrations.github.tools import GITHUB_TOOL_SCHEMAS
from src.integrations.figma.tools import FIGMA_TOOL_SCHEMAS
from src.integrations.notion.tools import NOTION_TOOL_SCHEMAS

# All integration tool schemas combined
ALL_INTEGRATION_TOOL_SCHEMAS: list[dict] = (
    JIRA_TOOL_SCHEMAS
    + SLACK_TOOL_SCHEMAS
    + GITHUB_TOOL_SCHEMAS
    + FIGMA_TOOL_SCHEMAS
    + NOTION_TOOL_SCHEMAS
)

# Role → allowed integration tool names
_ROLE_TOOLS: dict[str, list[str]] = {
    "pm_agent": [
        "jira_get_issue", "jira_search_issues", "jira_create_issue", "jira_update_issue",
        "notion_get_page", "notion_create_page", "notion_update_page", "notion_search",
        "slack_send_message",
    ],
    "designer_agent": [
        "figma_get_file", "figma_get_component", "figma_get_components", "figma_get_styles",
        "notion_get_page", "notion_create_page",
    ],
    "coder": [
        "github_create_pr", "github_get_pr", "github_add_pr_comment", "github_get_pr_diff",
        "jira_update_issue",
    ],
    "reviewer": [
        "github_get_pr", "github_get_pr_diff", "github_add_pr_comment",
        "jira_update_issue",
    ],
    "qa_agent": [
        "jira_create_issue", "jira_update_issue",
        "github_get_pr_diff",
    ],
    "devops_agent": [
        "github_create_pr", "github_get_pr", "github_add_pr_comment",
        "slack_send_message",
        "jira_update_issue",
    ],
    "supervisor": [
        "jira_update_issue", "jira_create_issue",
        "slack_send_message",
        "notion_create_page",
        "github_add_pr_comment",
    ],
    # Dev roles get GitHub tools by default
    "planner": [
        "github_get_pr_diff",
        "jira_get_issue",
        "notion_get_page",
    ],
    "tester": [
        "github_get_pr_diff",
        "jira_create_issue",
    ],
}


def get_tools_for_role(role: str) -> list[dict]:
    """Return integration tool schemas allowed for a given agent role."""
    allowed = set(_ROLE_TOOLS.get(role, []))
    if not allowed:
        return []
    schema_map = {s["name"]: s for s in ALL_INTEGRATION_TOOL_SCHEMAS}
    return [schema_map[name] for name in allowed if name in schema_map]


def get_all_tool_names() -> list[str]:
    """Return all integration tool names."""
    return [s["name"] for s in ALL_INTEGRATION_TOOL_SCHEMAS]
