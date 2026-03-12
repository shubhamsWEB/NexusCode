from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.planning.schemas import ImplementationPlan, PlanMetadata


@dataclass
class _Analysis:
    complexity: str
    sub_queries: list[str]


async def _empty_stream():
    if False:
        yield {}


class TestPlannerHelpers:
    def test_should_run_web_research_preserves_legacy_default(self, monkeypatch):
        import src.planning.claude_planner as planner

        monkeypatch.setattr(planner.settings, "web_research_selective_trigger", False, raising=False)

        assert planner._should_run_web_research(
            "simple refactor question",
            _Analysis(complexity="simple", sub_queries=[]),
        ) is True

    @pytest.mark.asyncio
    async def test_run_relevance_gate_strict_rejects(self, monkeypatch):
        import src.planning.claude_planner as planner
        from src.retrieval.relevance import RelevanceResult

        monkeypatch.setattr(planner.settings, "query_relevance_enabled", True)
        monkeypatch.setattr(planner.settings, "query_relevance_mode", "strict", raising=False)
        monkeypatch.setattr(planner.settings, "query_relevance_threshold", 0.35)
        monkeypatch.setattr(planner.settings, "default_model", "claude-test")

        relevance = RelevanceResult(is_relevant=False, best_score=0.12, reason="out_of_scope")
        with (
            patch("src.retrieval.relevance.check_query_relevance", new_callable=AsyncMock, return_value=relevance),
            patch("src.retrieval.relevance.build_out_of_scope_message", return_value="outside scope"),
        ):
            rejected, plan, returned = await planner._run_relevance_gate(
                "what is the capital of france",
                "acme",
                "backend",
                None,
            )

        assert rejected is True
        assert returned is relevance
        assert plan is not None
        assert plan.response_type == "out_of_scope"
        assert plan.out_of_scope_reason == "outside scope"
        assert plan.metadata is not None
        assert "0.120" in plan.metadata.retrieval_log

    @pytest.mark.asyncio
    async def test_run_relevance_gate_warn_mode_continues(self, monkeypatch):
        import src.planning.claude_planner as planner
        from src.retrieval.relevance import RelevanceResult

        monkeypatch.setattr(planner.settings, "query_relevance_enabled", True)
        monkeypatch.setattr(planner.settings, "query_relevance_mode", "warn", raising=False)

        relevance = RelevanceResult(is_relevant=False, best_score=0.20, reason="out_of_scope")
        with patch(
            "src.retrieval.relevance.check_query_relevance",
            new_callable=AsyncMock,
            return_value=relevance,
        ):
            rejected, plan, returned = await planner._run_relevance_gate(
                "capital of france",
                None,
                None,
                None,
            )

        assert rejected is False
        assert plan is None
        assert returned is relevance

    @pytest.mark.asyncio
    async def test_build_planner_context_centralizes_shared_setup(self, monkeypatch):
        import src.planning.claude_planner as planner

        analysis = _Analysis(complexity="complex", sub_queries=["a", "b"])
        monkeypatch.setattr(planner.settings, "default_model", "claude-test")
        monkeypatch.setattr(planner.settings, "planning_thinking_budget", 2048)

        with (
            patch("src.planning.retriever._analyze_query", return_value=analysis),
            patch("src.planning.claude_planner._get_retrieval_tools", return_value=[{"name": "search"}]),
            patch(
                "src.planning.claude_planner._maybe_run_web_research",
                new_callable=AsyncMock,
                return_value="web-notes",
            ),
            patch(
                "src.planning.claude_planner._safe_fetch_worldview_context",
                new_callable=AsyncMock,
                return_value="worldview",
            ),
        ):
            ctx = await planner._build_planner_context(
                query="refactor planner",
                repo_owner="acme",
                repo_name="backend",
                web_research=True,
                model=None,
                allowed_repos=["acme/backend"],
                relevance=SimpleNamespace(reason="ambiguous", best_score=0.42),
            )

        assert ctx.effective_model == "claude-test"
        assert ctx.analysis is analysis
        assert ctx.effective_thinking == 2048
        assert ctx.retrieval_tools == [{"name": "search"}]
        assert ctx.web_notes == "web-notes"
        assert ctx.worldview_preamble == "worldview"
        assert ctx.extra_context == {"allowed_repos": ["acme/backend"]}

    @pytest.mark.asyncio
    async def test_maybe_run_web_research_times_out(self, monkeypatch):
        import src.planning.claude_planner as planner

        monkeypatch.setattr(planner.settings, "web_research_timeout_s", 0.001, raising=False)
        monkeypatch.setattr(planner.settings, "web_research_max_chars", 100, raising=False)

        async def _slow_research(*args, **kwargs):
            await asyncio.sleep(0.01)
            return "notes"

        with (
            patch("src.planning.retriever._extract_stack_fingerprint", new_callable=AsyncMock, return_value="stack"),
            patch("src.planning.web_researcher.research_implementation", side_effect=_slow_research),
        ):
            result = await planner._maybe_run_web_research(
                query="best practices for planner refactor",
                repo_owner="acme",
                repo_name="backend",
                model="claude-test",
                analysis=_Analysis(complexity="complex", sub_queries=[]),
                enabled=True,
            )

        assert result == ""


