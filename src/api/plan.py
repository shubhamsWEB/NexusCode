"""
POST /plan  — Implementation planning endpoint.

Accepts a natural-language query (bug/feature/refactor) and returns a
complete ImplementationPlan. Claude searches the codebase iteratively via
tool calls, then outputs a structured plan grounded in the actual code.

Two response modes:
  stream=false (default)  →  JSON response  (ImplementationPlan)
  stream=true             →  SSE stream     (text/event-stream)

SSE event types:
  {"type": "status",             "message": "..."}
  {"type": "agent_tool_call",    "tool": str, "input_summary": str}
  {"type": "agent_tool_result",  "tool": str, "tokens": N, "cumulative_tokens": N}
  {"type": "thinking",           "text": "..."}
  {"type": "plan_chunk",         "text": "..."}
  {"type": "plan_complete",      "plan": {...}}
  {"type": "error",              "message": "..."}
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.middleware import get_repo_scope
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
async def create_plan(req: PlanRequest, allowed_repos: list[str] | None = Depends(get_repo_scope)):
    """
    Generate a complete implementation plan for a query.
    Set `stream=true` to receive a server-sent-events stream.
    """
    if req.stream:
        return _sse_response(req, allowed_repos)
    return await _sync_plan(req, allowed_repos)


async def _sync_plan(req: PlanRequest, allowed_repos: list[str] | None = None) -> JSONResponse:
    try:
        from src.planning.claude_planner import generate_plan
    except ImportError:
        return JSONResponse(
            {"error": "Service unavailable. Required modules not loaded."}, status_code=503
        )

    try:
        plan = await generate_plan(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            web_research=req.web_research,
            model=req.model,
            allowed_repos=allowed_repos,
        )
    except RuntimeError as exc:
        status = getattr(exc, "status_code", None)
        if status == 429:
            return JSONResponse(
                {"error": "Rate limit reached. Please try again in 60 seconds."},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        return JSONResponse({"error": "Service unavailable. Please try again."}, status_code=503)
    except Exception:
        logger.exception("plan generation failed")
        return JSONResponse({"error": "Plan generation failed. Please try again."}, status_code=500)

    task = asyncio.create_task(_save_plan(plan, req.repo_owner, req.repo_name))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JSONResponse(plan.model_dump())


# ── SSE streaming endpoint ────────────────────────────────────────────────────


def _sse_response(req: PlanRequest, allowed_repos: list[str] | None = None) -> StreamingResponse:
    return StreamingResponse(
        _sse_generator(req, allowed_repos),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _sse_generator(req: PlanRequest, allowed_repos: list[str] | None = None):
    def _event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    yield _event({"type": "status", "message": "Searching codebase…"})

    try:
        from src.planning.claude_planner import stream_generate_plan
    except ImportError:
        yield _event(
            {"type": "error", "message": "Service unavailable. Required modules not loaded."}
        )
        return

    try:
        async for chunk in stream_generate_plan(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            web_research=req.web_research,
            model=req.model,
            allowed_repos=allowed_repos,
        ):
            etype = chunk.get("type")

            if etype == "agent_tool_call":
                yield _event(
                    {
                        "type": "agent_tool_call",
                        "tool": chunk["tool"],
                        "input_summary": chunk.get("input_summary", ""),
                    }
                )

            elif etype == "agent_tool_result":
                yield _event(
                    {
                        "type": "agent_tool_result",
                        "tool": chunk["tool"],
                        "tokens": chunk.get("tokens", 0),
                        "cumulative_tokens": chunk.get("cumulative_tokens", 0),
                    }
                )

            elif etype == "thinking":
                yield _event({"type": "thinking", "text": chunk["text"]})

            elif etype == "token":
                yield _event({"type": "plan_chunk", "text": chunk["text"]})

            elif etype == "plan_complete":
                plan = chunk["plan"]
                await _save_plan(plan, req.repo_owner, req.repo_name)
                yield _event(
                    {
                        "type": "plan_complete",
                        "plan": plan.model_dump(),
                        "plan_id": plan.plan_id,
                    }
                )

    except RuntimeError as exc:
        status = getattr(exc, "status_code", None)
        if status == 429:
            yield _event(
                {
                    "type": "error",
                    "message": "Rate limit reached. Please try again in 60 seconds.",
                    "retry_after": 60,
                }
            )
        else:
            yield _event({"type": "error", "message": "Service unavailable. Please try again."})
    except Exception:
        logger.exception("plan streaming failed (SSE)")
        yield _event({"type": "error", "message": "Plan generation failed. Please try again."})
