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

import json
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from src.planning.schemas import PlanRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plan", tags=["planning"])


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
    except ImportError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)

    try:
        ctx = await retrieve_planning_context(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            web_research=req.web_research,
        )
    except Exception as exc:
        logger.exception("planning retriever failed")
        return JSONResponse({"error": f"Retrieval failed: {exc}"}, status_code=500)

    try:
        plan = await generate_plan(
            query=req.query,
            ctx=ctx,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
        )
    except RuntimeError as exc:
        # anthropic not installed / no API key
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:
        logger.exception("plan generation failed")
        return JSONResponse({"error": f"Plan generation failed: {exc}"}, status_code=500)

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
        from src.planning.claude_planner import generate_plan
        from src.planning.retriever import retrieve_planning_context
    except ImportError as exc:
        yield _event({"type": "error", "message": str(exc)})
        return

    # ── Phase: retrieval ──────────────────────────────────────────────────────
    try:
        ctx = await retrieve_planning_context(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            web_research=req.web_research,
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
        yield _event({"type": "error", "message": f"Retrieval failed: {exc}"})
        return

    # ── Phase: generation (sync — tool_use forces full response anyway) ───────
    yield _event({"type": "status", "message": "Generating implementation plan…"})

    try:
        plan = await generate_plan(
            query=req.query,
            ctx=ctx,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
        )
        yield _event({"type": "plan_complete", "plan": plan.model_dump()})
    except RuntimeError as exc:
        yield _event({"type": "error", "message": str(exc)})
    except Exception as exc:
        logger.exception("plan generation failed (SSE)")
        yield _event({"type": "error", "message": f"Plan generation failed: {exc}"})
