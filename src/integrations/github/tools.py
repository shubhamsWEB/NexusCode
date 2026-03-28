"""MCP-compatible tool schemas for GitHub integration."""

GITHUB_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "github_create_pr",
        "description": (
            "Create a GitHub pull request. Use this after the coder has produced a code diff "
            "and the reviewer has approved. Returns the PR number and URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner (username or org)"},
                "repo": {"type": "string", "description": "Repository name"},
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description (supports markdown)"},
                "head": {"type": "string", "description": "Source branch name"},
                "base": {"type": "string", "default": "main", "description": "Target branch (default: main)"},
                "draft": {"type": "boolean", "default": False, "description": "Create as draft PR"},
            },
            "required": ["owner", "repo", "title", "body", "head"],
        },
    },
    {
        "name": "github_get_pr",
        "description": "Get details of a pull request by number. Returns state, title, body, merge status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pr_number": {"type": "integer", "description": "Pull request number"},
            },
            "required": ["owner", "repo", "pr_number"],
        },
    },
    {
        "name": "github_add_pr_comment",
        "description": "Add a comment to a pull request. Use for review notes, deployment status, or team updates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
                "body": {"type": "string", "description": "Comment text (supports markdown)"},
            },
            "required": ["owner", "repo", "pr_number", "body"],
        },
    },
    {
        "name": "github_get_pr_diff",
        "description": (
            "Get the unified diff of a pull request. Use this to review the actual code changes "
            "before writing review notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["owner", "repo", "pr_number"],
        },
    },
    {
        "name": "github_create_issue",
        "description": "Create a GitHub issue for tracking bugs, tasks, or feature requests.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string", "default": ""},
                "labels": {"type": "array", "items": {"type": "string"}},
                "assignees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["owner", "repo", "title"],
        },
    },
]
