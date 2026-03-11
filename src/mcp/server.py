"""
MCP server — exposes 8 codebase intelligence tools via FastMCP.

Tools:
  1. search_codebase       — hybrid semantic+keyword search + rerank
  2. get_symbol            — fuzzy symbol lookup (like "Go to Definition")
  3. find_callers          — who calls this function/method?
  4. get_file_context      — structural map of a file (symbols + imports + imported_by)
  5. get_agent_context     — pre-assembled token-budget-aware context for a task
  6. plan_implementation   — web research + codebase context → structured implementation plan
  7. ask_codebase          — answer a natural-language question about the codebase
  8. get_semantic_context  — LLM-extracted architectural relationships between symbols

Mount the Starlette SSE app via:
    app.mount("/mcp", mcp_server.sse_app())
"""

from __future__ import annotations

import json
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from sqlalchemy import text

from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger
from src.utils.sanitize import sanitize_log

logger = get_secure_logger(__name__)


def _escape_ilike(value: str) -> str:
    """Escape ILIKE special characters to prevent wildcard injection."""
    return value.replace("%", r"\%").replace("_", r"\_")


mcp_server = FastMCP(
    name="codebase-intelligence",
    instructions=(
        "A live, always-fresh index of one or more GitHub repositories. "
        "All tools are scope-aware: if an API key is provided it restricts which repos are "
        "accessible; tools silently enforce this — never try to access repos outside the scope.\n\n"
        "TOOL SELECTION GUIDE:\n"
        "• search_codebase   — first choice for any 'find / where is / how does X work' question.\n"
        "• get_symbol        — when you know the exact function or class name and want its definition.\n"
        "• find_callers      — when you need to know what calls a function (blast-radius analysis).\n"
        "• get_file_context  — when you need the complete structure of a specific file.\n"
        "• get_agent_context — call this FIRST before starting any implementation task; it assembles "
        "all relevant context in one shot.\n"
        "• plan_implementation — for generating a full grounded implementation plan.\n"
        "• ask_codebase         — for conversational questions that need a mentor-style explanation.\n"
        "• get_semantic_context — after finding key symbols, call this to understand their "
        "architectural role (validates, delegates_to, coordinates, etc.).\n"
        "• list_skills          — to discover available skills and workflows.\n\n"
        "REPO TARGETING (in priority order):\n"
        "1. repo='owner/name' — always use this when you know the target repo. Most reliable.\n"
        "2. Include 'owner/name' in the query text (e.g. 'search myorg/auth-service for login') "
        "— unambiguous, auto-detected against the known repos list.\n"
        "3. Repo name as a word in the query (e.g. 'find login in auth-service') "
        "— auto-detected by comparing against the actual list of accessible repos "
        "(from the API key scope if present, otherwise from the Redis-cached index). "
        "Fires only when exactly one repo name matches — no guessing, no heuristics.\n"
        "4. Omit both — cross-repo routing searches all accessible repos automatically.\n\n"
        "Use current_repo='owner/name' to tell the system which repo the user is actively working in."
    ),
    warn_on_duplicate_tools=False,
)


# ── Tool 1: search_codebase ───────────────────────────────────────────────────


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
        "Restrict results to a specific language: 'python', 'typescript', 'javascript', 'go', etc. "
        "Omit to search across all languages.",
    ] = None,
    top_k: Annotated[
        int,
        "Max results to return per repo (1-20). Default 5. "
        "Use 10-15 for planning or deep research tasks.",
    ] = 5,
    mode: Annotated[
        str,
        "Search mode: "
        "'hybrid' (default) — semantic + keyword merged via RRF, best for most queries; "
        "'semantic' — vector similarity only, good for conceptual / description queries; "
        "'keyword' — exact identifiers, error strings, file-path substrings.",
    ] = "hybrid",
    cross_repo: Annotated[
        bool,
        "Enable cross-repo routing when no specific repo is identified (default true). "
        "Set to false to restrict to the current scope without routing. "
        "Ignored when repo= is set or a repo name is detected in the query.",
    ] = True,
) -> str:
    """
    Search the indexed codebase and return ranked code chunks with file locations,
    symbol names, line ranges, and source previews.

    ROUTING DECISION (automatic, in priority order):
    1. repo='owner/name' is set          → search that single repo directly. Most reliable.
    2. 'owner/name' in the query text    → unambiguous; search that repo directly.
       Example: 'how does myorg/auth-service handle JWT?'
    3. Repo name as a word in the query  → validated against the actual list of accessible
       repos (from API key scope if present, otherwise from Redis-cached index).
       Fires only when exactly one repo name matches the word — no heuristics.
       Example: 'find the token refresh logic in auth-service'
       If 'authservice' appears in the query but no repo is named 'authservice',
       nothing matches and the router decides instead — no false positives.
    4. Neither                           → cross-repo routing: score all accessible repos
       by semantic similarity + keyword Jaccard, search the top-N in parallel, assemble
       results with clear ╔REPO╗ section headers.

    WHEN TO USE:
    - 'Where is authentication handled?'
    - 'Find the Stripe webhook handler'
    - 'Show me how the RQ worker is started'
    - 'Search myorg/auth-service for the token refresh logic'
    - Any question whose answer lives in code

    WHEN NOT TO USE:
    - You need the full file structure → use get_file_context
    - You know the exact function name → use get_symbol (faster)
    - You are about to start a task → use get_agent_context first
    """
    from src.config import settings as _settings
    from src.retrieval.assembler import assemble, assemble_multi_repo
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import embed_query, search, search_cross_repo

    top_k = max(1, min(20, top_k))

    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    query_vector: list[float] = []
    if mode in ("semantic", "hybrid"):
        query_vector = await embed_query(query)

    # ── Cross-repo path ───────────────────────────────────────────────────────
    if repo is None and cross_repo and _settings.cross_repo_enabled:
        current: tuple[str, str] | None = None
        if current_repo and "/" in current_repo:
            parts = current_repo.split("/", 1)
            current = (parts[0], parts[1])

        results_by_repo, budgets = await search_cross_repo(
            query,
            query_vector,
            top_k=top_k,
            token_budget=_settings.context_token_budget,
            allowed_repos=None,  # unrestricted from MCP (no auth context available here)
            current_repo=current,
            language=language,
            search_quality="balanced",
        )

        if not results_by_repo:
            return json.dumps({"results": [], "context": "", "tokens_used": 0})

        ctx = assemble_multi_repo(results_by_repo, budgets, query=query)
        all_results = [r for rlist in results_by_repo.values() for r in rlist]
        output = {
            "query": query,
            "mode": mode,
            "repos_searched": [f"{o}/{n}" for o, n in results_by_repo],
            "results": [
                {
                    "file": r.file_path,
                    "repo": f"{r.repo_owner}/{r.repo_name}",
                    "symbol": r.symbol_name,
                    "kind": r.symbol_kind,
                    "scope": r.scope_chain,
                    "lines": f"{r.start_line}-{r.end_line}",
                    "language": r.language,
                    "score": round(r.rerank_score or r.score, 4),
                    "commit": r.commit_sha[:7] if r.commit_sha else "",
                    "preview": r.raw_content[:400],
                }
                for r in all_results
            ],
            "context": ctx.context_text,
            "tokens_used": ctx.tokens_used,
            "quality_score": round(ctx.quality_score, 4),
            "retrieval_log": ctx.retrieval_log,
        }
        return json.dumps(output, indent=2)

    # ── Single-repo path (unchanged) ─────────────────────────────────────────
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
        return json.dumps({"results": [], "context": "", "tokens_used": 0})

    if mode in ("semantic", "hybrid"):
        results = rerank(query, results, top_n=top_k)

    ctx = assemble(results, token_budget=8000, query=query)

    output = {
        "query": query,
        "mode": mode,
        "results": [
            {
                "file": r.file_path,
                "repo": f"{r.repo_owner}/{r.repo_name}",
                "symbol": r.symbol_name,
                "kind": r.symbol_kind,
                "scope": r.scope_chain,
                "lines": f"{r.start_line}-{r.end_line}",
                "language": r.language,
                "score": round(r.rerank_score or r.score, 4),
                "commit": r.commit_sha[:7] if r.commit_sha else "",
                "preview": r.raw_content[:400],
            }
            for r in results
        ],
        "context": ctx.context_text,
        "tokens_used": ctx.tokens_used,
        "retrieval_log": ctx.retrieval_log,
    }
    return json.dumps(output, indent=2)


