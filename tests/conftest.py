"""
Shared pytest fixtures for the nexusCode_server test suite.

All fixtures here are available to every test file without any import.
External dependencies (DB, Redis, Voyage AI, GitHub) are fully mocked so
the test suite runs offline with no live services.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Sample data factories ─────────────────────────────────────────────────────


@pytest.fixture
def sample_python_code() -> str:
    """Minimal Python source used across chunker / enricher / pipeline tests."""
    return '''\
import os
from typing import Optional


class UserService:
    """Manages user accounts."""

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url

    def get_user(self, user_id: int) -> Optional[dict]:
        """Return user by ID or None if not found."""
        return None

    def create_user(self, name: str, email: str) -> dict:
        """Create and return a new user record."""
        return {"id": 1, "name": name, "email": email}


def authenticate(token: str) -> bool:
    """Validate a bearer token. Returns True if valid."""
    return bool(token and len(token) > 10)
'''


@pytest.fixture
def sample_typescript_code() -> str:
    """Minimal TypeScript source for multi-language parser tests."""
    return """\
interface User {
  id: number;
  name: string;
  email: string;
}

export async function getUser(id: number): Promise<User | null> {
  const response = await fetch(`/api/users/${id}`);
  if (!response.ok) return null;
  return response.json();
}

export function validateEmail(email: string): boolean {
  return /^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(email);
}
"""


@pytest.fixture
def sample_chunk() -> dict:
    """A single chunk as it would be returned from the DB / assembler."""
    return {
        "id": "abc123def456",
        "file_path": "src/auth/service.py",
        "repo_owner": "acme",
        "repo_name": "backend",
        "language": "python",
        "symbol_name": "authenticate",
        "symbol_kind": "function",
        "scope_chain": "src.auth.service",
        "start_line": 38,
        "end_line": 42,
        "raw_content": "def authenticate(token: str) -> bool:\n    return bool(token)",
        "enriched_content": "# file: src/auth/service.py\n# scope: src.auth.service\ndef authenticate(token: str) -> bool:\n    return bool(token)",
        "token_count": 24,
        "score": 0.91,
        "rerank_score": 0.91,
        "commit_sha": "deadbeef1234",
        "commit_author": "dev@acme.com",
        "imports": ["os", "typing"],
    }


@pytest.fixture
def sample_repo_payload() -> dict:
    """Minimal GitHub push webhook payload for webhook handler tests."""
    return {
        "ref": "refs/heads/main",
        "repository": {
            "name": "backend",
            "owner": {"login": "acme"},
            "default_branch": "main",
        },
        "head_commit": {
            "id": "deadbeef1234567890abcdef",
            "message": "fix: resolve auth token validation edge case",
            "author": {"name": "Dev", "email": "dev@acme.com"},
            "added": ["src/auth/token.py"],
            "modified": ["src/auth/service.py"],
            "removed": [],
        },
        "commits": [
            {
                "id": "deadbeef1234567890abcdef",
                "added": ["src/auth/token.py"],
                "modified": ["src/auth/service.py"],
                "removed": [],
            }
        ],
    }


# ── Mock services ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_voyage_client():
    """Mock Voyage AI client that returns deterministic 1536-dim embeddings."""
    client = MagicMock()
    client.embed.return_value = MagicMock(embeddings=[[0.1] * 1536])
    return client


@pytest.fixture
def mock_db_session():
    """Async SQLAlchemy session mock for unit tests that touch the DB layer."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture
def mock_github_fetcher():
    """Mock GitHub fetcher that returns a small, deterministic file tree."""
    fetcher = AsyncMock()
    fetcher.get_file_content = AsyncMock(return_value="def hello():\n    return 'world'\n")
    fetcher.list_files = AsyncMock(
        return_value=[
            {"path": "src/main.py", "sha": "aabbcc", "type": "blob"},
            {"path": "src/utils.py", "sha": "ddeeff", "type": "blob"},
        ]
    )
    return fetcher


# ── Eval Suite Configuration ──────────────────────────────────────────────────


def pytest_addoption(parser):
    parser.addoption("--run-eval", action="store_true", default=False, help="run evaluation tests")


def pytest_configure(config):
    config.addinivalue_line("markers", "eval: mark test as an evaluation test")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-eval"):
        # --run-eval given in cli: do not skip eval tests
        return
    skip_eval = pytest.mark.skip(reason="need --run-eval option to run")
    for item in items:
        if "eval" in item.keywords:
            item.add_marker(skip_eval)
