"""
Tool executor — bridges Claude's tool_use calls to the DB-backed retrieval functions.

Called from AgentLoop on every tool_use block Claude emits.
Calls the same underlying functions that power the MCP tools, directly in-process
(no HTTP round-trip). The repo_owner/repo_name context is always injected from
the request scope so Claude doesn't need to specify it.

Repo Scoping Pattern
--------------------
All tool functions receive (repo_owner, repo_name, allowed_repos) and must
enforce repo scope via the two helpers at the top of this module:

  * SQL queries       → _build_repo_scope_filter(repo_owner, repo_name, allowed_repos)
  * In-memory lists   → _filter_results_by_scope(results, repo_owner, repo_name, allowed_repos)

Priority: pinned repo > allowed_repos key scope > unrestricted (all repos).
Any new tool added here MUST call these helpers — no manual if/elif repo_owner blocks.
"""

from __future__ import annotations

import json

from src.utils.logging import get_secure_logger
from src.utils.sanitize import sanitize_log

logger = get_secure_logger(__name__)


def _escape_ilike(v: str) -> str:
    return v.replace("%", r"\%").replace("_", r"\_")


# ── Repo scope helpers ────────────────────────────────────────────────────────
# All tool functions use these two helpers to enforce repo scoping uniformly.
# Adding a new tool? Call _build_repo_scope_filter() for SQL queries and
# _filter_results_by_scope() for in-memory SearchResult lists. That's it.


def _build_repo_scope_filter(
    repo_owner: str | None,
    repo_name: str | None,
    allowed_repos: list[str] | None,
    *,
    prefix: str = "",
) -> tuple[str, dict]:
    """
    Return (sql_fragment, params) that constrains a query to the correct repo scope.

    Priority:
      1. repo_owner set  → pin to specific repo (+ optional repo_name)
      2. allowed_repos   → restrict to the allowed set via SQL ANY()
      3. neither         → no restriction (all repos)

    sql_fragment starts with " AND " so it can be appended directly to a WHERE clause.
    prefix: optional table alias, e.g. "c." → "c.repo_owner".
    """
    p = prefix
    if repo_owner:
        sql = f" AND {p}repo_owner = :scope_repo_owner"
        params: dict = {"scope_repo_owner": repo_owner}
        if repo_name:
            sql += f" AND {p}repo_name = :scope_repo_name"
            params["scope_repo_name"] = repo_name
        return sql, params
    if allowed_repos:
        return (
            f" AND ({p}repo_owner || '/' || {p}repo_name) = ANY(:scope_allowed_repos)",
            {"scope_allowed_repos": allowed_repos},
        )
    return "", {}