# ── Tool 2: get_symbol ────────────────────────────────────────────────────────


@mcp_server.tool()
async def get_symbol(
    name: Annotated[
        str,
        "Symbol name to look up — exact ('authenticate'), qualified ('UserService.authenticate'), "
        "or a partial name ('auth'). Fuzzy matching finds close variants automatically.",
    ],
    repo: Annotated[
        str | None,
        "Scope to 'owner/name'. Omit to search all accessible repos. "
        "Respects API key scope automatically.",
    ] = None,
) -> str:
    """
    Look up a function, class, or method by name — like IDE 'Go to Definition'.
    Returns the symbol's exact file location, full signature, and docstring.
    Supports fuzzy matching: 'auth' will match 'authenticate', 'Authorization', etc.

    WHEN TO USE:
    - You know the function or class name and need its exact location and signature.
    - You want to see the docstring before deciding whether to read the full file.
    - Use search_codebase instead if you only have a description ('where is JWT validated?').
    """
    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    params: dict = {"name": name}
    where_clauses = [
        "similarity(name, :name) > 0.1 OR name ILIKE :name_like OR qualified_name ILIKE :name_like"
    ]
    params["name_like"] = f"%{_escape_ilike(name)}%"

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
            repo_owner, repo_name,
            start_line, end_line,
            signature, docstring, is_exported,
            GREATEST(
                similarity(name, :name),
                similarity(qualified_name, :name)
            ) AS sim_score
        FROM symbols
        WHERE {where}
        ORDER BY sim_score DESC, name
        LIMIT 10
    """)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    if not rows:
        return json.dumps({"symbols": [], "message": f"No symbols matching '{name}' found."})

    symbols = [
        {
            "name": r["name"],
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "file": r["file_path"],
            "repo": f"{r['repo_owner']}/{r['repo_name']}",
            "lines": f"{r['start_line']}-{r['end_line']}",
            "signature": r["signature"],
            "docstring": r["docstring"],
            "is_exported": r["is_exported"],
            "match_score": round(float(r["sim_score"] or 0), 4),
        }
        for r in rows
    ]
    return json.dumps({"symbols": symbols, "count": len(symbols)}, indent=2)


# ── Tool 3: find_callers ──────────────────────────────────────────────────────


@mcp_server.tool()
async def find_callers(
    symbol: Annotated[
        str,
        "Exact or qualified symbol name to trace (e.g. 'authenticate', 'PaymentService.charge'). "
        "Must be a real symbol name — not a description.",
    ],
    repo: Annotated[
        str | None,
        "Scope to 'owner/name'. Omit to trace callers across all accessible repos. "
        "Respects API key scope automatically.",
    ] = None,
    depth: Annotated[
        int,
        "Call-graph traversal depth (1-3). "
        "depth=1: who calls this symbol directly? "
        "depth=2: who calls the code that calls this symbol? "
        "depth=3: three hops — use for blast-radius analysis before a signature change.",
    ] = 1,
) -> str:
    """
    Find all code that calls a given function or method, with optional multi-hop traversal.
    Uses BFS: callers found in hop N become search targets for hop N+1.
    Returns file locations and call-site code snippets for each hop.

    WHEN TO USE:
    - Before changing a function signature: find everything that will break.
    - Tracing how a value propagates through the call graph.
    - 'What calls authenticate()?' or 'What triggers the payment charge?'

    WHEN NOT TO USE:
    - You want to understand what a function does → use search_codebase or get_symbol.
    - You want cross-file import relationships → use get_file_context.
    """
    from src.retrieval.searcher import _keyword_search

    depth = max(1, min(3, depth))

    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    _DEFINITION_PREFIXES = (
        "def ",
        "async def ",
        "class ",
        "function ",
        "const ",
        "export ",
        "export default ",
        "export async ",
        "pub fn ",
        "fn ",
    )

    def _extract_call_sites(results, target_sym: str) -> list[dict]:
        """Extract non-definition lines that reference `target_sym`."""
        callers = []
        for r in results:
            lines = r.raw_content.split("\n")
            call_lines = [
                {"line_no": r.start_line + i, "text": line.strip()}
                for i, line in enumerate(lines)
                if target_sym in line and not line.strip().startswith(_DEFINITION_PREFIXES)
            ]
            if call_lines:
                callers.append(
                    {
                        "file": r.file_path,
                        "repo": f"{r.repo_owner}/{r.repo_name}",
                        "symbol_context": r.symbol_name or "<module>",
                        "kind": r.symbol_kind,
                        "lines": f"{r.start_line}-{r.end_line}",
                        "call_sites": call_lines[:5],
                    }
                )
        return callers

    # ── BFS multi-hop traversal ───────────────────────────────────────────────
    all_hops: list[dict] = []
    seen_symbols: set[str] = {symbol}  # avoid re-querying the same symbol
    frontier: list[str] = [symbol]  # symbols to find callers for in this hop

    for hop_num in range(1, depth + 1):
        if not frontier:
            break

        hop_callers: list[dict] = []
        next_frontier: set[str] = set()

        for target_sym in frontier:
            try:
                results = await _keyword_search(
                    query=target_sym,
                    limit=20,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    language=None,
                )
            except Exception as exc:
                logger.warning(
                    "find_callers: keyword search failed for %r: %s",
                    sanitize_log(target_sym),
                    sanitize_log(exc),
                )
                continue

            call_entries = _extract_call_sites(results, target_sym)
            for entry in call_entries:
                entry["calls"] = target_sym  # tag which symbol triggered this hop
                hop_callers.append(entry)
                # Promote the caller's own symbol into the next hop's frontier
                caller_sym = entry["symbol_context"]
                if caller_sym and caller_sym != "<module>" and caller_sym not in seen_symbols:
                    next_frontier.add(caller_sym)
                    seen_symbols.add(caller_sym)

        if hop_callers:
            all_hops.append(
                {
                    "hop": hop_num,
                    "targets_searched": list(frontier),
                    "callers": hop_callers,
                }
            )

        frontier = list(next_frontier)

    total_callers = sum(len(h["callers"]) for h in all_hops)

    if not all_hops:
        return json.dumps(
            {
                "symbol": symbol,
                "depth": depth,
                "hops": [],
                "total_callers": 0,
                "message": (
                    f"No call sites found for '{symbol}'. "
                    "It may not be used in the indexed codebase, or it may only appear "
                    "in definition contexts."
                ),
            }
        )

    return json.dumps(
        {
            "symbol": symbol,
            "depth": depth,
            "hops_traversed": len(all_hops),
            "total_callers": total_callers,
            "hops": all_hops,
        },
        indent=2,
    )


# ── Tool 4: get_file_context ──────────────────────────────────────────────────


@mcp_server.tool()
async def get_file_context(
    path: Annotated[
        str,
        "File path relative to repo root, exact or partial "
        "(e.g. 'src/auth/service.py' or just 'service.py'). Partial paths use ILIKE matching.",
    ],
    repo: Annotated[
        str | None,
        "Scope to 'owner/name'. Required when the same filename exists in multiple repos. "
        "Respects API key scope automatically.",
    ] = None,
    include_deps: Annotated[
        bool,
        "Also return the list of files that import this file (reverse-dependency graph). "
        "Default true. Set false to speed up the call when you only need the symbol list.",
    ] = True,
) -> str:
    """
    Return the complete structural map of a file: every symbol defined in it
    (functions, classes, methods with signatures and docstrings), its imports,
    and which other files import it.

    WHEN TO USE:
    - You have a file path and want to understand its full contents without reading raw code.
    - Before modifying a file: check what it exports and what depends on it.
    - 'What functions are in src/auth/service.py?'
    - 'What imports webhook.py?'

    WHEN NOT TO USE:
    - You don't know the file path → use search_codebase to find it first.
    - You only need one symbol → use get_symbol (faster).
    """
    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    # Build WHERE for file_path match (support partial paths)
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
        # 1. Symbols in this file
        sym_sql = text(f"""
            SELECT name, qualified_name, kind, start_line, end_line,
                   signature, docstring, is_exported, file_path, repo_owner, repo_name
            FROM symbols
            WHERE ({file_where}){repo_filter}
            ORDER BY start_line
            LIMIT 100
        """)
        sym_rows = (await session.execute(sym_sql, params)).mappings().all()

        # 2. Chunk metadata — imports + commit info
        chunk_sql = text(f"""
            SELECT imports, commit_sha, commit_author, language, token_count, file_path
            FROM chunks
            WHERE ({file_where}){repo_filter}
              AND is_deleted = FALSE
            ORDER BY start_line
            LIMIT 1
        """)
        chunk_row = (await session.execute(chunk_sql, params)).mappings().first()

        # 3. Files that import this file (imported_by) — use imports ARRAY for accuracy
        if include_deps:
            path_no_ext = path.rsplit(".", 1)[0]
            dotted_path = path_no_ext.replace("/", ".")
            dep_params = {
                **params,
                "path_pattern": f"%{path_no_ext}%",
                "dotted_pattern": f"%{dotted_path}%",
            }
            dep_sql = text(f"""
                SELECT DISTINCT file_path, repo_owner, repo_name
                FROM chunks
                WHERE file_path NOT ILIKE :path_like
                  AND is_deleted = FALSE
                  AND EXISTS (
                      SELECT 1 FROM unnest(imports) AS imp
                      WHERE imp ILIKE :path_pattern
                         OR imp ILIKE :dotted_pattern
                  )
                  {repo_filter}
                LIMIT 20
            """)
            dep_rows = (await session.execute(dep_sql, dep_params)).mappings().all()
            imported_by = [f"{r['repo_owner']}/{r['repo_name']}:{r['file_path']}" for r in dep_rows]
        else:
            imported_by = []

    if not sym_rows and not chunk_row:
        return json.dumps(
            {
                "error": f"File '{path}' not found in index.",
                "hint": "Use search_codebase to find the correct path.",
            }
        )

    resolved_path = (
        sym_rows[0]["file_path"] if sym_rows else (chunk_row["file_path"] if chunk_row else path)
    )

    result = {
        "file": resolved_path,
        "language": chunk_row["language"] if chunk_row else None,
        "last_commit": chunk_row["commit_sha"][:7]
        if chunk_row and chunk_row["commit_sha"]
        else None,
        "commit_author": chunk_row["commit_author"] if chunk_row else None,
        "chunk_count": 0,  # filled below
        "imports": list(chunk_row["imports"] or []) if chunk_row and chunk_row["imports"] else [],
        "symbols": [
            {
                "name": r["name"],
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "lines": f"{r['start_line']}-{r['end_line']}",
                "signature": r["signature"],
                "docstring": (r["docstring"] or "")[:200] if r["docstring"] else None,
                "is_exported": r["is_exported"],
            }
            for r in sym_rows
        ],
        "imported_by": imported_by,
    }

    # Count total chunks for this file
    async with AsyncSessionLocal() as session:
        cnt_sql = text(f"""
            SELECT COUNT(*) FROM chunks
            WHERE ({file_where}){repo_filter} AND is_deleted = FALSE
        """)
        result["chunk_count"] = (await session.execute(cnt_sql, params)).scalar() or 0

    return json.dumps(result, indent=2)


# ── Tool 5: get_agent_context ─────────────────────────────────────────────────


@mcp_server.tool()
async def get_agent_context(
    task: Annotated[
        str,
        "Natural language description of the task you are about to perform. "
        "Be specific: 'Add rate limiting to POST /search' is better than 'rate limiting'. "
        "You may mention a repo name in the task text to target a specific repo automatically.",
    ],
    focal_files: Annotated[
        list[str] | None,
        "File paths you are actively editing or reading (up to 5). "
        "Their full content is included first before semantic search results. "
        "Example: ['src/api/search.py', 'src/retrieval/searcher.py']",
    ] = None,
    token_budget: Annotated[
        int,
        "Max tokens to return in the assembled context (default 8000, max 32000). "
        "Increase to 16000-32000 for complex multi-file tasks.",
    ] = 8000,
    repo: Annotated[
        str | None,
        "Scope to 'owner/name'. Omit to enable cross-repo routing — the system searches "
        "all accessible repos and assembles context from the most relevant ones. "
        "Respects API key scope automatically.",
    ] = None,
    current_repo: Annotated[
        str | None,
        "The repo the user is actively working in ('owner/name'). "
        "Always included first in cross-repo results. "
        "Set this whenever you know the user's working repo.",
    ] = None,
) -> str:
    """
    Assemble a complete, token-budget-aware context package for a coding task.
    Call this BEFORE starting any non-trivial implementation or analysis.

    What it does in one call:
    1. Includes the full content of focal_files (files you are editing) — highest priority.
    2. Runs semantic search for the task description across all relevant repos.
    3. Deduplicates and reranks all chunks.
    4. Truncates to token_budget and returns a ready-to-use context block.

    Cross-repo behaviour (same routing rules as search_codebase):
    - repo= set → single repo only.
    - Repo name in task text → auto-detected, single repo.
    - Neither → scores all accessible repos and assembles from the top-N.

    WHEN TO USE:
    - Always call this at the start of 'implement X', 'fix bug in Y', 'refactor Z' tasks.
    - It replaces 3-5 individual search_codebase calls with one optimised assembly.

    WHEN NOT TO USE:
    - Quick lookups ('where is function X?') → use get_symbol or search_codebase instead.
    """
    from sqlalchemy import text

    from src.config import settings as _settings
    from src.retrieval.assembler import assemble
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import (
        SearchResult,
        _semantic_search,
        embed_query,
        search_cross_repo,
    )
    from src.storage.db import AsyncSessionLocal

    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    token_budget = max(1000, min(32000, token_budget))
    all_results: list[SearchResult] = []
    seen_ids: set[str] = set()

    # 1. Chunks from focal files (highest priority — always include)
    if focal_files:
        for fpath in focal_files[:5]:  # cap at 5 focal files
            params: dict = {"path": fpath, "path_like": f"%{_escape_ilike(fpath)}%"}
            if repo_owner:
                params["repo_owner"] = repo_owner
            if repo_name:
                params["repo_name"] = repo_name

            repo_filter = ""
            if repo_owner:
                repo_filter += " AND repo_owner = :repo_owner"
            if repo_name:
                repo_filter += " AND repo_name = :repo_name"

            focal_sql = text(f"""
                SELECT
                    id, file_path, repo_owner, repo_name, language,
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
                    all_results.append(
                        SearchResult(
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
                            score=1.0,  # focal files get top score
                            rerank_score=10.0,
                        )
                    )

    # 2. Semantic search for the task description
    query_vector = await embed_query(task)

    if repo is None and _settings.cross_repo_enabled:
        # Cross-repo semantic search
        current: tuple[str, str] | None = None
        if current_repo and "/" in current_repo:
            parts = current_repo.split("/", 1)
            current = (parts[0], parts[1])
        cross_results_by_repo, _ = await search_cross_repo(
            task, query_vector,
            top_k=15,
            token_budget=token_budget,
            current_repo=current,
            search_quality="balanced",
        )
        for rlist in cross_results_by_repo.values():
            for r in rlist:
                if r.chunk_id not in seen_ids:
                    seen_ids.add(r.chunk_id)
                    all_results.append(r)
    else:
        semantic_results = await _semantic_search(
            vector=query_vector,
            limit=15,
            repo_owner=repo_owner,
            repo_name=repo_name,
            language=None,
        )
        for r in semantic_results:
            if r.chunk_id not in seen_ids:
                seen_ids.add(r.chunk_id)
                all_results.append(r)

    if not all_results:
        return json.dumps(
            {
                "task": task,
                "context": "",
                "tokens_used": 0,
                "message": "No relevant context found. Try a different task description or check that repos are indexed.",
            }
        )

    # 3. Rerank everything (except focal file chunks, which stay at the top)
    focal_chunks = [r for r in all_results if r.rerank_score == 10.0]
    search_chunks = [r for r in all_results if r.rerank_score != 10.0]

    if search_chunks:
        search_chunks = rerank(task, search_chunks, top_n=10)

    final_results = focal_chunks + search_chunks

    # 4. Assemble within token budget
    ctx = assemble(final_results, token_budget=token_budget, query=task)

    return json.dumps(
        {
            "task": task,
            "focal_files": focal_files or [],
            "context_text": ctx.context_text,
            "chunks_used": ctx.chunks_used,
            "tokens_used": ctx.tokens_used,
            "retrieval_log": ctx.retrieval_log,
        },
        indent=2,
    )


