"""
FastAPI dependency for API key → scope resolution.

get_repo_scope() returns:
  None              → no key provided; unrestricted (all repos)
  []                → valid key, empty allowed_repos (admin / all-access)
  ["owner/repo"...] → restricted to this set
"""

from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException, Request

from src.config import settings
from src.storage.db import get_scope_by_key_hash
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)


def _get_redis():
    import redis.asyncio as redis

    return redis.from_url(settings.redis_url)


async def get_repo_scope(request: Request) -> list[str] | None:
    """
    FastAPI dependency — extract and cache API key scope per request.

    Lookup order: X-Api-Key header → ?api_key= query param.
    Redis cache: 'scope:{key_hash}' → JSON list, TTL = api_key_cache_ttl.
    """
    raw_key = request.headers.get(settings.api_key_header) or request.query_params.get(
        settings.api_key_query_param
    )
    if not raw_key:
        return None  # unauthenticated = unrestricted

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    cache_key = f"scope:{key_hash}"

    # Try Redis cache first
    try:
        r = _get_redis()
        cached = await r.get(cache_key)
        if cached is not None:
            return json.loads(cached)
    except Exception:
        pass  # Redis unavailable — fall through to DB

    scope_row = await get_scope_by_key_hash(key_hash)
    if not scope_row:
        raise HTTPException(status_code=401, detail="Invalid API key")

    allowed: list[str] = scope_row["allowed_repos"] or []

    # Cache in Redis
    try:
        r = _get_redis()
        await r.setex(cache_key, settings.api_key_cache_ttl, json.dumps(allowed))
    except Exception:
        pass

    return allowed
