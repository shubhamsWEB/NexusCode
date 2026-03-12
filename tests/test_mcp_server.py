from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_ask_codebase_accepts_query_alias():
    from src.mcp.server import ask_codebase

    fake_result = SimpleNamespace(
        answer="It uses the webhook handler.",
        cited_files=["src/github/webhook.py:1-10"],
        follow_up_hints=["Where is the signature verified?"],
        context_tokens=123,
        elapsed_ms=45,
    )

    with patch("src.ask.ask_agent.generate_answer", new_callable=AsyncMock, return_value=fake_result) as mock_generate:
        result = await ask_codebase(query="How does the webhook pipeline work?")

    mock_generate.assert_awaited_once()
    assert mock_generate.await_args.kwargs["query"] == "How does the webhook pipeline work?"
    assert "It uses the webhook handler." in result


@pytest.mark.asyncio
async def test_ask_codebase_accepts_text_alias():
    from src.mcp.server import ask_codebase

    fake_result = SimpleNamespace(
        answer="Authentication is handled in the service layer.",
        cited_files=[],
        follow_up_hints=[],
        context_tokens=123,
        elapsed_ms=45,
    )

    with patch("src.ask.ask_agent.generate_answer", new_callable=AsyncMock, return_value=fake_result) as mock_generate:
        await ask_codebase(text="Where is authentication handled?")

    mock_generate.assert_awaited_once()
    assert mock_generate.await_args.kwargs["query"] == "Where is authentication handled?"


@pytest.mark.asyncio
async def test_ask_codebase_returns_helpful_error_when_prompt_missing():
    from src.mcp.server import ask_codebase

    result = await ask_codebase()

    assert "requires a 'question' field" in result
    assert "query" in result
