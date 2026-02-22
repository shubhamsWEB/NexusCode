"""
Main FastAPI application.
Mounts: webhook receiver, health check, search endpoint, MCP server (Day 6).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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


# ── Root ──────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return {"message": "Codebase Intelligence MCP Server — see /docs"}
