"""MCP-compatible tool schemas for Jira integration. Used by agents directly."""

JIRA_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "jira_get_issue",
        "description": (
            "Fetch a Jira issue by key (e.g. 'PROJ-123'). Returns title, description, "
            "status, assignee, priority, labels, and a direct URL.\n"
            "Use this when you need to read the details of a specific Jira ticket."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key, e.g. 'PROJ-123'"},
            },
            "required": ["issue_key"],
        },
    },
    {
        "name": "jira_search_issues",
        "description": (
            "Search Jira issues using JQL (Jira Query Language). Returns a list of matching issues "
            "with key, summary, status, and assignee.\n"
            "Examples:\n"
            "  'project = PROJ AND status = \"In Progress\"'\n"
            "  'assignee = currentUser() AND priority = High'\n"
            "  'labels = backend AND created >= -7d'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jql": {"type": "string", "description": "JQL query string"},
                "max_results": {"type": "integer", "default": 20, "description": "Max issues to return (1-50)"},
            },
            "required": ["jql"],
        },
    },
    {
        "name": "jira_create_issue",
        "description": (
            "Create a new Jira issue. Use this after the PM has approved a feature spec "
            "or when a bug needs to be tracked. Returns the issue key and URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_key": {"type": "string", "description": "Jira project key, e.g. 'PROJ'"},
                "summary": {"type": "string", "description": "Issue title/summary"},
                "description": {"type": "string", "description": "Issue description (plain text)"},
                "issue_type": {"type": "string", "default": "Story", "description": "Story | Bug | Task | Epic"},
                "priority": {"type": "string", "description": "Highest | High | Medium | Low | Lowest"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "List of labels"},
            },
            "required": ["project_key", "summary"],
        },
    },
    {
        "name": "jira_update_issue",
        "description": (
            "Update an existing Jira issue — change summary, description, transition status, "
            "or add a comment. Use 'status' to move a ticket through the workflow "
            "(e.g. 'In Progress', 'Done', 'In Review')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key to update"},
                "summary": {"type": "string", "description": "New summary (optional)"},
                "description": {"type": "string", "description": "New description (optional)"},
                "status": {"type": "string", "description": "Target status name (optional)"},
                "comment": {"type": "string", "description": "Comment to add (optional)"},
            },
            "required": ["issue_key"],
        },
    },
]
