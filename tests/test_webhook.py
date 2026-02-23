"""
Unit tests for webhook HMAC verification and push payload parsing.
No network calls, no DB, no Redis required.
"""

import hashlib
import hmac

from src.github.events import PushEvent
from src.github.webhook import _verify_signature

SECRET = "test-secret"


# ── Signature verification ────────────────────────────────────────────────────


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature():
    from unittest.mock import patch

    from src.github import webhook as wh

    body = b'{"foo": "bar"}'
    with patch.object(wh.settings, "github_webhook_secret", SECRET):
        assert _verify_signature(body, _sign(body)) is True


def test_invalid_signature():
    body = b'{"foo": "bar"}'
    assert _verify_signature(body, "sha256=deadbeef") is False


def test_missing_prefix():
    body = b"hello"
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert _verify_signature(body, digest) is False  # missing "sha256=" prefix


def test_wrong_secret():
    body = b"hello"
    sig = _sign(body, secret="wrong-secret")
    assert _verify_signature(body, sig) is False


# ── PushEvent parsing ─────────────────────────────────────────────────────────

PUSH_PAYLOAD = {
    "ref": "refs/heads/main",
    "after": "abc123def456",
    "repository": {
        "name": "my-repo",
        "owner": {"login": "myorg"},
    },
    "commits": [
        {
            "id": "commit1",
            "message": "feat: add auth\n\nLong description",
            "author": {"email": "dev@example.com"},
            "added": ["src/auth/service.py"],
            "modified": ["src/config.py"],
            "removed": ["old/file.py"],
        },
        {
            "id": "commit2",
            "message": "fix: edge case",
            "author": {"email": "dev@example.com"},
            "added": [],
            "modified": ["src/auth/service.py"],  # same file modified again
            "removed": [],
        },
    ],
}


def test_push_event_branch():
    event = PushEvent.from_dict(PUSH_PAYLOAD)
    assert event.branch == "main"
    assert event.repo_owner == "myorg"
    assert event.repo_name == "my-repo"
    assert event.after == "abc123def456"


def test_push_event_files_to_upsert_deduplicates():
    event = PushEvent.from_dict(PUSH_PAYLOAD)
    upsert = event.files_to_upsert
    # src/auth/service.py appears in both commits — should appear once
    assert upsert.count("src/auth/service.py") == 1
    assert "src/config.py" in upsert


def test_push_event_files_to_delete():
    event = PushEvent.from_dict(PUSH_PAYLOAD)
    assert "old/file.py" in event.files_to_delete


def test_push_event_commit_message_first_line_only():
    event = PushEvent.from_dict(PUSH_PAYLOAD)
    # Last commit message should be first line only
    assert event.head_commit_message == "fix: edge case"


def test_push_event_deleted_not_in_upsert():
    event = PushEvent.from_dict(PUSH_PAYLOAD)
    assert "old/file.py" not in event.files_to_upsert


def test_re_added_file_not_in_delete():
    """A file removed in one commit but re-added in another should not be deleted."""
    payload = {
        **PUSH_PAYLOAD,
        "commits": [
            {
                "id": "c1",
                "message": "remove file",
                "author": {"email": "a@b.com"},
                "added": [],
                "modified": [],
                "removed": ["src/foo.py"],
            },
            {
                "id": "c2",
                "message": "re-add file",
                "author": {"email": "a@b.com"},
                "added": ["src/foo.py"],
                "modified": [],
                "removed": [],
            },
        ],
    }
    event = PushEvent.from_dict(payload)
    assert "src/foo.py" not in event.files_to_delete
    assert "src/foo.py" in event.files_to_upsert
