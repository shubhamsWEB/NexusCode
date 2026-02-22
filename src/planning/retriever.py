"""
Context retrieval pipeline for implementation planning.

Phase 0  — fire web research as a background asyncio task (parallel with phases 1-5)
Phase 1  — embed the query with voyage-code-2
Phase 2  — hybrid search → 15 candidates (vector + keyword + RRF)
Phase 3  — cross-encoder rerank → top 10
Phase 4  — file structure maps for the top-5 unique files
Phase 5  — caller context for the top-3 unique symbols
           + optional second semantic pass using discovered symbol names
Phase 6  — collect web research notes (awaits the Phase-0 task)

The assembled context is split across three token-budget slices:
  65 %  primary chunks  (phases 2–3)
  20 %  caller context  (phase 5)
  15 %  expansion       (second semantic pass)

Web research runs in parallel with phases 1-5, so it adds zero extra
wall-clock time on the happy path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class PlanningContext:
    primary_context: str          # formatted code chunks (phases 2-3)
    file_maps: str                # structural file summaries (phase 4)
    caller_contexts: str          # call-site context (phase 5)
    expansion_context: str        # second-pass symbol context
    web_research_notes: str       # phase 0 — web search results (may be "")
    chunks_used: list[dict]       # chunk metadata for telemetry
    tokens_used: int
    retrieval_log: str


# ── Public entry point ────────────────────────────────────────────────────────

async def retrieve_planning_context(
    query: str,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
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
    primary_budget   = int(total_budget * 0.65)
    caller_budget    = int(total_budget * 0.20)
    expansion_budget = int(total_budget * 0.15)

    # ── Phase 0: fire web research in background ─────────────────────────────
    web_research_task = None
    if web_research:
        from src.planning.web_researcher import research_implementation
        logger.info("planning retriever: starting web research (background)")
        web_research_task = asyncio.create_task(research_implementation(query))

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
    caller_ctx_text = await _get_caller_contexts(
        top_symbols, caller_budget, repo_owner, repo_name
    )

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

    # ── Phase 6: collect web research notes ─────────────────────────────────
    web_research_notes = ""
    if web_research_task is not None:
        try:
            web_research_notes = await web_research_task
            if web_research_notes:
                logger.info("planning retriever: web research complete (%d chars)", len(web_research_notes))
            else:
                logger.info("planning retriever: web research returned empty (continuing)")
        except Exception as exc:
            logger.warning("planning retriever: web research task failed: %s", exc)

    retrieval_log = (
        f"{primary_ctx.retrieval_log}\n"
        f"file_maps: {len(top_files)} files\n"
        f"caller_context_symbols: {top_symbols}\n"
        f"expansion_chunks: {len(expansion_results)}\n"
        f"web_research: {'yes' if web_research_notes else 'no'}"
    )

    logger.info(
        "planning retriever done: %d chunks, %d tokens, web_research=%s",
        len(primary_ctx.chunks_used),
        primary_ctx.tokens_used,
        bool(web_research_notes),
    )

    return PlanningContext(
        primary_context=primary_ctx.context_text,
        file_maps=file_maps,
        caller_contexts=caller_ctx_text,
        expansion_context=expansion_text,
        web_research_notes=web_research_notes,
        chunks_used=primary_ctx.chunks_used,
        tokens_used=primary_ctx.tokens_used,
        retrieval_log=retrieval_log,
    )


# ── Phase 4 helper: file structure maps ───────────────────────────────────────

async def _get_file_structure_maps(
    file_paths: list[str],
    repo_owner: Optional[str],
    repo_name: Optional[str],
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
    repo_owner: Optional[str],
    repo_name: Optional[str],
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
                r for r in results
                if any(
                    sym in line and not line.strip().startswith(
                        ("def ", "class ", "function ", "const ", "export ", "async def ")
                    )
                    for line in r.raw_content.split("\n")
                )
            ][:3]

            if callers:
                block = f"## Callers of `{sym}`\n"
                for r in callers:
                    block += (
                        f"  {r.file_path}  L{r.start_line}-{r.end_line}\n"
                        f"  {r.raw_content[:300]}\n"
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
