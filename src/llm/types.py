"""
Unified types for the multi-LLM provider abstraction.

These types are provider-agnostic — consumers (claude_planner, ask_agent)
work with these instead of vendor-specific SDK types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class LLMToolSchema:
    """Provider-agnostic tool definition."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object


@dataclass
class LLMStreamEvent:
    """
    Unified streaming event emitted by all providers.

    type values:
      "thinking"   — extended thinking / reasoning trace (Anthropic only)
      "text"       — plain text content delta
      "input_json" — partial JSON for tool_use arguments
    """

    type: Literal["thinking", "text", "input_json"]
    text: str


@dataclass
class LLMToolCall:
    """A parsed tool call from the LLM response."""

    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """Final (non-streaming) response from an LLM provider."""

    tool_calls: list[LLMToolCall] = field(default_factory=list)
    text_content: str = ""
    stop_reason: str = ""
    raw: Any = None  # vendor-specific raw response for debugging
