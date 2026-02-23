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

logger = logging.getLogger(__name__)

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
) -> tuple[str, str] | None:
    """
    Fetch a single file's content from GitHub at a specific ref (commit SHA or branch).

    Returns (content_str, blob_sha) or None if the file doesn't exist / is binary.
    Rate limit: 5,000 req/hr (PAT) or 15,000 req/hr (GitHub App).
    """
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    async with _make_client() as client:
        resp = await client.get(url, params={"ref": ref})

    if resp.status_code == 404:
        logger.debug("fetch_file: not found %s/%s@%s %s", owner, repo, ref, path)
        return None

    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise RuntimeError(f"GitHub API rate limit hit fetching {path}")

    resp.raise_for_status()
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
) -> list[dict]:
    """
    Fetch the complete file tree for a repo at a given ref using the Git Trees API.
    Returns one API call (recursive=1) instead of one-per-file.

    Each item in the returned list is:
      {"path": "src/foo.py", "sha": "<blob_sha>", "size": 1234, "type": "blob"}
    """
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}"
    async with _make_client() as client:
        resp = await client.get(url, params={"recursive": "1"})

    if resp.status_code == 409:
        # Empty repo
        return []

    resp.raise_for_status()
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