# ── Tool 6: plan_implementation ───────────────────────────────────────────────


@mcp_server.tool()
async def plan_implementation(
    query: Annotated[
        str,
        "Bug report, feature request, or refactoring task description (min 10 chars). "
        "Be specific and include relevant context. "
        "You may mention a repo name in the query to target a specific repo "
        "(e.g. 'add rate limiting to POST /search in api-gateway').",
    ],
    repo: Annotated[
        str | None,
        "Scope to 'owner/name'. Omit to let cross-repo routing find the relevant repos "
        "automatically. Respects API key scope.",
    ] = None,
    web_research: Annotated[
        bool,
        "Search the web for current best practices, libraries, and patterns before planning. "
        "Default true. Set false for speed or offline environments.",
    ] = True,
    model: Annotated[
        str | None,
        "LLM model to use (e.g. 'claude-opus-4-6', 'gpt-4o'). "
        "Defaults to server config. Use a more capable model for complex architectural tasks.",
    ] = None,
) -> str:
    """
    Generate a complete, codebase-grounded implementation plan for a coding task.

    Combines two sources:
    1. Web research — best library, pattern, and current best practices for the task.
    2. Codebase retrieval — exact file paths, symbol names, and callers from the index.

    Returns a structured plan with:
    - Problem statement and clarifying assumptions
    - Current architecture analysis
    - Proposed solutions with trade-offs
    - Step-by-step implementation tasks (exact files, symbols, pseudocode)
    - Risk assessment and mitigation
    - Concrete test plan

    WHEN TO USE:
    - Before starting any non-trivial feature, bug fix, or refactor.
    - When you need to understand the full blast radius of a change.
    - 'Plan adding Redis caching to the embedding step'
    - 'How should I implement rate limiting on POST /search?'

    WHEN NOT TO USE:
    - Simple one-line fixes → just do it.
    - Pure code questions without implementation intent → use ask_codebase.
    """
    from src.planning.claude_planner import generate_plan

    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    try:
        plan = await generate_plan(
            query=query,
            repo_owner=repo_owner,
            repo_name=repo_name,
            web_research=web_research,
            model=model,
        )
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("plan_implementation MCP tool failed")
        return json.dumps({"error": f"Plan generation failed: {exc}"})

    # Format as markdown for readability in Claude Desktop
    return _format_plan_markdown(plan)


