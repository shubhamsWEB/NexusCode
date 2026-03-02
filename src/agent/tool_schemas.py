"""
Anthropic-format tool schemas for codebase retrieval.

These are given directly to Claude during inference so it can search
the vector DB dynamically, rather than receiving pre-fetched context.

Claude calls these tools iteratively — searching, following leads, tracing
call graphs — until it has enough real context to answer confidently.
"""

from __future__ import annotations

SEARCH_CODEBASE_SCHEMA: dict = {
    "name": "search_codebase",
    "description": (
        "Hybrid semantic + keyword search over the indexed codebase. "
        "Returns code chunks with file paths, symbol names, line numbers, and source previews. "
        "\n\nWHEN TO USE: any time you need to find relevant code — always start here. "
        "Prefer search_codebase for exploration; use get_symbol only when you know the exact name."
        "\nSTRATEGY: call multiple times with different angles: "
        "topic ('authentication flow'), identifier ('JWTMiddleware'), "
        "behaviour ('rate limiting'), error ('401 unauthorized')."
        "\nDO NOT use vague 1-word queries; specific phrases return far better results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Specific natural-language or identifier query "
                    "(e.g. 'JWT token validation', 'UserService.create', 'webhook HMAC verification'). "
                    "Be specific: 'JWT token validation' beats 'auth'."
                ),
            },
            "language": {
                "type": "string",
                "description": "Optional language filter (python, typescript, go, rust, java…)",
            },
            "top_k": {
                "type": "integer",
                "description": "Results to return (1–15). Default 8. Use 12–15 for broad exploration.",
                "default": 8,
            },
        },
        "required": ["query"],
    },
}

GET_SYMBOL_SCHEMA: dict = {
    "name": "get_symbol",
    "description": (
        "Look up a function, class, or method by name — like IDE 'Go to Definition'. "
        "Returns exact file location, line numbers, full signature, and docstring. "
        "Supports fuzzy matching: 'auth' finds 'authenticate', 'Authorization', 'AuthMiddleware'. "
        "Prefer search_codebase when exploring; use get_symbol when you know the exact name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Symbol name: exact ('authenticate'), "
                    "qualified ('UserService.authenticate'), "
                    "or partial ('auth' to find all auth-related symbols)."
                ),
            },
        },
        "required": ["name"],
    },
}

FIND_CALLERS_SCHEMA: dict = {
    "name": "find_callers",
    "description": (
        "Find all code that calls a given function or method. "
        "Use this to understand how a function is used across the codebase, "
        "or to assess the blast radius of a change before planning it. "
        "depth=1 returns direct callers; depth=2 returns callers of callers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Symbol name to find callers of (e.g. 'authenticate', 'PaymentService.charge').",
            },
            "depth": {
                "type": "integer",
                "description": "How many call hops deep (1–2). Default 1.",
                "default": 1,
            },
        },
        "required": ["symbol"],
    },
}

GET_FILE_CONTEXT_SCHEMA: dict = {
    "name": "get_file_context",
    "description": (
        "Get the structural map of a file: all symbols defined in it, its imports, "
        "and which other files import it. "
        "Use this after finding a file via search_codebase to understand its full structure. "
        "Partial paths are supported: 'app.py' will match 'src/api/app.py'. "
        "Do not pass full absolute paths; relative or partial paths are fine."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "File path relative to repo root (do not use absolute paths). "
                    "Partial paths are fine (e.g. 'webhook.py', 'src/api/app.py')."
                ),
            },
            "include_deps": {
                "type": "boolean",
                "description": "Include files that import this file (default true).",
                "default": True,
            },
        },
        "required": ["path"],
    },
}

# All four retrieval tool schemas — passed to Claude during inference
RETRIEVAL_TOOL_SCHEMAS: list[dict] = [
    SEARCH_CODEBASE_SCHEMA,
    GET_SYMBOL_SCHEMA,
    FIND_CALLERS_SCHEMA,
    GET_FILE_CONTEXT_SCHEMA,
]
