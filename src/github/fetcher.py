"""
GitHub REST API client.
Fetches raw file content and directory trees at specific commit SHAs.
No local git clone — everything goes through the API.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator

import httpx

from src.config import settings
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_GITHUB_API = "https://api.github.com"
_DEFAULT_TIMEOUT = 30.0


def _auth_headers() -> dict[str, str]:
    """Build Authorization header from config (PAT or GitHub App token)."""
    token = settings.github_token
    if not token:
        raise RuntimeError(
            "No GitHub credentials configured. "
            "Set GITHUB_TOKEN (or GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH) in .env"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=_auth_headers(),
        timeout=_DEFAULT_TIMEOUT,
        follow_redirects=True,
    )


# ── Public API ────────────────────────────────────────────────────────────────


async def fetch_file(
    owner: str,
    repo: str,
    path: str,
    ref: str,
    *,
    _max_retries: int = 3,
    _base_delay: float = 60.0,
) -> tuple[str, str] | None:
    """
    Fetch a single file's content from GitHub at a specific ref (commit SHA or branch).

    Returns (content_str, blob_sha) or None if the file doesn't exist / is binary.
    Rate limit: 5,000 req/hr (PAT) or 15,000 req/hr (GitHub App).

    Automatically retries with exponential backoff on rate-limit (403) responses.
    """
    import asyncio

    url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{path}"

    for attempt in range(_max_retries + 1):
        async with _make_client() as client:
            resp = await client.get(url, params={"ref": ref})

        if resp.status_code == 404:
            logger.debug("fetch_file: not found %s/%s@%s %s", owner, repo, ref, path)
            return None

        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            if attempt < _max_retries:
                delay = _base_delay * (2 ** attempt)  # 60s, 120s, 240s
                logger.warning(
                    "Rate limit hit fetching %s (attempt %d/%d). "
                    "Waiting %.0fs before retry...",
                    path, attempt + 1, _max_retries, delay,
                )
                await asyncio.sleep(delay)
                continue
            raise RuntimeError(f"GitHub API rate limit hit fetching {path} (exhausted retries)")

        resp.raise_for_status()
        break

    data = resp.json()

    # GitHub returns base64-encoded content; skip non-file objects
    if data.get("type") != "file":
        return None

    encoding = data.get("encoding", "")
    raw_content = data.get("content", "")
    blob_sha = data.get("sha", "")

    if encoding != "base64":
        logger.warning("Unexpected encoding '%s' for %s — skipping", encoding, path)
        return None

    try:
        content_bytes = base64.b64decode(raw_content)
        content_str = content_bytes.decode("utf-8")
    except (UnicodeDecodeError, Exception):
        logger.debug("fetch_file: binary file skipped %s", path)
        return None

    return content_str, blob_sha


async def fetch_full_tree(
    owner: str,
    repo: str,
    ref: str,
    *,
    _max_retries: int = 3,
    _base_delay: float = 60.0,
) -> list[dict]:
    """
    Fetch the complete file tree for a repo at a given ref using the Git Trees API.
    Returns one API call (recursive=1) instead of one-per-file.

    Each item in the returned list is:
      {"path": "src/foo.py", "sha": "<blob_sha>", "size": 1234, "type": "blob"}

    Automatically retries with backoff on rate-limit (403) responses.
    """
    import asyncio
    import time

    url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}"

    for attempt in range(_max_retries + 1):
        async with _make_client() as client:
            resp = await client.get(url, params={"recursive": "1"})

        if resp.status_code == 409:
            # Empty repo
            return []

        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            if attempt < _max_retries:
                # Try to use the X-RateLimit-Reset header for exact wait time
                reset_ts = resp.headers.get("x-ratelimit-reset")
                if reset_ts:
                    delay = max(int(reset_ts) - int(time.time()) + 5, 10)  # +5s buffer
                else:
                    delay = _base_delay * (2 ** attempt)
                logger.warning(
                    "Rate limit hit fetching tree for %s/%s (attempt %d/%d). "
                    "Waiting %ds before retry...",
                    owner, repo, attempt + 1, _max_retries, delay,
                )
                await asyncio.sleep(delay)
                continue
            raise RuntimeError(
                f"GitHub API rate limit hit fetching tree for {owner}/{repo} (exhausted retries)"
            )

        resp.raise_for_status()
        break

    data = resp.json()

    if data.get("truncated"):
        logger.warning(
            "Tree response truncated for %s/%s — repo may be too large for single tree call",
            owner,
            repo,
        )

    return [
        item
        for item in data.get("tree", [])
        if item.get("type") == "blob"  # skip trees (directories)
    ]


def filter_indexable_paths(
    paths: list[str],
    supported_extensions: set[str] | None = None,
    ignore_patterns: list[str] | None = None,
) -> list[str]:
    """
    Filter a list of file paths to only those we should index.
    Uses settings defaults if not provided.
    """
    if supported_extensions is None:
        supported_extensions = settings.supported_extensions_set
    if ignore_patterns is None:
        ignore_patterns = settings.ignore_patterns_list

    result = []
    for path in paths:
        # Check extension
        if not any(path.endswith(ext) for ext in supported_extensions):
            continue
        # Check ignore patterns (any segment of the path matches)
        if any(pattern in path for pattern in ignore_patterns):
            continue
        result.append(path)
    return result


async def fetch_indexable_files(
    owner: str,
    repo: str,
    ref: str,
) -> AsyncIterator[tuple[str, str, str]]:
    """
    High-level helper: yields (path, content, blob_sha) for every indexable file
    in the repo at the given ref.

    Uses fetch_full_tree for a single tree listing, then fetches each file.
    Skips files whose blob_sha hasn't changed (pass a known_sha dict to check).
    """
    tree = await fetch_full_tree(owner, repo, ref)
    all_paths = [item["path"] for item in tree]
    indexable = filter_indexable_paths(all_paths)
    blob_sha_map = {item["path"]: item["sha"] for item in tree}

    logger.info(
        "fetch_indexable_files: %d/%d files to index for %s/%s@%s",
        len(indexable),
        len(all_paths),
        owner,
        repo,
        ref,
    )

    for path in indexable:
        result = await fetch_file(owner, repo, path, ref)
        if result is None:
            continue
        content, blob_sha = result
        yield path, content, blob_sha_map.get(path, blob_sha)


# ── Webhook Management ────────────────────────────────────────────────────────


class WebhookCreationError(Exception):
    """Raised when a GitHub webhook cannot be created."""

    def __init__(self, message: str, *, manual_instructions: bool = False):
        super().__init__(message)
        self.manual_instructions = manual_instructions


async def create_webhook(
    owner: str,
    repo: str,
    webhook_url: str,
    webhook_secret: str,
) -> int:
    """
    Create a GitHub webhook for push events on the given repo.
    Returns the hook ID on success.
    Raises WebhookCreationError on failure.
    """
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/hooks"
    payload = {
        "name": "web",
        "active": True,
        "events": ["push"],
        "config": {
            "url": webhook_url,
            "content_type": "json",
            "secret": webhook_secret,
            "insecure_ssl": "0",
        },
    }

    async with _make_client() as client:
        resp = await client.post(url, json=payload)

    if resp.status_code == 201:
        data = resp.json()
        hook_id = data.get("id")
        logger.info("Webhook created: hook_id=%s for %s/%s", hook_id, owner, repo)
        return hook_id

    if resp.status_code == 422:
        # Likely "Hook already exists" — try to find and return existing
        existing = await _find_existing_webhook(owner, repo, webhook_url)
        if existing:
            logger.info("Webhook already exists: hook_id=%s for %s/%s", existing, owner, repo)
            return existing
        raise WebhookCreationError(
            f"GitHub returned 422 for {owner}/{repo} but could not find existing webhook. "
            f"Response: {resp.text[:200]}",
            manual_instructions=True,
        )

    if resp.status_code == 403:
        raise WebhookCreationError(
            f"Permission denied creating webhook for {owner}/{repo}. "
            "Your GitHub token needs 'admin:repo_hook' scope (or the GitHub App needs Webhooks write permission).",
            manual_instructions=True,
        )

    if resp.status_code == 404:
        raise WebhookCreationError(
            f"Repository {owner}/{repo} not found on GitHub. "
            "Check the owner/name and that your token has access.",
            manual_instructions=True,
        )

    raise WebhookCreationError(
        f"Unexpected GitHub API response ({resp.status_code}) creating webhook for {owner}/{repo}: "
        f"{resp.text[:200]}",
        manual_instructions=True,
    )


async def delete_webhook(owner: str, repo: str, hook_id: int) -> bool:
    """Delete a GitHub webhook. Returns True if deleted, False if not found."""
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/hooks/{hook_id}"
    async with _make_client() as client:
        resp = await client.delete(url)

    if resp.status_code == 204:
        logger.info("Webhook deleted: hook_id=%s for %s/%s", hook_id, owner, repo)
        return True
    if resp.status_code == 404:
        logger.warning("Webhook not found: hook_id=%s for %s/%s", hook_id, owner, repo)
        return False

    logger.error(
        "Failed to delete webhook hook_id=%s for %s/%s: %s %s",
        hook_id,
        owner,
        repo,
        resp.status_code,
        resp.text[:200],
    )
    return False


async def get_webhook_status(owner: str, repo: str, hook_id: int) -> dict | None:
    """Get the status of a GitHub webhook. Returns hook data dict or None if not found."""
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/hooks/{hook_id}"
    async with _make_client() as client:
        resp = await client.get(url)

    if resp.status_code == 200:
        data = resp.json()
        return {
            "id": data.get("id"),
            "active": data.get("active"),
            "events": data.get("events", []),
            "url": data.get("config", {}).get("url"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "last_response": data.get("last_response"),
        }
    if resp.status_code == 404:
        return None

    logger.warning(
        "Unexpected response checking webhook hook_id=%s for %s/%s: %s",
        hook_id,
        owner,
        repo,
        resp.status_code,
    )
    return None


async def _find_existing_webhook(owner: str, repo: str, webhook_url: str) -> int | None:
    """Search existing webhooks for one matching our URL. Returns hook_id or None."""
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/hooks"
    async with _make_client() as client:
        resp = await client.get(url)

    if resp.status_code != 200:
        return None

    for hook in resp.json():
        config_url = hook.get("config", {}).get("url", "")
        if config_url == webhook_url:
            return hook.get("id")
    return None
