import json
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text

from src.storage.db import AsyncSessionLocal
from src.retrieval.call_graph import get_call_graph_for_file, get_call_graph_for_symbol
from src.utils.logging import get_secure_logger
from src.utils.sanitize import sanitize_log

logger = get_secure_logger(__name__)


def _escape_ilike(value: str) -> str:
    """Escape ILIKE special characters to prevent wildcard injection."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _make_mcp_server(name: str, instructions: str) -> FastMCP:
    """Create and configure the MCP server."""
    return FastMCP(
        name=name,
        instructions=instructions,
        transport_security=TransportSecuritySettings(
            require_encryption=False,
            allow_localhost=True,
        ),
    )


mcp_server = _make_mcp_server(
    name="NexusCode",
    instructions=(
        "You are an expert code retrieval and analysis assistant. "
        "Use the available tools to search codebases, understand dependencies, "
        "and analyze call graphs for impact assessment."
    ),
)


@mcp_server.tool()
async def search_codebase(
    query: Annotated[
        str,
        "Natural language or identifier query. "
        "You may embed a repo name directly in the query text to target a specific repo — "
        "e.g. 'find the login handler in auth-service' or 'search myorg/frontend for useAuth'. "
        "The system detects the repo name automatically, so you do NOT need to set repo= as well.",
    ],
    repo: Annotated[
        str | None,
        "Explicitly scope to a single repo in 'owner/name' format (e.g. 'myorg/backend'). "
        "Use this when you are certain which repo to search. "
        "Omit to let the system auto-detect a repo from the query text, or to enable "
        "cross-repo routing across all accessible repos.",
    ] = None,
    current_repo: Annotated[
        str | None,
        "The repo the user is actively working in, as 'owner/name'. "
        "When set, this repo is always included first in cross-repo results regardless of its "
        "relevance score. Useful when the developer is editing code in repo A but also wants "
        "context from related repos B and C.",
    ] = None,
    language: Annotated[
        str | None,
        "Filter results to a single programming language (e.g. 'python', 'typescript', 'go'). "
        "Omit to search all languages.",
    ] = None,
    top_k: Annotated[
        int,
        "Maximum number of results to return (default 5, max 15). "
        "Use 10-15 for broad exploration; 3-5 for targeted lookup.",
    ] = 5,
) -> str:
    """
    Search the codebase using hybrid semantic + keyword search.

    Returns the most relevant code chunks ranked by relevance score.
    Each result includes file path, symbol name, code preview, and match score.

    WHEN TO USE:
    - Finding code by description: 'JWT token validation logic'
    - Locating specific functions: 'authenticate', 'UserService.create'
    - Understanding patterns: 'error handling middleware', 'webhook HMAC verification'

    WHEN NOT TO USE:
    - You want all callers of a function → use find_callers or get_call_graph
    - You want file structure → use get_file_context
    - You want a specific symbol definition → use get_symbol
    """
    return "search_codebase not implemented in this stub"


@mcp_server.tool()
async def get_symbol(
    name: Annotated[
        str,
        "Symbol name to look up. Can be: exact ('authenticate'), "
        "qualified ('UserService.authenticate'), partial ('auth' matches authenticate/AuthMiddleware), "
        "or abbreviated ('JWTMid' fuzzy-matches JWTMiddleware).",
    ],
) -> str:
    """
    Look up a specific function, class, method, or variable by name.

    Returns the symbol's exact file location, full signature, docstring,
    line range, and export status.

    WHEN TO USE:
    - You know (or suspect) the name of what you're looking for
    - You want the exact definition location and signature
    - After search_codebase returns a symbol name, use this to get full details

    WHEN NOT TO USE:
    - Exploring unknown code — use search_codebase instead
    - You only have a description — use search_codebase instead
    """
    return "get_symbol not implemented in this stub"


@mcp_server.tool()
async def get_file_context(
    path: Annotated[
        str,
        "File path relative to the repository root. Partial paths are fine and will be matched. "
        "Examples: 'src/api/app.py', 'webhook.py', 'app/shopify.server.ts'.",
    ],
) -> str:
    """
    Get the complete structural map of a source file.

    Returns all symbols it defines, all modules it imports, and which other files
    import it (reverse dependencies).

    WHEN TO USE:
    - After search_codebase returns a file — understand its full structure
    - Before editing a file — see all its exports and dependents
    - To check what a file imports (its dependencies)
    - To find all files that would be affected if this file's API changes

    WHEN NOT TO USE:
    - You want to search for code — use search_codebase
    - You want to find callers — use find_callers or get_call_graph
    """
    return "get_file_context not implemented in this stub"


@mcp_server.tool()
async def find_callers(
    symbol: Annotated[
        str,
        "Symbol name or qualified name (e.g. 'authenticate' or 'UserService.authenticate'). "
        "The system searches for all places that call this symbol.",
    ],
    repo: Annotated[
        str | None,
        "Optional scope to 'owner/name'. Omit to search across all accessible repos.",
    ] = None,
) -> str:
    """
    Find all direct callers of a symbol (one hop only).

    Returns a list of files and functions that call the given symbol,
    with line numbers and confidence scores.

    WHEN TO USE:
    - Quick check: 'Who calls this function?'
    - Before changing a function signature: 'What will break?'
    - One-hop impact analysis

    WHEN NOT TO USE:
    - You want multi-hop analysis → use get_call_graph with depth > 1
    - You want to understand what a function does → use search_codebase
    - You want file imports → use get_file_context
    """
    return "find_callers not implemented in this stub"


@mcp_server.tool()
async def get_call_graph(
    target: Annotated[
        str,
        "File path (e.g. 'src/auth/service.py') or symbol name (e.g. 'authenticate'). "
        "The system auto-detects whether this is a file or symbol.",
    ],
    repo: Annotated[
        str | None,
        "Scope to 'owner/name'. Omit to search across all accessible repos.",
    ] = None,
    depth: Annotated[
        int,
        "Call-graph traversal depth (1-3, default 2). "
        "depth=1: direct callers only. "
        "depth=2: callers of callers. "
        "depth=3: three hops for comprehensive impact analysis.",
    ] = 2,
    include_semantic: Annotated[
        bool,
        "Include semantic edges (type-based relationships) in addition to direct calls. "
        "Useful for finding indirect dependencies.",
    ] = True,
) -> str:
    """
    Get the complete call graph for a file or symbol.

    Use this to understand the blast radius of deleting a file or changing a function signature.
    Returns all callers at each hop level with file locations and confidence scores.

    WHEN TO USE:
    - Before deleting a file: 'What will break?'
    - Before changing a function signature: 'What calls this?'
    - Impact analysis: 'How many places depend on this?'

    WHEN NOT TO USE:
    - You want to understand what a function does → use search_codebase
    - You want file imports → use get_file_context
    """
    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    depth = max(1, min(3, depth))

    # Auto-detect: if target contains '/' or ends with file extension, treat as file
    is_file = "/" in target or any(
        target.endswith(ext) for ext in [".py", ".ts", ".js", ".go", ".rs", ".java"]
    )

    try:
        if is_file:
            result = await get_call_graph_for_file(
                file_path=target,
                repo_owner=repo_owner,
                repo_name=repo_name,
                depth=depth,
                include_semantic=include_semantic,
            )
        else:
            result = await get_call_graph_for_symbol(
                symbol=target,
                repo_owner=repo_owner,
                repo_name=repo_name,
                depth=depth,
                include_semantic=include_semantic,
            )
        return json.dumps(result, indent=2)
    except Exception as exc:
        logger.error(
            "get_call_graph failed: %s",
            sanitize_log(exc),
            exc_info=True,
        )
        return json.dumps(
            {
                "error": f"Call graph traversal failed: {str(exc)}",
                "target": target,
            }
        )
