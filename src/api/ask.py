"""
POST /ask  — Codebase Q&A endpoint (Ask Mode).

Returns a grounded, mentor-style answer with inline code citations.
Unlike POST /plan, this endpoint never generates file changes — it
only answers questions about the codebase.

Two response modes:
  stream=false (default)  →  JSON response  (AskResponse)
  stream=true             →  SSE stream     (text/event-stream)

SSE event types:
  {"type": "status",             "message": "..."}
  {"type": "retrieval_complete", "log": "...", "chunks": N, "tokens": N}
  {"type": "answer_chunk",       "text": "..."}
  {"type": "answer_complete",    "answer": "...", "cited_files": [...], "follow_up_hints": [...], "elapsed_ms": N}
  {"type": "error",              "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid as _uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from src.planning.schemas import AskRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ask", tags=["ask"])


# ── Persistence helper ─────────────────────────────────────────────────────────


async def _save_ask_turn(session_id: str, query: str, result, ctx, repo_owner, repo_name):
    """Best-effort DB persistence — never raises, never blocks the response."""
    try:
        from src.storage.db import append_chat_turn, ensure_chat_session
        await ensure_chat_session(session_id, query, repo_owner=repo_owner, repo_name=repo_name)
        await append_chat_turn(
            session_id=session_id,
            user_query=query,
            answer=result.answer,
            cited_files=result.cited_files,
            follow_up_hints=result.follow_up_hints,
            elapsed_ms=result.elapsed_ms,
            context_tokens=ctx.tokens_used,
            context_files=len(ctx.chunks_used),
            query_complexity=getattr(ctx, "query_complexity", None),
        )
    except Exception:
        logger.exception("ask turn persistence failed (non-fatal)")


# ── Sync endpoint ──────────────────────────────────────────────────────────────


@router.post("", response_model=None)
async def ask_question(req: AskRequest):
    """
    Answer a natural-language question about the codebase.

    Set `stream=true` to receive a server-sent-events stream instead of
    waiting for the full response.
    """
    if req.stream:
        return _sse_response(req)
    return await _sync_ask(req)


async def _sync_ask(req: AskRequest) -> JSONResponse:
    effective_session_id = req.session_id or str(_uuid.uuid4())

    try:
        from src.ask.ask_agent import generate_answer
        from src.planning.retriever import retrieve_planning_context
    except ImportError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)

    try:
        ctx = await retrieve_planning_context(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            web_research=False,  # Ask Mode is fast — no web research by default
            model=req.model,
        )
    except Exception as exc:
        logger.exception("ask retriever failed")
        return JSONResponse({"error": f"Retrieval failed: {exc}"}, status_code=500)

    try:
        result = await generate_answer(
            query=req.query,
            ctx=ctx,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            model=req.model,
        )
    except RuntimeError as exc:
        status = getattr(exc, "status_code", None)
        if status == 429:
            return JSONResponse(
                {"error": str(exc)},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:
        logger.exception("ask generation failed")
        return JSONResponse({"error": f"Answer generation failed: {exc}"}, status_code=500)

    asyncio.create_task(_save_ask_turn(
        effective_session_id, req.query, result, ctx, req.repo_owner, req.repo_name
    ))

    return JSONResponse(
        {
            "query": req.query,
            "answer": result.answer,
            "cited_files": result.cited_files,
            "follow_up_hints": result.follow_up_hints,
            "elapsed_ms": result.elapsed_ms,
            "session_id": effective_session_id,
            "metadata": {
                "context_tokens": ctx.tokens_used,
                "context_files": len(ctx.chunks_used),
                "retrieval_log": ctx.retrieval_log,
                "query_complexity": ctx.query_complexity,
            },
        }
    )


# ── SSE streaming endpoint ─────────────────────────────────────────────────────


def _sse_response(req: AskRequest) -> StreamingResponse:
    return StreamingResponse(
        _sse_generator(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _sse_generator(req: AskRequest):
    def _event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    effective_session_id = req.session_id or str(_uuid.uuid4())

    yield _event({"type": "status", "message": "Searching codebase…"})

    try:
        from src.ask.ask_agent import stream_generate_answer
        from src.planning.retriever import retrieve_planning_context
    except ImportError as exc:
        yield _event({"type": "error", "message": str(exc)})
        return

    # ── Phase: retrieval ───────────────────────────────────────────────────────
    try:
        ctx = await retrieve_planning_context(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            web_research=False,
            model=req.model,
        )
        yield _event(
            {
                "type": "retrieval_complete",
                "log": ctx.retrieval_log,
                "chunks": len(ctx.chunks_used),
                "tokens": ctx.tokens_used,
            }
        )
    except Exception as exc:
        logger.exception("ask retriever failed (SSE)")
        yield _event({"type": "error", "message": f"Retrieval failed: {exc}"})
        return

    # ── Phase: answer generation ───────────────────────────────────────────────
    yield _event({"type": "status", "message": "Generating answer…"})

    try:
        async for chunk in stream_generate_answer(
            query=req.query,
            ctx=ctx,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            model=req.model,
        ):
            if chunk["type"] == "token":
                yield _event({"type": "answer_chunk", "text": chunk["text"]})
            elif chunk["type"] == "answer_complete":
                result = chunk["result"]
                await _save_ask_turn(
                    effective_session_id, req.query, result, ctx, req.repo_owner, req.repo_name
                )
                yield _event(
                    {
                        "type": "answer_complete",
                        "answer": result.answer,
                        "cited_files": result.cited_files,
                        "follow_up_hints": result.follow_up_hints,
                        "elapsed_ms": result.elapsed_ms,
                        "session_id": effective_session_id,
                        "metadata": {
                            "context_tokens": ctx.tokens_used,
                            "context_files": len(ctx.chunks_used),
                            "retrieval_log": ctx.retrieval_log,
                            "query_complexity": ctx.query_complexity,
                        },
                    }
                )
    except RuntimeError as exc:
        status = getattr(exc, "status_code", None)
        if status == 429:
            yield _event(
                {
                    "type": "error",
                    "message": str(exc),
                    "retry_after": 60,
                }
            )
        else:
            yield _event({"type": "error", "message": str(exc)})
    except Exception as exc:
        logger.exception("ask streaming failed (SSE)")
        yield _event({"type": "error", "message": f"Answer generation failed: {exc}"})
