"""
Hybrid search: pgvector cosine + tsvector keyword + Reciprocal Rank Fusion.

Three modes:
  "semantic"  — vector cosine similarity only
  "keyword"   — tsvector full-text + pg_trgm symbol name match only
  "hybrid"    — both lists merged via RRF, then cross-encoder reranked
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from src.config import settings
from src.storage.db import AsyncSessionLocal
from src.utils.sanitize import sanitize_log

from src.utils.logging import get_secure_logger
logger = get_secure_logger(__name__)

# RRF constant — higher = less penalty for low-ranked results
_RRF_K = 60

# How many candidates to pull from each sub-search before RRF
_CANDIDATE_MULTIPLIER = 4


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
    rerank_score: float = 0.0  # set by reranker


# ── Public entry point ────────────────────────────────────────────────────────


async def search(
    query: str,
    query_vector: list[float],
    top_k: int = 5,
    mode: str = "hybrid",
    repo_owner: str | None = None,
    repo_name: str | None = None,
    language: str | None = None,
) -> list[SearchResult]:
    """
    Run a search and return top_k results.

    query_vector must already be embedded by the caller (so the caller
    controls the embedding step and can cache it).
    """
    candidates = top_k * _CANDIDATE_MULTIPLIER

    if mode == "semantic":
        results = await _semantic_search(query_vector, candidates, repo_owner, repo_name, language)

    elif mode == "keyword":
        results = await _keyword_search(query, candidates, repo_owner, repo_name, language)

    else:  # hybrid
        semantic = await _semantic_search(query_vector, candidates, repo_owner, repo_name, language)
        keyword = await _keyword_search(query, candidates, repo_owner, repo_name, language)
        results = _reciprocal_rank_fusion(semantic, keyword)

    return results[:top_k]


# ── Semantic search ───────────────────────────────────────────────────────────


async def _semantic_search(
    vector: list[float],
    limit: int,
    repo_owner: str | None,
    repo_name: str | None,
    language: str | None,
) -> list[SearchResult]:
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
    where, params = _build_where(repo_owner, repo_name, language)
    params["limit"] = limit
    params["vec"] = vec_str

    sql = text(f"""
        SELECT
            id, file_path, repo_owner, repo_name, language,
            symbol_name, symbol_kind, scope_chain,
            start_line, end_line, raw_content, enriched_content,
            commit_sha, commit_author, token_count,
            1 - (embedding <=> :vec ::vector) AS score
        FROM chunks
        WHERE {where}
          AND embedding IS NOT NULL
        ORDER BY embedding <=> :vec ::vector
        LIMIT :limit
    """)

    return await _execute_search(sql, params)


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
    sql = text(f"""
        SELECT
            id, file_path, repo_owner, repo_name, language,
            symbol_name, symbol_kind, scope_chain,
            start_line, end_line, raw_content, enriched_content,
            commit_sha, commit_author, token_count,
            (
                ts_rank(to_tsvector('english', raw_content),
                        plainto_tsquery('english', :query)) * 0.7
                + COALESCE(similarity(symbol_name, :query), 0) * 0.3
            ) AS score
        FROM chunks
        WHERE {where}
          AND (
            to_tsvector('english', raw_content) @@ plainto_tsquery('english', :query)
            OR symbol_name % :query
          )
        ORDER BY score DESC
        LIMIT :limit
    """)

    return await _execute_search(sql, params)


# ── RRF merge ────────────────────────────────────────────────────────────────


def _reciprocal_rank_fusion(
    semantic: list[SearchResult],
    keyword: list[SearchResult],
) -> list[SearchResult]:
    """
    Merge two ranked lists using Reciprocal Rank Fusion.
    Score = Σ 1/(k + rank_i) across all lists that contain the item.
    """
    # Build lookup: chunk_id → SearchResult (semantic takes priority for metadata)
    by_id: dict[str, SearchResult] = {}
    for r in semantic + keyword:
        if r.chunk_id not in by_id:
            by_id[r.chunk_id] = r

    rrf_scores: dict[str, float] = {}

    for rank, result in enumerate(semantic, 1):
        rrf_scores[result.chunk_id] = rrf_scores.get(result.chunk_id, 0) + 1 / (_RRF_K + rank)

    for rank, result in enumerate(keyword, 1):
        rrf_scores[result.chunk_id] = rrf_scores.get(result.chunk_id, 0) + 1 / (_RRF_K + rank)

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


async def _execute_search(sql, params: dict) -> list[SearchResult]:
    async with AsyncSessionLocal() as session:
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
                score=float(row.get("score") or 0),
            )
        )
    return results


# ── Embed query (convenience wrapper) ────────────────────────────────────────


async def embed_query(query: str) -> list[float]:
    """Embed a search query using voyage-code-2 with input_type='query'."""
    import asyncio

    from src.pipeline.embedder import _make_client

    client = _make_client()
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: client.embed([query], model=settings.embedding_model, input_type="query"),
    )
    return result.embeddings[0]


async def embed_queries_batch(queries: list[str]) -> list[list[float]]:
    """
    Embed multiple search queries in a single Voyage AI API call.

    Batching N sub-queries into one call reduces round-trip overhead
    and avoids N-fold rate-limit pressure.  The Voyage API accepts up to
    128 texts per request; callers should not exceed that limit.

    Returns a list of embedding vectors in the same order as `queries`.
    Falls back to per-query calls if the batch request fails.
    """
    import asyncio

    from src.pipeline.embedder import _make_client

    if not queries:
        return []

    client = _make_client()
    loop = asyncio.get_running_loop()

    try:
        result = await loop.run_in_executor(
            None,
            lambda: client.embed(
                queries,
                model=settings.embedding_model,
                input_type="query",
            ),
        )
        return result.embeddings
    except Exception as exc:
        # Batch failed — fall back to sequential per-query calls
        logger.warning(
            "embed_queries_batch: batch of %d failed (%s), falling back to sequential",
            len(queries),
            sanitize_log(exc),
        )
        return [await embed_query(q) for q in queries]
