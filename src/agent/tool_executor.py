import json

from src.retrieval.call_graph import (
    get_call_graph_for_file,
    get_call_graph_for_symbol,
)
from src.utils.logging import get_secure_logger
from src.utils.sanitize import sanitize_log

logger = get_secure_logger(__name__)


def _escape_ilike(v: str) -> str:
    """Escape ILIKE special characters (% and _) to prevent wildcard injection."""
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_repo_scope_filter(
    repo_owner: str | None,
    repo_name: str | None,
    allowed_repos: list[str] | None,
    *,
    prefix: str = "",
) -> tuple[str, dict]:
    """Return (sql_fragment, params) that constrains a query to the correct repo scope.

    Priority:
      1. repo_owner set  → pin to specific repo (+ opt. repo_name)
      2. allowed_repos   → filter to list of repos
      3. Neither         → no filter (cross-repo)
    """
    params: dict = {}
    clauses: list[str] = []

    if repo_owner:
        clauses.append(f"{prefix}repo_owner = :repo_owner")
        params["repo_owner"] = repo_owner
        if repo_name:
            clauses.append(f"{prefix}repo_name = :repo_name")
            params["repo_name"] = repo_name
    elif allowed_repos:
        clauses.append(f"{prefix}(repo_owner, repo_name) IN :allowed_repos")
        params["allowed_repos"] = [tuple(r.split("/", 1)) for r in allowed_repos]

    sql_fragment = " AND ".join(clauses) if clauses else ""
    return sql_fragment, params


def _filter_results_by_scope(
    results: list,
    repo_owner: str | None,
    repo_name: str | None,
    allowed_repos: list[str] | None,
) -> list:
    """Post-filter a list of SearchResult (or similar) objects by repo scope.
    Use when SQL-level filtering isn't practical (e.g. _keyword_search).
    Obeys the same priority as _build_repo_scope_filter.
    """
    if repo_owner:
        results = [
            r
            for r in results
            if r.get("repo_owner") == repo_owner
            and (not repo_name or r.get("repo_name") == repo_name)
        ]
    elif allowed_repos:
        allowed_set = set(allowed_repos)
        results = [
            r
            for r in results
            if f"{r.get('repo_owner')}/{r.get('repo_name')}" in allowed_set
        ]

    return results


def _normalize_input(raw: object, tool_name: str) -> dict:
    """Coerce whatever a model returns as tool input into a plain dict.

    Some providers (Ollama/GLM, older OpenAI-compat layers) return tool
    arguments as a JSON string instead of a dict. This normalizes both cases.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Tool %s received unparseable JSON string: %s",
                tool_name,
                sanitize_log(raw),
            )
            return {}
    logger.warning(
        "Tool %s received unexpected input type %s: %s",
        tool_name,
        type(raw).__name__,
        sanitize_log(raw),
    )
    return {}


def execute_tool(
    name: str,
    tool_input: object,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    extra_context: dict | None = None,
) -> str:
    """Execute a retrieval tool by name and return a JSON string result.

    Supported tools:
      - search_codebase
      - get_symbol
      - get_file_context
      - find_callers
      - get_call_graph

    Args:
        name: Tool name
        tool_input: Tool arguments (dict or JSON string)
        repo_owner: Repo owner to scope the search
        repo_name: Repo name to scope the search
        extra_context: Additional context (unused for now)

    Returns:
        JSON string with tool result or error
    """
    inp = _normalize_input(tool_input, name)

    if name == "get_call_graph":
        return _get_call_graph(inp, repo_owner, repo_name)

    return json.dumps({"error": f"Unknown tool: {name}"})


def _get_call_graph(
    inp: dict,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    """
    Get call graph for a deleted file or symbol.

    Supports two modes:
    1. file_path: Get all callers of symbols defined in this file
    2. symbol: Get all callers of a specific symbol

    Returns JSON with structure:
    {
      "type": "file" | "symbol",
      "target": "file_path" | "symbol_name",
      "total_callers": int,
      "hops": [
        {
          "hop": 1,
          "callers": [
            {
              "file": "path/to/file.py",
              "symbol_context": "function_name",
              "lines": "10-20",
              "calls": "target_symbol",
              "confidence": 0.95,
              "edge_type": "calls" | "semantic"
            }
          ]
        }
      ]
    }
    """
    file_path = inp.get("file_path") or inp.get("file")
    symbol = inp.get("symbol") or inp.get("name")
    depth = max(1, min(3, int(inp.get("depth", 2))))
    include_semantic = inp.get("include_semantic", True)

    if not file_path and not symbol:
        return json.dumps(
            {
                "error": "get_call_graph requires either 'file_path' or 'symbol' field.",
                "received_keys": list(inp.keys()),
            }
        )

    try:
        if file_path:
            result = get_call_graph_for_file(
                file_path=file_path,
                repo_owner=repo_owner,
                repo_name=repo_name,
                depth=depth,
                include_semantic=include_semantic,
            )
        else:
            result = get_call_graph_for_symbol(
                symbol=symbol,
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
                "target": file_path or symbol,
            }
        )