# ── Tool 7: ask_codebase ──────────────────────────────────────────────────────


@mcp_server.tool()
async def ask_codebase(
    question: Annotated[
        str,
        "Natural-language question about the codebase (min 5 chars). "
        "You may mention a repo name to target a specific repo "
        "(e.g. 'How does auth-service handle token refresh?').",
    ],
    repo: Annotated[
        str | None,
        "Scope to 'owner/name'. Omit to let cross-repo routing find the relevant repos. "
        "Respects API key scope.",
    ] = None,
    model: Annotated[
        str | None,
        "LLM model to use (e.g. 'claude-opus-4-6', 'gpt-4o'). Defaults to server config.",
    ] = None,
) -> str:
    """
    Answer a natural-language question about the codebase in a mentor tone.

    Unlike plan_implementation (which returns file changes and steps), this tool
    answers conversationally — explaining how code works, tracing data flows,
    clarifying architecture decisions, and pointing to real file locations.

    Returns a markdown answer with:
    - Inline file citations (e.g. `src/pipeline/pipeline.py` lines 42-80)
    - Fenced code snippets for key examples
    - 2-3 concrete follow-up questions grounded in the codebase

    WHEN TO USE:
    - 'How does the webhook processing pipeline work?'
    - 'Where is authentication handled?'
    - 'What does the reranker do and when is it called?'
    - 'Explain the chunking algorithm'
    - Any question whose answer needs explanation, not just a code location.

    WHEN NOT TO USE:
    - You need code locations only → use search_codebase (faster, lower cost).
    - You need a full implementation plan → use plan_implementation.
    """
    from src.ask.ask_agent import generate_answer

    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    try:
        result = await generate_answer(
            query=question,
            repo_owner=repo_owner,
            repo_name=repo_name,
            model=model,
        )
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("ask_codebase MCP tool failed")
        return json.dumps({"error": f"Answer generation failed: {exc}"})

    lines = [result.answer]

    if result.cited_files:
        lines.append("\n**Referenced files:**")
        for f in result.cited_files:
            lines.append(f"- `{f}`")

    if result.follow_up_hints:
        lines.append("\n**Follow-up questions you might ask:**")
        for hint in result.follow_up_hints:
            lines.append(f"- {hint}")

    lines.append(
        f"\n---\n_{result.context_tokens} tokens · {result.elapsed_ms:.0f}ms_"
    )

    return "\n".join(lines)


