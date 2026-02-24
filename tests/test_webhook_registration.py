"""
Unit tests for webhook auto-registration logic.
No network calls, no DB, no Redis required — all external calls are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.github.fetcher import WebhookCreationError, create_webhook

# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_response(status_code: int, json_data: dict | list | None = None, text: str = "") -> httpx.Response:
    """Build a fake httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text or str(json_data)
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


# ── create_webhook tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_webhook_success():
    """201 response → returns hook_id."""
    mock_resp = _mock_response(201, {"id": 42, "active": True})

    with patch("src.github.fetcher._make_client") as mock_client:
        ctx = AsyncMock()
        ctx.post = AsyncMock(return_value=mock_resp)
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.return_value = ctx

        hook_id = await create_webhook("org", "repo", "https://example.com/webhook", "secret")

    assert hook_id == 42


@pytest.mark.asyncio
async def test_create_webhook_already_exists():
    """422 + existing webhook found → returns existing hook_id."""
    mock_post_resp = _mock_response(422, text="Hook already exists on this repository")

    # _find_existing_webhook should find the existing hook
    mock_list_resp = _mock_response(200, [
        {"id": 99, "config": {"url": "https://example.com/webhook"}},
    ])

    with patch("src.github.fetcher._make_client") as mock_client:
        ctx = AsyncMock()
        # First call is POST (create), second is GET (find existing)
        ctx.post = AsyncMock(return_value=mock_post_resp)
        ctx.get = AsyncMock(return_value=mock_list_resp)
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.return_value = ctx

        hook_id = await create_webhook("org", "repo", "https://example.com/webhook", "secret")

    assert hook_id == 99


@pytest.mark.asyncio
async def test_create_webhook_permission_denied():
    """403 → raises WebhookCreationError with manual_instructions=True."""
    mock_resp = _mock_response(403, text="Must have admin rights")

    with patch("src.github.fetcher._make_client") as mock_client:
        ctx = AsyncMock()
        ctx.post = AsyncMock(return_value=mock_resp)
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.return_value = ctx

        with pytest.raises(WebhookCreationError) as exc_info:
            await create_webhook("org", "repo", "https://example.com/webhook", "secret")

    assert exc_info.value.manual_instructions is True
    assert "Permission denied" in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_webhook_not_found():
    """404 → raises WebhookCreationError."""
    mock_resp = _mock_response(404, text="Not Found")

    with patch("src.github.fetcher._make_client") as mock_client:
        ctx = AsyncMock()
        ctx.post = AsyncMock(return_value=mock_resp)
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.return_value = ctx

        with pytest.raises(WebhookCreationError) as exc_info:
            await create_webhook("org", "repo", "https://example.com/webhook", "secret")

    assert exc_info.value.manual_instructions is True


# ── _try_auto_register_webhook tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_try_auto_register_no_public_url():
    """When PUBLIC_BASE_URL is not set, should return failure with manual instructions."""
    from src.api.repos import _try_auto_register_webhook

    with patch("src.api.repos.settings") as mock_settings:
        mock_settings.webhook_url = None
        mock_settings.public_base_url = None
        mock_settings.github_webhook_secret = "secret"

        result = await _try_auto_register_webhook("org", "repo")

    assert result["success"] is False
    assert result["hook_id"] is None
    assert "PUBLIC_BASE_URL" in result["message"]
    assert result["manual_instructions"] is not None
    assert "github.com/org/repo" in result["manual_instructions"]


@pytest.mark.asyncio
async def test_try_auto_register_success():
    """Full success path: webhook created + hook_id stored."""
    from src.api.repos import _try_auto_register_webhook

    with (
        patch("src.api.repos.settings") as mock_settings,
        patch("src.github.fetcher._make_client") as mock_client,
        patch("src.storage.db.update_repo_webhook", new_callable=AsyncMock) as mock_update,
    ):
        mock_settings.webhook_url = "https://example.com/webhook"
        mock_settings.public_base_url = "https://example.com"
        mock_settings.github_webhook_secret = "secret"

        ctx = AsyncMock()
        mock_resp = _mock_response(201, {"id": 77, "active": True})
        ctx.post = AsyncMock(return_value=mock_resp)
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.return_value = ctx

        result = await _try_auto_register_webhook("org", "repo")

    assert result["success"] is True
    assert result["hook_id"] == 77
    mock_update.assert_awaited_once_with("org", "repo", 77)


# ── Manual instructions tests ────────────────────────────────────────────────


def test_manual_instructions_format():
    """Verify manual instructions contain the correct repo URL and steps."""
    from src.api.repos import _manual_webhook_instructions

    with patch("src.api.repos.settings") as mock_settings:
        mock_settings.webhook_url = "https://my-server.com/webhook"

        instructions = _manual_webhook_instructions("myorg", "myrepo")

    assert "https://github.com/myorg/myrepo/settings/hooks/new" in instructions
    assert "https://my-server.com/webhook" in instructions
    assert "application/json" in instructions
    assert "GITHUB_WEBHOOK_SECRET" in instructions


def test_manual_instructions_no_public_url():
    """Without PUBLIC_BASE_URL, instructions should show placeholder."""
    from src.api.repos import _manual_webhook_instructions

    with patch("src.api.repos.settings") as mock_settings:
        mock_settings.webhook_url = None

        instructions = _manual_webhook_instructions("org", "repo")

    assert "<YOUR_SERVER_URL>/webhook" in instructions


# ── webhook_url property test ─────────────────────────────────────────────────


def test_webhook_url_property_with_base():
    """webhook_url should append /webhook to public_base_url."""
    from src.config import Settings

    # Test the property logic directly via a mock
    mock_settings = MagicMock(spec=Settings)
    mock_settings.public_base_url = "https://my-app.railway.app"
    # Call the actual property implementation
    result = Settings.webhook_url.fget(mock_settings)
    assert result == "https://my-app.railway.app/webhook"


def test_webhook_url_property_strips_trailing_slash():
    """Trailing slash on public_base_url should not create double slash."""
    from src.config import Settings

    mock_settings = MagicMock(spec=Settings)
    mock_settings.public_base_url = "https://my-app.railway.app/"
    result = Settings.webhook_url.fget(mock_settings)
    assert result == "https://my-app.railway.app/webhook"


def test_webhook_url_property_none():
    """No public_base_url → webhook_url is None."""
    from src.config import Settings

    mock_settings = MagicMock(spec=Settings)
    mock_settings.public_base_url = None
    result = Settings.webhook_url.fget(mock_settings)
    assert result is None
