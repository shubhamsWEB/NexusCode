"""
MCP server — exposes 7 codebase intelligence tools via FastMCP.

Tools:
  1. search_codebase      — hybrid semantic+keyword search + rerank
  2. get_symbol           — fuzzy symbol lookup (like "Go to Definition")
  3. find_callers         — who calls this function/method?
  4. get_file_context     — structural map of a file (symbols + imports + imported_by)
  5. get_agent_context    — pre-assembled token-budget-aware context for a task
  6. plan_implementation  — web research + codebase context → structured implementation plan
  7. ask_codebase         — answer a natural-language question about the codebase

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
        "A live, always-fresh index of your GitHub codebase. "
        "Use search_codebase for natural-language or identifier queries. "
        "Use get_symbol for exact function/class lookup. "
        "Use find_callers to trace usage of a symbol. "
        "Use get_file_context for a structural overview of a file. "
        "Use get_agent_context to get pre-assembled context before starting a task. "
        "Use plan_implementation to generate a complete implementation plan for a bug/feature/refactor. "
        "Use ask_codebase to answer any natural-language question about the codebase in a mentor tone. "
        "Use list_skills to discover available skills and their capabilities."
    ),
    warn_on_duplicate_tools=False,
)


# ── Tool 1: search_codebase ───────────────────────────────────────────────────


@mcp_server.tool()
async def search_codebase(
    query: Annotated[str, "Natural language or identifier query"],
    repo: Annotated[str | None, "Scope to 'owner/name' — defaults to all repos"] = None,
    language: Annotated[str | None, "Filter by language: python, typescript, javascript…"] = None,
    top_k: Annotated[int, "Number of results to return (1-20)"] = 5,
    mode: Annotated[str, "Search mode: 'semantic', 'keyword', or 'hybrid'"] = "hybrid",
) -> str:
    """
    Search the codebase using semantic vector search, keyword search, or both.
    Returns ranked code chunks with file locations, symbol names, and source previews.
    Use this for any question like 'where is auth handled?' or 'find the payment logic'.
    """
    from src.retrieval.assembler import assemble
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import embed_query, search

    top_k = max(1, min(20, top_k))

    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

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
        "Symbol name — exact ('authenticate'), qualified ('UserService.authenticate'), or natural language",
    ],
    repo: Annotated[str | None, "Scope to 'owner/name' — defaults to all repos"] = None,
) -> str:
    """
    Look up a function, class, or method by name (like IDE 'Go to Definition').
    Returns the symbol's file location, full signature, and docstring.
    Supports fuzzy matching — 'auth' will find 'authenticate', 'Authorization', etc.
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
        str, "Symbol name to find callers of (e.g. 'authenticate', 'PaymentService.charge')"
    ],
    repo: Annotated[str | None, "Scope to 'owner/name' — defaults to all repos"] = None,
    depth: Annotated[int, "How many call hops deep (1-3). depth=2 finds callers-of-callers."] = 1,
) -> str:
    """
    Find all code that calls a given function or method, with optional multi-hop traversal.

    depth=1: direct callers only (who calls 'authenticate'?)
    depth=2: callers of callers (who calls the code that calls 'authenticate'?)
    depth=3: three hops deep — useful for tracing blast radius of a signature change

    Each hop builds a call graph using BFS: the callers found in hop N become the
    targets for hop N+1, stopping when no new call sites are found.

    Returns file locations and code snippets for each discovered call site per hop.
    Answers: 'what breaks if I change this function's signature?'
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
    path: Annotated[str, "File path relative to repo root (e.g. 'app/shopify.server.ts')"],
    repo: Annotated[str | None, "Scope to 'owner/name'"] = None,
    include_deps: Annotated[bool, "Include files this file imports (default true)"] = True,
) -> str:
    """
    Get the complete structural map of a file: all symbols, imports, and what imports it.
    Answers: 'what is in this file and how does it relate to others?'
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

        # 3. Files that import this file (imported_by)
        if include_deps and sym_rows:
            # Look for chunks where raw_content contains a reference to this file's name
            file_stem = path.split("/")[-1].rsplit(".", 1)[0]
            dep_params = {**params, "stem": f"%{file_stem}%"}
            dep_sql = text(f"""
                SELECT DISTINCT file_path, repo_owner, repo_name
                FROM chunks
                WHERE raw_content ILIKE :stem
                  AND file_path NOT ILIKE :path_like
                  AND is_deleted = FALSE
                  {repo_filter.replace("AND ", "AND ", 1)}
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
    task: Annotated[str, "Natural language description of the task you are about to perform"],
    focal_files: Annotated[
        list[str] | None, "Files you are actively working on — their chunks get priority"
    ] = None,
    token_budget: Annotated[int, "Max tokens to return (default 8000)"] = 8000,
    repo: Annotated[str | None, "Scope to 'owner/name' — defaults to all repos"] = None,
) -> str:
    """
    Pre-assembled, token-budget-aware context for a specific coding task.
    Call this at the START of a task before you begin reasoning.
    It combines focal file content + semantic search + import graph to give you
    everything relevant in one shot, deduplicated and ranked.
    """
    from sqlalchemy import text

    from src.retrieval.assembler import assemble
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import SearchResult, _semantic_search, embed_query
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
        str, "Bug report, feature request, or refactoring task description (min 10 chars)"
    ],
    repo: Annotated[str | None, "Scope to 'owner/name' — defaults to all repos"] = None,
    web_research: Annotated[
        bool, "Search the web for best practices before generating the plan (default true)"
    ] = True,
    model: Annotated[
        str | None,
        "LLM model to use (e.g. 'gpt-4o', 'claude-opus-4-6'). Defaults to server config.",
    ] = None,
) -> str:
    """
    Generate a complete, grounded implementation plan for a coding task.

    Combines two information sources:
    1. Web research — searches for the best library, pattern, and current best practices
    2. Codebase context — retrieves the actual files, symbols, and callers from the index

    Returns a structured plan with:
    - Exact file paths and symbol names to change (from codebase)
    - Library/approach recommendation (from web research)
    - Step-by-step execution order with dependencies
    - Pseudocode for complex logic
    - Risk assessment and mitigation strategies
    - A concrete test plan

    Use this BEFORE starting any non-trivial implementation to get a
    Cursor-style planning overview grounded in your actual codebase.
    """
    from src.planning.claude_planner import generate_plan
    from src.planning.retriever import retrieve_planning_context

    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    try:
        ctx = await retrieve_planning_context(
            query=query,
            repo_owner=repo_owner,
            repo_name=repo_name,
            web_research=web_research,
            model=model,
        )
    except Exception as exc:
        return json.dumps({"error": f"Retrieval failed: {exc}"})

    try:
        plan = await generate_plan(
            query=query,
            ctx=ctx,
            repo_owner=repo_owner,
            repo_name=repo_name,
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
    question: Annotated[str, "Natural-language question about the codebase (min 5 chars)"],
    repo: Annotated[str | None, "Scope to 'owner/name' — defaults to all repos"] = None,
    model: Annotated[
        str | None,
        "LLM model to use (e.g. 'gpt-4o', 'claude-opus-4-6'). Defaults to server config.",
    ] = None,
) -> str:
    """
    Answer a natural-language question about the codebase in a mentor tone.

    Unlike plan_implementation (which outputs file changes and steps), this tool
    answers questions conversationally — explaining how code works, tracing data
    flows, clarifying architecture decisions, and pointing to real file locations.

    Returns a markdown answer with:
    - Inline file citations (e.g. `src/pipeline/pipeline.py` lines 42-80)
    - Fenced code snippets for key examples
    - 2-3 concrete follow-up questions grounded in the codebase

    Use this for questions like:
    - "How does the webhook processing pipeline work?"
    - "Where is authentication handled?"
    - "What does the reranker do and when is it called?"
    - "Explain the chunking algorithm"
    """
    from src.ask.ask_agent import generate_answer
    from src.planning.retriever import retrieve_planning_context

    repo_owner = repo_name = None
    if repo and "/" in repo:
        repo_owner, repo_name = repo.split("/", 1)

    try:
        ctx = await retrieve_planning_context(
            query=question,
            repo_owner=repo_owner,
            repo_name=repo_name,
            web_research=False,
            model=model,
        )
    except Exception as exc:
        return json.dumps({"error": f"Retrieval failed: {exc}"})

    try:
        result = await generate_answer(
            query=question,
            ctx=ctx,
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
        f"\n---\n_Context: {ctx.tokens_used} tokens · "
        f"{len(ctx.chunks_used)} chunks · {result.elapsed_ms:.0f}ms_"
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
        "## Summary",
        plan.summary,
        "",
    ]

    # NOTE: Stack fingerprint and web research are NOT rendered in the plan output.
    # They are reference-only context for the planner — not user-facing content.

    if plan.clarifying_assumptions:
        lines += ["## Assumptions"]
        for a in plan.clarifying_assumptions:
            lines.append(f"- {a}")
        lines.append("")

    if plan.files:
        lines += ["## Files to Change"]
        for f in plan.files:
            lines.append(f"### `{f.path}` — {f.action.upper()}")
            lines.append(f"_{f.reason}_")
            for c in f.changes:
                sym = f" `{c.symbol}`" if c.symbol else ""
                lines.append(f"- **{c.kind.upper()}**{sym}: {c.description}")
                if c.pseudocode:
                    lines.append(f"  ```\n  {c.pseudocode}\n  ```")
        lines.append("")

    if plan.steps:
        lines += ["## Execution Steps"]
        for s in plan.steps:
            deps = f" _(after steps {s.depends_on_steps})_" if s.depends_on_steps else ""
            lines.append(f"### Step {s.step_number}: {s.title}{deps}")
            lines.append(s.description)
            if s.files_involved:
                lines.append(f"_Files: {', '.join(f'`{f}`' for f in s.files_involved)}_")
            if s.verification:
                lines.append(f"✅ **Verify:** {s.verification}")
        lines.append("")

    if plan.risks:
        lines += ["## Risks"]
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


# ── Tool 8: list_skills ───────────────────────────────────────────────────────


@mcp_server.tool()
async def list_skills(
    filter: Annotated[
        str | None, "Optional text to filter skills by name or description"
    ] = None,
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

    result = [
        {"name": s.name, "description": s.description, "source": s.source}
        for s in skills
    ]
    return json.dumps({"skills": result, "total": len(result)}, indent=2)