def _format_plan_markdown(plan) -> str:
    """Format an ImplementationPlan as a clean markdown string for MCP consumers."""

    # ── Analysis response type (improvement / review / audit) ────────────────
    if plan.response_type == "analysis":
        lines = [f"**Query:** {plan.query}", ""]
        lines.append(plan.analysis or "_No analysis generated._")
        if plan.key_files:
            lines.append("")
            lines.append("**Analyzed files:** " + " · ".join(f"`{f}`" for f in plan.key_files))
        if plan.metadata:
            m = plan.metadata
            cq = f" · complexity: {m.query_complexity}" if m.query_complexity else ""
            lines.append(
                f"\n---\n_ID: `{plan.plan_id}` · {m.context_tokens} tokens · "
                f"{m.context_files} chunks · {m.elapsed_ms:.0f}ms{cq}_"
            )
            if m.grounding_warnings:
                lines.append("\n> **⚠ Retrieval warnings:**")
                for w in m.grounding_warnings:
                    lines.append(f"> - {w}")
        return "\n".join(lines)

    # ── Answer response type ──────────────────────────────────────────────────
    if plan.response_type == "answer":
        lines = [f"**Query:** {plan.query}", ""]
        lines.append(plan.answer or "_No answer generated._")
        if plan.key_files:
            lines.append("")
            lines.append("**Referenced files:** " + " · ".join(f"`{f}`" for f in plan.key_files))
        if plan.metadata:
            m = plan.metadata
            cq = f" · complexity: {m.query_complexity}" if m.query_complexity else ""
            lines.append(
                f"\n---\n_ID: `{plan.plan_id}` · {m.context_tokens} tokens · "
                f"{m.context_files} chunks · {m.elapsed_ms:.0f}ms{cq}_"
            )
            if m.grounding_warnings:
                lines.append("\n> **⚠ Retrieval warnings:**")
                for w in m.grounding_warnings:
                    lines.append(f"> - {w}")
        return "\n".join(lines)

    # ── Implementation plan ───────────────────────────────────────────────────
    lines = [
        "# Implementation Plan",
        f"**Query:** {plan.query}",
        "",
    ]

    # NOTE: Stack fingerprint and web research are NOT rendered in the plan output.
    # They are reference-only context for the planner — not user-facing content.

    # ── 1. Problem Statement ──────────────────────────────────────────────────
    problem = getattr(plan, "problem_statement", "") or ""
    if problem:
        lines += ["## 1. Problem Statement", problem, ""]
    elif plan.summary:
        # Backward compat: fall back to summary for old plans
        lines += ["## Summary", plan.summary, ""]

    if plan.clarifying_assumptions:
        lines += ["### Assumptions"]
        for a in plan.clarifying_assumptions:
            lines.append(f"- {a}")
        lines.append("")

    # ── 2. Current Architecture ───────────────────────────────────────────────
    arch = getattr(plan, "current_architecture", "") or ""
    if arch:
        lines += ["## 2. Current Architecture", arch, ""]

    # ── 3. Proposed Solutions ─────────────────────────────────────────────────
    solutions = getattr(plan, "proposed_solutions", []) or []
    if solutions:
        lines += ["## 3. Proposed Solutions"]
        for i, sol in enumerate(solutions):
            name = sol.get("name", f"Option {chr(65 + i)}")
            recommended = sol.get("is_recommended", False)
            label = " (Recommended)" if recommended else ""
            lines.append(f"### Option {chr(65 + i)}: {name}{label}")
            lines.append(sol.get("approach", ""))
            pros = sol.get("pros", [])
            if pros:
                lines.append("\n**Pros:**")
                for p in pros:
                    lines.append(f"- {p}")
            cons = sol.get("cons", [])
            if cons:
                lines.append("\n**Cons:**")
                for c in cons:
                    lines.append(f"- {c}")
            lines.append("")
    elif plan.design_alternatives:
        # Backward compat: render old design_alternatives
        lines += ["## Design Alternatives"]
        for alt in plan.design_alternatives:
            lines.append(f"### {alt.get('approach', 'Alternative')}")
            for p in alt.get("pros", []):
                lines.append(f"- ✅ {p}")
            for c in alt.get("cons", []):
                lines.append(f"- ❌ {c}")
            reason = alt.get("rejected_reason", "")
            if reason:
                lines.append(f"_Rejected: {reason}_")
        lines.append("")

    # ── 4. Recommendation ─────────────────────────────────────────────────────
    rec = getattr(plan, "recommendation", "") or ""
    if rec:
        lines += ["## 4. Recommendation", rec, ""]

    # ── 5. Implementation Plan ────────────────────────────────────────────────
    prereqs = getattr(plan, "prerequisites", []) or []
    has_impl_section = prereqs or plan.steps
    if has_impl_section:
        lines += ["## 5. Implementation Plan"]

    if prereqs:
        lines += ["### 5.1 Prerequisites"]
        for p in prereqs:
            lines.append(f"- [ ] {p}")
        lines.append("")

    if plan.steps:
        if prereqs:
            lines += ["### 5.2 Dev Tasks"]
        for s in plan.steps:
            deps = f" _(after steps {s.depends_on_steps})_" if s.depends_on_steps else ""
            lines.append(f"**Step {s.step_number}: {s.title}**{deps}")
            lines.append(s.description)
            if s.files_involved:
                lines.append(f"_Files: {', '.join(f'`{f}`' for f in s.files_involved)}_")
            if s.verification:
                lines.append(f"✅ **Verify:** {s.verification}")
            lines.append("")

    if has_impl_section:
        lines.append("")

    # ── 6. Files to Change ────────────────────────────────────────────────────
    if plan.files:
        lines += ["## 6. Files to Change"]
        for f in plan.files:
            lines.append(f"### `{f.path}` — {f.action.upper()}")
            lines.append(f"_{f.reason}_")
            for c in f.changes:
                sym = f" `{c.symbol}`" if c.symbol else ""
                lines.append(f"- **{c.kind.upper()}**{sym}: {c.description}")
                if c.pseudocode:
                    lines.append(f"  ```\n  {c.pseudocode}\n  ```")
        lines.append("")

    # ── 7. Risks ──────────────────────────────────────────────────────────────
    if plan.risks:
        lines += ["## 7. Risks"]
        severity_emoji = {"low": "🟡", "medium": "🟠", "high": "🔴"}
        for r in plan.risks:
            emoji = severity_emoji.get(r.severity, "⬜")
            lines.append(f"- {emoji} **{r.severity.upper()}**: {r.description}")
            if r.affected_symbols:
                lines.append(f"  _Affected: {', '.join(r.affected_symbols)}_")
            lines.append(f"  _Mitigation: {r.mitigation}_")
        lines.append("")

    if plan.test_plan:
        lines += ["## Test Plan", plan.test_plan, ""]

    # ── 8. Open Questions ─────────────────────────────────────────────────────
    oq = getattr(plan, "open_questions", "") or ""
    if oq:
        lines += ["## 8. Open Questions", oq, ""]

    # ── 9. References ─────────────────────────────────────────────────────────
    refs = getattr(plan, "references", []) or []
    if refs:
        lines += ["## 9. References"]
        for r in refs:
            lines.append(f"- `{r}`")
        lines.append("")

    if plan.metadata:
        m = plan.metadata
        cq = f" · complexity: {m.query_complexity}" if m.query_complexity else ""
        qs = f" · quality: {m.quality_score:.2f}" if m.quality_score else ""
        lines.append(
            f"---\n_Plan ID: `{plan.plan_id}` · Model: {m.model} · "
            f"Context: {m.context_tokens} tokens · {m.context_files} chunks · "
            f"{m.elapsed_ms:.0f}ms{cq}{qs}_"
        )
        if m.grounding_warnings:
            lines.append("\n> **⚠ Retrieval warnings:**")
            for w in m.grounding_warnings:
                lines.append(f"> - {w}")

    return "\n".join(lines)


