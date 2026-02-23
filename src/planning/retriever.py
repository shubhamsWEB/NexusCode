"""
Context retrieval pipeline for implementation planning.

Phase 0a — extract stack fingerprint (fast DB query: dep files + aggregated imports)
Phase 0b — fire stack-aware web research as a background asyncio task
Phase 1  — embed the query with voyage-code-2
Phase 2  — hybrid search → 15 candidates (vector + keyword + RRF)
Phase 3  — cross-encoder rerank → top 10
Phase 4  — file structure maps for the top-5 unique files
Phase 5  — caller context for the top-3 unique symbols
           + optional second semantic pass using discovered symbol names
Phase 6  — collect web research notes (awaits the Phase-0b task)

The assembled context is split across three token-budget slices:
  65 %  primary chunks  (phases 2-3)
  20 %  caller context  (phase 5)
  15 %  expansion       (second semantic pass)

Phase 0a completes first (fast, ~50 ms) so the web researcher gets the
actual stack (installed packages, language, framework) before searching.
Phase 0b then runs in parallel with phases 1-5 — zero extra wall-clock time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import settings

logger = logging.getLogger(__name__)


# ── Output dataclass ──────────────────────────────────────────────────────────


@dataclass
class PlanningContext:
    primary_context: str  # formatted code chunks (phases 2-3)
    file_maps: str  # structural file summaries (phase 4)
    caller_contexts: str  # call-site context (phase 5)
    expansion_context: str  # second-pass symbol context
    component_context: str  # full component files for improve/analysis queries
    stack_fingerprint: str  # phase 0a — installed packages, language, framework
    web_research_notes: str  # phase 0b — gap-focused web research (may be "")
    is_improvement_query: bool  # True → query is about improving/reviewing something
    chunks_used: list[dict]  # chunk metadata for telemetry
    tokens_used: int
    retrieval_log: str


# ── Public entry point ────────────────────────────────────────────────────────


async def retrieve_planning_context(
    query: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    web_research: bool = True,
) -> PlanningContext:
    """
    Run the retrieval pipeline and return a PlanningContext ready to inject
    into the Claude prompt.

    Web research (Phase 0) fires immediately as a background task and runs
    in parallel with codebase retrieval (Phases 1-5), adding no extra latency.
    Set web_research=False to skip it entirely.
    """
    import asyncio

    from src.retrieval.assembler import assemble
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import embed_query, search

    total_budget = settings.planning_context_budget
    primary_budget = int(total_budget * 0.65)
    caller_budget = int(total_budget * 0.20)
    expansion_budget = int(total_budget * 0.15)

    # ── Phase 0a: classify query intent + extract stack fingerprint ─────────
    # Intent detection comes first so we can gate web research correctly.
    is_improvement = _is_improvement_query(query)
    logger.info(
        "planning retriever: query intent = %s",
        "improve/analyze" if is_improvement else "standard",
    )

    stack_fingerprint = await _extract_stack_fingerprint(repo_owner, repo_name)
    if stack_fingerprint:
        logger.info(
            "planning retriever: stack fingerprint ready (%d chars)", len(stack_fingerprint)
        )

    # ── Phase 0b: web research — SKIP for internal improvement queries ────────
    # Improvement queries are about the codebase itself — web search produces
    # irrelevant generic advice (e.g. "add conversation history", "use tiktoken")
    # that poisons the prompt. Only run web research for external technology tasks.
    web_research_task = None
    effective_web_research = web_research and not is_improvement
    if effective_web_research:
        from src.planning.web_researcher import research_implementation

        logger.info("planning retriever: starting stack-aware web research (background)")
        web_research_task = asyncio.create_task(
            research_implementation(query, stack_context=stack_fingerprint)
        )
    elif is_improvement and web_research:
        logger.info(
            "planning retriever: skipping web research for improvement query "
            "(would generate irrelevant external suggestions)"
        )

    # ── Phase 1: embed query ─────────────────────────────────────────────────
    logger.info("planning retriever: embedding query")
    query_vector = await embed_query(query)

    # ── Phase 2: hybrid search (15 candidates) ───────────────────────────────
    logger.info("planning retriever: hybrid search")
    candidates = await search(
        query=query,
        query_vector=query_vector,
        top_k=15,
        mode="hybrid",
        repo_owner=repo_owner,
        repo_name=repo_name,
    )

    # ── Phase 3: cross-encoder rerank → top 10 ───────────────────────────────
    if candidates:
        logger.info("planning retriever: reranking %d candidates", len(candidates))
        candidates = rerank(query, candidates, top_n=10)

    # ── Phase 4: file structure maps for top-5 unique files ──────────────────
    top_files = _unique_paths(candidates, limit=5)
    file_maps = await _get_file_structure_maps(top_files, repo_owner, repo_name)

    # ── Phase 5: caller context for top-3 unique symbols ─────────────────────
    top_symbols = [r.symbol_name for r in candidates[:5] if r.symbol_name]
    top_symbols = list(dict.fromkeys(top_symbols))[:3]  # deduplicate, keep order
    caller_ctx_text = await _get_caller_contexts(top_symbols, caller_budget, repo_owner, repo_name)

    # ── Second semantic pass using discovered symbol names ───────────────────
    expansion_results = []
    seen_ids = {r.chunk_id for r in candidates}
    for sym in top_symbols[:2]:
        try:
            sym_vector = await embed_query(sym)
            sym_results = await search(
                query=sym,
                query_vector=sym_vector,
                top_k=5,
                mode="hybrid",
                repo_owner=repo_owner,
                repo_name=repo_name,
            )
            for r in sym_results:
                if r.chunk_id not in seen_ids:
                    seen_ids.add(r.chunk_id)
                    expansion_results.append(r)
        except Exception as exc:
            logger.warning("expansion search failed for symbol %r: %s", sym, exc)

    # ── Assemble context strings ─────────────────────────────────────────────
    primary_ctx = assemble(candidates, token_budget=primary_budget, query=query)

    if expansion_results:
        expansion_ctx = assemble(expansion_results, token_budget=expansion_budget, query=query)
        expansion_text = expansion_ctx.context_text
    else:
        expansion_text = ""

    # ── Phase 5.5: component-aware full retrieval for improvement queries ─────
    # When the query is about improving a specific component, fetch ALL chunks
    # from those component files so Claude sees the complete picture, not fragments.
    component_context = ""
    if is_improvement:
        component_context = await _fetch_component_context(
            query=query,
            candidates=candidates,
            repo_owner=repo_owner,
            repo_name=repo_name,
            token_budget=int(total_budget * 0.60),  # 60% of budget for full component source
        )
        if component_context:
            logger.info(
                "planning retriever: component context loaded (%d chars)", len(component_context)
            )

    # ── Phase 6: collect web research notes ─────────────────────────────────
    web_research_notes = ""
    if web_research_task is not None:
        try:
            web_research_notes = await web_research_task
            if web_research_notes:
                logger.info(
                    "planning retriever: web research complete (%d chars)", len(web_research_notes)
                )
            else:
                logger.info("planning retriever: web research returned empty (continuing)")
        except Exception as exc:
            logger.warning("planning retriever: web research task failed: %s", exc)

    retrieval_log = (
        f"{primary_ctx.retrieval_log}\n"
        f"file_maps: {len(top_files)} files\n"
        f"caller_context_symbols: {top_symbols}\n"
        f"expansion_chunks: {len(expansion_results)}\n"
        f"component_context: {'yes (' + str(len(component_context)) + ' chars)' if component_context else 'no'}\n"
        f"is_improvement_query: {is_improvement}\n"
        f"stack_fingerprint: {'yes' if stack_fingerprint else 'no'}\n"
        f"web_research: {'yes' if web_research_notes else 'no (skipped for improvement query)' if is_improvement else 'no'}"
    )

    logger.info(
        "planning retriever done: %d chunks, %d tokens, improvement=%s, component=%s, web=%s",
        len(primary_ctx.chunks_used),
        primary_ctx.tokens_used,
        is_improvement,
        bool(component_context),
        bool(web_research_notes),
    )

    return PlanningContext(
        primary_context=primary_ctx.context_text,
        file_maps=file_maps,
        caller_contexts=caller_ctx_text,
        expansion_context=expansion_text,
        component_context=component_context,
        stack_fingerprint=stack_fingerprint,
        web_research_notes=web_research_notes,
        is_improvement_query=is_improvement,
        chunks_used=primary_ctx.chunks_used,
        tokens_used=primary_ctx.tokens_used,
        retrieval_log=retrieval_log,
    )


# ── Intent detection ──────────────────────────────────────────────────────────

_IMPROVEMENT_PATTERNS = (
    # "how can I make X better/faster/smarter"
    "how can i",
    "how to improve",
    "how do i improve",
    "how to make",
    "make it better",
    "make the",
    "make this",
    "make /",
    # "improve / enhance / optimize / review / audit"
    "improve",
    "enhance",
    "optimize",
    "optimise",
    "review",
    "audit",
    "refactor",
    "redesign",
    "rethink",
    "restructure",
    # "what's wrong with / what are the weaknesses of"
    "what's wrong",
    "whats wrong",
    "what are the issues",
    "what are the weaknesses",
    "what are the problems",
    "what could be better",
    "what can be improved",
    # explicit quality queries
    "world class",
    "production ready",
    "better response",
    "better quality",
    "response quality",
    "context aware",
    "smarter",
    "more accurate",
)


def _is_improvement_query(query: str) -> bool:
    """
    Return True if the query is asking to improve/enhance/review something
    that already exists in the codebase, rather than implement something new
    or ask a pure question.

    These queries need FULL component context and should NOT trigger web research
    (web search returns irrelevant generic advice for internal improvement queries).
    """
    q = query.lower().strip()
    return any(pattern in q for pattern in _IMPROVEMENT_PATTERNS)


# ── Component-aware full retrieval ────────────────────────────────────────────


async def _fetch_component_context(
    query: str,
    candidates: list,
    repo_owner: str | None,
    repo_name: str | None,
    token_budget: int,
) -> str:
    """
    For improvement/analysis queries: fetch ALL chunks from the component files
    that the semantic search identified as relevant.

    Instead of showing Claude 10 random fragments, we show the complete source
    of the top 3-5 most relevant files. This gives Claude a genuine understanding
    of the current implementation to analyze.
    """
    if not candidates:
        return ""

    from sqlalchemy import text

    from src.pipeline.chunker import count_tokens
    from src.storage.db import AsyncSessionLocal

    # Use the top unique files from semantic candidates as the component scope
    component_files = _unique_paths(candidates, limit=5)
    if not component_files:
        return ""

    params: dict = {}
    repo_filter = ""
    if repo_owner:
        repo_filter += " AND repo_owner = :repo_owner"
        params["repo_owner"] = repo_owner
    if repo_name:
        repo_filter += " AND repo_name = :repo_name"
        params["repo_name"] = repo_name

    # Build file filter — all chunks from these component files
    file_placeholders = ", ".join(f":file_{i}" for i in range(len(component_files)))
    for i, fp in enumerate(component_files):
        params[f"file_{i}"] = fp

    sql = text(f"""
        SELECT file_path, start_line, end_line, language,
               symbol_name, scope_chain, raw_content, token_count
        FROM chunks
        WHERE file_path IN ({file_placeholders})
          AND NOT is_deleted
          {repo_filter}
        ORDER BY file_path, start_line
    """)

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(sql, params)).mappings().all()
    except Exception as exc:
        logger.warning("component context: DB query failed: %s", exc)
        return ""

    if not rows:
        return ""

    # Group by file and assemble with token budget
    sections: list[str] = []
    tokens_used = 0
    seen_files: set[str] = set()

    for row in rows:
        fp = row["file_path"]
        header = (
            f"────────────────────────────────────────\n"
            f"File: {fp}  [L{row['start_line']}-{row['end_line']}]"
            f"  ({row['language']})"
        )
        if row["symbol_name"]:
            header += f"\nSymbol: {row['symbol_name']}"
        if row["scope_chain"] and row["scope_chain"] != row["symbol_name"]:
            header += f"  Scope: {row['scope_chain']}"
        chunk_text = header + "\n\n" + row["raw_content"]

        chunk_tokens = row["token_count"] or count_tokens(chunk_text)
        if tokens_used + chunk_tokens > token_budget:
            if fp not in seen_files:
                # Always include at least one chunk per file as a stub
                stub = header + "\n\n[… truncated — token budget reached]"
                sections.append(stub)
                seen_files.add(fp)
            break

        sections.append(chunk_text)
        seen_files.add(fp)
        tokens_used += chunk_tokens

    logger.debug(
        "component context: %d chunks from %d files, %d tokens",
        len(sections),
        len(seen_files),
        tokens_used,
    )

    if not sections:
        return ""

    return (
        f"## Full Component Source ({len(seen_files)} files, {tokens_used} tokens)\n"
        "_Complete source of the relevant component files for deep analysis._\n\n"
        + "\n\n".join(sections)
    )


# ── Phase 0a helper: stack fingerprint ────────────────────────────────────────


async def _extract_stack_fingerprint(
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    """
    Build a compact picture of what the codebase already has installed and
    what patterns it uses — so web research can focus on gaps, not on
    re-explaining things that are already present.

    Two queries:
      1. Dependency files (requirements.txt, package.json, pyproject.toml, etc.)
         → raw content tells us exact installed packages + versions
      2. Aggregated imports across all chunks
         → confirms which packages are actively used in code (not just listed)

    Returns a compact markdown string or "" if the DB is unreachable.
    """
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    params: dict = {}
    repo_filter = ""
    if repo_owner:
        repo_filter += " AND repo_owner = :repo_owner"
        params["repo_owner"] = repo_owner
    if repo_name:
        repo_filter += " AND repo_name = :repo_name"
        params["repo_name"] = repo_name

    parts: list[str] = []

    # ── Query 1: dependency files ─────────────────────────────────────────────
    dep_sql = text(f"""
        SELECT file_path, raw_content, language
        FROM chunks
        WHERE NOT is_deleted
          {repo_filter}
          AND (
              file_path ILIKE '%requirements%.txt'
           OR file_path ILIKE '%pyproject.toml'
           OR file_path ILIKE '%package.json'
           OR file_path ILIKE '%Pipfile'
           OR file_path ILIKE '%setup.cfg'
           OR file_path ILIKE '%setup.py'
           OR file_path ILIKE '%go.mod'
           OR file_path ILIKE '%Cargo.toml'
           OR file_path ILIKE '%pom.xml'
          )
        ORDER BY length(raw_content) DESC
        LIMIT 4
    """)

    # ── Query 2: most-used imports across all files ───────────────────────────
    import_sql = text(f"""
        SELECT
            language,
            unnest(imports) AS import_stmt,
            count(*) AS uses
        FROM chunks
        WHERE NOT is_deleted
          AND array_length(imports, 1) > 0
          {repo_filter}
        GROUP BY language, import_stmt
        ORDER BY uses DESC
        LIMIT 80
    """)

    # ── Query 3: language distribution ───────────────────────────────────────
    lang_sql = text(f"""
        SELECT language, count(*) AS chunk_count
        FROM chunks
        WHERE NOT is_deleted
          {repo_filter}
        GROUP BY language
        ORDER BY chunk_count DESC
        LIMIT 6
    """)

    try:
        async with AsyncSessionLocal() as session:
            dep_rows = (await session.execute(dep_sql, params)).mappings().all()
            import_rows = (await session.execute(import_sql, params)).mappings().all()
            lang_rows = (await session.execute(lang_sql, params)).mappings().all()
    except Exception as exc:
        logger.warning("stack fingerprint: DB query failed: %s", exc)
        return ""

    # ── Build language summary ────────────────────────────────────────────────
    if lang_rows:
        lang_summary = ", ".join(f"{r['language']} ({r['chunk_count']} chunks)" for r in lang_rows)
        parts.append(f"**Languages detected:** {lang_summary}")

    # ── Build dependency file section ─────────────────────────────────────────
    dep_sections: list[str] = []
    for row in dep_rows:
        fp = row["file_path"]
        content = row["raw_content"]
        # Truncate very long dep files — first 60 lines is enough
        lines = content.splitlines()[:60]
        dep_sections.append(f"### {fp}\n```\n" + "\n".join(lines) + "\n```")
    if dep_sections:
        parts.append("**Dependency files found:**\n\n" + "\n\n".join(dep_sections))

    # ── Build actively-used imports section ───────────────────────────────────
    if import_rows:
        # Group by language
        by_lang: dict[str, list[str]] = {}
        for row in import_rows:
            lang = row["language"] or "unknown"
            stmt = row["import_stmt"].strip()
            if stmt:
                by_lang.setdefault(lang, []).append(stmt)

        import_lines: list[str] = []
        for lang, stmts in by_lang.items():
            import_lines.append(f"**{lang}** — top imports (by usage frequency):")
            for s in stmts[:20]:
                import_lines.append(f"  {s}")
        if import_lines:
            parts.append("**Actively used imports across codebase:**\n" + "\n".join(import_lines))

    if not parts:
        return ""

    return (
        "## Codebase Stack Fingerprint\n"
        "_What is already installed and actively used in this codebase._\n\n" + "\n\n".join(parts)
    )


# ── Phase 4 helper: file structure maps ───────────────────────────────────────


async def _get_file_structure_maps(
    file_paths: list[str],
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    """
    For each file path, fetch all its symbols and produce a compact
    structural map: filename → list of (kind, name, lines).
    """
    if not file_paths:
        return ""

    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    sections: list[str] = []

    for fpath in file_paths:
        params: dict = {"path": fpath, "path_like": f"%{fpath}%"}
        repo_filter = ""
        if repo_owner:
            repo_filter += " AND repo_owner = :repo_owner"
            params["repo_owner"] = repo_owner
        if repo_name:
            repo_filter += " AND repo_name = :repo_name"
            params["repo_name"] = repo_name

        sql = text(f"""
            SELECT name, qualified_name, kind, start_line, end_line, signature
            FROM symbols
            WHERE (file_path = :path OR file_path ILIKE :path_like)
              {repo_filter}
            ORDER BY start_line
            LIMIT 50
        """)

        try:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(sql, params)).mappings().all()

            if rows:
                lines = [f"# {fpath}"]
                for r in rows:
                    sig = f"  {r['kind']:12s} {r['qualified_name']}  (L{r['start_line']}-{r['end_line']})"
                    if r["signature"]:
                        sig += f"\n             sig: {r['signature'][:120]}"
                    lines.append(sig)
                sections.append("\n".join(lines))
        except Exception as exc:
            logger.warning("file map failed for %r: %s", fpath, exc)

    return "\n\n".join(sections)


# ── Phase 5 helper: caller context ────────────────────────────────────────────


async def _get_caller_contexts(
    symbols: list[str],
    token_budget: int,
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    """
    For each symbol, find up to 3 call-sites using keyword search.
    Returns a compact text block of call-site previews.
    """
    if not symbols:
        return ""

    from src.retrieval.searcher import _keyword_search

    all_sections: list[str] = []
    tokens_used = 0

    for sym in symbols:
        try:
            results = await _keyword_search(
                query=sym,
                limit=5,
                repo_owner=repo_owner,
                repo_name=repo_name,
                language=None,
            )
            # Filter definition chunks (lines that start with def/class/function)
            callers = [
                r
                for r in results
                if any(
                    sym in line
                    and not line.strip().startswith(
                        ("def ", "class ", "function ", "const ", "export ", "async def ")
                    )
                    for line in r.raw_content.split("\n")
                )
            ][:3]

            if callers:
                block = f"## Callers of `{sym}`\n"
                for r in callers:
                    block += (
                        f"  {r.file_path}  L{r.start_line}-{r.end_line}\n  {r.raw_content[:300]}\n"
                    )
                from src.pipeline.chunker import count_tokens

                block_tokens = count_tokens(block)
                if tokens_used + block_tokens > token_budget:
                    break
                all_sections.append(block)
                tokens_used += block_tokens

        except Exception as exc:
            logger.warning("caller context failed for symbol %r: %s", sym, exc)

    return "\n".join(all_sections)


# ── Utility ───────────────────────────────────────────────────────────────────


def _unique_paths(candidates, limit: int) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for r in candidates:
        if r.file_path not in seen:
            seen.add(r.file_path)
            paths.append(r.file_path)
        if len(paths) >= limit:
            break
    return paths
