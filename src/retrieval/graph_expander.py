"""
Graph-Augmented Context Expansion.

After an initial vector/keyword search, this module expands the result set by
traversing the knowledge-graph edges (CALLS, IMPORTS) stored in kg_edges.

Why this matters for accuracy
──────────────────────────────
Vector similarity finds "semantically similar" code, but misses structural
relationships.  When the query matches function A, and A calls function B,
the LLM should ALSO see B's code.  Without graph expansion:
  • Dependency code is omitted → hallucinated implementations
  • Callers/callees are missing → incomplete refactoring plans
  • Import chain context is lost → wrong module assumptions

Algorithm
─────────
1. Extract symbol names + file paths from the top-N retrieved chunks.
2. One SQL query: find all 1-hop neighbors in kg_edges via CALLS + IMPORTS.
   • CALLS forward  (what does this symbol call?)
   • CALLS backward (what calls this symbol?)
   • IMPORTS forward (what files does this file import?)
3. Join against chunks to get full chunk content.
4. Return new SearchResult objects (excluding already-retrieved IDs).

The caller (retriever / tool executor) merges these with the primary results,
re-reranks, and assembles within the token budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

if TYPE_CHECKING:
    from src.retrieval.searcher import SearchResult

logger = get_secure_logger(__name__)


async def expand_with_graph(
    results: list[SearchResult],
    repo_owner: str | None,
    repo_name: str | None,
    max_neighbors: int = 25,
    hop_types: tuple[str, ...] = ("calls", "imports"),
) -> list[SearchResult]:
    """
    Expand search results by 1 hop in the knowledge graph.

    For each retrieved symbol, finds:
      • callee chunks  — functions/methods this symbol calls
      • caller chunks  — functions/methods that call this symbol
      • imported files — files imported by the top retrieved files

    Returns NEW SearchResult objects not already in `results`.
    Scores are set from edge confidence (0.5–0.8) so the reranker
    can properly re-sort the merged set.

    Args:
        results:       Initial search results (already vector/keyword ranked).
        repo_owner:    Scope filter — None means cross-repo (uses first result's repo).
        repo_name:     Scope filter.
        max_neighbors: Hard cap on graph-expanded chunks returned.
        hop_types:     Which edge types to traverse.
    """
    if not results:
        return []

    # Use the top-12 results for graph expansion (diminishing returns beyond that)
    top = results[:12]

    top_symbols: list[str] = [r.symbol_name for r in top if r.symbol_name]
    top_files: list[str] = list(dict.fromkeys(r.file_path for r in top))
    existing_ids: set[str] = {r.chunk_id for r in results}

    if not top_symbols and not top_files:
        return []

    # Infer repo scope from results if not provided
    effective_owner = repo_owner or (top[0].repo_owner if top else None)
    effective_name = repo_name or (top[0].repo_name if top else None)

    params: dict = {
        "symbols": top_symbols or ["__none__"],
        "files": top_files or ["__none__"],
        "limit": max_neighbors,
    }
    repo_join_filter = ""
    if effective_owner:
        params["owner"] = effective_owner
        repo_join_filter += " AND c.repo_owner = :owner"
    if effective_name:
        params["name"] = effective_name
        repo_join_filter += " AND c.repo_name = :name"

    # Build the edge-type filter
    hop_type_list = list(hop_types)

    # One query: traverse CALLS (both directions) + IMPORTS edges
    # and join to chunks to get full content.
    sql = text(f"""
        SELECT DISTINCT
            c.id           AS chunk_id,
            c.file_path,
            c.repo_owner,
            c.repo_name,
            c.language,
            c.symbol_name,
            c.symbol_kind,
            c.scope_chain,
            c.start_line,
            c.end_line,
            c.raw_content,
            c.enriched_content,
            c.commit_sha,
            c.commit_author,
            c.token_count,
            e.confidence   AS edge_confidence,
            e.edge_type    AS edge_type
        FROM kg_edges e
        JOIN chunks c
          ON (
            -- CALLS forward: fetch callee chunks (what our symbols call)
            (   e.edge_type = 'calls'
            AND c.symbol_name = e.target_id
            AND e.source_id = ANY(:symbols)
            )
            OR
            -- CALLS backward: fetch caller chunks (what calls our symbols)
            (   e.edge_type = 'calls'
            AND c.symbol_name = e.source_id
            AND e.target_id = ANY(:symbols)
            )
            OR
            -- IMPORTS: fetch chunks from files our top files import
            (   e.edge_type = 'imports'
            AND c.file_path = e.target_id
            AND e.source_id = ANY(:files)
            )
          )
         AND c.is_deleted = FALSE
         AND c.symbol_kind IN ('function', 'method', 'class', 'file_summary')
         {repo_join_filter}
        ORDER BY e.confidence DESC, c.start_line
        LIMIT :limit
    """)

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(sql, params)).mappings().all()
    except Exception as exc:
        logger.warning("graph_expander: query failed: %s", exc)
        return []

    from src.retrieval.searcher import SearchResult

    new_results: list[SearchResult] = []
    for row in rows:
        cid = row["chunk_id"]
        if cid in existing_ids:
            continue
        existing_ids.add(cid)

        # Score: use edge confidence as a proxy for relevance.
        # CALLS edges have confidence 0.8 (AST-extracted); IMPORTS have 1.0.
        # We set a modest base score so these rank behind the primary results
        # but still get considered by the reranker.
        edge_score = float(row.get("edge_confidence") or 0.7)

        new_results.append(
            SearchResult(
                chunk_id=cid,
                file_path=row["file_path"],
                repo_owner=row["repo_owner"],
                repo_name=row["repo_name"],
                language=row["language"] or "",
                symbol_name=row.get("symbol_name"),
                symbol_kind=row.get("symbol_kind"),
                scope_chain=row.get("scope_chain"),
                start_line=row["start_line"] or 0,
                end_line=row["end_line"] or 0,
                raw_content=row["raw_content"] or "",
                enriched_content=row.get("enriched_content") or "",
                commit_sha=row.get("commit_sha") or "",
                commit_author=row.get("commit_author"),
                token_count=row.get("token_count") or 0,
                score=edge_score,
            )
        )

    logger.debug(
        "graph_expander: %d primary + %d graph-expanded chunks",
        len(results),
        len(new_results),
    )
    return new_results
