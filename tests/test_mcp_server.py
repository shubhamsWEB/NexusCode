from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_default_mcp_profile_exposes_only_core_tools():
    from src.mcp.server import core_mcp_server

    tools = await core_mcp_server.list_tools()
    names = {tool.name for tool in tools}

    assert names == {
        "search_codebase",
        "get_symbol",
        "find_callers",
        "get_file_context",
        "get_agent_context",
        "get_semantic_context",
    }


@pytest.mark.asyncio
async def test_full_mcp_profile_keeps_extended_tools():
    from src.mcp.server import mcp_server

    tools = await mcp_server.list_tools()
    names = {tool.name for tool in tools}

    assert "plan_implementation" in names
    assert "ask_codebase" in names
    assert "list_skills" in names
    assert "get_evolution_metrics" in names
    assert "reflect_and_improve" in names


def test_fastapi_mounts_full_mcp_before_core_mcp():
    from src.api.app import app

    mcp_mount_paths = [route.path for route in app.routes if getattr(route, "path", "").startswith("/mcp")]

    assert "/mcp/full" in mcp_mount_paths
    assert "/mcp" in mcp_mount_paths
    assert mcp_mount_paths.index("/mcp/full") < mcp_mount_paths.index("/mcp")


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
