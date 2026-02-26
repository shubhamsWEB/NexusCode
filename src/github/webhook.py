"""
FastAPI webhook receiver for GitHub push events.
- Verifies HMAC-SHA256 signature (X-Hub-Signature-256)
- Parses push payload into PushEvent
- Filters to configured branch (main/master)
- Filters indexable files
- Enqueues indexing job to Redis
- Must return HTTP 200/202 within 10 seconds (GitHub requirement)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from rq import Queue

from src.config import settings
from src.github.events import PushEvent
from src.github.fetcher import filter_indexable_paths
from src.storage.db import log_webhook_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])

# Redis queue is initialised lazily so tests don't require a running Redis
_queue: Queue | None = None


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        import redis

        conn = redis.from_url(settings.redis_url)
        _queue = Queue("indexing", connection=conn)
    return _queue


def _is_duplicate_job(owner: str, repo: str, commit_sha: str) -> bool:
    """Check if an indexing job for this exact commit is already queued/running."""
    import redis as _redis
    conn = _redis.from_url(settings.redis_url)
    dedup_key = f"index:{owner}/{repo}:{commit_sha}"
    # SET NX with 60s TTL — returns True only if the key was newly set
    was_set = conn.set(dedup_key, "1", nx=True, ex=60)
    return not was_set  # True if key already existed (duplicate)


# ── HMAC verification ─────────────────────────────────────────────────────────


def _verify_signature(body: bytes, signature_header: str) -> bool:
    """
    Constant-time comparison of GitHub's HMAC-SHA256 signature.
    signature_header format: "sha256=<hex_digest>"
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = (
        "sha256="
        + hmac.new(
            settings.github_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature_header)


# ── Webhook endpoint ──────────────────────────────────────────────────────────


@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(..., alias="X-GitHub-Event"),
    x_hub_signature_256: str = Header("", alias="X-Hub-Signature-256"),
    x_github_delivery: str = Header("", alias="X-GitHub-Delivery"),
) -> JSONResponse:
    """
    Receives GitHub webhook events.
    Heavy processing is offloaded to the Redis queue worker.
    """
    body = await request.body()

    # 1. Verify signature
    if not _verify_signature(body, x_hub_signature_256):
        logger.warning("Webhook signature mismatch — delivery_id=%s", x_github_delivery)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload: dict[str, Any] = await request.json()

    # 2. Only process push events
    if x_github_event == "ping":
        logger.info("GitHub ping received — webhook configured correctly")
        return JSONResponse({"message": "pong"})

    if x_github_event != "push":
        logger.debug("Ignoring event type: %s", x_github_event)
        return JSONResponse({"message": f"event '{x_github_event}' ignored"})

    # 3. Parse push payload
    event = PushEvent.from_dict(payload, delivery_id=x_github_delivery)

    # 4. Only track the configured branch
    if event.branch != settings.github_default_branch:
        logger.debug(
            "Ignoring push to branch '%s' (tracking '%s')",
            event.branch,
            settings.github_default_branch,
        )
        return JSONResponse({"message": f"branch '{event.branch}' not tracked"})

    # 5. Filter to indexable files
    files_to_upsert = filter_indexable_paths(event.files_to_upsert)
    files_to_delete = event.files_to_delete  # deletions always processed

    total_files = len(files_to_upsert) + len(files_to_delete)
    logger.info(
        "Push event %s: %s/%s@%s — upsert=%d delete=%d",
        x_github_delivery,
        event.repo_owner,
        event.repo_name,
        event.after[:7],
        len(files_to_upsert),
        len(files_to_delete),
    )

    # 6. Log to DB (fire-and-forget, don't block response)
    try:
        await log_webhook_event(
            delivery_id=x_github_delivery,
            event_type=x_github_event,
            repo_owner=event.repo_owner,
            repo_name=event.repo_name,
            commit_sha=event.after,
            files_changed=total_files,
        )
    except Exception as exc:
        logger.warning("Failed to log webhook event: %s", exc)

    # 7. Enqueue indexing job (non-blocking)
    if files_to_upsert or files_to_delete:
        if _is_duplicate_job(event.repo_owner, event.repo_name, event.after):
            logger.info(
                "Skipping duplicate indexing job for %s/%s@%s",
                event.repo_owner,
                event.repo_name,
                event.after[:7],
            )
        else:
            job_payload = {
                "repo_owner": event.repo_owner,
                "repo_name": event.repo_name,
                "commit_sha": event.after,
                "commit_author": event.head_commit_author,
                "commit_message": event.head_commit_message,
                "files_to_upsert": files_to_upsert,
                "files_to_delete": files_to_delete,
                "delivery_id": x_github_delivery,
            }
            try:
                queue = get_queue()
                job = queue.enqueue(
                    "src.pipeline.pipeline.run_incremental_index",
                    job_payload,
                    job_timeout=600,  # 10 min max per job
                    result_ttl=3600,
                )
                logger.info("Enqueued job %s for %d files", job.id, total_files)
            except Exception as exc:
                logger.error("Failed to enqueue indexing job: %s", exc)
                # Don't fail the webhook response — GitHub would retry
    else:
        logger.info("No indexable files changed — nothing to enqueue")

    return JSONResponse(
        {
            "delivery_id": x_github_delivery,
            "repo": f"{event.repo_owner}/{event.repo_name}",
            "commit": event.after[:7],
            "files_to_upsert": len(files_to_upsert),
            "files_to_delete": len(files_to_delete),
            "status": "queued",
        },
        status_code=status.HTTP_202_ACCEPTED,
    )