# ── Tool 8: get_semantic_context ──────────────────────────────────────────────


@mcp_server.tool()
async def get_semantic_context(
    symbols: Annotated[
        list[str],
        "Symbol names or qualified names to retrieve semantic relationships for. "
        "Example: ['AuthService', 'JWTValidator', 'PaymentFlow.charge']",
    ],
    repo: Annotated[
        str | None,
        "Target repo as 'owner/name'. Omit to use the default/current repo.",
    ] = None,
    concept: Annotated[
        str | None,
        "Optional concept filter — only return relationships matching this concept. "
        "Example: 'authentication', 'caching', 'validation'.",
    ] = None,
) -> str:
    """
    Retrieve LLM-extracted semantic architectural relationships for a set of symbols.

    Returns facts the structural call graph cannot express:
      "AuthService —[validates]→ JWTToken  (confidence: 0.92)"
      "PaymentFlow —[coordinates]→ StripeClient  (confidence: 0.88)"

    WHEN TO USE:
    - After search_codebase / get_symbol found key symbols — understand their role.
    - Cross-cutting architecture questions: "what relates to authentication?"
    - Before planning a refactor — know a module's semantic dependencies.
    - When the call graph alone doesn't explain *why* two components are coupled.

    RETURNS:
      context:         Markdown-formatted relationship graph ready to read.
      symbols_queried: List of symbols looked up.
      tip:             Guidance if no data is available yet.

    NOTE: Requires semantic enrichment to have run for the repo first.
    Trigger enrichment via POST /graph/{owner}/{name}/enrich if context is empty.
    """
    from src.graph.semantic_enricher import get_semantic_context_for_symbols

    if not symbols:
        return json.dumps({"error": "symbols list must not be empty"})

    repo_owner: str | None = None
    repo_name: str | None = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    context = await get_semantic_context_for_symbols(
        symbols=symbols,
        owner=repo_owner or "",
        repo_name=repo_name or "",
        concept=concept,
    )

    tip = (
        "No semantic data yet. Trigger POST /graph/{owner}/{name}/enrich to run enrichment."
        if not context
        else ""
    )
    return json.dumps({
        "context": context,
        "symbols_queried": symbols,
        "tip": tip,
    }, indent=2)


