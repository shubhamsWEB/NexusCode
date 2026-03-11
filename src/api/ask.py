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

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.middleware import get_repo_scope
from src.config import settings
from src.planning.schemas import AskRequest
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

router = APIRouter(prefix="/ask", tags=["ask"])

_background_tasks: set[asyncio.Task] = set()


# ── Persistence helper ─────────────────────────────────────────────────────────


def _retrieval_params_snapshot() -> dict:
    """Snapshot the current retrieval settings at call time for telemetry."""
    return {
        "retrieval_strategy": "hybrid",
        "hnsw_ef_search": settings.hnsw_ef_search,
        "retrieval_rrf_k": settings.retrieval_rrf_k,
        "retrieval_candidate_multiplier": settings.retrieval_candidate_multiplier,
        "reranker_top_n": settings.reranker_top_n,
        "query_relevance_threshold": settings.query_relevance_threshold,
        "ask_max_iterations": settings.ask_max_iterations,
    }


async def _record_ask_telemetry(
    repo_owner: str | None,
    repo_name: str | None,
    query: str,
    result,
    session_id: str,
    retrieval_params: dict,
) -> int | None:
    """Fire-and-forget telemetry capture — returns metric_id or None."""
    if repo_owner is None or repo_name is None:
        return None
    try:
        from src.evolution.telemetry import record_ask_metrics

        return await record_ask_metrics(
            repo_owner=repo_owner,
            repo_name=repo_name,
            query=query,
            quality_score=getattr(result, "quality_score", None),
            iterations=getattr(result, "iterations", 0),
            tool_calls_count=getattr(result, "tool_calls_count", 0),
            context_tokens=getattr(result, "context_tokens", 0),
            elapsed_ms=getattr(result, "elapsed_ms", 0.0),
            retrieval_params=retrieval_params,
            session_id=session_id,
        )
    except Exception:
        logger.debug("Evolution telemetry capture failed (non-fatal)")
        return None


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
async def ask_question(req: AskRequest, allowed_repos: list[str] | None = Depends(get_repo_scope)):
    """
    Answer a natural-language question about the codebase.
    Set `stream=true` to receive a server-sent-events stream.
    """
    if req.stream:
        return _sse_response(req, allowed_repos)
    return await _sync_ask(req, allowed_repos)


async def _sync_ask(req: AskRequest, allowed_repos: list[str] | None = None) -> JSONResponse:
    effective_session_id = req.session_id or str(_uuid.uuid4())
    retrieval_params = _retrieval_params_snapshot()

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
        logger.exception("ask generation failed")
        return JSONResponse(
            {"error": "Answer generation failed. Please try again."}, status_code=500
        )

    # Persist chat turn (fire-and-forget)
    task = asyncio.create_task(
        _save_ask_turn(effective_session_id, req.query, result, req.repo_owner, req.repo_name)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    # Await telemetry so we can return the interaction_id for feedback linkage
    metric_id = await _record_ask_telemetry(
        req.repo_owner, req.repo_name, req.query, result, effective_session_id, retrieval_params
    )

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
                "interaction_id": metric_id,
            },
        }
    )


# ── SSE streaming endpoint ─────────────────────────────────────────────────────


def _sse_response(req: AskRequest, allowed_repos: list[str] | None = None) -> StreamingResponse:
    return StreamingResponse(
        _sse_generator(req, allowed_repos),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _sse_generator(req: AskRequest, allowed_repos: list[str] | None = None):
    def _event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    effective_session_id = req.session_id or str(_uuid.uuid4())
    retrieval_params = _retrieval_params_snapshot()

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
                yield _event({"type": "answer_chunk", "text": chunk["text"]})

            elif etype == "answer_complete":
                result = chunk["result"]
                await _save_ask_turn(
                    effective_session_id, req.query, result, req.repo_owner, req.repo_name
                )
                metric_id = await _record_ask_telemetry(
                    req.repo_owner, req.repo_name, req.query, result,
                    effective_session_id, retrieval_params,
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
                            "interaction_id": metric_id,
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