class TestPlannerPublicAPI:
    @pytest.mark.asyncio
    async def test_generate_plan_annotates_metadata(self):
        import src.planning.claude_planner as planner

        ctx = planner.PlannerExecutionContext(
            query="refactor planner",
            repo_owner="acme",
            repo_name="backend",
            effective_model="claude-test",
            analysis=_Analysis(complexity="complex", sub_queries=["one", "two"]),
            effective_thinking=4096,
            retrieval_tools=[{"name": "search"}],
            web_notes="web-notes",
            worldview_preamble="worldview",
            extra_context={"allowed_repos": ["acme/backend"]},
            relevance=SimpleNamespace(reason="ambiguous", best_score=0.45),
        )
        stats = {
            "elapsed_ms": 123.0,
            "iterations": 2,
            "tool_calls": 3,
            "search_tools_called": 1,
            "context_tokens": 456,
        }
        tool_block = {
            "name": "answer_codebase_question",
            "input": {"answer": "Planner answer", "key_files": ["src/planning/claude_planner.py"]},
        }

        with (
            patch("src.planning.claude_planner._run_relevance_gate", new_callable=AsyncMock, return_value=(False, None, ctx.relevance)),
            patch("src.planning.claude_planner._build_planner_context", new_callable=AsyncMock, return_value=ctx),
            patch("src.planning.claude_planner._run_planning_loop", new_callable=AsyncMock, return_value=(tool_block, stats)),
        ):
            plan = await planner.generate_plan("refactor planner", "acme", "backend")

        assert plan.response_type == "answer"
        assert plan.metadata is not None
        assert plan.metadata.web_research_used is True
        assert plan.metadata.query_complexity == "complex"
        assert plan.metadata.sub_queries_count == 2
        assert any("ambiguous" in warning for warning in plan.metadata.grounding_warnings)

    @pytest.mark.asyncio
    async def test_stream_generate_plan_passes_through_and_finishes(self):
        import src.planning.claude_planner as planner

        ctx = planner.PlannerExecutionContext(
            query="refactor planner",
            repo_owner="acme",
            repo_name="backend",
            effective_model="claude-test",
            analysis=_Analysis(complexity="moderate", sub_queries=[]),
            effective_thinking=0,
            retrieval_tools=[{"name": "search"}],
            web_notes="",
            worldview_preamble="",
            extra_context=None,
            relevance=None,
        )

        async def _fake_stream(_ctx):
            yield {"type": "thinking", "text": "checking code"}
            yield {
                "type": "done",
                "tool_block": {
                    "name": "answer_codebase_question",
                    "input": {"answer": "streamed", "key_files": []},
                },
                "stats": {"elapsed_ms": 12.0, "iterations": 1, "tool_calls": 1, "context_tokens": 10},
            }

        with (
            patch("src.planning.claude_planner._run_relevance_gate", new_callable=AsyncMock, return_value=(False, None, None)),
            patch("src.planning.claude_planner._build_planner_context", new_callable=AsyncMock, return_value=ctx),
            patch("src.planning.claude_planner._stream_planning_loop", side_effect=_fake_stream),
        ):
            events = [event async for event in planner.stream_generate_plan("refactor planner", "acme", "backend")]

        assert events[0] == {"type": "thinking", "text": "checking code"}
        assert events[1]["type"] == "plan_complete"
        assert events[1]["plan"].response_type == "answer"

    @pytest.mark.asyncio
    async def test_stream_generate_plan_returns_rejected_plan_immediately(self):
        import src.planning.claude_planner as planner

        rejected_plan = ImplementationPlan(
            query="off topic",
            response_type="out_of_scope",
            out_of_scope_reason="outside scope",
        )
        rejected_plan.metadata = PlanMetadata(
            model="claude-test",
            context_tokens=0,
            context_files=0,
            retrieval_log="relevance gate",
            elapsed_ms=0.0,
        )

        with (
            patch("src.planning.claude_planner._run_relevance_gate", new_callable=AsyncMock, return_value=(True, rejected_plan, None)),
            patch("src.planning.claude_planner._build_planner_context", new_callable=AsyncMock),
            patch("src.planning.claude_planner._stream_planning_loop", side_effect=_empty_stream),
        ):
            events = [event async for event in planner.stream_generate_plan("off topic")]

        assert events == [{"type": "plan_complete", "plan": rejected_plan}]
