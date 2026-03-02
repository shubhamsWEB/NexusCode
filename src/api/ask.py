"""
POST /ask  — Codebase Q&A endpoint (Ask Mode).

Returns a grounded, mentor-style answer with inline code citations.
Claude searches the vector DB iteratively via tool calls, then answers.

Two response modes:
  stream=false (default)  →  JSON response
  stream=true             →  SSE stream (text/event-stream)

SSE event types:
  {"type": "status",             "message": "..."}
  {"type": "agent_tool_call",    "tool": "search_codebase", "input_summary": "..."}
  {"type": "agent_tool_result",  "tool": "search_codebase", "tokens": N, "cumulative_tokens": N}
  {"type": "thinking",           "text": "..."}
  {"type": "answer_chunk",       "text": "..."}
  {"type": "answer_complete",    "answer": "...", "cited_files": [...], "follow_up_hints": [...], "elapsed_ms": N}
  {"type": "error",              "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import uuid as _uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from src.planning.schemas import AskRequest
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

router = APIRouter(prefix="/ask", tags=["ask"])

_background_tasks: set[asyncio.Task] = set()


# ── Persistence helper ─────────────────────────────────────────────────────────


async def _save_ask_turn(session_id: str, query: str, result, repo_owner, repo_name):
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
            context_tokens=result.context_tokens,
            context_files=result.tool_calls_count,
            query_complexity=None,
        )
    except Exception:
        logger.exception("ask turn persistence failed (non-fatal)")


# ── Sync endpoint ──────────────────────────────────────────────────────────────


@router.post("", response_model=None)
async def ask_question(req: AskRequest):
    """
    Answer a natural-language question about the codebase.
    Set `stream=true` to receive a server-sent-events stream.
    """
    if req.stream:
        return _sse_response(req)
    return await _sync_ask(req)


async def _sync_ask(req: AskRequest) -> JSONResponse:
    effective_session_id = req.session_id or str(_uuid.uuid4())

    try:
        from src.ask.ask_agent import generate_answer
    except ImportError:
        return JSONResponse(
            {"error": "Service unavailable. Required modules not loaded."}, status_code=503
        )

    try:
        result = await generate_answer(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            model=req.model,
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
        logger.exception("ask generation failed")
        return JSONResponse(
            {"error": "Answer generation failed. Please try again."}, status_code=500
        )

    task = asyncio.create_task(
        _save_ask_turn(effective_session_id, req.query, result, req.repo_owner, req.repo_name)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return JSONResponse(
        {
            "query": req.query,
            "answer": result.answer,
            "cited_files": result.cited_files,
            "follow_up_hints": result.follow_up_hints,
            "elapsed_ms": result.elapsed_ms,
            "session_id": effective_session_id,
            "metadata": {
                "context_tokens": result.context_tokens,
                "context_files": result.tool_calls_count,
                "retrieval_log": (
                    f"Agentic: {result.iterations} iterations, "
                    f"{result.tool_calls_count} tool calls"
                ),
                "query_complexity": None,
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
    except ImportError:
        yield _event(
            {"type": "error", "message": "Service unavailable. Required modules not loaded."}
        )
        return

    try:
        async for chunk in stream_generate_answer(
            query=req.query,
            repo_owner=req.repo_owner,
            repo_name=req.repo_name,
            model=req.model,
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
                yield _event({"type": "answer_chunk", "text": chunk["text"]})

            elif etype == "answer_complete":
                result = chunk["result"]
                await _save_ask_turn(
                    effective_session_id, req.query, result, req.repo_owner, req.repo_name
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
                            "context_tokens": result.context_tokens,
                            "context_files": result.tool_calls_count,
                            "retrieval_log": (
                                f"Agentic: {result.iterations} iterations, "
                                f"{result.tool_calls_count} tool calls"
                            ),
                            "query_complexity": None,
                        },
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
        logger.exception("ask streaming failed (SSE)")
        yield _event({"type": "error", "message": "Answer generation failed. Please try again."})
