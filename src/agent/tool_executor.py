"""
Tool executor — bridges Claude's tool_use calls to the DB-backed retrieval functions.

Called from AgentLoop on every tool_use block Claude emits.
Calls the same underlying functions that power the MCP tools, directly in-process
(no HTTP round-trip). The repo_owner/repo_name context is always injected from
the request scope so Claude doesn't need to specify it.
"""

from __future__ import annotations

import json

from src.utils.logging import get_secure_logger
from src.utils.sanitize import sanitize_log

logger = get_secure_logger(__name__)


def _escape_ilike(v: str) -> str:
    return v.replace("%", r"\%").replace("_", r"\_")


def _normalize_input(raw: object, tool_name: str) -> dict:
    """Coerce whatever a model returns as tool input into a plain dict.

    Some providers (Ollama/GLM, older OpenAI-compat layers) return tool
    arguments as a JSON *string* rather than a pre-parsed dict, or they
    return None / empty when parsing fails on their side.  This normalizer
    handles all observed variants so downstream code can assume a dict.
    """
    if raw is None:
        logger.warning("tool_executor: %s received None input", tool_name)
        return {}
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            logger.warning(
                "tool_executor: %s input parsed to non-dict type %s",
                tool_name, type(parsed).__name__,
            )
            return {}
        except json.JSONDecodeError:
            logger.warning("tool_executor: %s input is invalid JSON string: %.120r", tool_name, raw)
            return {}
    if isinstance(raw, dict):
        return raw
    logger.warning("tool_executor: %s unexpected input type %s", tool_name, type(raw).__name__)
    return {}


