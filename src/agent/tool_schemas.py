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
        "Hybrid semantic + keyword search. Returns code chunks with file paths, symbols, line numbers. "
        "Always start here. Call multiple times with different angles: "
        "topic ('authentication flow'), identifier ('JWTMiddleware'), error ('401 unauthorized'). "
        "Specific phrases beat vague 1-word queries."
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
                "description": "Results to return (1–15). Default 5. Use 10–15 for broad exploration.",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

GET_SYMBOL_SCHEMA: dict = {
    "name": "get_symbol",
    "description": (
        "Look up a symbol by name (like Go to Definition). Returns file, line numbers, signature, docstring. "
        "Fuzzy: 'auth' finds 'authenticate', 'AuthMiddleware'. "
        "Use when you know the name; prefer search_codebase for exploration."
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
        "Find all callers of a function/method. "
        "Use to understand usage or assess blast radius of a change. "
        "depth=1: direct callers; depth=2: callers of callers."
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
        "Get a file's structural map: symbols, imports, reverse dependencies. "
        "Use after search_codebase. Partial paths OK: 'app.py' matches 'src/api/app.py'."
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

# Trimmed schemas for Ask Mode — drops get_file_context to save ~300 tokens/turn
ASK_RETRIEVAL_TOOL_SCHEMAS: list[dict] = [
    SEARCH_CODEBASE_SCHEMA,
    GET_SYMBOL_SCHEMA,
    FIND_CALLERS_SCHEMA,
]
