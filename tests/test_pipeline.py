"""
Unit tests for the indexing pipeline.

Mocks all external I/O (GitHub API, Voyage AI, database writes) so tests
run without any network access or running services.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_push_payload(
    *,
    added: list[str] | None = None,
    modified: list[str] | None = None,
    removed: list[str] | None = None,
    branch: str = "main",
    commit_sha: str = "abc123" * 6 + "ab",
    repo_owner: str = "testorg",
    repo_name: str = "testrepo",
) -> dict:
    commit = {
        "id": commit_sha,
        "message": "test commit",
        "author": {"email": "user@test.com"},
        "added": added or [],
        "modified": modified or [],
        "removed": removed or [],
    }
    return {
        "ref": f"refs/heads/{branch}",
        "before": "0" * 40,
        "after": commit_sha,
        "repository": {
            "name": repo_name,
            "owner": {"login": repo_owner},
            "default_branch": "main",
        },
        "head_commit": commit,
        "commits": [commit],
        "pusher": {"name": "Test Bot"},
    }


def _sign_payload(payload_bytes: bytes, secret: str | None = None) -> str:
    """Sign payload using the same secret as the running server (from env/settings)."""
    if secret is None:
        from src.config import settings

        secret = settings.github_webhook_secret
    digest = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ── PushEvent tests ───────────────────────────────────────────────────────────


class TestPushEvent:
    def test_files_to_upsert_deduplication(self):
        from src.github.events import GitHubCommit, PushEvent

        event = PushEvent(
            ref="refs/heads/main",
            after="abc",
            repo_owner="org",
            repo_name="repo",
            commits=[
                GitHubCommit(
                    id="c1",
                    message="m1",
                    author_email="a@a.com",
                    added=["a.py", "b.py"],
                    modified=[],
                ),
                GitHubCommit(
                    id="c2", message="m2", author_email="a@a.com", added=["b.py"], modified=["c.py"]
                ),
            ],
        )
        # b.py appears in both commits → deduplicated
        assert event.files_to_upsert == ["a.py", "b.py", "c.py"]

    def test_files_to_delete_excludes_readded(self):
        from src.github.events import GitHubCommit, PushEvent

        event = PushEvent(
            ref="refs/heads/main",
            after="abc",
            repo_owner="org",
            repo_name="repo",
            commits=[
                GitHubCommit(
                    id="c1",
                    message="m",
                    author_email="a@a.com",
                    removed=["old.py", "also-deleted.py"],
                ),
                GitHubCommit(
                    id="c2", message="m", author_email="a@a.com", added=["old.py"]
                ),  # re-added → not deleted
            ],
        )
        assert event.files_to_delete == ["also-deleted.py"]

    def test_branch_property(self):
        from src.github.events import PushEvent

        event = PushEvent(
            ref="refs/heads/feature/x", after="a", repo_owner="o", repo_name="r", commits=[]
        )
        assert event.branch == "feature/x"

    def test_from_dict_parses_commits(self):
        from src.github.events import PushEvent

        payload = _make_push_payload(
            added=["new.py"],
            modified=["existing.py"],
            removed=["old.py"],
        )
        event = PushEvent.from_dict(payload)
        assert "new.py" in event.files_to_upsert
        assert "existing.py" in event.files_to_upsert
        assert "old.py" in event.files_to_delete
        assert event.repo_owner == "testorg"
        assert event.repo_name == "testrepo"


# ── Webhook HMAC tests ────────────────────────────────────────────────────────


class TestWebhookSignature:
    def test_valid_signature_accepted(self):
        from src.github.webhook import _verify_signature

        body = b'{"test": true}'
        sig = _sign_payload(body)
        assert _verify_signature(body, sig) is True

    def test_invalid_signature_rejected(self):
        from src.github.webhook import _verify_signature

        body = b'{"test": true}'
        assert _verify_signature(body, "sha256=badvalue") is False

    def test_missing_prefix_rejected(self):
        from src.github.webhook import _verify_signature

        body = b'{"test": true}'
        assert _verify_signature(body, "not-a-valid-sig") is False

    def test_empty_header_rejected(self):
        from src.github.webhook import _verify_signature

        assert _verify_signature(b"body", "") is False


# ── Merkle skip logic tests ───────────────────────────────────────────────────


class TestMerkleSkip:
    @pytest.mark.asyncio
    async def test_merkle_hit_skips_file(self):
        """If the stored blob SHA matches, the file must be skipped."""
        BLOB_SHA = "abcdef1234567890" * 2 + "ab12"

        with (
            patch(
                "src.github.fetcher.fetch_file",
                new_callable=AsyncMock,
                return_value=("content", BLOB_SHA),
            ),
            patch(
                "src.storage.db.get_merkle_hash", new_callable=AsyncMock, return_value=BLOB_SHA
            ),  # same → skip
            patch("src.pipeline.parser.parse_file") as mock_parse,
        ):
            from src.pipeline.pipeline import _handle_upserts

            stats = {
                "files_processed": 0,
                "files_skipped_merkle": 0,
                "chunks_upserted": 0,
                "symbols_upserted": 0,
                "embed_cache_hits": 0,
                "errors": 0,
            }
            log = MagicMock()
            log.bind.return_value = log

            await _handle_upserts(
                "org",
                "repo",
                "sha",
                "author",
                "msg",
                ["src/auth.py"],
                stats,
                log,
            )

        assert stats["files_skipped_merkle"] == 1
        assert stats["files_processed"] == 0
        mock_parse.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_blob_sha_triggers_reindex(self):
        """If the stored blob SHA differs, the file must be re-indexed."""
        NEW_BLOB = "new_blob_sha_111"
        OLD_BLOB = "old_blob_sha_222"
        PYTHON_CONTENT = "def hello():\n    pass\n"

        mock_chunk = MagicMock()
        mock_chunk.chunk_id = "cid1"
        mock_enriched = MagicMock()
        mock_enriched.chunk_id = "cid1"
        mock_enriched.language = "python"
        mock_enriched.symbol_name = "hello"
        mock_enriched.symbol_kind = "function"
        mock_enriched.scope_chain = "hello"
        mock_enriched.start_line = 1
        mock_enriched.end_line = 2
        mock_enriched.raw_content = PYTHON_CONTENT
        mock_enriched.enriched_content = PYTHON_CONTENT
        mock_enriched.imports = []
        mock_enriched.token_count = 5

        mock_parsed = MagicMock()
        mock_parsed.all_symbols = []

        with (
            patch(
                "src.github.fetcher.fetch_file",
                new_callable=AsyncMock,
                return_value=(PYTHON_CONTENT, NEW_BLOB),
            ),
            patch(
                "src.storage.db.get_merkle_hash", new_callable=AsyncMock, return_value=OLD_BLOB
            ),  # different → re-index
            patch("src.pipeline.parser.parse_file", return_value=mock_parsed),
            patch("src.pipeline.chunker.chunk_file", return_value=[mock_chunk]),
            patch("src.pipeline.enricher.enrich_chunks", return_value=[mock_enriched]),
            patch(
                "src.pipeline.embedder.get_existing_chunk_ids",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(
                "src.pipeline.embedder.embed_chunks",
                new_callable=AsyncMock,
                return_value={"cid1": [0.1] * 1536},
            ),
            patch("src.storage.db.soft_delete_chunks", new_callable=AsyncMock),
            patch("src.storage.db.delete_symbols_for_file", new_callable=AsyncMock),
            patch("src.storage.db.upsert_chunks", new_callable=AsyncMock, return_value=1),
            patch("src.storage.db.upsert_symbols", new_callable=AsyncMock, return_value=0),
            patch("src.storage.db.upsert_merkle_node", new_callable=AsyncMock),
        ):
            from src.pipeline.pipeline import _handle_upserts

            stats = {
                "files_processed": 0,
                "files_skipped_merkle": 0,
                "chunks_upserted": 0,
                "symbols_upserted": 0,
                "embed_cache_hits": 0,
                "errors": 0,
            }
            log = MagicMock()
            log.bind.return_value = log

            await _handle_upserts(
                "org",
                "repo",
                "commit_sha",
                "author",
                "msg",
                ["src/hello.py"],
                stats,
                log,
            )

        assert stats["files_processed"] == 1
        assert stats["files_skipped_merkle"] == 0
        assert stats["chunks_upserted"] == 1


# ── Deletion tests ────────────────────────────────────────────────────────────


class TestDeletion:
    @pytest.mark.asyncio
    async def test_deletion_calls_all_cleanup(self):
        """Deleting a file must soft-delete chunks, hard-delete symbols + merkle."""
        with (
            patch(
                "src.storage.db.soft_delete_chunks", new_callable=AsyncMock, return_value=2
            ) as mock_soft,
            patch(
                "src.storage.db.delete_symbols_for_file", new_callable=AsyncMock, return_value=1
            ) as mock_sym,
            patch("src.storage.db.delete_merkle_node", new_callable=AsyncMock) as mock_merkle,
        ):
            from src.pipeline.pipeline import _handle_deletions

            stats = {"files_deleted": 0, "errors": 0}
            log = MagicMock()
            log.bind.return_value = log

            await _handle_deletions("org", "repo", ["src/old.py"], stats, log)

        mock_soft.assert_called_once_with("src/old.py", "org", "repo")
        mock_sym.assert_called_once_with("src/old.py", "org", "repo")
        mock_merkle.assert_called_once_with("src/old.py", "org", "repo")
        assert stats["files_deleted"] == 1

    @pytest.mark.asyncio
    async def test_deletion_error_increments_errors(self):
        """A failure in deletion must be logged and not crash the pipeline."""
        with patch(
            "src.storage.db.soft_delete_chunks",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            from src.pipeline.pipeline import _handle_deletions

            stats = {"files_deleted": 0, "errors": 0}
            log = MagicMock()
            log.bind.return_value = log

            await _handle_deletions("org", "repo", ["src/bad.py"], stats, log)

        assert stats["errors"] == 1
        assert stats["files_deleted"] == 0


# ── Webhook endpoint integration tests ───────────────────────────────────────


class TestWebhookEndpoint:
    @pytest.mark.asyncio
    async def test_valid_push_enqueues_job(self):
        """A properly signed push webhook must return 202 and enqueue a job."""
        from fastapi.testclient import TestClient

        from src.api.app import app

        mock_queue = MagicMock()
        mock_job = MagicMock()
        mock_job.id = "test-job-id"
        mock_queue.enqueue.return_value = mock_job

        payload = _make_push_payload(
            added=["app/auth.py"],
            repo_owner="testorg",
            repo_name="testrepo",
        )
        payload_bytes = json.dumps(payload).encode()
        signature = _sign_payload(payload_bytes)

        with (
            patch("src.github.webhook.get_queue", return_value=mock_queue),
            patch("src.storage.db.log_webhook_event", new_callable=AsyncMock),
            patch("src.github.webhook._is_duplicate_job", return_value=False),
        ):
            client = TestClient(app)
            resp = client.post(
                "/webhook",
                content=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": signature,
                    "X-GitHub-Delivery": "test-delivery-001",
                },
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "queued"
        assert data["files_to_upsert"] == 1
        mock_queue.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_signature_rejected(self):
        """A webhook with wrong signature must return 401."""
        from fastapi.testclient import TestClient

        from src.api.app import app

        payload_bytes = json.dumps(_make_push_payload(added=["x.py"])).encode()

        client = TestClient(app)
        resp = client.post(
            "/webhook",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": "sha256=badhash",
                "X-GitHub-Delivery": "test-002",
            },
        )

        assert resp.status_code == 401

    def test_non_main_branch_ignored(self):
        """A push to a non-tracked branch must return 200 with 'not tracked' message."""
        from fastapi.testclient import TestClient

        from src.api.app import app

        payload = _make_push_payload(added=["x.py"], branch="develop")
        payload_bytes = json.dumps(payload).encode()
        signature = _sign_payload(payload_bytes)

        client = TestClient(app)
        resp = client.post(
            "/webhook",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "test-003",
            },
        )

        assert resp.status_code == 200  # acknowledged but not queued
        assert "not tracked" in resp.json().get("message", "")

    def test_ping_event_returns_pong(self):
        """GitHub ping events must be acknowledged."""
        from fastapi.testclient import TestClient

        from src.api.app import app

        payload_bytes = b'{"zen": "Speak like a human."}'
        signature = _sign_payload(payload_bytes)

        client = TestClient(app)
        resp = client.post(
            "/webhook",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": signature,
                "X-GitHub-Delivery": "test-004",
            },
        )

        assert resp.status_code == 200
        assert resp.json()["message"] == "pong"