# ── Tool 9: list_skills ───────────────────────────────────────────────────────


@mcp_server.tool()
async def list_skills(
    filter: Annotated[str | None, "Optional text to filter skills by name or description"] = None,
) -> str:
    """
    List all available NexusCode skills. Skills describe capabilities,
    workflows, and domain knowledge for AI agents using this server.

    Returns JSON list of skills with name, description, and source.
    Use filter to narrow results by keyword.
    """
    from src.skills.loader import load_all_skills

    skills = load_all_skills()
    if filter:
        fl = filter.lower()
        skills = [s for s in skills if fl in s.name.lower() or fl in s.description.lower()]

    result = [{"name": s.name, "description": s.description, "source": s.source} for s in skills]
    return json.dumps({"skills": result, "total": len(result)}, indent=2)


# ── Tool 10: get_evolution_metrics ────────────────────────────────────────────


@mcp_server.tool()
async def get_evolution_metrics(
    repo: Annotated[str | None, "Repository in 'owner/name' format, e.g. 'acme/backend'"] = None,
    days: Annotated[int, "Look-back window in days (default 7)"] = 7,
) -> str:
    """
    Return performance metrics and evolution status for a repository.

    Shows mean retrieval quality, latency percentiles, user feedback summary,
    latest worldview version, and the most recent reflection cycle outcome.

    Use this to understand how well NexusCode is serving a repo and what
    self-improvements have been applied.
    """
    if not repo or "/" not in repo:
        return json.dumps({"error": "repo must be in 'owner/name' format"})

    owner, name = repo.split("/", 1)

    from src.evolution.telemetry import get_repo_performance_window
    from src.evolution.feedback import get_feedback_summary

    stats = await get_repo_performance_window(owner, name, days)
    feedback = await get_feedback_summary(owner, name, days)

    # Fetch latest worldview version
    worldview_version = None
    async with AsyncSessionLocal() as session:
        wv_row = (
            await session.execute(
                text("""
                    SELECT version, generated_at FROM repo_worldviews
                    WHERE repo_owner = :owner AND repo_name = :name
                    ORDER BY version DESC LIMIT 1
                """),
                {"owner": owner, "name": name},
            )
        ).mappings().first()
        if wv_row:
            worldview_version = {
                "version": wv_row["version"],
                "generated_at": wv_row["generated_at"].isoformat() if wv_row["generated_at"] else None,
            }

    # Fetch last evolution cycle
    last_cycle = None
    async with AsyncSessionLocal() as session:
        cy_row = (
            await session.execute(
                text("""
                    SELECT cycle_number, status, improvements_applied,
                           cycle_completed_at, discovered_patterns
                    FROM evolution_log
                    WHERE repo_owner = :owner AND repo_name = :name
                    ORDER BY cycle_number DESC LIMIT 1
                """),
                {"owner": owner, "name": name},
            )
        ).mappings().first()
        if cy_row:
            last_cycle = {
                "cycle_number": cy_row["cycle_number"],
                "status": cy_row["status"],
                "improvements_applied": cy_row["improvements_applied"],
                "completed_at": cy_row["cycle_completed_at"].isoformat() if cy_row["cycle_completed_at"] else None,
                "patterns_discovered": cy_row["discovered_patterns"] or [],
            }

    return json.dumps(
        {
            "repo": repo,
            "lookback_days": days,
            "performance": {
                "total_interactions": stats.total_interactions,
                "mean_quality": stats.mean_quality,
                "low_quality_ratio": stats.low_quality_ratio,
                "p50_latency_ms": stats.p50_latency_ms,
                "p95_latency_ms": stats.p95_latency_ms,
                "mean_iterations": stats.mean_iterations,
                "by_complexity": stats.by_complexity,
            },
            "feedback": feedback,
            "worldview": worldview_version,
            "last_evolution_cycle": last_cycle,
        },
        indent=2,
    )