async def execute_tool(
    name: str,
    tool_input: object,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> str:
    """
    Execute a retrieval tool by name and return a JSON string result.

    repo_owner / repo_name are injected from the request context.
    Returns JSON string — never raises (errors are returned as JSON).
    """
    inp = _normalize_input(tool_input, name)

    if not inp:
        logger.warning(
            "tool_executor: %s called with empty/unparseable input (raw=%r) — "
            "this often means the model did not populate the tool arguments correctly. "
            "Required fields: search_codebase→query, get_symbol→name, "
            "find_callers→symbol, get_file_context→path.",
            name, tool_input,
        )

    try:
        if name == "search_codebase":
            return await _search_codebase(inp, repo_owner, repo_name)
        elif name == "get_symbol":
            return await _get_symbol(inp, repo_owner, repo_name)
        elif name == "find_callers":
            return await _find_callers(inp, repo_owner, repo_name)
        elif name == "get_file_context":
            return await _get_file_context(inp, repo_owner, repo_name)
        else:
            from src.agent.mcp_bridge import call_external_tool, is_external_tool

            if is_external_tool(name):
                return await call_external_tool(name, inp)
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as exc:
        logger.exception("tool_executor: %s failed", sanitize_log(name))
        return json.dumps({"error": f"Tool {name} failed: {exc}"})


# ── search_codebase ────────────────────────────────────────────────────────────


async def _search_codebase(
    inp: dict,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    from src.retrieval.assembler import assemble
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import embed_query, search

    query = inp.get("query") or inp.get("text") or inp.get("search") or ""
    if not query:
        return json.dumps({
            "error": "search_codebase requires a 'query' field. "
                     "Example: {\"query\": \"JWT token validation\"}",
            "received_keys": list(inp.keys()),
        })
    language = inp.get("language")
    top_k = max(1, min(15, int(inp.get("top_k", 8))))
    mode = inp.get("mode", "hybrid")

    query_vector: list[float] = []
    if mode in ("semantic", "hybrid"):
        query_vector = await embed_query(query)

    results = await search(
        query=query,
        query_vector=query_vector,
        top_k=top_k,
        mode=mode,
        repo_owner=repo_owner,
        repo_name=repo_name,
        language=language,
    )

    if not results:
        return json.dumps(
            {
                "query": query,
                "results": [],
                "context": "",
                "message": "No results found. Try a different query or check that the repo is indexed.",
            }
        )

    if mode in ("semantic", "hybrid"):
        results = rerank(query, results, top_n=top_k)

    # 6K token budget per tool call — keeps individual results focused
    ctx = assemble(results, token_budget=6000, query=query)

    return json.dumps(
        {
            "query": query,
            "results_count": len(results),
            "results": [
                {
                    "file": r.file_path,
                    "symbol": r.symbol_name,
                    "kind": r.symbol_kind,
                    "lines": f"{r.start_line}-{r.end_line}",
                    "language": r.language,
                    "score": round(r.rerank_score or r.score, 4),
                    "preview": r.raw_content[:400],
                }
                for r in results
            ],
            "context": ctx.context_text,
            "tokens_used": ctx.tokens_used,
        },
        indent=2,
    )


# ── get_symbol ────────────────────────────────────────────────────────────────


async def _get_symbol(
    inp: dict,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    name = inp.get("name") or inp.get("symbol") or inp.get("identifier") or ""
    if not name:
        return json.dumps({
            "error": "get_symbol requires a 'name' field. "
                     "Example: {\"name\": \"authenticate\"}",
            "received_keys": list(inp.keys()),
        })
    params: dict = {"name": name, "name_like": f"%{_escape_ilike(name)}%"}
    where_clauses = [
        "similarity(name, :name) > 0.1 OR name ILIKE :name_like OR qualified_name ILIKE :name_like"
    ]

    if repo_owner:
        where_clauses.append("repo_owner = :repo_owner")
        params["repo_owner"] = repo_owner
    if repo_name:
        where_clauses.append("repo_name = :repo_name")
        params["repo_name"] = repo_name

    where = " AND ".join(where_clauses)

    sql = text(f"""
        SELECT
            name, qualified_name, kind, file_path,
            repo_owner, repo_name, start_line, end_line,
            signature, docstring, is_exported,
            GREATEST(similarity(name, :name), similarity(qualified_name, :name)) AS sim_score
        FROM symbols
        WHERE {where}
        ORDER BY sim_score DESC, name
        LIMIT 10
    """)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    if not rows:
        return json.dumps(
            {"symbols": [], "message": f"No symbols matching '{name}' found in the index."}
        )

    return json.dumps(
        {
            "symbols": [
                {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file": r["file_path"],
                    "repo": f"{r['repo_owner']}/{r['repo_name']}",
                    "lines": f"{r['start_line']}-{r['end_line']}",
                    "signature": r["signature"],
                    "docstring": (r["docstring"] or "")[:200] if r["docstring"] else None,
                    "is_exported": r["is_exported"],
                    "match_score": round(float(r["sim_score"] or 0), 4),
                }
                for r in rows
            ],
            "count": len(rows),
        },
        indent=2,
    )


# ── find_callers ──────────────────────────────────────────────────────────────


async def _find_callers(
    inp: dict,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    from src.retrieval.searcher import _keyword_search

    symbol = inp.get("symbol") or inp.get("name") or inp.get("function") or ""
    if not symbol:
        return json.dumps({
            "error": "find_callers requires a 'symbol' field. "
                     "Example: {\"symbol\": \"authenticate\"}",
            "received_keys": list(inp.keys()),
        })
    depth = max(1, min(2, int(inp.get("depth", 1))))

    _DEFINITION_PREFIXES = (
        "def ",
        "async def ",
        "class ",
        "function ",
        "const ",
        "export ",
        "pub fn ",
        "fn ",
    )

    def _extract_call_sites(results, target_sym: str) -> list[dict]:
        callers = []
        for r in results:
            lines = r.raw_content.split("\n")
            call_lines = [
                {"line_no": r.start_line + i, "text": line.strip()}
                for i, line in enumerate(lines)
                if target_sym in line
                and not line.strip().startswith(_DEFINITION_PREFIXES)
            ]
            if call_lines:
                callers.append(
                    {
                        "file": r.file_path,
                        "symbol_context": r.symbol_name or "<module>",
                        "lines": f"{r.start_line}-{r.end_line}",
                        "call_sites": call_lines[:5],
                    }
                )
        return callers

    all_hops: list[dict] = []
    seen: set[str] = {symbol}
    frontier = [symbol]

    for hop_num in range(1, depth + 1):
        if not frontier:
            break

        hop_callers: list[dict] = []
        next_frontier: set[str] = set()

        for target in frontier:
            try:
                results = await _keyword_search(
                    query=target,
                    limit=20,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    language=None,
                )
            except Exception as exc:
                logger.warning(
                    "find_callers: keyword search failed for %r: %s",
                    sanitize_log(target),
                    sanitize_log(exc),
                )
                continue

            for entry in _extract_call_sites(results, target):
                entry["calls"] = target
                hop_callers.append(entry)
                caller_sym = entry["symbol_context"]
                if caller_sym and caller_sym != "<module>" and caller_sym not in seen:
                    next_frontier.add(caller_sym)
                    seen.add(caller_sym)

        if hop_callers:
            all_hops.append({"hop": hop_num, "callers": hop_callers})

        frontier = list(next_frontier)

    if not all_hops:
        return json.dumps(
            {
                "symbol": symbol,
                "hops": [],
                "total_callers": 0,
                "message": f"No call sites found for '{symbol}' in the indexed codebase.",
            }
        )

    return json.dumps(
        {
            "symbol": symbol,
            "total_callers": sum(len(h["callers"]) for h in all_hops),
            "hops": all_hops,
        },
        indent=2,
    )


# ── get_file_context ──────────────────────────────────────────────────────────


async def _get_file_context(
    inp: dict,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    path = inp.get("path") or inp.get("file") or inp.get("file_path") or ""
    if not path:
        return json.dumps({
            "error": "get_file_context requires a 'path' field. "
                     "Example: {\"path\": \"src/api/app.py\"}",
            "received_keys": list(inp.keys()),
        })
    include_deps = inp.get("include_deps", True)

    params: dict = {"path": path, "path_like": f"%{_escape_ilike(path)}%"}
    file_where = "file_path = :path OR file_path ILIKE :path_like"
    repo_filter = ""
    if repo_owner:
        repo_filter += " AND repo_owner = :repo_owner"
        params["repo_owner"] = repo_owner
    if repo_name:
        repo_filter += " AND repo_name = :repo_name"
        params["repo_name"] = repo_name

    async with AsyncSessionLocal() as session:
        sym_sql = text(f"""
            SELECT name, qualified_name, kind, start_line, end_line,
                   signature, docstring, is_exported, file_path
            FROM symbols
            WHERE ({file_where}){repo_filter}
            ORDER BY start_line
            LIMIT 60
        """)
        sym_rows = (await session.execute(sym_sql, params)).mappings().all()

        chunk_sql = text(f"""
            SELECT imports, commit_sha, language, token_count, file_path
            FROM chunks
            WHERE ({file_where}){repo_filter} AND is_deleted = FALSE
            ORDER BY start_line
            LIMIT 1
        """)
        chunk_row = (await session.execute(chunk_sql, params)).mappings().first()

        imported_by: list[str] = []
        if include_deps:
            path_no_ext = path.rsplit(".", 1)[0]
            dotted = path_no_ext.replace("/", ".")
            dep_params = {
                **params,
                "path_pattern": f"%{_escape_ilike(path_no_ext)}%",
                "dotted_pattern": f"%{_escape_ilike(dotted)}%",
            }
            dep_sql = text(f"""
                SELECT DISTINCT file_path, repo_owner, repo_name
                FROM chunks
                WHERE file_path NOT ILIKE :path_like
                  AND is_deleted = FALSE
                  AND EXISTS (
                      SELECT 1 FROM unnest(imports) AS imp
                      WHERE imp ILIKE :path_pattern OR imp ILIKE :dotted_pattern
                  )
                  {repo_filter}
                LIMIT 15
            """)
            dep_rows = (await session.execute(dep_sql, dep_params)).mappings().all()
            imported_by = [
                f"{r['repo_owner']}/{r['repo_name']}:{r['file_path']}" for r in dep_rows
            ]

    if not sym_rows and not chunk_row:
        return json.dumps(
            {
                "error": f"File '{path}' not found in index.",
                "hint": "Use search_codebase to find the correct path.",
            }
        )

    resolved_path = (
        sym_rows[0]["file_path"]
        if sym_rows
        else (chunk_row["file_path"] if chunk_row else path)
    )

    return json.dumps(
        {
            "file": resolved_path,
            "language": chunk_row["language"] if chunk_row else None,
            "imports": list(chunk_row["imports"] or []) if chunk_row and chunk_row["imports"] else [],
            "symbols": [
                {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "lines": f"{r['start_line']}-{r['end_line']}",
                    "signature": r["signature"],
                    "docstring": (r["docstring"] or "")[:150] if r["docstring"] else None,
                    "is_exported": r["is_exported"],
                }
                for r in sym_rows
            ],
            "imported_by": imported_by,
        },
        indent=2,
    )
