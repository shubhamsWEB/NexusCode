"""
Context retrieval pipeline for implementation planning.

Phase 0a — extract stack fingerprint (fast DB query: dep files + aggregated imports)
Phase 0b — fire stack-aware web research as a background asyncio task
Phase 1  — embed the query with voyage-code-2
Phase 1b — query decomposition: split complex queries into sub-queries
Phase 2  — hybrid search → adaptive candidates (scaled to codebase size)
Phase 3  — cross-encoder rerank → adaptive top-N
Phase 4  — file structure maps for the top unique files
Phase 5  — caller context for the top unique symbols
           + import-chain following (dependency-aware context)
           + second semantic pass using discovered symbol names
Phase 6  — collect web research notes (awaits the Phase-0b task)
Phase 7  — grounding validation (verify context sufficiency)

The assembled context uses adaptive token budgets:
  - Simple queries:  base budget (10K)
  - Complex queries: scaled up to max budget (30K)
  - Improvement queries: 60% allocated to full component source

Supports:
  - Monorepos (path-prefix scoping within a single repo)
  - Multi-repo (cross-repo dependency tracing)
  - Micro-frontends (follows package.json / import maps)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.config import settings

logger = logging.getLogger(__name__)


def _escape_ilike(value: str) -> str:
    """Escape ILIKE special characters to prevent wildcard injection."""
    return value.replace("%", r"\%").replace("_", r"\_")


# ── Output dataclass ──────────────────────────────────────────────────────────


@dataclass
class PlanningContext:
    primary_context: str  # formatted code chunks (phases 2-3)
    file_maps: str  # structural file summaries (phase 4)
    caller_contexts: str  # call-site context (phase 5)
    expansion_context: str  # second-pass symbol context
    component_context: str  # full component files for improve/analysis queries
    dependency_context: str  # import-chain followed context
    stack_fingerprint: str  # phase 0a — installed packages, language, framework
    web_research_notes: str  # phase 0b — gap-focused web research (may be "")
    is_improvement_query: bool  # True → query is about improving/reviewing something
    query_complexity: str  # "simple", "moderate", "complex"
    sub_queries: list[str]  # decomposed sub-queries for complex tasks
    chunks_used: list[dict]  # chunk metadata for telemetry
    tokens_used: int
    grounding_warnings: list[str]  # any gaps detected in context
    retrieval_log: str


# ── Query complexity analysis ────────────────────────────────────────────────


@dataclass
class QueryAnalysis:
    """Analysis of a planning query to determine retrieval strategy."""
    complexity: str  # "simple", "moderate", "complex"
    sub_queries: list[str]  # decomposed queries for multi-concern tasks
    is_improvement: bool
    is_cross_cutting: bool  # touches many modules (e.g. "add auth to all endpoints")
    mentioned_paths: list[str]  # explicit file/dir paths mentioned in query
    mentioned_symbols: list[str]  # explicit function/class names in query


# Patterns that suggest cross-cutting changes
_CROSS_CUTTING_PATTERNS = (
    "all endpoints", "every endpoint", "all routes", "every route",
    "all files", "every file", "across the codebase", "everywhere",
    "all services", "every service", "all handlers", "every handler",
    "global", "system-wide", "application-wide", "codebase-wide",
)

_IMPROVEMENT_PATTERNS = (
    "how can i", "how to improve", "how do i improve", "how to make",
    "make it better", "make the", "make this", "make /",
    "improve", "enhance", "optimize", "optimise", "review", "audit",
    "refactor", "redesign", "rethink", "restructure",
    "what's wrong", "whats wrong", "what are the issues",
    "what are the weaknesses", "what are the problems",
    "what could be better", "what can be improved",
    "world class", "production ready", "better response",
    "better quality", "response quality", "context aware",
    "smarter", "more accurate",
)

# Regex to detect explicit file paths in queries
_PATH_PATTERN = re.compile(
    r'(?:^|\s)(?:src/|lib/|app/|pkg/|internal/|cmd/)[\w/\-\.]+',
    re.MULTILINE,
)

# Regex to detect symbol references (CamelCase or snake_case with parens)
_SYMBOL_PATTERN = re.compile(
    r'\b(?:[A-Z][a-zA-Z0-9]+(?:\.[a-z_]\w+)?|[a-z_]\w+(?:_[a-z]\w+)+)\b(?=\s*\()?'
)


def _analyze_query(query: str) -> QueryAnalysis:
    """
    Analyze a query to determine its complexity and decompose it.

    Complexity levels:
    - simple: single concern, <30 words (e.g. "what does X do?")
    - moderate: 2-3 concerns or touches 2-3 files (e.g. "add rate limiting to the API")
    - complex: 4+ concerns, cross-cutting, or monorepo-scale (e.g. "add auth to all endpoints")
    """
    q = query.lower().strip()
    words = q.split()
    word_count = len(words)

    is_improvement = any(p in q for p in _IMPROVEMENT_PATTERNS)
    is_cross_cutting = any(p in q for p in _CROSS_CUTTING_PATTERNS)

    # Extract mentioned paths and symbols
    mentioned_paths = [m.strip() for m in _PATH_PATTERN.findall(query)]
    mentioned_symbols = [m for m in _SYMBOL_PATTERN.findall(query) if len(m) > 3]

    # Count concerns (heuristic: conjunctions, commas, "and also", numbered items)
    concern_count = 1
    concern_count += q.count(" and ")
    concern_count += q.count(", ")
    concern_count += len(re.findall(r'\d+\.\s', query))  # numbered items

    # Determine complexity
    if is_cross_cutting or concern_count >= 4 or word_count > 80:
        complexity = "complex"
    elif concern_count >= 2 or word_count > 40 or len(mentioned_paths) >= 2:
        complexity = "moderate"
    else:
        complexity = "simple"

    # Decompose complex queries into sub-queries
    sub_queries = _decompose_query(query, complexity)

    return QueryAnalysis(
        complexity=complexity,
        sub_queries=sub_queries,
        is_improvement=is_improvement,
        is_cross_cutting=is_cross_cutting,
        mentioned_paths=mentioned_paths,
        mentioned_symbols=mentioned_symbols,
    )


def _decompose_query(query: str, complexity: str) -> list[str]:
    """
    Break a complex query into focused sub-queries for better retrieval.

    For simple queries, returns [query] unchanged.
    For complex queries, splits on natural boundaries (and, commas, numbered items).
    """
    if complexity == "simple":
        return [query]

    # Try splitting on "and also", "and then", numbered items
    parts: list[str] = []

    # Split on explicit numbered items: "1. do X  2. do Y"
    numbered = re.split(r'\d+\.\s+', query)
    if len(numbered) > 2:
        parts = [p.strip() for p in numbered if p.strip() and len(p.strip()) > 10]
        if parts:
            return parts

    # Split on " and " or ". " but only if segments are meaningful
    segments = re.split(r'\s+and\s+|\.\s+', query)
    meaningful = [s.strip() for s in segments if len(s.strip()) > 15]
    if len(meaningful) >= 2:
        return meaningful

    # Can't decompose meaningfully — return as-is
    return [query]


# ── Adaptive budget calculation ───────────────────────────────────────────────


def _compute_budgets(analysis: QueryAnalysis) -> dict:
    """
    Compute adaptive token budgets based on query complexity.

    Simple queries get the base budget; complex queries scale up.
    """
    base = settings.planning_context_budget
    max_budget = settings.planning_max_context_budget

    if analysis.complexity == "complex":
        total = max_budget
    elif analysis.complexity == "moderate":
        total = int(base + (max_budget - base) * 0.5)
    else:
        total = base

    if analysis.is_improvement:
        return {
            "total": total,
            "primary": int(total * 0.20),
            "component": int(total * 0.55),
            "caller": int(total * 0.10),
            "expansion": int(total * 0.10),
            "dependency": int(total * 0.05),
        }
    else:
        return {
            "total": total,
            "primary": int(total * 0.55),
            "component": 0,
            "caller": int(total * 0.15),
            "expansion": int(total * 0.15),
            "dependency": int(total * 0.15),
        }


def _compute_candidates(analysis: QueryAnalysis, codebase_size: int) -> dict:
    """
    Scale candidate counts based on query complexity and codebase size.

    codebase_size: approximate number of active chunks in the repo.
    """
    base_candidates = settings.planning_candidate_base
    max_candidates = settings.planning_candidate_max
    base_rerank = settings.planning_rerank_base
    max_rerank = settings.planning_rerank_max

    # Scale factor: 1.0 for small repos, up to 2.0 for large ones
    if codebase_size > 5000:
        size_factor = 2.0
    elif codebase_size > 1000:
        size_factor = 1.5
    else:
        size_factor = 1.0

    complexity_factor = {"simple": 1.0, "moderate": 1.5, "complex": 2.0}[analysis.complexity]

    combined = min(size_factor * complexity_factor, 2.5)  # cap at 2.5x

    candidates = min(int(base_candidates * combined), max_candidates)
    rerank_n = min(int(base_rerank * combined), max_rerank)

    return {"candidates": candidates, "rerank_top_n": rerank_n}


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

    Improvements over v1:
    - Adaptive token budgets scaled to query complexity
    - Query decomposition for multi-concern tasks
    - Import-chain following for dependency context
    - Codebase-size-aware candidate scaling
    - Post-retrieval grounding validation
    """
    import asyncio

    from src.retrieval.assembler import assemble
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import embed_queries_batch, embed_query, search

    # ── Phase 0: analyze query + extract stack fingerprint ──────────────
    analysis = _analyze_query(query)
    logger.info(
        "planning retriever: query analysis — complexity=%s, improvement=%s, "
        "cross_cutting=%s, sub_queries=%d, mentioned_paths=%s",
        analysis.complexity,
        analysis.is_improvement,
        analysis.is_cross_cutting,
        len(analysis.sub_queries),
        analysis.mentioned_paths or "none",
    )

    stack_fingerprint = await _extract_stack_fingerprint(repo_owner, repo_name)

    # Get codebase size for candidate scaling
    codebase_size = await _get_codebase_size(repo_owner, repo_name)
    logger.info("planning retriever: codebase size = %d chunks", codebase_size)

    budgets = _compute_budgets(analysis)
    candidate_config = _compute_candidates(analysis, codebase_size)
    logger.info(
        "planning retriever: budgets=%s, candidates=%d, rerank_top_n=%d",
        {k: v for k, v in budgets.items() if v > 0},
        candidate_config["candidates"],
        candidate_config["rerank_top_n"],
    )

    # ── Phase 0b: web research (background) ─────────────────────────────
    web_research_task = None
    effective_web_research = web_research and not analysis.is_improvement
    if effective_web_research:
        from src.planning.web_researcher import research_implementation

        logger.info("planning retriever: starting stack-aware web research (background)")
        web_research_task = asyncio.create_task(
            research_implementation(query, stack_context=stack_fingerprint)
        )
    elif analysis.is_improvement and web_research:
        logger.info(
            "planning retriever: skipping web research for improvement query"
        )

    # ── Phase 1: embed query ────────────────────────────────────────────
    logger.info("planning retriever: embedding query")
    query_vector = await embed_query(query)

    # ── Phase 1b: embed sub-queries in a single batch call ──────────────
    # Use embed_queries_batch to send all sub-queries to Voyage in one request
    # instead of N separate API calls — saves latency and rate-limit budget.
    sub_query_vectors: dict[str, list[float]] = {}
    distinct_sub_queries = [sq for sq in analysis.sub_queries if sq != query]
    if distinct_sub_queries:
        try:
            batch_vectors = await embed_queries_batch(distinct_sub_queries)
            for sq, vec in zip(distinct_sub_queries, batch_vectors):
                sub_query_vectors[sq] = vec
        except Exception as exc:
            logger.warning("sub-query batch embedding failed: %s", exc)

    # ── Phase 2: hybrid search (adaptive candidates) ────────────────────
    num_candidates = candidate_config["candidates"]
    logger.info("planning retriever: hybrid search (%d candidates)", num_candidates)

    # Primary search
    candidates = await search(
        query=query,
        query_vector=query_vector,
        top_k=num_candidates,
        mode="hybrid",
        repo_owner=repo_owner,
        repo_name=repo_name,
    )

    # Sub-query searches (parallel) — merge results
    if sub_query_vectors:
        async def _sub_search(sq: str, sv: list[float]):
            return await search(
                query=sq, query_vector=sv, top_k=num_candidates // 2,
                mode="hybrid", repo_owner=repo_owner, repo_name=repo_name,
            )

        sub_search_tasks = [_sub_search(sq, sv) for sq, sv in sub_query_vectors.items()]
        sub_search_results = await asyncio.gather(*sub_search_tasks, return_exceptions=True)

        seen_ids = {r.chunk_id for r in candidates}
        for result in sub_search_results:
            if isinstance(result, Exception):
                logger.warning("sub-query search failed: %s", result)
                continue
            for r in result:
                if r.chunk_id not in seen_ids:
                    seen_ids.add(r.chunk_id)
                    candidates.append(r)

    # ── Mentioned-path boosting ─────────────────────────────────────────
    # If the user explicitly mentioned file paths, ensure those files appear
    if analysis.mentioned_paths:
        candidates = await _boost_mentioned_paths(
            candidates, analysis.mentioned_paths, repo_owner, repo_name
        )

    # ── Phase 3: cross-encoder rerank → adaptive top-N ──────────────────
    rerank_n = candidate_config["rerank_top_n"]
    if candidates:
        logger.info("planning retriever: reranking %d → top %d", len(candidates), rerank_n)
        candidates = rerank(query, candidates, top_n=rerank_n)

    # ── Phase 4: file structure maps ────────────────────────────────────
    file_limit = min(8, 5 + len(analysis.sub_queries))  # more files for complex queries
    top_files = _unique_paths(candidates, limit=file_limit)
    file_maps = await _get_file_structure_maps(top_files, repo_owner, repo_name)

    # ── Phase 5: caller context ─────────────────────────────────────────
    top_symbols = [r.symbol_name for r in candidates[:8] if r.symbol_name]
    top_symbols = list(dict.fromkeys(top_symbols))[:5]  # deduplicate, keep order

    # Include any explicitly-mentioned symbols
    for sym in analysis.mentioned_symbols:
        if sym not in top_symbols:
            top_symbols.append(sym)

    caller_ctx_text = await _get_caller_contexts(
        top_symbols[:5], budgets["caller"], repo_owner, repo_name
    )

    # ── Phase 5b: import-chain following (dependency context) ───────────
    dependency_context = ""
    if budgets["dependency"] > 0 and top_files:
        dependency_context = await _follow_import_chains(
            file_paths=top_files[:5],
            repo_owner=repo_owner,
            repo_name=repo_name,
            token_budget=budgets["dependency"],
            max_depth=settings.planning_import_depth,
        )
        if dependency_context:
            logger.info(
                "planning retriever: dependency context loaded (%d chars)",
                len(dependency_context),
            )

    # ── Second semantic pass using discovered symbols (parallel) ─────────
    expansion_results = []
    seen_ids = {r.chunk_id for r in candidates}

    async def _expand_symbol(sym: str):
        sym_vector = await embed_query(sym)
        return await search(
            query=sym, query_vector=sym_vector, top_k=5,
            mode="hybrid", repo_owner=repo_owner, repo_name=repo_name,
        )

    expand_symbols = top_symbols[:3]  # more expansion for complex queries
    if expand_symbols:
        expansion_tasks = [_expand_symbol(sym) for sym in expand_symbols]
        expansion_task_results = await asyncio.gather(*expansion_tasks, return_exceptions=True)
        for i, result in enumerate(expansion_task_results):
            if isinstance(result, Exception):
                logger.warning("expansion search failed for symbol %r: %s", expand_symbols[i], result)
                continue
            for r in result:
                if r.chunk_id not in seen_ids:
                    seen_ids.add(r.chunk_id)
                    expansion_results.append(r)

    # ── Assemble context strings ────────────────────────────────────────
    primary_ctx = assemble(candidates, token_budget=budgets["primary"], query=query)

    if expansion_results:
        expansion_ctx = assemble(expansion_results, token_budget=budgets["expansion"], query=query)
        expansion_text = expansion_ctx.context_text
    else:
        expansion_text = ""

    # ── Phase 5.5: component-aware full retrieval (improvement queries) ──
    component_context = ""
    if analysis.is_improvement and budgets["component"] > 0:
        component_context = await _fetch_component_context(
            query=query,
            candidates=candidates,
            repo_owner=repo_owner,
            repo_name=repo_name,
            token_budget=budgets["component"],
        )
        if component_context:
            logger.info(
                "planning retriever: component context loaded (%d chars)",
                len(component_context),
            )

    # ── Phase 6: collect web research notes ─────────────────────────────
    web_research_notes = ""
    if web_research_task is not None:
        try:
            web_research_notes = await web_research_task
            if web_research_notes:
                logger.info(
                    "planning retriever: web research complete (%d chars)",
                    len(web_research_notes),
                )
        except Exception as exc:
            logger.warning("planning retriever: web research task failed: %s", exc)

    # ── Phase 7: grounding validation ───────────────────────────────────
    grounding_warnings = _validate_grounding(
        query=query,
        analysis=analysis,
        candidates=candidates,
        top_files=top_files,
        component_context=component_context,
    )
    if grounding_warnings:
        logger.warning(
            "planning retriever: grounding warnings: %s",
            "; ".join(grounding_warnings),
        )

    retrieval_log = (
        f"{primary_ctx.retrieval_log}\n"
        f"query_complexity: {analysis.complexity}\n"
        f"sub_queries: {len(analysis.sub_queries)}\n"
        f"codebase_size: {codebase_size} chunks\n"
        f"candidates_searched: {num_candidates}\n"
        f"rerank_top_n: {rerank_n}\n"
        f"file_maps: {len(top_files)} files\n"
        f"caller_context_symbols: {top_symbols}\n"
        f"expansion_chunks: {len(expansion_results)}\n"
        f"dependency_context: {'yes (' + str(len(dependency_context)) + ' chars)' if dependency_context else 'no'}\n"
        f"component_context: {'yes (' + str(len(component_context)) + ' chars)' if component_context else 'no'}\n"
        f"is_improvement_query: {analysis.is_improvement}\n"
        f"stack_fingerprint: {'yes' if stack_fingerprint else 'no'}\n"
        f"web_research: {'yes' if web_research_notes else 'no (skipped for improvement query)' if analysis.is_improvement else 'no'}\n"
        f"grounding_warnings: {grounding_warnings or 'none'}"
    )

    logger.info(
        "planning retriever done: %d chunks, %d tokens, complexity=%s, improvement=%s",
        len(primary_ctx.chunks_used),
        primary_ctx.tokens_used,
        analysis.complexity,
        analysis.is_improvement,
    )

    return PlanningContext(
        primary_context=primary_ctx.context_text,
        file_maps=file_maps,
        caller_contexts=caller_ctx_text,
        expansion_context=expansion_text,
        component_context=component_context,
        dependency_context=dependency_context,
        stack_fingerprint=stack_fingerprint,
        web_research_notes=web_research_notes,
        is_improvement_query=analysis.is_improvement,
        query_complexity=analysis.complexity,
        sub_queries=analysis.sub_queries,
        chunks_used=primary_ctx.chunks_used,
        tokens_used=primary_ctx.tokens_used,
        grounding_warnings=grounding_warnings,
        retrieval_log=retrieval_log,
    )


