"""
Redis-backed cache for query embeddings and full search results.
"""

from __future__ import annotations

import hashlib
import json

from src.config import settings


def _get_redis():
    import redis.asyncio as redis

    return redis.from_url(settings.redis_url)


async def get_cached_embedding(query: str) -> list[float] | None:
    """Retrieve embedding vector from Redis if available."""
    try:
        r = _get_redis()
        res = await r.get(f"embed:{query}")
        if res:
            return json.loads(res)
    except Exception:
        pass  # Key not found or Redis is down
    return None


async def set_cached_embedding(query: str, vector: list[float]) -> None:
    """Cache embedding vector in Redis."""
    try:
        r = _get_redis()
        # Cache for 24 hours
        await r.set(f"embed:{query}", json.dumps(vector), ex=86400)
    except Exception:
        pass  # Redis is down, cache miss on next read


# ── Search result cache ───────────────────────────────────────────────────────


def make_search_cache_key(
    query: str,
    repo_owner: str | None,
    repo_name: str | None,
    mode: str,
    language: str | None,
    top_k: int,
) -> str:
    q_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
    return f"search:{repo_owner or '_'}:{repo_name or '_'}:{mode}:{language or '_'}:{top_k}:{q_hash}"


async def get_cached_search_results(cache_key: str) -> list[dict] | None:
    try:
        r = _get_redis()
        res = await r.get(cache_key)
        if res:
            return json.loads(res)
    except Exception:
        pass
    return None


async def set_cached_search_results(
    cache_key: str, results: list[dict], ttl: int = 300
) -> None:
    try:
        r = _get_redis()
        await r.set(cache_key, json.dumps(results), ex=ttl)
    except Exception:
        pass


async def invalidate_search_cache(repo_owner: str, repo_name: str) -> None:
    """Clear all search result cache entries for a repo (call after indexing)."""
    try:
        r = _get_redis()
        pattern = f"search:{repo_owner}:{repo_name}:*"
        async for key in r.scan_iter(pattern):
            await r.delete(key)
    except Exception:
        pass
