"""
Local filesystem fetcher.

Provides the same interface as src.github.fetcher for the pipeline:
  read_file(local_path, rel_path) -> (content, sha256) | None
  walk_indexable_files(local_path) -> list[str]
  get_local_commit_meta(local_path) -> dict
"""

from __future__ import annotations

import datetime
import hashlib
import os
import subprocess
from pathlib import Path

from src.github.fetcher import filter_indexable_paths


def read_file(local_path: str, rel_path: str) -> tuple[str, str] | None:
    """
    Read a file from the local filesystem.
    Returns (content, sha256_of_content) or None if the file is binary or missing.
    """
    try:
        content = (Path(local_path) / rel_path).read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    blob_sha = hashlib.sha256(content.encode()).hexdigest()
    return content, blob_sha


def walk_indexable_files(local_path: str) -> list[str]:
    """
    Return relative paths of all indexable files under local_path.
    Skips hidden directories (e.g. .git, .venv).
    """
    base = Path(local_path)
    all_paths: list[str] = []
    for root, dirs, files in os.walk(base):
        # Skip hidden directories in-place so os.walk doesn't descend into them
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            rel = Path(root).relative_to(base) / f
            all_paths.append(str(rel))
    return filter_indexable_paths(all_paths)


def get_local_commit_meta(local_path: str) -> dict:
    """
    Return git HEAD metadata if a .git directory exists, otherwise synthetic values.
    """
    git_dir = Path(local_path) / ".git"
    if git_dir.exists():
        try:
            def _git(*args: str) -> str:
                return subprocess.check_output(
                    ["git", *args],
                    cwd=local_path,
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()

            return {
                "commit_sha": _git("rev-parse", "HEAD"),
                "commit_author": _git("log", "-1", "--format=%an"),
                "commit_message": _git("log", "-1", "--format=%s"),
                "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            }
        except Exception:
            pass

    # Fallback — no .git or git command failed
    ts = datetime.datetime.utcnow().isoformat()
    sha = hashlib.sha256(ts.encode()).hexdigest()[:40]
    return {
        "commit_sha": sha,
        "commit_author": "local",
        "commit_message": "local index",
        "branch": "local",
    }
