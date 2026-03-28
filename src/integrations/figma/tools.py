"""MCP-compatible tool schemas for Figma integration."""

FIGMA_TOOL_SCHEMAS: list[dict] = [
    {
        "name": "figma_get_file",
        "description": (
            "Get the structure of a Figma file — pages, frames, and components. "
            "Use this to understand the design system before specifying UI components. "
            "Accepts a Figma file URL or file key."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key_or_url": {"type": "string", "description": "Figma file URL or file key"},
                "depth": {"type": "integer", "default": 2, "description": "Tree depth to traverse (1-4)"},
            },
            "required": ["file_key_or_url"],
        },
    },
    {
        "name": "figma_get_component",
        "description": (
            "Get details of a specific Figma component by node ID. "
            "Use this when you need specs for a particular UI component."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key_or_url": {"type": "string"},
                "node_id": {"type": "string", "description": "Node ID from the Figma file"},
            },
            "required": ["file_key_or_url", "node_id"],
        },
    },
    {
        "name": "figma_get_components",
        "description": "List all components defined in a Figma file. Useful for understanding the design system's component library.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key_or_url": {"type": "string"},
            },
            "required": ["file_key_or_url"],
        },
    },
    {
        "name": "figma_get_styles",
        "description": "List all styles (colors, typography, effects) in a Figma file. Use this to identify design tokens before writing CSS.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_key_or_url": {"type": "string"},
            },
            "required": ["file_key_or_url"],
        },
    },
]