# ── Codebase size estimation ────────────────────────────────────────────────


async def _get_codebase_size(
    repo_owner: str | None,
    repo_name: str | None,
) -> int:
    """Get the approximate number of active chunks in the target repo(s)."""
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

    sql = text(f"""
        SELECT count(*) AS cnt
        FROM chunks
        WHERE NOT is_deleted
          {repo_filter}
    """)

    try:
        async with AsyncSessionLocal() as session:
            result = (await session.execute(sql, params)).scalar()
            return result or 0
    except Exception as exc:
        logger.warning("codebase size query failed: %s", exc)
        return 0


# ── Mentioned-path boosting ──────────────────────────────────────────────────


async def _boost_mentioned_paths(
    candidates: list,
    mentioned_paths: list[str],
    repo_owner: str | None,
    repo_name: str | None,
) -> list:
    """
    Ensure chunks from explicitly-mentioned file paths appear in results.

    If the user says "fix the bug in src/pipeline/pipeline.py", we MUST
    include that file even if semantic search didn't rank it highly.
    """
    from sqlalchemy import text

    from src.storage.db import AsyncSessionLocal

    existing_paths = {r.file_path for r in candidates}
    missing_paths = [p for p in mentioned_paths if not any(p in ep for ep in existing_paths)]

    if not missing_paths:
        return candidates

    logger.info("mentioned-path boost: adding %d paths not in results", len(missing_paths))

    params: dict = {}
    repo_filter = ""
    if repo_owner:
        repo_filter += " AND repo_owner = :repo_owner"
        params["repo_owner"] = repo_owner
    if repo_name:
        repo_filter += " AND repo_name = :repo_name"
        params["repo_name"] = repo_name

    # Build path ILIKE conditions
    path_conditions = []
    for i, path in enumerate(missing_paths[:5]):
        params[f"boost_path_{i}"] = f"%{_escape_ilike(path)}%"
        path_conditions.append(f"file_path ILIKE :boost_path_{i}")

    where = " OR ".join(path_conditions)
    sql = text(f"""
        SELECT id, file_path, repo_owner, repo_name, language,
               symbol_name, symbol_kind, scope_chain,
               start_line, end_line, raw_content, enriched_content,
               commit_sha, commit_author, token_count,
               0.5 AS score
        FROM chunks
        WHERE NOT is_deleted
          AND ({where})
          {repo_filter}
        ORDER BY start_line
        LIMIT 20
    """)

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(sql, params)).mappings().all()

        from src.retrieval.searcher import SearchResult

        seen_ids = {r.chunk_id for r in candidates}
        boosted = []
        for row in rows:
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                boosted.append(SearchResult(
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
                    score=0.5,
                ))

        # Insert boosted results near the top (after first 3 semantic results)
        if boosted:
            return candidates[:3] + boosted + candidates[3:]

    except Exception as exc:
        logger.warning("mentioned-path boost failed: %s", exc)

    return candidates


