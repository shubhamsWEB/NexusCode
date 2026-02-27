"""
POST /plan  — Implementation planning endpoint.

Accepts a natural-language query (bug/feature/refactor) and returns a
complete ImplementationPlan grounded in the live codebase index.

Two response modes:
  stream=false (default)  →  JSON response  (ImplementationPlan)
  stream=true             →  SSE stream     (text/event-stream)

SSE event types:
  {"type": "status",             "message": "..."}
  {"type": "retrieval_complete", "log": "..."}
  {"type": "plan_chunk",         "text": "..."}
  {"type": "plan_complete",      "plan": {...}}
  {"type": "error",              "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from src.planning.schemas import PlanRequest

from src.utils.logging import get_secure_logger
logger = get_secure_logger(__name__)

router = APIRouter(prefix="/plan", tags=["planning"])

_background_tasks: set[asyncio.Task] = set()


# ── Persistence helper ─────────────────────────────────────────────────────────


async def _save_plan(plan, repo_owner, repo_name):
    """Best-effort plan history persistence — never raises."""
    try:
        from src.storage.db import save_plan_history
        metadata = plan.metadata
        await save_plan_history(
            plan_id=plan.plan_id,
            query=plan.query,
            response_type=plan.response_type,
            plan_json_str=json.dumps(plan.model_dump()),
            repo_owner=repo_owner,
            repo_name=repo_name,
            elapsed_ms=metadata.elapsed_ms if metadata else None,
            context_tokens=metadata.context_tokens if metadata else None,
            web_research_used=metadata.web_research_used if metadata else False,
        )
    except Exception:
        logger.exception("plan history persistence failed (non-fatal)")


# ── Sync endpoint ─────────────────────────────────────────────────────────────


@router.post("", response_model=None)
async def create_plan(req: PlanRequest):
    """
    Generate a complete implementation plan for a query.

    Set `stream=true` to receive a server-sent-events stream instead of
    waiting for the full response.
    """
    if req.stream:
        return _sse_response(req)
    return await _sync_plan(req)


async def _sync_plan(req: PlanRequest) -> JSONResponse:
    try:
        from src.planning.claude_planner import generate_plan
        from src.planning.retriever import retrieve_planning_context
    except ImportError:
        return JSONResponse({"error": "Service unavailable. Required modules not loaded."}, status_code=503)

    try:
        ctx = await retrieve_planning_context(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            web_research=req.web_research,
            model=req.model,
        )
    except Exception as exc:
        logger.exception("planning retriever failed")
        return JSONResponse({"error": "Retrieval failed. Please try again."}, status_code=500)

    try:
        plan = await generate_plan(
            query=req.query,
            ctx=ctx,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            model=req.model,
        )
    except RuntimeError as exc:
        # Check if it's a rate limit error (our custom RateLimitOrOverloadError)
        status = getattr(exc, "status_code", None)
        if status == 429:
            return JSONResponse(
                {"error": "Rate limit reached. Please try again in 60 seconds."},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        # anthropic not installed / no API key / overload
        return JSONResponse({"error": "Service unavailable. Please try again."}, status_code=503)
    except Exception as exc:
        logger.exception("plan generation failed")
        return JSONResponse({"error": "Plan generation failed. Please try again."}, status_code=500)

    task = asyncio.create_task(_save_plan(plan, req.repo_owner, req.repo_name))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JSONResponse(plan.model_dump())


# ── SSE streaming endpoint ────────────────────────────────────────────────────


def _sse_response(req: PlanRequest) -> StreamingResponse:
    return StreamingResponse(
        _sse_generator(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


async def _sse_generator(req: PlanRequest):
    def _event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    web_label = " + web research" if req.web_research else ""
    yield _event({"type": "status", "message": f"Retrieving code context{web_label}…"})

    try:
        from src.planning.claude_planner import stream_generate_plan
        from src.planning.retriever import retrieve_planning_context
    except ImportError:
        yield _event({"type": "error", "message": "Service unavailable. Required modules not loaded."})
        return

    # ── Phase: retrieval ──────────────────────────────────────────────────────
    try:
        ctx = await retrieve_planning_context(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            web_research=req.web_research,
            model=req.model,
        )
        yield _event(
            {
                "type": "retrieval_complete",
                "log": ctx.retrieval_log,
                "chunks": len(ctx.chunks_used),
                "tokens": ctx.tokens_used,
                "web_research_used": bool(ctx.web_research_notes),
            }
        )
    except Exception as exc:
        logger.exception("planning retriever failed (SSE)")
        yield _event({"type": "error", "message": "Retrieval failed. Please try again."})
        return

    # ── Phase: generation (real token streaming via input_json_delta) ─────────
    yield _event({"type": "status", "message": "Generating implementation plan…"})

    try:
        async for chunk in stream_generate_plan(
            query=req.query,
            ctx=ctx,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            model=req.model,
        ):
            if chunk["type"] == "thinking":
                yield _event({"type": "thinking", "text": chunk["text"]})
            elif chunk["type"] == "token":
                yield _event({"type": "plan_chunk", "text": chunk["text"]})
            elif chunk["type"] == "plan_complete":
                plan = chunk["plan"]
                await _save_plan(plan, req.repo_owner, req.repo_name)
                yield _event({
                    "type": "plan_complete",
                    "plan": plan.model_dump(),
                    "plan_id": plan.plan_id,
                })
    except RuntimeError as exc:
        status = getattr(exc, "status_code", None)
        if status == 429:
            yield _event({
                "type": "error",
                "message": "Rate limit reached. Please try again in 60 seconds.",
                "retry_after": 60,
            })
        else:
            yield _event({"type": "error", "message": "Service unavailable. Please try again."})
    except Exception as exc:
        logger.exception("plan streaming failed (SSE)")
        yield _event({"type": "error", "message": "Plan generation failed. Please try again."})
