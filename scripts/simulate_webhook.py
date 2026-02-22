"""
Simulate a GitHub push webhook for local end-to-end testing.

Usage:
    # Force re-index one file (drops its merkle hash first):
    python scripts/simulate_webhook.py --file app/shopify.server.ts

    # Simulate a file deletion:
    python scripts/simulate_webhook.py --delete app/routes/app.additional.tsx

    # Combine: modify one file, delete another:
    python scripts/simulate_webhook.py \\
        --file app/shopify.server.ts \\
        --delete app/routes/app.additional.tsx

Environment:
    Reads GITHUB_WEBHOOK_SECRET, DATABASE_URL from .env
    Targets http://localhost:8000/webhook by default (override with --url)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx


# ── Defaults ──────────────────────────────────────────────────────────────────

_REPO_OWNER   = "shubhamsWEB"
_REPO_NAME    = "shopify-chatbot"
_BRANCH       = "main"
_COMMIT_SHA   = "4e86a85c000000000000000000000000deadbeef"  # fake — overridden below
_WEBHOOK_URL  = "http://localhost:8000/webhook"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sign(payload_bytes: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _build_payload(
    files_to_upsert: list[str],
    files_to_delete: list[str],
    commit_sha: str,
) -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    commit_entry = {
        "id": commit_sha,
        "message": "[simulate] test commit",
        "timestamp": ts,
        "author": {"name": "Test Bot", "email": "bot@test.local"},
        "added":    list(files_to_upsert),
        "modified": [],
        "removed":  list(files_to_delete),
    }

    return {
        "ref": f"refs/heads/{_BRANCH}",
        "before": "0" * 40,
        "after": commit_sha,
        "repository": {
            "full_name": f"{_REPO_OWNER}/{_REPO_NAME}",
            "name": _REPO_NAME,
            "owner": {"login": _REPO_OWNER},
            "default_branch": _BRANCH,
        },
        "head_commit": commit_entry,
        "commits": [commit_entry],  # PushEvent.from_dict reads from commits[]
        "pusher": {"name": "Test Bot"},
    }


async def _drop_merkle(files: list[str]) -> None:
    """Remove stored merkle hashes for these files so the pipeline re-indexes them."""
    from src.storage.db import delete_merkle_node
    print(f"  Dropping merkle hashes for {len(files)} file(s)…")
    for f in files:
        await delete_merkle_node(f, _REPO_OWNER, _REPO_NAME)
        print(f"    Dropped: {f}")


async def _get_current_commit_sha() -> str:
    """Fetch the real HEAD commit SHA from the repo (via /health chunk metadata)."""
    try:
        from sqlalchemy import text
        from src.storage.db import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                text("SELECT commit_sha FROM chunks WHERE is_deleted = FALSE LIMIT 1")
            )).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return _COMMIT_SHA


async def run(
    files_to_upsert: list[str],
    files_to_delete: list[str],
    webhook_url: str,
    secret: str,
    drop_merkle: bool,
) -> None:
    # Optionally drop merkle so files are forced through the pipeline
    if drop_merkle and files_to_upsert:
        await _drop_merkle(files_to_upsert)

    commit_sha = await _get_current_commit_sha()
    # Use the REAL commit SHA — fetch_file needs a SHA that exists on GitHub.
    # Dropping the merkle hash ensures the pipeline re-processes even at the same SHA.

    payload = _build_payload(files_to_upsert, files_to_delete, commit_sha)
    payload_bytes = json.dumps(payload).encode()
    signature = _sign(payload_bytes, secret)
    delivery_id = str(uuid.uuid4())

    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": delivery_id,
    }

    print(f"\nSending simulated push webhook → {webhook_url}")
    print(f"  Delivery ID : {delivery_id}")
    print(f"  Commit SHA  : {commit_sha}")
    print(f"  Files upsert: {files_to_upsert or '(none)'}")
    print(f"  Files delete: {files_to_delete or '(none)'}")

    async with httpx.AsyncClient(timeout=15) as client:
        t0 = time.monotonic()
        resp = await client.post(webhook_url, content=payload_bytes, headers=headers)
        elapsed = time.monotonic() - t0

    print(f"\nResponse: HTTP {resp.status_code} ({elapsed*1000:.0f}ms)")
    try:
        data = resp.json()
        print(json.dumps(data, indent=2))
    except Exception:
        print(resp.text)

    if resp.status_code != 202:
        sys.exit(1)

    print(f"\nJob queued. Watch the worker logs for job progress.")
    print(f"Poll /health to see last_indexed update:")
    print(f"  watch -n2 'curl -s http://localhost:8000/health | python3 -m json.tool'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a GitHub push webhook")
    parser.add_argument("--file", "-f", action="append", default=[],
                        dest="files", metavar="PATH",
                        help="File to mark as modified (can repeat). Merkle hash dropped automatically.")
    parser.add_argument("--delete", "-d", action="append", default=[],
                        dest="deletes", metavar="PATH",
                        help="File to mark as deleted (can repeat)")
    parser.add_argument("--url", default=_WEBHOOK_URL,
                        help=f"Webhook URL (default: {_WEBHOOK_URL})")
    parser.add_argument("--no-drop-merkle", action="store_true",
                        help="Don't drop merkle hashes (file may be skipped as unchanged)")
    args = parser.parse_args()

    if not args.files and not args.deletes:
        parser.error("Specify at least one --file or --delete")

    # Load settings
    import os, sys
    sys.path.insert(0, ".")
    from dotenv import load_dotenv
    load_dotenv()
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "dev-webhook-secret")

    asyncio.run(run(
        files_to_upsert=args.files,
        files_to_delete=args.deletes,
        webhook_url=args.url,
        secret=secret,
        drop_merkle=not args.no_drop_merkle,
    ))


if __name__ == "__main__":
    main()
