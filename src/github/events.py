"""
Dataclasses for GitHub webhook push event payloads.
We only model the fields we actually use; unknown fields are ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GitHubCommit:
    id: str
    message: str
    author_email: str
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "GitHubCommit":
        return cls(
            id=d.get("id", ""),
            message=(d.get("message") or "").splitlines()[0],  # first line only
            author_email=d.get("author", {}).get("email", ""),
            added=d.get("added") or [],
            modified=d.get("modified") or [],
            removed=d.get("removed") or [],
        )


@dataclass
class PushEvent:
    """Parsed representation of a GitHub push webhook payload."""
    ref: str                   # "refs/heads/main"
    after: str                 # HEAD commit SHA after the push
    repo_owner: str
    repo_name: str
    commits: list[GitHubCommit]
    delivery_id: str = ""      # X-GitHub-Delivery header (set by webhook handler)

    @property
    def branch(self) -> str:
        """Short branch name, e.g. 'main'."""
        return self.ref.removeprefix("refs/heads/")

    @property
    def files_to_upsert(self) -> list[str]:
        """Union of added + modified files across all commits, deduplicated."""
        seen: set[str] = set()
        result: list[str] = []
        for commit in self.commits:
            for path in commit.added + commit.modified:
                if path not in seen:
                    seen.add(path)
                    result.append(path)
        return result

    @property
    def files_to_delete(self) -> list[str]:
        """Files removed in any commit (not re-added by a later commit)."""
        removed: set[str] = set()
        re_added: set[str] = set()
        for commit in self.commits:
            removed.update(commit.removed)
            re_added.update(commit.added + commit.modified)
        return list(removed - re_added)

    @property
    def head_commit_author(self) -> str:
        return self.commits[-1].author_email if self.commits else ""

    @property
    def head_commit_message(self) -> str:
        return self.commits[-1].message if self.commits else ""

    @classmethod
    def from_dict(cls, payload: dict, delivery_id: str = "") -> "PushEvent":
        repo = payload.get("repository", {})
        owner = repo.get("owner", {})
        return cls(
            ref=payload.get("ref", ""),
            after=payload.get("after", ""),
            repo_owner=owner.get("login", owner.get("name", "")),
            repo_name=repo.get("name", ""),
            commits=[GitHubCommit.from_dict(c) for c in payload.get("commits", [])],
            delivery_id=delivery_id,
        )
