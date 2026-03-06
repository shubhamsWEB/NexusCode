"""
Unit tests for the query relevance gate.

Mocks embed_query and _semantic_search so tests run without DB or network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_result(score: float, file_path: str = "src/foo.py"):
    r = MagicMock()
    r.score = score
    r.file_path = file_path
    return r


# ── check_query_relevance ─────────────────────────────────────────────────────


class TestCheckQueryRelevance:
    @pytest.mark.asyncio
    async def test_empty_index_returns_no_index(self):
        with (
            patch("src.retrieval.searcher.embed_query", new_callable=AsyncMock, return_value=[0.1] * 1536),
            patch("src.retrieval.searcher._semantic_search", new_callable=AsyncMock, return_value=[]),
        ):
            from src.retrieval.relevance import check_query_relevance

            result = await check_query_relevance("what is the meaning of life?")

        assert result.is_relevant is False
        assert result.reason == "no_index"
        assert result.best_score == 0.0

    @pytest.mark.asyncio
    async def test_low_score_returns_out_of_scope(self):
        low_results = [_make_result(0.10), _make_result(0.08), _make_result(0.05)]
        with (
            patch("src.retrieval.searcher.embed_query", new_callable=AsyncMock, return_value=[0.1] * 1536),
            patch("src.retrieval.searcher._semantic_search", new_callable=AsyncMock, return_value=low_results),
        ):
            from importlib import reload

            import src.retrieval.relevance as mod
            reload(mod)
            result = await mod.check_query_relevance("capital of france")

        assert result.is_relevant is False
        assert result.reason == "out_of_scope"
        assert result.best_score == pytest.approx(0.10)

    @pytest.mark.asyncio
    async def test_high_score_returns_relevant(self):
        high_results = [_make_result(0.75, "src/auth/service.py")]
        with (
            patch("src.retrieval.searcher.embed_query", new_callable=AsyncMock, return_value=[0.1] * 1536),
            patch("src.retrieval.searcher._semantic_search", new_callable=AsyncMock, return_value=high_results),
        ):
            from importlib import reload

            import src.retrieval.relevance as mod
            reload(mod)
            result = await mod.check_query_relevance("how does authentication work?")

        assert result.is_relevant is True
        assert result.reason == "relevant"
        assert result.top_file == "src/auth/service.py"

    @pytest.mark.asyncio
    async def test_ambiguous_score_returns_ambiguous(self):
        # Score between threshold (0.35) and soft_threshold (0.50)
        mid_results = [_make_result(0.42)]
        with (
            patch("src.retrieval.searcher.embed_query", new_callable=AsyncMock, return_value=[0.1] * 1536),
            patch("src.retrieval.searcher._semantic_search", new_callable=AsyncMock, return_value=mid_results),
        ):
            from importlib import reload

            import src.retrieval.relevance as mod
            reload(mod)
            result = await mod.check_query_relevance("general software architecture question")

        assert result.is_relevant is True
        assert result.reason == "ambiguous"

    @pytest.mark.asyncio
    async def test_embed_failure_passes_through(self):
        """Embedding errors should not block the query — gate opens."""
        with patch(
            "src.retrieval.searcher.embed_query",
            new_callable=AsyncMock,
            side_effect=RuntimeError("voyage down"),
        ):
            from importlib import reload

            import src.retrieval.relevance as mod
            reload(mod)
            result = await mod.check_query_relevance("some query")

        assert result.is_relevant is True
        assert result.reason == "relevant"

    @pytest.mark.asyncio
    async def test_db_failure_passes_through(self):
        """DB errors should not block the query — gate opens."""
        with (
            patch("src.retrieval.searcher.embed_query", new_callable=AsyncMock, return_value=[0.0] * 1536),
            patch(
                "src.retrieval.searcher._semantic_search",
                new_callable=AsyncMock,
                side_effect=Exception("db connection refused"),
            ),
        ):
            from importlib import reload

            import src.retrieval.relevance as mod
            reload(mod)
            result = await mod.check_query_relevance("any query")

        assert result.is_relevant is True
        assert result.reason == "relevant"


# ── build_out_of_scope_message ────────────────────────────────────────────────


class TestBuildOutOfScopeMessage:
    def test_no_index_message(self):
        from src.retrieval.relevance import RelevanceResult, build_out_of_scope_message

        r = RelevanceResult(is_relevant=False, best_score=0.0, reason="no_index")
        msg = build_out_of_scope_message("anything", r)
        assert "No repositories are indexed" in msg

    def test_out_of_scope_message_contains_score(self):
        from src.retrieval.relevance import RelevanceResult, build_out_of_scope_message

        r = RelevanceResult(is_relevant=False, best_score=0.12, reason="out_of_scope")
        msg = build_out_of_scope_message("capital of france", r)
        assert "0.12" in msg
        assert "QUERY_RELEVANCE_THRESHOLD" in msg


# ── Integration: gate in generate_answer ─────────────────────────────────────


class TestAskAgentGate:
    @pytest.mark.asyncio
    async def test_out_of_scope_skips_agent_loop(self):
        from src.retrieval.relevance import RelevanceResult

        oos = RelevanceResult(is_relevant=False, best_score=0.05, reason="out_of_scope")
        with (
            patch("src.retrieval.relevance.check_query_relevance", new_callable=AsyncMock, return_value=oos),
            patch("src.config.settings") as mock_settings,
        ):
            mock_settings.query_relevance_enabled = True
            mock_settings.query_relevance_threshold = 0.35
            mock_settings.default_model = "claude-haiku-4-5-20251001"

            # AgentLoop.run should never be called
            with patch("src.agent.loop.AgentLoop.run", new_callable=AsyncMock) as mock_loop:
                from src.ask.ask_agent import generate_answer

                result = await generate_answer("what is 2+2?")

            mock_loop.assert_not_called()

        assert result.quality_score == pytest.approx(0.05)
        assert "relevance" in result.answer.lower() or "indexed" in result.answer.lower()
