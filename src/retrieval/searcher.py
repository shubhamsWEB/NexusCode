"""
Hybrid search: pgvector cosine + tsvector keyword + Reciprocal Rank Fusion.

Three modes:
  "semantic"  — vector cosine similarity only
  "keyword"   — tsvector full-text + pg_trgm symbol name match only
  "hybrid"    — both lists merged via RRF, then cross-encoder reranked
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from src.config import settings
from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger
from src.utils.sanitize import sanitize_log

logger = get_secure_logger(__name__)


@dataclass
class SearchResult:
    chunk_id: str
    file_path: str
    repo_owner: str
    repo_name: str
    language: str
    symbol_name: str | None
    symbol_kind: str | None
    scope_chain: str | None
    start_line: int
    end_line: int
    raw_content: str
    enriched_content: str
    commit_sha: str
    commit_author: str | None
    token_count: int
    score: float  # final score (cosine, BM25, or RRF)
    parent_chunk_id: str | None = None
    rerank_score: float = 0.0  # set by reranker
    quality_score: float = 0.0  # sigmoid-normalized rerank_score (0.0-1.0)


# ── ef_search quality presets ─────────────────────────────────────────────────

_EF_PRESETS = {
    "fast":     lambda base: max(10, base // 2),
    "balanced": lambda base: base,
    "thorough": lambda base: min(200, base * 2),
}

# ── Public entry point ────────────────────────────────────────────────────────


async def search(
    query: str,
    query_vector: list[float],
    top_k: int = 5,
    mode: str = "hybrid",
    repo_owner: str | None = None,
    repo_name: str | None = None,
    language: str | None = None,
    hyde: bool = False,
    search_quality: str = "balanced",
) -> list[SearchResult]:
    """
    Run a search and return top_k results.

    query_vector must already be embedded by the caller (so the caller
    controls the embedding step and can cache it).
    """
    if hyde:
        from src.retrieval.hyde import hyde_search

        return await hyde_search(
            query=query,
            query_vector=query_vector,
            top_k=top_k,
            mode=mode,
            repo_owner=repo_owner,
            repo_name=repo_name,
            language=language,
        )

    # Check cache (skip for HyDE which is handled above)
    from src.retrieval.embed_cache import (
        get_cached_search_results,
        make_search_cache_key,
        set_cached_search_results,
    )

    cache_key = make_search_cache_key(query, repo_owner, repo_name, mode, language, top_k)
    cached = await get_cached_search_results(cache_key)
    if cached is not None:
        return [SearchResult(**r) for r in cached]

    candidates = top_k * settings.retrieval_candidate_multiplier

    if mode == "semantic":
        results = await _semantic_search(
            query_vector, candidates, repo_owner, repo_name, language, search_quality
        )

    elif mode == "keyword":
        results = await _keyword_search(query, candidates, repo_owner, repo_name, language)

    else:  # hybrid
        semantic = await _semantic_search(
            query_vector, candidates, repo_owner, repo_name, language, search_quality
        )
        keyword = await _keyword_search(query, candidates, repo_owner, repo_name, language)
        results = _reciprocal_rank_fusion(semantic, keyword)

    results = results[:top_k]

    if results:
        await set_cached_search_results(
            cache_key,
            [r.__dict__ for r in results],
            ttl=settings.search_result_cache_ttl,
        )

    return results


# ── Semantic search ───────────────────────────────────────────────────────────


async def _semantic_search(
    vector: list[float],
    limit: int,
    repo_owner: str | None,
    repo_name: str | None,
    language: str | None,
    search_quality: str = "balanced",
) -> list[SearchResult]:
    ef = _EF_PRESETS.get(search_quality, lambda b: b)(settings.hnsw_ef_search)
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
    where, params = _build_where(repo_owner, repo_name, language)
    params["limit"] = limit
    params["vec"] = vec_str

    sql = text(f"""
        SELECT
            id, file_path, repo_owner, repo_name, language,
            symbol_name, symbol_kind, scope_chain,
            start_line, end_line, raw_content, enriched_content,
            commit_sha, commit_author, token_count, parent_chunk_id,
            1 - (embedding <=> :vec ::vector) AS score
        FROM chunks
        WHERE {where}
          AND embedding IS NOT NULL
        ORDER BY embedding <=> :vec ::vector
        LIMIT :limit
    """)

    # SET LOCAL hnsw.ef_search — no-op if ivfflat is still in use
    pre_stmts = [text(f"SET LOCAL hnsw.ef_search = {ef}")]
    return await _execute_search(sql, params, pre_statements=pre_stmts)


# ── Keyword search ────────────────────────────────────────────────────────────


async def _keyword_search(
    query: str,
    limit: int,
    repo_owner: str | None,
    repo_name: str | None,
    language: str | None,
) -> list[SearchResult]:
    where, params = _build_where(repo_owner, repo_name, language)
    params.update({"query": query, "limit": limit})

    # Combine tsvector full-text rank + trigram symbol name similarity
    # Now searching enriched_content instead of raw_content
    ts_weight = settings.retrieval_keyword_tsvector_weight
    trgm_weight = settings.retrieval_keyword_trgm_weight
    sql = text(f"""
        SELECT
            id, file_path, repo_owner, repo_name, language,
            symbol_name, symbol_kind, scope_chain,
            start_line, end_line, raw_content, enriched_content,
            commit_sha, commit_author, token_count, parent_chunk_id,
            (
                ts_rank(to_tsvector('english', enriched_content),
                        plainto_tsquery('english', :query)) * {ts_weight}
                + COALESCE(similarity(symbol_name, :query), 0) * {trgm_weight}
            ) AS score
        FROM chunks
        WHERE {where}
          AND (
            to_tsvector('english', enriched_content) @@ plainto_tsquery('english', :query)
            OR symbol_name % :query
          )
        ORDER BY score DESC
        LIMIT :limit
    """)

    return await _execute_search(sql, params)


# ── RRF merge ────────────────────────────────────────────────────────────────


def _reciprocal_rank_fusion(
    *result_lists: list[SearchResult],
) -> list[SearchResult]:
    """
    Merge two ranked lists using Reciprocal Rank Fusion.
    Score = Σ 1/(k + rank_i) across all lists that contain the item.
    """
    # Build lookup: chunk_id → SearchResult (semantic takes priority for metadata)
    by_id: dict[str, SearchResult] = {}
    for lst in result_lists:
        for r in lst:
            if r.chunk_id not in by_id:
                by_id[r.chunk_id] = r

    rrf_scores: dict[str, float] = {}

    for lst in result_lists:
        for rank, result in enumerate(lst, 1):
            rrf_scores[result.chunk_id] = rrf_scores.get(result.chunk_id, 0) + 1 / (
                settings.retrieval_rrf_k + rank
            )

    sorted_ids = sorted(rrf_scores, key=rrf_scores.__getitem__, reverse=True)

    merged = []
    for chunk_id in sorted_ids:
        result = by_id[chunk_id]
        result.score = rrf_scores[chunk_id]
        merged.append(result)

    return merged


# ── Shared helpers ────────────────────────────────────────────────────────────


def _build_where(
    repo_owner: str | None,
    repo_name: str | None,
    language: str | None,
) -> tuple[str, dict]:
    clauses = ["is_deleted = FALSE"]
    params: dict[str, Any] = {}

    if repo_owner:
        clauses.append("repo_owner = :repo_owner")
        params["repo_owner"] = repo_owner
    if repo_name:
        clauses.append("repo_name = :repo_name")
        params["repo_name"] = repo_name
    if language:
        clauses.append("language = :language")
        params["language"] = language

    return " AND ".join(clauses), params


async def _execute_search(
    sql,
    params: dict,
    pre_statements: list | None = None,
) -> list[SearchResult]:
    async with AsyncSessionLocal() as session:
        if pre_statements:
            for stmt in pre_statements:
                await session.execute(stmt)
        rows = (await session.execute(sql, params)).mappings().all()

    results = []
    for row in rows:
        results.append(
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
                parent_chunk_id=row.get("parent_chunk_id"),
                score=float(row.get("score") or 0),
            )
        )
    return results


# ── Embed query (convenience wrapper) ────────────────────────────────────────


async def embed_query(query: str) -> list[float]:
    """Embed a search query using voyage-code-2 with input_type='query'."""
    from src.pipeline.embedder import _make_client
    from src.retrieval.embed_cache import get_cached_embedding, set_cached_embedding

    cached = await get_cached_embedding(query)
    if cached:
        return cached

    client = _make_client()
    result = await client.embed([query], model=settings.embedding_model, input_type="query")

    vec = result.embeddings[0]
    await set_cached_embedding(query, vec)
    return vec


async def embed_queries_batch(queries: list[str]) -> list[list[float]]:
    """
    Embed multiple search queries in a single Voyage AI API call.

    Batching N sub-queries into one call reduces round-trip overhead
    and avoids N-fold rate-limit pressure.  The Voyage API accepts up to
    128 texts per request; callers should not exceed that limit.

    Returns a list of embedding vectors in the same order as `queries`.
    Falls back to per-query calls if the batch request fails.
    """
    from src.pipeline.embedder import _make_client

    if not queries:
        return []

    client = _make_client()

    try:
        from src.retrieval.embed_cache import get_cached_embedding, set_cached_embedding

        # Check cache first
        cached_results = []
        missing_indices = []
        missing_queries = []

        for i, q in enumerate(queries):
            c = await get_cached_embedding(q)
            cached_results.append(c)  # None if missing
            if c is None:
                missing_indices.append(i)
                missing_queries.append(q)

        if not missing_queries:
            return cached_results

        result = await client.embed(
            missing_queries,
            model=settings.embedding_model,
            input_type="query",
        )

        # Merge back and cache
        for idx, q, vec in zip(missing_indices, missing_queries, result.embeddings):
            cached_results[idx] = vec
            await set_cached_embedding(q, vec)

        return cached_results
    except Exception as exc:
        # Batch failed — fall back to sequential per-query calls
        logger.warning(
            "embed_queries_batch: batch of %d failed (%s), falling back to sequential",
            len(queries),
            sanitize_log(exc),
        )
        return [await embed_query(q) for q in queries]