# ── Import-chain following ────────────────────────────────────────────────────


async def _follow_import_chains(
    file_paths: list[str],
    repo_owner: str | None,
    repo_name: str | None,
    token_budget: int,
    max_depth: int = 2,
) -> str:
    """
    Follow import statements from the top files to find dependency context.

    For each top file, extract its imports, resolve them to file paths,
    and fetch those file's structure maps. This gives Claude visibility
    into the interfaces of dependencies without loading full file content.

    Works across repos if import paths reference other indexed repos.
    """
    from sqlalchemy import text

    from src.pipeline.chunker import count_tokens
    from src.storage.db import AsyncSessionLocal

    if not file_paths:
        return ""

    params: dict = {}
    repo_filter = ""
    if repo_owner:
        repo_filter += " AND repo_owner = :repo_owner"
        params["repo_owner"] = repo_owner
    if repo_name:
        repo_filter += " AND repo_name = :repo_name"
        params["repo_name"] = repo_name

    # Step 1: Get imports from the top files
    file_placeholders = ", ".join(f":dep_file_{i}" for i in range(len(file_paths)))
    for i, fp in enumerate(file_paths):
        params[f"dep_file_{i}"] = fp

    import_sql = text(f"""
        SELECT DISTINCT file_path, unnest(imports) AS import_stmt
        FROM chunks
        WHERE file_path IN ({file_placeholders})
          AND NOT is_deleted
          AND array_length(imports, 1) > 0
          {repo_filter}
    """)

    try:
        async with AsyncSessionLocal() as session:
            import_rows = (await session.execute(import_sql, params)).mappings().all()
    except Exception as exc:
        logger.warning("import chain: failed to fetch imports: %s", exc)
        return ""

    if not import_rows:
        return ""

    # Step 2: Resolve import statements to file paths
    imported_modules = set()
    for row in import_rows:
        stmt = row["import_stmt"].strip()
        resolved = _resolve_import_to_path(stmt)
        if resolved:
            imported_modules.add(resolved)

    # Remove files already in the top files list
    imported_modules -= set(file_paths)

    if not imported_modules:
        return ""

    # Step 3: Find matching files in the index and get their symbols
    # Use ILIKE with the resolved paths to match partial paths
    dep_params: dict = {}
    dep_repo_filter = ""
    if repo_owner:
        dep_repo_filter += " AND s.repo_owner = :dep_repo_owner"
        dep_params["dep_repo_owner"] = repo_owner
    if repo_name:
        dep_repo_filter += " AND s.repo_name = :dep_repo_name"
        dep_params["dep_repo_name"] = repo_name

    path_conditions = []
    for i, mod in enumerate(list(imported_modules)[:15]):
        dep_params[f"mod_{i}"] = f"%{_escape_ilike(mod)}%"
        path_conditions.append(f"s.file_path ILIKE :mod_{i}")

    if not path_conditions:
        return ""

    path_where = " OR ".join(path_conditions)
    sym_sql = text(f"""
        SELECT s.file_path, s.name, s.qualified_name, s.kind,
               s.start_line, s.end_line, s.signature, s.docstring
        FROM symbols s
        WHERE ({path_where})
          {dep_repo_filter}
        ORDER BY s.file_path, s.start_line
        LIMIT 60
    """)

    try:
        async with AsyncSessionLocal() as session:
            sym_rows = (await session.execute(sym_sql, dep_params)).mappings().all()
    except Exception as exc:
        logger.warning("import chain: symbol query failed: %s", exc)
        return ""

    if not sym_rows:
        return ""

    # Step 4: Format dependency context
    sections: list[str] = []
    tokens_used = 0
    current_file = ""

    for row in sym_rows:
        if row["file_path"] != current_file:
            current_file = row["file_path"]
            header = f"\n### {current_file} (dependency)"
            header_tokens = count_tokens(header)
            if tokens_used + header_tokens > token_budget:
                break
            sections.append(header)
            tokens_used += header_tokens

        sig_line = f"  {row['kind']:12s} {row['qualified_name']}  (L{row['start_line']}-{row['end_line']})"
        if row["signature"]:
            sig_line += f"\n             sig: {row['signature'][:120]}"
        if row["docstring"]:
            doc = row["docstring"][:100].replace("\n", " ")
            sig_line += f"\n             doc: {doc}"

        line_tokens = count_tokens(sig_line)
        if tokens_used + line_tokens > token_budget:
            break
        sections.append(sig_line)
        tokens_used += line_tokens

    if not sections:
        return ""

    dep_files = len({row["file_path"] for row in sym_rows})
    return (
        f"## Dependency Interfaces ({dep_files} imported files)\n"
        "_Symbol signatures from files imported by the top relevant files._\n"
        + "\n".join(sections)
    )


