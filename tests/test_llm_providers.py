"""
Tests for the multi-LLM provider abstraction layer.

Tests cover:
  - Type definitions
  - Tool schema conversion (Anthropic <-> OpenAI <-> LLMToolSchema)
  - Registry (model resolution, available models, singleton behavior)
  - AnthropicProvider construction and schema conversion
  - OpenAIProvider construction and schema conversion
  - GrokProvider inherits from OpenAI

All tests are unit tests — no network calls, no API keys required.
"""

from __future__ import annotations

import pytest


# ── Types ─────────────────────────────────────────────────────────────────────


class TestLLMTypes:
    def test_tool_schema_creation(self):
        from src.llm.types import LLMToolSchema

        schema = LLMToolSchema(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        assert schema.name == "test_tool"
        assert schema.description == "A test tool"
        assert schema.parameters["type"] == "object"

    def test_stream_event_creation(self):
        from src.llm.types import LLMStreamEvent

        event = LLMStreamEvent(type="text", text="hello")
        assert event.type == "text"
        assert event.text == "hello"

    def test_tool_call_creation(self):
        from src.llm.types import LLMToolCall

        tc = LLMToolCall(name="my_tool", input={"key": "value"})
        assert tc.name == "my_tool"
        assert tc.input == {"key": "value"}

    def test_response_creation(self):
        from src.llm.types import LLMResponse, LLMToolCall

        resp = LLMResponse(
            tool_calls=[LLMToolCall(name="t", input={})],
            text_content="hello",
            stop_reason="end_turn",
        )
        assert len(resp.tool_calls) == 1
        assert resp.text_content == "hello"
        assert resp.stop_reason == "end_turn"

    def test_response_defaults(self):
        from src.llm.types import LLMResponse

        resp = LLMResponse()
        assert resp.tool_calls == []
        assert resp.text_content == ""
        assert resp.stop_reason == ""
        assert resp.raw is None


# ── Tool converter ────────────────────────────────────────────────────────────


class TestToolConverter:
    def test_from_anthropic_schema(self):
        from src.llm.tool_converter import from_anthropic_schema

        anthropic_schema = {
            "name": "answer_question",
            "description": "Answers a question",
            "input_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        }
        tool = from_anthropic_schema(anthropic_schema)
        assert tool.name == "answer_question"
        assert tool.description == "Answers a question"
        assert tool.parameters["type"] == "object"
        assert "answer" in tool.parameters["properties"]

    def test_to_anthropic_format(self):
        from src.llm.tool_converter import from_anthropic_schema, to_anthropic_format

        original = {
            "name": "test",
            "description": "desc",
            "input_schema": {"type": "object", "properties": {}},
        }
        tool = from_anthropic_schema(original)
        result = to_anthropic_format(tool)
        assert result["name"] == "test"
        assert result["description"] == "desc"
        assert result["input_schema"] == {"type": "object", "properties": {}}

    def test_to_openai_format(self):
        from src.llm.tool_converter import to_openai_format
        from src.llm.types import LLMToolSchema

        tool = LLMToolSchema(
            name="my_func",
            description="Does stuff",
            parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        )
        result = to_openai_format(tool)
        assert result["type"] == "function"
        assert result["function"]["name"] == "my_func"
        assert result["function"]["description"] == "Does stuff"
        assert result["function"]["parameters"]["properties"]["x"]["type"] == "integer"

    def test_roundtrip_anthropic(self):
        """from_anthropic -> to_anthropic should be lossless."""
        from src.llm.tool_converter import from_anthropic_schema, to_anthropic_format

        original = {
            "name": "output_implementation_plan",
            "description": "Generate a plan",
            "input_schema": {
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
        }
        roundtripped = to_anthropic_format(from_anthropic_schema(original))
        assert roundtripped == original

    def test_from_anthropic_missing_description(self):
        from src.llm.tool_converter import from_anthropic_schema

        schema = {"name": "no_desc", "input_schema": {"type": "object"}}
        tool = from_anthropic_schema(schema)
        assert tool.description == ""

    def test_from_anthropic_missing_input_schema(self):
        from src.llm.tool_converter import from_anthropic_schema

        schema = {"name": "bare", "description": "bare tool"}
        tool = from_anthropic_schema(schema)
        assert tool.parameters == {}


# ── Registry ──────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_resolve_known_model(self):
        from src.llm.registry import resolve_provider

        assert resolve_provider("claude-sonnet-4-6") == "anthropic"
        assert resolve_provider("gpt-4o") == "openai"
        assert resolve_provider("grok-3") == "grok"
        assert resolve_provider("o3") == "openai"
        assert resolve_provider("gpt-4o-mini") == "openai"
        assert resolve_provider("grok-3-mini") == "grok"

    def test_resolve_heuristic_fallback(self):
        from src.llm.registry import resolve_provider

        assert resolve_provider("claude-some-future-model") == "anthropic"
        assert resolve_provider("gpt-5-turbo") == "openai"
        assert resolve_provider("grok-4") == "grok"

    def test_resolve_unknown_raises(self):
        from src.llm.registry import resolve_provider

        with pytest.raises(ValueError, match="Unknown model"):
            resolve_provider("llama-3.3-70b")

    def test_model_registry_completeness(self):
        """All models in registry map to valid provider names."""
        from src.llm.registry import MODEL_REGISTRY

        valid_providers = {"anthropic", "openai", "grok"}
        for model, provider in MODEL_REGISTRY.items():
            assert provider in valid_providers, f"{model} maps to unknown provider {provider}"

    def test_list_available_models_returns_list(self):
        from src.llm.registry import list_available_models

        result = list_available_models()
        assert isinstance(result, list)
        # Each entry should have model and provider keys
        for entry in result:
            assert "model" in entry
            assert "provider" in entry

    def test_get_provider_singleton(self, monkeypatch):
        """get_provider should return the same instance for repeated calls."""
        from src.llm import registry

        # Clear cached providers
        registry._providers.clear()

        # Mock the settings to have an anthropic key
        monkeypatch.setattr(registry.settings, "anthropic_api_key", "fake-key")
        monkeypatch.setattr(registry.settings, "default_model", "claude-sonnet-4-6")

        p1 = registry.get_provider("claude-sonnet-4-6")
        p2 = registry.get_provider("claude-sonnet-4-6")
        assert p1 is p2

        # Clean up
        registry._providers.clear()

    def test_get_provider_default_model(self, monkeypatch):
        """get_provider(None) should use settings.default_model."""
        from src.llm import registry

        registry._providers.clear()
        monkeypatch.setattr(registry.settings, "anthropic_api_key", "fake-key")
        monkeypatch.setattr(registry.settings, "default_model", "claude-sonnet-4-6")

        p = registry.get_provider(None)
        assert p.provider_name == "anthropic"

        registry._providers.clear()


# ── AnthropicProvider ─────────────────────────────────────────────────────────


class TestAnthropicProvider:
    def test_provider_name(self):
        from src.llm.anthropic_provider import AnthropicProvider

        p = AnthropicProvider()
        assert p.provider_name == "anthropic"
        assert p.supports_web_search is True
        assert p.supports_thinking is True

    def test_convert_tool_choice_auto(self):
        from src.llm.anthropic_provider import AnthropicProvider

        p = AnthropicProvider()
        assert p._convert_tool_choice("auto", [object()]) == {"type": "auto"}

    def test_convert_tool_choice_name(self):
        from src.llm.anthropic_provider import AnthropicProvider

        p = AnthropicProvider()
        result = p._convert_tool_choice({"name": "my_tool"}, [object()])
        assert result == {"type": "tool", "name": "my_tool"}

    def test_convert_tool_choice_string_name(self):
        from src.llm.anthropic_provider import AnthropicProvider

        p = AnthropicProvider()
        result = p._convert_tool_choice("my_tool", [object()])
        assert result == {"type": "tool", "name": "my_tool"}

    def test_convert_tool_choice_none(self):
        from src.llm.anthropic_provider import AnthropicProvider

        p = AnthropicProvider()
        assert p._convert_tool_choice(None, [object()]) == {"type": "auto"}
        assert p._convert_tool_choice(None, []) is None

    def test_build_call_params_without_thinking(self):
        from src.llm.anthropic_provider import AnthropicProvider
        from src.llm.types import LLMToolSchema

        p = AnthropicProvider()
        tools = [LLMToolSchema(name="t", description="d", parameters={"type": "object"})]
        params = p._build_call_params(
            model="claude-sonnet-4-6",
            system="sys prompt",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            tool_choice="auto",
            max_tokens=4096,
            thinking_budget=0,
        )
        assert params["model"] == "claude-sonnet-4-6"
        assert params["max_tokens"] == 4096
        assert params["system"] == "sys prompt"
        assert "thinking" not in params
        assert len(params["tools"]) == 1

    def test_build_call_params_with_thinking(self):
        from src.llm.anthropic_provider import AnthropicProvider

        p = AnthropicProvider()
        params = p._build_call_params(
            model="claude-sonnet-4-6",
            system="sys",
            messages=[],
            tools=None,
            tool_choice=None,
            max_tokens=4096,
            thinking_budget=10000,
        )
        assert params["max_tokens"] == 14096  # 4096 + 10000
        assert params["thinking"]["budget_tokens"] == 10000

    def test_rate_limit_error(self):
        from src.llm.anthropic_provider import RateLimitOrOverloadError

        err = RateLimitOrOverloadError(cause=None)
        assert "overloaded" in str(err).lower()

        # Simulate a 429 cause
        class FakeCause(Exception):
            status_code = 429

        err429 = RateLimitOrOverloadError(cause=FakeCause())
        assert err429.status_code == 429
        assert "rate limit" in str(err429).lower()


# ── OpenAIProvider ────────────────────────────────────────────────────────────


class TestOpenAIProvider:
    def test_provider_name(self):
        from src.llm.openai_provider import OpenAIProvider

        p = OpenAIProvider()
        assert p.provider_name == "openai"
        assert p.supports_web_search is False
        assert p.supports_thinking is False

    def test_convert_tool_choice_auto(self):
        from src.llm.openai_provider import OpenAIProvider

        p = OpenAIProvider()
        assert p._convert_tool_choice("auto", [object()]) == "auto"
        assert p._convert_tool_choice("none", [object()]) == "none"
        assert p._convert_tool_choice("required", [object()]) == "required"

    def test_convert_tool_choice_name(self):
        from src.llm.openai_provider import OpenAIProvider

        p = OpenAIProvider()
        result = p._convert_tool_choice({"name": "my_func"}, [object()])
        assert result == {"type": "function", "function": {"name": "my_func"}}

    def test_build_messages_prepends_system(self):
        from src.llm.openai_provider import OpenAIProvider

        p = OpenAIProvider()
        msgs = p._build_messages("system prompt", [{"role": "user", "content": "hi"}])
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "system prompt"
        assert msgs[1]["role"] == "user"

    def test_build_call_params_regular_model(self):
        from src.llm.openai_provider import OpenAIProvider
        from src.llm.types import LLMToolSchema

        p = OpenAIProvider()
        tools = [LLMToolSchema(name="f", description="d", parameters={"type": "object"})]
        params = p._build_call_params(
            model="gpt-4o",
            system="sys",
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
            tool_choice="auto",
            max_tokens=4096,
            thinking_budget=0,
        )
        assert params["model"] == "gpt-4o"
        assert params["max_tokens"] == 4096
        assert "max_completion_tokens" not in params
        assert "reasoning_effort" not in params
        assert len(params["tools"]) == 1
        assert params["tools"][0]["type"] == "function"

    def test_build_call_params_o_series(self):
        from src.llm.openai_provider import OpenAIProvider

        p = OpenAIProvider()
        params = p._build_call_params(
            model="o3",
            system="sys",
            messages=[],
            tools=None,
            tool_choice=None,
            max_tokens=4096,
            thinking_budget=10000,
        )
        assert params["max_completion_tokens"] == 4096
        assert "max_tokens" not in params
        assert params["reasoning_effort"] == "medium"

    def test_build_call_params_o_series_no_thinking(self):
        from src.llm.openai_provider import OpenAIProvider

        p = OpenAIProvider()
        params = p._build_call_params(
            model="o3",
            system="sys",
            messages=[],
            tools=None,
            tool_choice=None,
            max_tokens=4096,
            thinking_budget=0,
        )
        assert "reasoning_effort" not in params

    def test_json_repair(self):
        from src.llm.openai_provider import _repair_json

        assert _repair_json('{"key": "value"') == {"key": "value"}
        # '{"key": "val' + '"}'  => '{"key": "val"}' which is valid
        assert _repair_json('{"key": "val') == {"key": "val"}
        assert _repair_json("") == {}
        assert _repair_json("   ") == {}
        # Truly unrepairable
        assert "_raw_arguments" in _repair_json("not json at all")


# ── GrokProvider ──────────────────────────────────────────────────────────────


class TestGrokProvider:
    def test_inherits_openai(self):
        from src.llm.grok_provider import GrokProvider
        from src.llm.openai_provider import OpenAIProvider

        assert issubclass(GrokProvider, OpenAIProvider)

    def test_provider_name(self):
        from src.llm.grok_provider import GrokProvider

        p = GrokProvider()
        assert p.provider_name == "grok"

    def test_base_url(self):
        from src.llm.grok_provider import GrokProvider

        p = GrokProvider()
        assert p._base_url == "https://api.x.ai/v1"


# ── Schema integration ────────────────────────────────────────────────────────


class TestSchemaIntegration:
    """Test that existing Anthropic tool schemas convert correctly."""

    def test_plan_tool_converts(self):
        from src.llm.tool_converter import from_anthropic_schema, to_openai_format
        from src.planning.schemas import PLAN_TOOL_SCHEMA

        tool = from_anthropic_schema(PLAN_TOOL_SCHEMA)
        assert tool.name == "output_implementation_plan"
        openai_fmt = to_openai_format(tool)
        assert openai_fmt["function"]["name"] == "output_implementation_plan"
        assert openai_fmt["function"]["parameters"]["required"]

    def test_answer_tool_converts(self):
        from src.llm.tool_converter import from_anthropic_schema, to_openai_format
        from src.planning.schemas import ANSWER_TOOL_SCHEMA

        tool = from_anthropic_schema(ANSWER_TOOL_SCHEMA)
        assert tool.name == "answer_codebase_question"
        openai_fmt = to_openai_format(tool)
        assert openai_fmt["type"] == "function"

    def test_analyze_tool_converts(self):
        from src.llm.tool_converter import from_anthropic_schema, to_openai_format
        from src.planning.schemas import ANALYZE_IMPROVE_TOOL_SCHEMA

        tool = from_anthropic_schema(ANALYZE_IMPROVE_TOOL_SCHEMA)
        assert tool.name == "analyze_and_improve"
        openai_fmt = to_openai_format(tool)
        assert openai_fmt["function"]["description"]

    def test_ask_answer_tool_converts(self):
        from src.ask.ask_agent import ASK_ANSWER_TOOL
        from src.llm.tool_converter import from_anthropic_schema, to_openai_format

        tool = from_anthropic_schema(ASK_ANSWER_TOOL)
        assert tool.name == "answer_question"
        openai_fmt = to_openai_format(tool)
        assert "cited_files" in openai_fmt["function"]["parameters"]["properties"]


# ── Request model field ───────────────────────────────────────────────────────


class TestRequestSchemas:
    def test_plan_request_has_model(self):
        from src.planning.schemas import PlanRequest

        req = PlanRequest(query="test query")
        assert req.model is None

        req2 = PlanRequest(query="test query", model="gpt-4o")
        assert req2.model == "gpt-4o"

    def test_ask_request_has_model(self):
        from src.planning.schemas import AskRequest

        req = AskRequest(query="test query")
        assert req.model is None

        req2 = AskRequest(query="test query", model="grok-3")
        assert req2.model == "grok-3"


# ── Config ────────────────────────────────────────────────────────────────────


class TestConfig:
    def test_new_settings_exist(self):
        """Verify the new config fields are accessible."""
        from src.config import Settings

        # Check that the fields exist in the model
        fields = Settings.model_fields
        assert "openai_api_key" in fields
        assert "grok_api_key" in fields
        assert "default_model" in fields