# ── Tool 11: get_repo_worldview ───────────────────────────────────────────────


@mcp_server.tool()
async def get_repo_worldview(
    repo: Annotated[str, "Repository in 'owner/name' format, e.g. 'acme/backend'"],
) -> str:
    """
    Return the semantic worldview NexusCode has built for a repository.

    The worldview is an LLM-generated understanding of the codebase:
    architecture, key design patterns, conventions, and areas where retrieval
    is typically difficult.

    This worldview is automatically injected into Ask and Plan mode prompts,
    making responses progressively smarter as it evolves.
    """
    if "/" not in repo:
        return json.dumps({"error": "repo must be in 'owner/name' format"})

    owner, name = repo.split("/", 1)

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT version, architecture_summary, key_patterns,
                           difficult_zones, conventions, recent_changes,
                           full_worldview, generated_at
                    FROM repo_worldviews
                    WHERE repo_owner = :owner AND repo_name = :name
                    ORDER BY version DESC LIMIT 1
                """),
                {"owner": owner, "name": name},
            )
        ).mappings().first()

    if not row:
        return json.dumps(
            {
                "repo": repo,
                "worldview": None,
                "message": "No worldview yet. Trigger a reflection cycle via POST /evolution/cycle.",
            }
        )

    return json.dumps(
        {
            "repo": repo,
            "version": row["version"],
            "architecture_summary": row["architecture_summary"],
            "key_patterns": row["key_patterns"] or [],
            "difficult_zones": row["difficult_zones"] or [],
            "conventions": row["conventions"] or [],
            "recent_changes": row["recent_changes"],
            "full_worldview": row["full_worldview"],
            "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
        },
        indent=2,
    )


# ── Tool 12: reflect_and_improve ──────────────────────────────────────────────


@mcp_server.tool()
async def reflect_and_improve(
    repo: Annotated[str, "Repository in 'owner/name' format, e.g. 'acme/backend'"],
    lookback_days: Annotated[int, "Days of interaction history to analyze (default 30)"] = 30,
    force: Annotated[bool, "Run even if interaction threshold not met (default false)"] = False,
) -> str:
    """
    Trigger a self-reflection cycle for a repository.

    NexusCode will:
    1. Analyze recent interaction quality and retrieval efficiency
    2. Discover weak query patterns and opportunities for improvement
    3. Propose and autonomously apply parameter + prompt improvements
    4. Generate a fresh semantic worldview of the repository
    5. Log all changes in the evolution_log (full audit trail)

    Returns a summary of what was analyzed, changed, and learned.
    Use get_evolution_metrics to track impact over time.
    """
    if "/" not in repo:
        return json.dumps({"error": "repo must be in 'owner/name' format"})

    owner, name = repo.split("/", 1)

    from src.evolution.reflection_cycle import run_reflection_cycle

    result = await run_reflection_cycle(
        repo_owner=owner,
        repo_name=name,
        lookback_days=lookback_days,
        force=force,
    )

    return json.dumps(
        {
            "repo": repo,
            "cycle_number": result.cycle_number,
            "status": result.status,
            "metrics_analyzed": result.metrics_analyzed,
            "parameters_changed": result.parameters_changed,
            "prompts_improved": [
                {"target": p.get("target"), "reason": p.get("reason", "")}
                for p in result.prompts_improved
            ],
            "discovered_patterns": result.discovered_patterns,
            "new_worldview_version": result.new_worldview_version,
            "error": result.error,
        },
        indent=2,
    )
