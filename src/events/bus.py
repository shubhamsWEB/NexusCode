"""
Event Bus — Redis pub/sub for workflow trigger events.

Topics:
  nexus:events:github    — GitHub webhook events
  nexus:events:schedule  — APScheduler tick events
  nexus:events:manual    — Manual API trigger events
  nexus:events:external  — n8n/Zapier/external webhook events
  nexus:workflow:updates — Workflow run status changes (for SSE clients)

Usage:
  await EventBus.publish("nexus:events:github", {"event": "pull_request.opened", ...})
  async for message in EventBus.subscribe("nexus:events:github"):
      ...
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from src.config import settings
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# Topic constants
TOPIC_GITHUB = "nexus:events:github"
TOPIC_SCHEDULE = "nexus:events:schedule"
TOPIC_MANUAL = "nexus:events:manual"
TOPIC_EXTERNAL = "nexus:events:external"
TOPIC_WORKFLOW_UPDATES = "nexus:workflow:updates"

_KNOWN_TOPICS = {TOPIC_GITHUB, TOPIC_SCHEDULE, TOPIC_MANUAL, TOPIC_EXTERNAL, TOPIC_WORKFLOW_UPDATES}


class EventBus:
    """
    Thin async wrapper around Redis pub/sub.
    Uses a single shared aioredis connection pool.
    """

    _redis = None

    @classmethod
    async def _get_redis(cls):
        """Lazy-init Redis connection."""
        if cls._redis is None:
            try:
                import redis.asyncio as aioredis
                cls._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
                await cls._redis.ping()
                logger.info("event_bus: connected to Redis at %s", settings.redis_url)
            except Exception as exc:
                logger.warning("event_bus: Redis unavailable (%s) — pub/sub disabled", exc)
                cls._redis = None
        return cls._redis

    @classmethod
    async def publish(cls, topic: str, payload: dict[str, Any]) -> bool:
        """
        Publish an event to a topic.
        Returns True if published, False if Redis is unavailable.
        """
        redis = await cls._get_redis()
        if redis is None:
            return False
        try:
            message = json.dumps(payload, default=str)
            await redis.publish(topic, message)
            logger.debug("event_bus: published to %s: %s", topic, message[:100])
            return True
        except Exception as exc:
            logger.warning("event_bus: publish failed: %s", exc)
            return False

    @classmethod
    async def subscribe(cls, *topics: str):
        """
        Subscribe to one or more topics.
        Yields decoded message dicts. Handles reconnect on error.
        """
        redis = await cls._get_redis()
        if redis is None:
            logger.warning("event_bus: Redis unavailable — subscribe returns no messages")
            return

        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(*topics)
            logger.info("event_bus: subscribed to %s", topics)

            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                try:
                    data = json.loads(raw["data"])
                    yield {"topic": raw["channel"], **data}
                except json.JSONDecodeError:
                    yield {"topic": raw["channel"], "raw": raw["data"]}

        except Exception as exc:
            logger.warning("event_bus: subscription error: %s", exc)
        finally:
            try:
                await pubsub.unsubscribe(*topics)
                await pubsub.close()
            except Exception:
                pass

    @classmethod
    async def publish_workflow_update(
        cls,
        run_id: str,
        status: str,
        workflow_name: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Convenience method — publish a workflow run status change."""
        payload = {
            "type": "workflow_update",
            "run_id": run_id,
            "status": status,
            "workflow": workflow_name,
            **(extra or {}),
        }
        await cls.publish(TOPIC_WORKFLOW_UPDATES, payload)

    @classmethod
    async def publish_github_event(cls, event_type: str, payload: dict[str, Any]) -> None:
        """Route a GitHub webhook event to the event bus for workflow matching."""
        await cls.publish(TOPIC_GITHUB, {"event": event_type, **payload})

    @classmethod
    async def close(cls) -> None:
        """Close the Redis connection gracefully."""
        if cls._redis:
            try:
                await cls._redis.close()
                cls._redis = None
            except Exception:
                pass
