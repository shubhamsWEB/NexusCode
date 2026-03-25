"""
Tool schemas for the agent.

Defines the JSON schema for all available tools that the agent can use.
"""

TOOL_SCHEMAS = {
    "get_call_graph": {
        "type": "object",
        "description": (
            "Get the complete call graph for a file or symbol to understand impact of deletion or changes. "
            "Traverses the knowledge graph using BFS to find all callers at each hop level. "
            "Supports semantic edges for indirect dependencies.\n\n"
            "HOW IT WORKS:\n"
            "  1. Takes a file path or symbol name as input.\n"
            "  2. Queries kg_edges table for CALLS and optionally SEMANTIC edges.\n"
            "  3. Performs multi-hop BFS traversal up to specified depth.\n"
            "  4. Returns all callers grouped by hop with confidence scores.\n\n"
            "USE CASES:\n"
            "  • Before deleting a file: 'What will break?'\n"
            "  • Before changing a signature: 'What calls this?'\n"
            "  • Impact analysis: 'How many places depend on this?'\n"
        ),
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "File path (e.g. 'src/auth/service.py') or symbol name (e.g. 'authenticate'). "
                    "Auto-detected based on presence of '/' or file extension."
                ),
            },
            "repo": {
                "type": "string",
                "description": (
                    "Optional scope to 'owner/name' (e.g. 'myorg/backend'). "
                    "Omit to search across all accessible repos."
                ),
            },
            "depth": {
                "type": "integer",
                "description": (
                    "Call-graph traversal depth (1-3, default 2). "
                    "depth=1: direct callers only. "
                    "depth=2: callers of callers. "
                    "depth=3: comprehensive impact analysis (may be slow on large codebases)."
                ),
            },
            "include_semantic": {
                "type": "boolean",
                "description": (
                    "Include semantic edges (type-based relationships) in addition to direct calls. "
                    "Default true. Set false for faster results with only direct call edges."
                ),
            },
        },
        "required": ["target"],
    },
}
