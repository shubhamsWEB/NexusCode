"""
Main FastAPI application.
Mounts: webhook receiver, health check, search endpoint, MCP server (Day 6).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from src.api.agent_roles import router as agent_roles_router
from src.api.evolution import router as evolution_router
from src.api.integrations import router as integrations_router
from src.api.api_keys import router as api_keys_router
from src.api.ask import router as ask_router
from src.api.documents import router as documents_router
from src.api.graph import router as graph_router
from src.api.history import router as history_router
from src.api.mcp_servers import router as mcp_servers_router
from src.api.plan import router as plan_router
from src.api.repos import router as repos_router
from src.api.skills import router as skills_router
from src.api.workflows import router as workflows_router
from src.api.workflows import webhook_router as workflow_webhook_router
from src.github.webhook import router as webhook_router
from src.mcp.auth import router as auth_router
from src.mcp.server import core_mcp_server, mcp_server
from src.storage.db import get_index_stats


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Pre-load heavy models at startup so the first request isn't slow."""
    import asyncio

    loop = asyncio.get_running_loop()

    # Warm the cross-encoder reranker in a thread (CPU-bound model load)
    from src.retrieval.reranker import warmup as warmup_reranker

    await loop.run_in_executor(None, warmup_reranker)

    # Warm skill cache at startup so GET /skills is instant on first request
    from src.skills.loader import load_all_skills

    load_all_skills()

    # Initialise external MCP bridge (non-fatal if no servers configured)
    from src.agent.mcp_bridge import init_bridge

    await init_bridge()

    # Initialise LangGraph PostgreSQL checkpointer for enterprise graph workflows
    from src.workflows.graph_engine import init_graph_checkpointer

    await init_graph_checkpointer()

    # On startup, mark any runs that were left "running" from a previous process
    # as failed. Background asyncio tasks are not durable — a server restart
    # (including uvicorn --reload) kills them silently. This prevents runs from
    # appearing stuck forever in the UI.
    try:
        from src.workflows.registry import list_runs, update_run_status
        orphaned = await list_runs(limit=100)
        for run in orphaned:
            if run.get("status") == "running":
                await update_run_status(
                    run["id"],
                    "failed",
                    error_message=(
                        "Run interrupted: the API server restarted while this run was active. "
                        "Re-trigger the workflow to restart from the beginning."
                    ),
                )
                logger.warning("startup: marked orphaned run %s as failed", run["id"])
    except Exception as _sweep_exc:
        logger.warning("startup: orphaned-run sweep failed: %s", _sweep_exc)

    # Bootstrap integration credentials from env vars (non-fatal)
    from src.integrations.auth.credential_store import bootstrap_from_config

    try:
        await bootstrap_from_config()
    except Exception:
        pass

    # Initialise event bus connection (non-fatal if Redis unavailable)
    from src.events.bus import EventBus

    await EventBus._get_redis()

    # Run the MCP Streamable HTTP session managers for the full server lifetime.
    # streamable_http_app() lazily creates _session_manager at mount time (below);
    # these context managers create the anyio task groups that all MCP sessions need.
    # Without them every POST to the mounted MCP routes raises
    # RuntimeError("Task group not initialized").
    async with core_mcp_server._session_manager.run():
        async with mcp_server._session_manager.run():
            yield


class _MCPPathNormalizer:
    """Pure ASGI middleware: rewrites mounted MCP paths to include a trailing slash.

    Starlette's Mount regex for ``app.mount("/mcp", ...)`` is ``^/mcp/(?P<path>.*)$``.
    A request to ``POST /mcp`` (no trailing slash) misses that regex, falls through
    to the Router's redirect_slashes logic, and gets a 307 Temporary Redirect.
    MCP clients (Cursor, Claude Desktop) do **not** follow POST redirects, so they
    never receive the tools list and the server appears broken.

    This middleware normalises the path to ``/mcp/`` and ``/mcp/full/`` before
    routing so the Mount matches directly — no redirect, no client-side workaround
    needed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in {"/mcp", "/mcp/full"}:
            scope = dict(scope)
            scope["path"] = scope["path"] + "/"
        await self.app(scope, receive, send)


app = FastAPI(
    title="Codebase Intelligence MCP Server",
    version="0.2.0",
    description="Centralized, always-fresh codebase knowledge service.",
    lifespan=lifespan,
)

# Normalise /mcp → /mcp/ so Cursor/Claude Desktop don't need the trailing slash.
# Must be added AFTER app is constructed so it wraps the full middleware stack.
app.add_middleware(_MCPPathNormalizer)


app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(repos_router)
app.include_router(plan_router)
app.include_router(ask_router)
app.include_router(history_router)
app.include_router(skills_router)
app.include_router(mcp_servers_router)
app.include_router(graph_router)
app.include_router(workflows_router)
app.include_router(workflow_webhook_router)
app.include_router(agent_roles_router)
app.include_router(documents_router)
app.include_router(api_keys_router)
app.include_router(evolution_router)
app.include_router(integrations_router)


# ── MCP Streamable HTTP transport (MCP 2025-03-26 spec) ──────────────────────
# Full endpoint:    POST /mcp/full  → complete NexusCode MCP surface
# Default endpoint: POST /mcp       → core context tools only
# Mount the more-specific path first so Starlette does not route /mcp/full
# through the broader /mcp mount.
# IMPORTANT: streamable_http_app() here creates each server's _session_manager
# before the lifespan's async with ... _session_manager.run() contexts run.
app.mount("/mcp/full", mcp_server.streamable_http_app())
app.mount("/mcp", core_mcp_server.streamable_http_app())


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["ops"])
async def health() -> JSONResponse:
    stats = await get_index_stats()
    return JSONResponse({"status": "ok", **stats})


# ── Available LLM models ─────────────────────────────────────────────────────


@app.get("/models", tags=["ops"])
async def available_models() -> JSONResponse:
    """Return available models across all configured providers."""
    from src.config import settings

    models: list[dict] = []

    if settings.anthropic_api_key:
        models += [
            {"model": "claude-sonnet-4-6", "provider": "anthropic"},
            {"model": "claude-opus-4-6", "provider": "anthropic"},
            {"model": "claude-haiku-4-5-20251001", "provider": "anthropic"},
        ]

    if settings.ollama_base_url and settings.ollama_models:
        for m in settings.ollama_models.split(","):
            m = m.strip()
            if m:
                models.append({"model": m, "provider": "ollama"})

    return JSONResponse(models)


# ── Search ────────────────────────────────────────────────────────────────────


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Natural language or identifier query")
    repo: str | None = Field(None, description="Scope to a repo: 'owner/name'")
    language: str | None = Field(None, description="Filter by language: python, typescript…")
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

    return JSONResponse(
        {
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
        }
    )


# ── Webhook events feed ───────────────────────────────────────────────────────


@app.get("/events", tags=["ops"])
async def list_events(
    limit: int = 20,
    repo_owner: str | None = None,
    repo_name: str | None = None,
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


# ── Visual Workflow Builder (React + ReactFlow) ───────────────────────────────
# Serves the built frontend at GET /builder/*
# Build with: cd frontend && npm install && npm run build

_FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
if os.path.isdir(_FRONTEND_DIST):
    app.mount("/builder", StaticFiles(directory=_FRONTEND_DIST, html=True), name="builder")


# ── Root ──────────────────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
async def root():
    return {"message": "Codebase Intelligence MCP Server — see /docs"}