def _resolve_import_to_path(import_stmt: str) -> str | None:
    """
    Best-effort resolution of an import statement to a file path fragment.

    Examples:
      "from src.config import settings" → "src/config"
      "import src.pipeline.pipeline" → "src/pipeline/pipeline"
      "from .schemas import X" → None (relative — can't resolve without context)
      "import React from 'react'" → None (external package)
    """
    stmt = import_stmt.strip()

    # Python: from X.Y.Z import ... / import X.Y.Z
    py_match = re.match(r'(?:from\s+)([\w\.]+)(?:\s+import)?', stmt)
    if py_match:
        module = py_match.group(1)
        if module.startswith("."):
            return None  # relative import
        # Skip stdlib/external packages (heuristic: if first segment is lowercase single word)
        parts = module.split(".")
        if len(parts) == 1 and parts[0].islower():
            return None  # likely stdlib
        return module.replace(".", "/")

    py_import = re.match(r'import\s+([\w\.]+)', stmt)
    if py_import:
        module = py_import.group(1)
        parts = module.split(".")
        if len(parts) == 1 and parts[0].islower():
            return None
        return module.replace(".", "/")

    # TypeScript/JS: import ... from 'X' / require('X')
    ts_match = re.match(r"(?:import|require)\s*\(?['\"]([^'\"]+)['\"]", stmt)
    if ts_match:
        path = ts_match.group(1)
        if path.startswith("."):
            return None  # relative
        if "/" not in path:
            return None  # npm package
        return path

    return None


