#!/usr/bin/env python3
"""
Trigger a full initial index of a GitHub repository.
This fetches ALL indexable files via the Git Trees API and queues them for processing.

Usage:
    python scripts/full_index.py <owner> <repo> [--branch main]

Example:
    python scripts/full_index.py octocat Hello-World --branch main
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

from src.config import settings
from src.github.fetcher import fetch_full_tree, filter_indexable_paths
from src.storage.db import get_index_stats, register_repo, update_repo_status


async def main(owner: str, repo: str, branch: str) -> None:
    print(f"\nStarting full index: {owner}/{repo}@{branch}\n")

    # Register the repo in the DB
    await register_repo(owner, repo, branch=branch)
    await update_repo_status(owner, repo, "indexing")

    # Fetch the complete file tree (single API call)
    logger.info("Fetching file tree from GitHub...")
    tree = await fetch_full_tree(owner, repo, ref=branch)
    all_paths = [item["path"] for item in tree]
    indexable = filter_indexable_paths(all_paths)

    print(f"  Total files in repo : {len(all_paths)}")
    print(f"  Indexable files     : {len(indexable)}")
    print(f"  Skipped             : {len(all_paths) - len(indexable)}")

    if not indexable:
        print("No indexable files found. Check SUPPORTED_EXTENSIONS in .env")
        return

    # Enqueue one job per file (the pipeline worker handles each file)
    import redis
    from rq import Queue

    conn = redis.from_url(settings.redis_url)
    queue = Queue("indexing", connection=conn)

    # Find the current HEAD commit SHA
    from src.github.fetcher import _GITHUB_API, _make_client

    async with _make_client() as client:
        resp = await client.get(
            f"{_GITHUB_API}/repos/{owner}/{repo}/commits/{branch}",
            params={"per_page": 1},
        )
        resp.raise_for_status()
        commit_data = resp.json()
        head_sha = commit_data.get("sha", branch)
        commit_author = commit_data.get("commit", {}).get("author", {}).get("email", "")
        commit_message = commit_data.get("commit", {}).get("message", "").splitlines()[0]

    print(f"\n  HEAD commit: {head_sha[:7]}  ({commit_message[:60]})")
    print(f"  Enqueueing {len(indexable)} indexing jobs...\n")

    job_payload = {
        "repo_owner": owner,
        "repo_name": repo,
        "commit_sha": head_sha,
        "commit_author": commit_author,
        "commit_message": commit_message,
        "files_to_upsert": indexable,
        "files_to_delete": [],
        "delivery_id": f"full-index-{owner}-{repo}",
    }

    job = queue.enqueue(
        "src.pipeline.pipeline.run_incremental_index",
        job_payload,
        job_timeout=3600,  # 1 hour for large repos
        result_ttl=3600,
    )

    print(f"  Job ID : {job.id}")
    print(f"  Queue  : indexing ({queue.count} jobs pending)")
    print("\nFull index job enqueued. Start the worker to process:")
    print("  python -m rq worker indexing --url $REDIS_URL\n")

    # Print current DB stats
    stats = await get_index_stats()
    print("Current index stats:")
    for k, v in stats.items():
        print(f"  {k:15s}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trigger full index of a GitHub repo")
    parser.add_argument("owner", help="GitHub repo owner (org or user)")
    parser.add_argument("repo", help="GitHub repo name")
    parser.add_argument("--branch", default=settings.github_default_branch, help="Branch to index")
    args = parser.parse_args()

    asyncio.run(main(args.owner, args.repo, args.branch))
