"""
Repository management, config inspection, and webhook test endpoints.
Used by the admin dashboard for all configuration flows.

Endpoints
---------
GET    /repos                     — list all repos with live stats
POST   /repos                     — register a new repo
DELETE /repos/{owner}/{name}      — unregister repo + delete all indexed data
POST   /repos/{owner}/{name}/index — trigger a fresh full-index job via RQ
GET    /config                    — current (masked) env configuration
POST   /webhook/ping              — send a self-test ping to /webhook
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.config import settings
from src.utils.sanitize import sanitize_log

router = APIRouter(tags=["management"])

logger = __import__("logging").getLogger(__name__)


# ── Request / response models ─────────────────────────────────────────────────


class RegisterRepoRequest(BaseModel):
    owner: str = Field(..., description="GitHub owner (org or user), or 'local' for local repos")
    name: str = Field(..., description="Repository name")
    branch: str = Field("main", description="Branch to track (GitHub repos only)")
    description: str | None = Field(None, description="Optional description")
    source_type: str = Field("github", pattern="^(github|local)$", description="'github' or 'local'")
    local_path: str | None = Field(None, description="Absolute path on this machine (local repos only)")


class IndexJobResponse(BaseModel):
    job_id: str
    repo: str
    files_found: int
    message: str


# ── Helper: build and enqueue a full-index job ────────────────────────────────


async def _enqueue_full_index(repo_row) -> dict:
    """
    Builds the RQ job payload for a full index run and enqueues it.
    Dispatches to local filesystem walker or GitHub API based on repo source_type.
    Returns job metadata.
    """
    import redis
    from rq import Queue

    from src.storage.db import update_repo_status

    owner = repo_row.owner
    name = repo_row.name

    await update_repo_status(owner, name, "indexing")

    if repo_row.source_type == "local":
        from src.local.fetcher import get_local_commit_meta, walk_indexable_files

        indexable = walk_indexable_files(repo_row.local_path)
        if not indexable:
            await update_repo_status(owner, name, "error")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"No indexable files found under {repo_row.local_path}. "
                "Check SUPPORTED_EXTENSIONS in your config.",
            )

        meta = get_local_commit_meta(repo_row.local_path)
        head_sha = meta["commit_sha"]
        delivery_id = f"full-index-{owner}-{name}-{head_sha[:7]}-{uuid.uuid4().hex[:6]}"
        job_payload = {
            "repo_owner": owner,
            "repo_name": name,
            "local_path": repo_row.local_path,
            "commit_sha": head_sha,
            "commit_author": meta["commit_author"],
            "commit_message": meta["commit_message"],
            "files_to_upsert": indexable,
            "files_to_delete": [],
            "delivery_id": delivery_id,
        }
        branch = meta["branch"]
    else:
        from src.github.fetcher import (
            _GITHUB_API,
            _make_client,
            fetch_full_tree,
            filter_indexable_paths,
        )

        branch = repo_row.branch
        tree = await fetch_full_tree(owner, name, ref=branch)
        all_paths = [item["path"] for item in tree]
        indexable = filter_indexable_paths(all_paths)

        if not indexable:
            await update_repo_status(owner, name, "error")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"No indexable files found in {owner}/{name}@{branch}. "
                "Check SUPPORTED_EXTENSIONS in your config.",
            )

        async with _make_client() as client:
            resp = await client.get(
                f"{_GITHUB_API}/repos/{owner}/{name}/commits/{branch}",
                params={"per_page": 1},
            )
            if resp.status_code == 404:
                await update_repo_status(owner, name, "error")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Repository {owner}/{name} not found or branch '{branch}' doesn't exist.",
                )
            resp.raise_for_status()
            commit_data = resp.json()

        head_sha = commit_data.get("sha", branch)
        commit_author = commit_data.get("commit", {}).get("author", {}).get("email", "")
        commit_message = commit_data.get("commit", {}).get("message", "").splitlines()[0]
        delivery_id = f"full-index-{owner}-{name}-{head_sha[:7]}-{uuid.uuid4().hex[:6]}"
        job_payload = {
            "repo_owner": owner,
            "repo_name": name,
            "commit_sha": head_sha,
            "commit_author": commit_author,
            "commit_message": commit_message,
            "files_to_upsert": indexable,
            "files_to_delete": [],
            "delivery_id": delivery_id,
        }

    conn = redis.from_url(settings.redis_url)
    queue = Queue("indexing", connection=conn)
    job = queue.enqueue(
        "src.pipeline.pipeline.run_incremental_index",
        job_payload,
        job_timeout=3600,
        result_ttl=3600,
    )

    return {
        "job_id": job.id,
        "repo": f"{owner}/{name}",
        "branch": branch,
        "head_sha": head_sha[:7],
        "files_found": len(indexable),
        "delivery_id": delivery_id,
        "message": f"Full index job enqueued for {len(indexable)} files. Start the RQ worker to process.",
    }


# ── Webhook auto-registration helpers ─────────────────────────────────────────


def _manual_webhook_instructions(owner: str, name: str) -> str:
    """Return step-by-step manual webhook setup instructions."""
    wh_url = settings.webhook_url or "<YOUR_SERVER_URL>/webhook"
    return (
        f"To manually configure a webhook for {owner}/{name}:\n"
        f"1. Go to https://github.com/{owner}/{name}/settings/hooks/new\n"
        f"2. Set Payload URL to: {wh_url}\n"
        f"3. Set Content type to: application/json\n"
        f"4. Set Secret to your GITHUB_WEBHOOK_SECRET value\n"
        f"5. Select 'Just the push event'\n"
        f"6. Ensure 'Active' is checked\n"
        f"7. Click 'Add webhook'"
    )


async def _try_auto_register_webhook(owner: str, name: str) -> dict:
    """
    Attempt to auto-register a GitHub webhook for the repo.
    Returns a dict with success, hook_id, message, and optional manual_instructions.
    Never raises — always returns a result dict.
    """
    from src.github.fetcher import WebhookCreationError, create_webhook
    from src.storage.db import update_repo_webhook

    if not settings.webhook_url:
        return {
            "success": False,
            "hook_id": None,
            "message": "PUBLIC_BASE_URL not configured — cannot auto-register webhook.",
            "manual_instructions": _manual_webhook_instructions(owner, name),
        }

    try:
        hook_id = await create_webhook(
            owner, name, settings.webhook_url, settings.github_webhook_secret
        )
        await update_repo_webhook(owner, name, hook_id)
        return {
            "success": True,
            "hook_id": hook_id,
            "message": f"Webhook registered successfully (hook #{hook_id}).",
            "manual_instructions": None,
        }
    except WebhookCreationError as exc:
        logger.warning(
            "Auto-register webhook failed for %s/%s: %s",
            sanitize_log(owner),
            sanitize_log(name),
            sanitize_log(exc),
        )
        return {
            "success": False,
            "hook_id": None,
            "message": "Webhook registration failed. Check logs or set up manually.",
            "manual_instructions": _manual_webhook_instructions(owner, name)
            if exc.manual_instructions
            else None,
        }
    except Exception:
        logger.exception(
            "Unexpected error auto-registering webhook for %s/%s",
            sanitize_log(owner),
            sanitize_log(name),
        )
        return {
            "success": False,
            "hook_id": None,
            "message": "An unexpected error occurred. Check server logs.",
            "manual_instructions": _manual_webhook_instructions(owner, name),
        }


# ── GET /repos ────────────────────────────────────────────────────────────────


@router.get("/repos", summary="List all registered repositories with stats")
async def list_repos() -> JSONResponse:
    from src.storage.db import get_repo_stats

    rows = await get_repo_stats()

    def _fmt(row: dict) -> dict:
        hook_id = row.get("webhook_hook_id")
        return {
            "owner": row["owner"],
            "name": row["name"],
            "repo": f"{row['owner']}/{row['name']}",
            "branch": row["branch"],
            "source_type": row.get("source_type", "github"),
            "local_path": row.get("local_path"),
            "status": row["status"],
            "active_chunks": row["active_chunks"] or 0,
            "deleted_chunks": row["deleted_chunks"] or 0,
            "files": row["files"] or 0,
            "symbols": row["symbols"] or 0,
            "webhook_hook_id": hook_id,
            "webhook_registered": hook_id is not None,
            "registered_at": row["registered_at"].isoformat() if row["registered_at"] else None,
            "last_indexed": row["last_indexed"].isoformat() if row["last_indexed"] else None,
        }

    return JSONResponse([_fmt(r) for r in rows])


# ── POST /repos ───────────────────────────────────────────────────────────────


@router.post("/repos", summary="Register a new repository (and optionally trigger indexing)")
async def register_repo_endpoint(req: RegisterRepoRequest) -> JSONResponse:
    from src.storage.db import register_repo

    # Validate local repos
    if req.source_type == "local":
        if not req.local_path or not os.path.isdir(req.local_path):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="local_path must be an existing directory on this machine.",
            )

    repo = await register_repo(
        owner=req.owner,
        name=req.name,
        branch=req.branch,
        description=req.description or "",
        source_type=req.source_type,
        local_path=req.local_path,
    )

    # Auto-register GitHub webhook (skip for local repos)
    if req.source_type == "local":
        webhook_result = {
            "success": False,
            "hook_id": None,
            "message": "Webhooks are not used for local repos.",
            "manual_instructions": None,
        }
    else:
        webhook_result = await _try_auto_register_webhook(repo.owner, repo.name)

    return JSONResponse(
        {
            "repo": f"{repo.owner}/{repo.name}",
            "branch": repo.branch,
            "source_type": repo.source_type,
            "local_path": repo.local_path,
            "status": repo.status,
            "registered_at": repo.registered_at.isoformat(),
            "webhook": webhook_result,
            "message": f"Registered {repo.owner}/{repo.name}. "
            f"Call POST /repos/{repo.owner}/{repo.name}/index to start indexing.",
        },
        status_code=status.HTTP_201_CREATED,
    )


# ── DELETE /repos/{owner}/{name} ──────────────────────────────────────────────


@router.delete(
    "/repos/{owner}/{name}",
    summary="Unregister a repo and permanently delete all its indexed data",
)
async def delete_repo_endpoint(owner: str, name: str) -> JSONResponse:
    from src.storage.db import delete_repo, get_repos

    # Best-effort: clean up GitHub webhook before deleting
    try:
        repos = await get_repos()
        repo = next((r for r in repos if r.owner == owner and r.name == name), None)
        if repo and repo.webhook_hook_id:
            from src.github.fetcher import delete_webhook

            await delete_webhook(owner, name, repo.webhook_hook_id)
    except Exception:
        logger.warning(
            "Failed to clean up webhook for %s/%s during delete",
            sanitize_log(owner),
            sanitize_log(name),
        )

    deleted = await delete_repo(owner, name)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository {owner}/{name} is not registered.",
        )
    return JSONResponse(
        {
            "repo": f"{owner}/{name}",
            "deleted": True,
            "message": f"All chunks, symbols, merkle nodes, and the repo record for {owner}/{name} have been permanently deleted.",
        }
    )


# ── POST /repos/{owner}/{name}/index ─────────────────────────────────────────


@router.post(
    "/repos/{owner}/{name}/index",
    summary="Trigger a fresh full-index job for an already-registered repository",
)
async def trigger_index(owner: str, name: str) -> JSONResponse:
    from src.storage.db import get_repos

    # Confirm the repo is registered
    repos = await get_repos()
    repo = next((r for r in repos if r.owner == owner and r.name == name), None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository {owner}/{name} is not registered. Call POST /repos first.",
        )

    result = await _enqueue_full_index(repo)
    return JSONResponse(result, status_code=status.HTTP_202_ACCEPTED)


# ── GET /config ───────────────────────────────────────────────────────────────


def _mask(value: str | None, show: int = 4) -> str:
    """Show first `show` chars then *** — or 'not set' if empty."""
    if not value:
        return "not set"
    if len(value) <= show:
        return "***"
    return value[:show] + "***"


@router.get("/config", summary="Show current (masked) server configuration")
async def get_config() -> JSONResponse:
    s = settings
    return JSONResponse(
        {
            "github": {
                "token": _mask(s.github_token),
                "app_id": str(s.github_app_id) if s.github_app_id else "not set",
                "app_private_key_path": s.github_app_private_key_path or "not set",
                "webhook_secret": _mask(s.github_webhook_secret),
                "default_branch": s.github_default_branch,
            },
            "database": {
                "url": _mask(s.database_url, show=20),
                "pool_size": s.db_pool_size,
                "max_overflow": s.db_max_overflow,
            },
            "redis": {
                "url": s.redis_url,
            },
            "embeddings": {
                "voyage_api_key": _mask(s.voyage_api_key),
                "model": s.embedding_model,
                "dimensions": s.embedding_dimensions,
                "batch_size": s.embedding_batch_size,
            },
            "auth": {
                "jwt_secret": _mask(s.jwt_secret),
                "jwt_expiry_hours": s.jwt_expiry_hours,
                "oauth_client_id": _mask(s.github_oauth_client_id)
                if s.github_oauth_client_id
                else "not set",
            },
            "indexing": {
                "chunk_target_tokens": s.chunk_target_tokens,
                "chunk_overlap_tokens": s.chunk_overlap_tokens,
                "chunk_min_tokens": s.chunk_min_tokens,
                "context_token_budget": s.context_token_budget,
                "supported_extensions": s.supported_extensions,
                "ignore_patterns": s.ignore_patterns,
            },
            "reranker": {
                "model": s.reranker_model,
                "top_n": s.reranker_top_n,
            },
            "webhook": {
                "public_base_url": s.public_base_url or "not set",
                "webhook_url": s.webhook_url or "not set",
            },
            "optional": {
                "anthropic_api_key": _mask(s.anthropic_api_key)
                if s.anthropic_api_key
                else "not set",
            },
        }
    )


# ── POST /config/env ──────────────────────────────────────────────────────────


class EnvUpdateRequest(BaseModel):
    updates: dict[str, str] = Field(
        ...,
        description="Key-value pairs to write to the .env file",
        examples=[{"VOYAGE_API_KEY": "pa-xxx", "GITHUB_TOKEN": "ghp-xxx"}],
    )


@router.post("/config/env", summary="Write one or more values to the .env file")
async def update_env(req: EnvUpdateRequest) -> JSONResponse:
    """
    Reads the existing .env file, updates or appends the given keys,
    and writes it back. Does not restart the server — a restart is needed
    for changes to take effect.
    """
    from pathlib import Path

    env_path = Path(".env")

    # Read existing lines
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    else:
        lines = []

    # Build a map of existing keys → line index
    key_to_line: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip().upper()
            key_to_line[key] = i

    # Apply updates
    updated_keys = []
    for key, value in req.updates.items():
        key = key.upper()
        new_line = f"{key}={value}"
        if key in key_to_line:
            lines[key_to_line[key]] = new_line
        else:
            lines.append(new_line)
        updated_keys.append(key)

    env_path.write_text("\n".join(lines) + "\n")

    return JSONResponse(
        {
            "updated": updated_keys,
            "message": "Values written to .env. Restart the server for changes to take effect.",
        }
    )


# ── POST /webhook/ping ────────────────────────────────────────────────────────


@router.post(
    "/webhook/ping", summary="Send a self-test ping to verify the webhook endpoint is live"
)
async def webhook_ping() -> JSONResponse:
    """
    Constructs a valid GitHub-style ping payload, signs it with
    GITHUB_WEBHOOK_SECRET, and POSTs it to /webhook.
    Returns the response from the webhook handler.
    """
    import json

    payload = {
        "zen": "Non-blocking is better than blocking.",
        "hook_id": 0,
        "hook": {"type": "Repository", "events": ["push"]},
    }
    body = json.dumps(payload).encode()

    # Sign exactly as GitHub does
    sig = (
        "sha256="
        + hmac.new(
            settings.github_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    )

    delivery_id = f"self-test-{uuid.uuid4().hex[:8]}"

    try:
        async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=10.0) as client:
            resp = await client.post(
                "/webhook",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "ping",
                    "X-Hub-Signature-256": sig,
                    "X-GitHub-Delivery": delivery_id,
                },
            )
        return JSONResponse(
            {
                "ok": resp.status_code == 200,
                "status_code": resp.status_code,
                "delivery_id": delivery_id,
                "response": resp.json(),
            }
        )
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not connect to /webhook — is the API server running on port 8000?",
        ) from exc


# ── GET /jobs ─────────────────────────────────────────────────────────────────


@router.get("/jobs", summary="List recent RQ indexing jobs and their status")
async def list_jobs() -> JSONResponse:
    """
    Returns the last 20 jobs from the RQ indexing queue:
    queued, started, finished, and failed.
    """
    import redis
    from rq import Queue
    from rq.job import Job
    from rq.registry import FailedJobRegistry, FinishedJobRegistry, StartedJobRegistry

    conn = redis.from_url(settings.redis_url)
    queue = Queue("indexing", connection=conn)

    jobs: list[dict] = []

    def _fetch(job_ids: list[str], state: str) -> None:
        for jid in job_ids[:20]:
            try:
                j = Job.fetch(jid, connection=conn)
                jobs.append(
                    {
                        "id": jid,
                        "state": state,
                        "enqueued_at": j.enqueued_at.isoformat() if j.enqueued_at else None,
                        "started_at": j.started_at.isoformat() if j.started_at else None,
                        "ended_at": j.ended_at.isoformat() if j.ended_at else None,
                        "result": j.result if state == "finished" else None,
                        "exc_info": str(j.exc_info)[:200] if j.exc_info else None,
                    }
                )
            except Exception:
                pass

    _fetch(queue.job_ids, "queued")
    _fetch(StartedJobRegistry("indexing", connection=conn).get_job_ids(), "started")
    _fetch(FinishedJobRegistry("indexing", connection=conn).get_job_ids(), "finished")
    _fetch(FailedJobRegistry("indexing", connection=conn).get_job_ids(), "failed")

    # Sort by enqueued_at desc
    jobs.sort(key=lambda j: j.get("enqueued_at") or "", reverse=True)

    return JSONResponse({"jobs": jobs[:20], "queued_count": queue.count})


# ── Webhook CRUD endpoints ───────────────────────────────────────────────────


@router.post(
    "/repos/{owner}/{name}/webhook",
    summary="Register a GitHub webhook for a repository",
)
async def register_webhook(owner: str, name: str) -> JSONResponse:
    """One-click webhook registration for a repo."""
    from src.storage.db import get_repos

    repos = await get_repos()
    repo = next((r for r in repos if r.owner == owner and r.name == name), None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository {owner}/{name} is not registered.",
        )

    result = await _try_auto_register_webhook(owner, name)
    status_code = (
        status.HTTP_201_CREATED if result["success"] else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    return JSONResponse(result, status_code=status_code)


@router.delete(
    "/repos/{owner}/{name}/webhook",
    summary="Remove the GitHub webhook for a repository",
)
async def remove_webhook(owner: str, name: str) -> JSONResponse:
    """Delete the webhook from GitHub and clear the hook ID in the DB."""
    from src.github.fetcher import delete_webhook
    from src.storage.db import get_repos, update_repo_webhook

    repos = await get_repos()
    repo = next((r for r in repos if r.owner == owner and r.name == name), None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository {owner}/{name} is not registered.",
        )

    if not repo.webhook_hook_id:
        return JSONResponse(
            {"success": False, "message": "No webhook registered for this repo."},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    deleted = await delete_webhook(owner, name, repo.webhook_hook_id)
    await update_repo_webhook(owner, name, None)

    return JSONResponse(
        {
            "success": True,
            "deleted_from_github": deleted,
            "message": f"Webhook #{repo.webhook_hook_id} removed."
            + ("" if deleted else " (Note: webhook was already gone from GitHub)"),
        }
    )


@router.get(
    "/repos/{owner}/{name}/webhook",
    summary="Check webhook status from GitHub API",
)
async def check_webhook(owner: str, name: str) -> JSONResponse:
    """Query GitHub for the current status of the registered webhook."""
    from src.github.fetcher import get_webhook_status
    from src.storage.db import get_repos

    repos = await get_repos()
    repo = next((r for r in repos if r.owner == owner and r.name == name), None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository {owner}/{name} is not registered.",
        )

    if not repo.webhook_hook_id:
        return JSONResponse(
            {
                "registered": False,
                "hook_id": None,
                "message": "No webhook registered for this repo.",
                "manual_instructions": _manual_webhook_instructions(owner, name),
            }
        )

    hook_status = await get_webhook_status(owner, name, repo.webhook_hook_id)
    if hook_status is None:
        # Hook was registered but no longer exists on GitHub
        from src.storage.db import update_repo_webhook

        await update_repo_webhook(owner, name, None)
        return JSONResponse(
            {
                "registered": False,
                "hook_id": repo.webhook_hook_id,
                "message": f"Webhook #{repo.webhook_hook_id} no longer exists on GitHub. DB record cleared.",
                "manual_instructions": _manual_webhook_instructions(owner, name),
            }
        )

    return JSONResponse(
        {
            "registered": True,
            "hook_id": repo.webhook_hook_id,
            "github_status": hook_status,
        }
    )
