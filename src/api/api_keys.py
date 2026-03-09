"""
CRUD endpoints for scoped API keys.

POST   /api-keys          — create a new scoped key (returns raw_key ONCE)
GET    /api-keys          — list all keys (never returns raw key_hash)
DELETE /api-keys/{id}     — delete a key scope
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class CreateKeyRequest(BaseModel):
    name: str = Field(..., description="Human-readable name, e.g. 'frontend-team'")
    description: str = Field("", description="Optional description")
    allowed_repos: list[str] = Field(
        default_factory=list,
        description="List of 'owner/name' strings. Empty = all repos (admin).",
    )


@router.post("", summary="Create a new scoped API key")
async def create_key(req: CreateKeyRequest) -> JSONResponse:
    """
    Create a scoped API key.
    `allowed_repos` is a list of 'owner/name' strings restricting which repos
    this key can search. Empty list = all repos (admin key).

    The raw key is returned **once** — it is never stored.
    """
    from src.storage.db import create_api_key_scope

    result = await create_api_key_scope(
        name=req.name,
        description=req.description,
        allowed_repos=req.allowed_repos,
    )
    return JSONResponse(result, status_code=201)


@router.get("", summary="List all API key scopes")
async def list_keys() -> JSONResponse:
    """Return all API key scopes. Never returns the raw key or its hash."""
    from src.storage.db import list_api_key_scopes

    scopes = await list_api_key_scopes()
    return JSONResponse(scopes)


@router.delete("/{scope_id}", summary="Delete an API key scope")
async def delete_key(scope_id: int) -> JSONResponse:
    """Permanently delete a scoped API key. Any requests using it will receive 401."""
    from src.storage.db import delete_api_key_scope

    ok = await delete_api_key_scope(scope_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"API key scope {scope_id} not found.")
    return JSONResponse({"deleted": scope_id})
