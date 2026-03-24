"""MCP-compatible tool schemas for Notion integration."""

NOTION_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "notion_get_page",
        "description": "Read a Notion page by ID. Returns title, content, and metadata. Use to read existing specs, docs, or meeting notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Notion page ID or URL"},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "notion_create_page",
        "description": (
            "Create a new Notion page. Use to document PRDs, ADRs, or meeting notes. "
            "parent_id is the ID of the parent page or database."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_id": {"type": "string", "description": "Parent page or database ID"},
                "title": {"type": "string", "description": "Page title"},
                "content": {"type": "string", "description": "Page content (plain text, newlines become blocks)"},
                "parent_type": {"type": "string", "default": "page", "description": "'page' or 'database'"},
            },
            "required": ["parent_id", "title"],
        },
    },
    {
        "name": "notion_update_page",
        "description": "Update a Notion page title and/or append content to it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "title": {"type": "string", "description": "New title (optional)"},
                "content": {"type": "string", "description": "Content to append (optional)"},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "notion_search",
        "description": "Search Notion pages and databases by title or content. Use to find existing documentation before creating new pages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
]
