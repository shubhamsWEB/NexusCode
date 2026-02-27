"""
GET /history/ask                 — list chat sessions
GET /history/ask/{session_id}   — session + all turns
GET /history/plan                — list plan history entries
GET /history/plan/{plan_id}     — single plan entry (plan_json parsed)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/history", tags=["history"])


@router.get("/ask")
async def list_ask_sessions(
    limit: int = 20,
    offset: int = 0,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> JSONResponse:
    """List chat sessions ordered by last_active_at DESC."""
    try:
        from src.storage.db import list_chat_sessions
        sessions = await list_chat_sessions(
            limit=limit, offset=offset, repo_owner=repo_owner, repo_name=repo_name
        )
        return JSONResponse(sessions)
    except Exception as exc:
        logger.exception("list_chat_sessions failed")
        return JSONResponse({"error": "An internal error occurred."}, status_code=500)


@router.get("/ask/{session_id}")
async def get_ask_session(session_id: str) -> JSONResponse:
    """Return a session and all its turns ordered by turn_index."""
    try:
        from src.storage.db import get_chat_session_with_turns
        result = await get_chat_session_with_turns(session_id)
        if result is None:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return JSONResponse(result)
    except Exception as exc:
        logger.exception("get_chat_session_with_turns failed")
        return JSONResponse({"error": "An internal error occurred."}, status_code=500)


@router.get("/plan")
async def list_plan_entries(
    limit: int = 20,
    offset: int = 0,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    response_type: str | None = None,
) -> JSONResponse:
    """List plan history entries ordered by created_at DESC."""
    try:
        from src.storage.db import list_plan_history
        entries = await list_plan_history(
            limit=limit,
            offset=offset,
            repo_owner=repo_owner,
            repo_name=repo_name,
            response_type=response_type,
        )
        return JSONResponse(entries)
    except Exception as exc:
        logger.exception("list_plan_history failed")
        return JSONResponse({"error": "An internal error occurred."}, status_code=500)


@router.get("/plan/{plan_id}")
async def get_plan_entry(plan_id: str) -> JSONResponse:
    """Return a single plan history entry with plan_json parsed to dict."""
    try:
        from src.storage.db import get_plan_history_entry
        result = await get_plan_history_entry(plan_id)
        if result is None:
            return JSONResponse({"error": "Plan not found"}, status_code=404)
        return JSONResponse(result)
    except Exception as exc:
        logger.exception("get_plan_history_entry failed")
        return JSONResponse({"error": "An internal error occurred."}, status_code=500)