def _filter_results_by_scope(
    results: list,
    repo_owner: str | None,
    repo_name: str | None,
    allowed_repos: list[str] | None,
) -> list:
    """
    Post-filter a list of SearchResult (or similar) objects by repo scope.
    Use when SQL-level filtering isn't practical (e.g. _keyword_search).
    Objects must have .repo_owner and .repo_name attributes.
    """
    if repo_owner:
        results = [r for r in results if r.repo_owner == repo_owner]
        if repo_name:
            results = [r for r in results if r.repo_name == repo_name]
        return results
    if allowed_repos:
        allowed_set = set(allowed_repos)
        return [r for r in results if f"{r.repo_owner}/{r.repo_name}" in allowed_set]
    return results


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
    extra_context: dict | None = None,
) -> str:
    """
    Execute a retrieval tool by name and return a JSON string result.

    repo_owner / repo_name are injected from the request context.
    extra_context carries run_id / step_id from the workflow executor.
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

    allowed_repos = (extra_context or {}).get("allowed_repos")

    try:
        if name == "search_codebase":
            return await _search_codebase(inp, repo_owner, repo_name, allowed_repos)
        elif name == "get_symbol":
            return await _get_symbol(inp, repo_owner, repo_name, allowed_repos)
        elif name == "find_callers":
            return await _find_callers(inp, repo_owner, repo_name, allowed_repos)
        elif name == "get_file_context":
            return await _get_file_context(inp, repo_owner, repo_name, allowed_repos)
        elif name == "get_agent_context":
            return await _get_agent_context(inp, repo_owner, repo_name, allowed_repos)
        elif name == "plan_implementation":
            return await _plan_implementation(inp, repo_owner, repo_name, allowed_repos)
        elif name == "ask_codebase":
            return await _ask_codebase(inp, repo_owner, repo_name, allowed_repos)
        elif name == "generate_pdf":
            return await _generate_pdf(inp, extra_context)
        elif name == "think":
            # Side-effect-free scratchpad: echo thought back so it appears in conversation history.
            # Claude uses this to reason about whether it has sufficient context before deciding
            # to search more or call the final answer tool (Anthropic "think" tool pattern).
            return json.dumps({"thought": inp.get("thought", ""), "status": "ok"})
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
    allowed_repos: list[str] | None = None,
) -> str:
    from src.config import settings as _settings
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

    # Cross-repo scoped search when no specific repo is pinned but a scope is active.
    # Always embed the query here — the RepoRouter needs the vector for centroid scoring
    # even when mode is "keyword".
    if repo_owner is None and allowed_repos and _settings.cross_repo_enabled:
        if not query_vector:
            query_vector = await embed_query(query)
        from src.retrieval.assembler import assemble_multi_repo
        from src.retrieval.searcher import search_cross_repo

        results_by_repo, budgets = await search_cross_repo(
            query=query,
            query_vector=query_vector,
            top_k=top_k,
            token_budget=6000,
            allowed_repos=allowed_repos,
            language=language,
        )
        if results_by_repo:
            ctx = assemble_multi_repo(results_by_repo, budgets, query=query)
            all_results = [r for rlist in results_by_repo.values() for r in rlist]
            return json.dumps(
                {
                    "query": query,
                    "results_count": len(all_results),
                    "repos_searched": [f"{o}/{n}" for o, n in results_by_repo],
                    "results": [
                        {
                            "file": r.file_path,
                            "repo": f"{r.repo_owner}/{r.repo_name}",
                            "symbol": r.symbol_name,
                            "kind": r.symbol_kind,
                            "lines": f"{r.start_line}-{r.end_line}",
                            "language": r.language,
                            "score": round(r.rerank_score or r.score, 4),
                            "preview": r.raw_content[:400],
                        }
                        for r in all_results
                    ],
                    "context": ctx.context_text,
                    "tokens_used": ctx.tokens_used,
                },
                indent=2,
            )

    results = await search(
        query=query,
        query_vector=query_vector,
        top_k=top_k,
        mode=mode,
        repo_owner=repo_owner,
        repo_name=repo_name,
        language=language,
        search_quality="thorough",
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
    allowed_repos: list[str] | None = None,
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

    scope_sql, scope_params = _build_repo_scope_filter(repo_owner, repo_name, allowed_repos)
    if scope_sql:
        where_clauses.append(scope_sql.removeprefix(" AND "))
        params.update(scope_params)

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
    allowed_repos: list[str] | None = None,
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
                results = _filter_results_by_scope(results, repo_owner, repo_name, allowed_repos)
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
    allowed_repos: list[str] | None = None,
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
    repo_filter, scope_params = _build_repo_scope_filter(repo_owner, repo_name, allowed_repos)
    params.update(scope_params)

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


# ── get_agent_context ──────────────────────────────────────────────────────────


async def _get_agent_context(
    inp: dict,
    repo_owner: str | None,
    repo_name: str | None,
    allowed_repos: list[str] | None = None,
) -> str:
    from src.retrieval.assembler import assemble
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import SearchResult, _semantic_search, embed_query
    from src.storage.db import AsyncSessionLocal
    from sqlalchemy import text

    task = inp.get("task") or inp.get("query") or inp.get("description") or ""
    if not task:
        return json.dumps({
            "error": "get_agent_context requires a 'task' field. "
                     "Example: {\"task\": \"Add rate limiting to the auth endpoint\"}",
            "received_keys": list(inp.keys()),
        })

    focal_files = inp.get("focal_files") or []
    token_budget = max(1000, min(32000, int(inp.get("token_budget", 8000))))

    all_results: list[SearchResult] = []
    seen_ids: set[str] = set()

    # 1. Chunks from focal files (highest priority)
    for fpath in focal_files[:5]:
        esc_path = fpath.replace("%", r"\%").replace("_", r"\_")
        params: dict = {"path": fpath, "path_like": f"%{esc_path}%"}
        repo_filter, scope_params = _build_repo_scope_filter(repo_owner, repo_name, allowed_repos)
        params.update(scope_params)

        focal_sql = text(f"""
            SELECT id, file_path, repo_owner, repo_name, language,
                   symbol_name, symbol_kind, scope_chain,
                   start_line, end_line, raw_content, enriched_content,
                   commit_sha, commit_author, token_count
            FROM chunks
            WHERE (file_path = :path OR file_path ILIKE :path_like)
              AND is_deleted = FALSE
              {repo_filter}
            ORDER BY start_line
            LIMIT 20
        """)
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(focal_sql, params)).mappings().all()

        for row in rows:
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                all_results.append(SearchResult(
                    chunk_id=row["id"],
                    file_path=row["file_path"],
                    repo_owner=row["repo_owner"],
                    repo_name=row["repo_name"],
                    language=row["language"],
                    symbol_name=row.get("symbol_name"),
                    symbol_kind=row.get("symbol_kind"),
                    scope_chain=row.get("scope_chain"),
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    raw_content=row["raw_content"],
                    enriched_content=row.get("enriched_content", ""),
                    commit_sha=row.get("commit_sha", ""),
                    commit_author=row.get("commit_author"),
                    token_count=row.get("token_count", 0),
                    score=1.0,
                    rerank_score=10.0,  # focal files get top score
                ))

    # 2. Semantic search for the task
    query_vector = await embed_query(task)
    semantic_results = await _semantic_search(
        vector=query_vector,
        limit=15,
        repo_owner=repo_owner,
        repo_name=repo_name,
        language=None,
    )
    semantic_results = _filter_results_by_scope(semantic_results, repo_owner, repo_name, allowed_repos)
    for r in semantic_results:
        if r.chunk_id not in seen_ids:
            seen_ids.add(r.chunk_id)
            all_results.append(r)

    if not all_results:
        return json.dumps({
            "task": task,
            "context_text": "",
            "tokens_used": 0,
            "message": "No relevant context found. Try a different task description or check that repos are indexed.",
        })

    # 3. Rerank (focal chunks keep top priority, others get reranked)
    focal_chunks = [r for r in all_results if r.rerank_score == 10.0]
    search_chunks = [r for r in all_results if r.rerank_score != 10.0]
    if search_chunks:
        search_chunks = rerank(task, search_chunks, top_n=10)
    final_results = focal_chunks + search_chunks

    # 4. Assemble within token budget
    ctx = assemble(final_results, token_budget=token_budget, query=task)

    return json.dumps({
        "task": task,
        "focal_files": focal_files,
        "context_text": ctx.context_text,
        "chunks_used": ctx.chunks_used,
        "tokens_used": ctx.tokens_used,
    }, indent=2)


# ── plan_implementation ────────────────────────────────────────────────────────


async def _plan_implementation(
    inp: dict,
    repo_owner: str | None,
    repo_name: str | None,
    allowed_repos: list[str] | None = None,
) -> str:
    from src.planning.claude_planner import generate_plan

    query = inp.get("query") or inp.get("task") or inp.get("description") or ""
    if not query or len(query) < 10:
        return json.dumps({
            "error": "plan_implementation requires a 'query' field (min 10 chars). "
                     "Describe the bug, feature, or refactoring task in detail.",
            "received_keys": list(inp.keys()),
        })

    web_research = bool(inp.get("web_research", True))
    model = inp.get("model") or None

    try:
        plan = await generate_plan(
            query=query,
            repo_owner=repo_owner,
            repo_name=repo_name,
            web_research=web_research,
            model=model,
            allowed_repos=allowed_repos,
        )
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("tool_executor: plan_implementation failed")
        return json.dumps({"error": f"Plan generation failed: {exc}"})

    # Return as JSON so the agent can work with structured data
    return json.dumps({
        "query": plan.query,
        "summary": plan.summary,
        "files": [
            {
                "path": f.path,
                "action": f.action,
                "reason": f.reason,
                "changes": [
                    {"kind": c.kind, "symbol": c.symbol, "description": c.description}
                    for c in f.changes
                ],
            }
            for f in (plan.files or [])
        ],
        "steps": [
            {
                "step": s.step_number,
                "title": s.title,
                "description": s.description,
                "files": s.files_involved,
                "depends_on": s.depends_on_steps,
                "verification": s.verification,
            }
            for s in (plan.steps or [])
        ],
        "risks": [
            {"severity": r.severity, "description": r.description, "mitigation": r.mitigation}
            for r in (plan.risks or [])
        ],
        "test_plan": plan.test_plan,
        "key_files": plan.key_files,
    }, indent=2)


# ── ask_codebase ───────────────────────────────────────────────────────────────


async def _ask_codebase(
    inp: dict,
    repo_owner: str | None,
    repo_name: str | None,
    allowed_repos: list[str] | None = None,
) -> str:
    from src.ask.ask_agent import generate_answer

    question = inp.get("question") or inp.get("query") or inp.get("text") or ""
    if not question or len(question) < 5:
        return json.dumps({
            "error": "ask_codebase requires a 'question' field (min 5 chars). "
                     "Example: {\"question\": \"How does the webhook pipeline work?\"}",
            "received_keys": list(inp.keys()),
        })

    model = inp.get("model") or None

    try:
        result = await generate_answer(
            query=question,
            repo_owner=repo_owner,
            repo_name=repo_name,
            model=model,
            allowed_repos=allowed_repos,
        )
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("tool_executor: ask_codebase failed")
        return json.dumps({"error": f"Answer generation failed: {exc}"})

    return json.dumps({
        "question": question,
        "answer": result.answer,
        "cited_files": result.cited_files,
        "follow_up_hints": result.follow_up_hints,
        "context_tokens": result.context_tokens,
        "elapsed_ms": round(result.elapsed_ms),
    }, indent=2)


# ── generate_pdf ───────────────────────────────────────────────────────────────


async def _generate_pdf(
    inp: dict,
    extra_context: dict | None,
) -> str:
    content = inp.get("content") or ""
    title = inp.get("title") or ""

    if not content:
        return json.dumps({
            "error": "generate_pdf requires a 'content' field with the markdown document text.",
            "received_keys": list(inp.keys()),
        })
    if not title:
        return json.dumps({
            "error": "generate_pdf requires a 'title' field.",
            "received_keys": list(inp.keys()),
        })

    metadata = inp.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    # Derive filename from explicit param or slugify the title
    from src.tools.pdf_generator import slugify
    raw_filename = inp.get("filename") or ""
    filename = (raw_filename.rstrip(".pdf") or slugify(title)) + ".pdf"
    filename_no_ext = filename[:-4]  # strip .pdf for storage; re-added on download

    run_id = (extra_context or {}).get("run_id") or None
    step_id = (extra_context or {}).get("step_id") or None

    try:
        from src.tools import pdf_generator

        pdf_bytes = await _run_in_executor(
            pdf_generator.generate_pdf_from_markdown, content, title, metadata
        )
        doc_id = await pdf_generator.store_document(
            pdf_bytes=pdf_bytes,
            title=title,
            filename=filename_no_ext,
            run_id=run_id,
            step_id=step_id,
            metadata=metadata,
        )
    except Exception as exc:
        logger.exception("tool_executor: generate_pdf failed")
        return json.dumps({"error": f"PDF generation failed: {exc}"})

    return json.dumps({
        "doc_id": doc_id,
        "download_url": f"/documents/{doc_id}/download",
        "filename": filename,
        "size_bytes": len(pdf_bytes),
    })


async def _run_in_executor(fn, *args):
    """Run a synchronous (CPU-bound) function in the default thread pool."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)