# ── Grounding validation ──────────────────────────────────────────────────────


def _validate_grounding(
    query: str,
    analysis: QueryAnalysis,
    candidates: list,
    top_files: list[str],
    component_context: str,
) -> list[str]:
    """
    Post-retrieval validation to detect if the context is sufficient.

    Returns a list of warning strings that get injected into the planner prompt
    to prevent hallucination.
    """
    warnings: list[str] = []

    # Check 1: Did we find any results at all?
    if not candidates:
        warnings.append(
            "NO_RESULTS: No code context was found for this query. "
            "The response should clearly state this limitation."
        )
        return warnings

    # Check 2: Were explicitly mentioned paths found?
    if analysis.mentioned_paths:
        found_paths = {r.file_path for r in candidates}
        for path in analysis.mentioned_paths:
            if not any(path in fp for fp in found_paths):
                warnings.append(
                    f"MISSING_PATH: The query mentions '{path}' but no matching "
                    f"file was found in the index. Do not reference this file."
                )

    # Check 3: Were explicitly mentioned symbols found?
    if analysis.mentioned_symbols:
        found_symbols = {r.symbol_name for r in candidates if r.symbol_name}
        for sym in analysis.mentioned_symbols:
            if not any(sym.lower() in (fs or "").lower() for fs in found_symbols):
                # Check in raw content too
                in_content = any(sym in r.raw_content for r in candidates)
                if not in_content:
                    warnings.append(
                        f"MISSING_SYMBOL: The query mentions '{sym}' but it was not "
                        f"found in the retrieved context. Do not invent details about it."
                    )

    # Check 4: For complex cross-cutting queries, did we cover enough files?
    if analysis.is_cross_cutting and len(top_files) < 3:
        warnings.append(
            "LOW_COVERAGE: This is a cross-cutting query but only "
            f"{len(top_files)} files were found. The plan may miss affected areas."
        )

    # Check 5: Token coverage — did we use a reasonable portion of the budget?
    total_tokens = sum(r.token_count or 0 for r in candidates)
    if total_tokens < 500 and analysis.complexity != "simple":
        warnings.append(
            f"LOW_CONTEXT: Only {total_tokens} tokens of context retrieved for "
            "a non-simple query. Response quality may be limited."
        )

    return warnings


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

    Uses a two-pass approach:
    1. Calculate total tokens per file
    2. Include complete files that fit within budget (never show half a file)
    """
    if not candidates:
        return ""

    from sqlalchemy import text

    from src.pipeline.chunker import count_tokens
    from src.storage.db import AsyncSessionLocal

    # Use the top unique files from semantic candidates as the component scope
    component_files = _unique_paths(candidates, limit=8)  # increased from 5
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

    # Pass 1: group chunks by file and calculate per-file token cost
    from collections import defaultdict
    file_chunks: dict[str, list] = defaultdict(list)
    file_tokens: dict[str, int] = defaultdict(int)

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

        file_chunks[fp].append(chunk_text)
        file_tokens[fp] += chunk_tokens

    # Pass 2: include complete files that fit within budget
    sections: list[str] = []
    tokens_used = 0
    files_included = 0

    for fp in component_files:
        if fp not in file_chunks:
            continue
        ft = file_tokens[fp]
        if tokens_used + ft <= token_budget:
            sections.extend(file_chunks[fp])
            tokens_used += ft
            files_included += 1
        else:
            stub = (
                f"────────────────────────────────────────\n"
                f"File: {fp}  [skipped — {ft} tokens exceeds remaining budget]"
            )
            sections.append(stub)

    logger.debug(
        "component context: %d complete files, %d tokens",
        files_included, tokens_used,
    )

    if not sections:
        return ""

    return (
        f"## Full Component Source ({files_included} files, {tokens_used} tokens)\n"
        "_Complete source of the relevant component files for deep analysis._\n\n"
        + "\n\n".join(sections)
    )


# ── Phase 0a helper: stack fingerprint ────────────────────────────────────────


async def _extract_stack_fingerprint(
    repo_owner: str | None,
    repo_name: str | None,
) -> str:
    """
    Build a compact picture of what the codebase already has installed.
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

    if lang_rows:
        lang_summary = ", ".join(f"{r['language']} ({r['chunk_count']} chunks)" for r in lang_rows)
        parts.append(f"**Languages detected:** {lang_summary}")

    dep_sections: list[str] = []
    for row in dep_rows:
        fp = row["file_path"]
        content = row["raw_content"]
        lines = content.splitlines()[:60]
        dep_sections.append(f"### {fp}\n```\n" + "\n".join(lines) + "\n```")
    if dep_sections:
        parts.append("**Dependency files found:**\n\n" + "\n\n".join(dep_sections))

    if import_rows:
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
        "_What is already installed and actively used in this codebase._\n\n"
        + "\n\n".join(parts)
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
        params: dict = {"path": fpath, "path_like": f"%{_escape_ilike(fpath)}%"}
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
    """
    if not symbols:
        return ""

    from src.retrieval.searcher import _keyword_search

    all_sections: list[str] = []
    tokens_used = 0

    for sym in symbols:
        try:
            results = await _keyword_search(
                query=sym, limit=5,
                repo_owner=repo_owner, repo_name=repo_name, language=None,
            )
            callers = [
                r for r in results
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
