"""
Main FastAPI application.
Mounts: webhook receiver, health check, search endpoint, MCP server (Day 6).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api.plan import router as plan_router
from src.api.repos import router as repos_router
from src.github.webhook import router as webhook_router
from src.mcp.auth import router as auth_router
from src.mcp.server import mcp_server
from src.storage.db import get_index_stats

app = FastAPI(
    title="Codebase Intelligence MCP Server",
    version="0.2.0",
    description="Centralized, always-fresh codebase knowledge service.",
)

app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(repos_router)
app.include_router(plan_router)

# Mount MCP server — exposes /mcp/sse and /mcp/messages/
app.mount("/mcp", mcp_server.sse_app())


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health() -> JSONResponse:
    stats = await get_index_stats()
    return JSONResponse({"status": "ok", **stats})


# ── Search ────────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Natural language or identifier query")
    repo: Optional[str] = Field(None, description="Scope to a repo: 'owner/name'")
    language: Optional[str] = Field(None, description="Filter by language: python, typescript…")
    top_k: int = Field(5, ge=1, le=20, description="Number of results to return")
    mode: Literal["semantic", "keyword", "hybrid"] = Field("hybrid")
    rerank: bool = Field(True, description="Apply cross-encoder reranking")
    token_budget: int = Field(8000, description="Max tokens in assembled context")


@app.post("/search", tags=["retrieval"])
async def search_endpoint(req: SearchRequest) -> JSONResponse:
    from src.retrieval.assembler import assemble
    from src.retrieval.reranker import rerank
    from src.retrieval.searcher import embed_query, search

    # Parse optional repo filter
    repo_owner = repo_name = None
    if req.repo and "/" in req.repo:
        repo_owner, repo_name = req.repo.split("/", 1)

    # Embed query (always needed for semantic + hybrid)
    query_vector: list[float] = []
    if req.mode in ("semantic", "hybrid"):
        query_vector = await embed_query(req.query)

    # Retrieve candidates
    results = await search(
        query=req.query,
        query_vector=query_vector,
        top_k=req.top_k,
        mode=req.mode,
        repo_owner=repo_owner,
        repo_name=repo_name,
        language=req.language,
    )

    if not results:
        return JSONResponse({"results": [], "context": "", "tokens_used": 0})

    # Rerank with cross-encoder
    if req.rerank and results:
        results = rerank(req.query, results, top_n=req.top_k)

    # Assemble context
    ctx = assemble(results, token_budget=req.token_budget, query=req.query)

    return JSONResponse({
        "query": req.query,
        "mode": req.mode,
        "results": [
            {
                "file": r.file_path,
                "repo": f"{r.repo_owner}/{r.repo_name}",
                "symbol": r.symbol_name,
                "kind": r.symbol_kind,
                "scope": r.scope_chain,
                "lines": f"{r.start_line}-{r.end_line}",
                "language": r.language,
                "score": round(r.score, 4),
                "rerank_score": round(r.rerank_score, 4),
                "commit": r.commit_sha[:7],
                "preview": r.raw_content[:300],
            }
            for r in results
        ],
        "context": ctx.context_text,
        "tokens_used": ctx.tokens_used,
        "retrieval_log": ctx.retrieval_log,
    })


# ── Webhook events feed ───────────────────────────────────────────────────────

@app.get("/events", tags=["ops"])
async def list_events(
    limit: int = 20,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> JSONResponse:
    """Return recent webhook events ordered by received_at DESC."""
    from sqlalchemy import text
    from src.storage.db import AsyncSessionLocal

    where_parts = []
    params: dict = {"limit": limit}
    if repo_owner:
        where_parts.append("repo_owner = :repo_owner")
        params["repo_owner"] = repo_owner
    if repo_name:
        where_parts.append("repo_name = :repo_name")
        params["repo_name"] = repo_name

    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    sql = text(f"""
        SELECT delivery_id, event_type, repo_owner, repo_name,
               commit_sha, files_changed, status, error_message,
               received_at, processed_at
        FROM webhook_events
        {where_clause}
        ORDER BY received_at DESC
        LIMIT :limit
    """)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    def _fmt(row):
        d = dict(row)
        for k in ("received_at", "processed_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        return d

    return JSONResponse([_fmt(r) for r in rows])


# ── Stats endpoints ───────────────────────────────────────────────────────────

@app.get("/stats/repos", tags=["ops"])
async def stats_repos() -> JSONResponse:
    """Per-repository chunk/file breakdown."""
    from sqlalchemy import text
    from src.storage.db import AsyncSessionLocal

    sql = text("""
        SELECT repo_owner, repo_name,
               COUNT(*) FILTER (WHERE is_deleted = FALSE) AS active_chunks,
               COUNT(*) FILTER (WHERE is_deleted = TRUE)  AS deleted_chunks,
               COUNT(DISTINCT file_path) FILTER (WHERE is_deleted = FALSE) AS files,
               MAX(indexed_at) AS last_indexed
        FROM chunks
        GROUP BY repo_owner, repo_name
        ORDER BY active_chunks DESC
    """)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql)).mappings().all()

    def _fmt(r):
        d = dict(r)
        if d.get("last_indexed") is not None:
            d["last_indexed"] = d["last_indexed"].isoformat()
        return d

    return JSONResponse([_fmt(r) for r in rows])


@app.get("/stats/recent-files", tags=["ops"])
async def stats_recent_files(limit: int = 20) -> JSONResponse:
    """Recently indexed files ordered by indexed_at DESC."""
    from sqlalchemy import text
    from src.storage.db import AsyncSessionLocal

    sql = text("""
        SELECT file_path, repo_owner, repo_name,
               language, token_count, commit_sha, indexed_at
        FROM chunks
        WHERE is_deleted = FALSE
        ORDER BY indexed_at DESC
        LIMIT :limit
    """)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, {"limit": limit})).mappings().all()

    def _fmt(r):
        d = dict(r)
        if d.get("indexed_at") is not None:
            d["indexed_at"] = d["indexed_at"].isoformat()
        return d

    return JSONResponse([_fmt(r) for r in rows])


@app.get("/stats/chunk-distribution", tags=["ops"])
async def stats_chunk_distribution() -> JSONResponse:
    """Token-count bucket distribution for active chunks."""
    from sqlalchemy import text
    from src.storage.db import AsyncSessionLocal

    sql = text("""
        SELECT CASE
                   WHEN token_count < 100 THEN '<100'
                   WHEN token_count < 200 THEN '100-199'
                   WHEN token_count < 300 THEN '200-299'
                   WHEN token_count < 400 THEN '300-399'
                   WHEN token_count < 512 THEN '400-511'
                   ELSE '512+'
               END AS bucket,
               COUNT(*) AS count
        FROM chunks
        WHERE is_deleted = FALSE AND token_count IS NOT NULL
        GROUP BY bucket ORDER BY bucket
    """)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql)).mappings().all()

    return JSONResponse([dict(r) for r in rows])


# ── Root ──────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return {"message": "Codebase Intelligence MCP Server — see /docs"}
