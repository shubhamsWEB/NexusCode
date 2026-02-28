"""
Redis-backed cache for query embeddings to save cost and latency.
"""
from __future__ import annotations

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
        pass
    return None

async def set_cached_embedding(query: str, vector: list[float]) -> None:
    """Cache embedding vector in Redis."""
    try:
        r = _get_redis()
        # Cache for 24 hours
        await r.set(f"embed:{query}", json.dumps(vector), ex=86400)
    except Exception:
        pass
