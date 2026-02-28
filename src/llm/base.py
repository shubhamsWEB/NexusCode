"""
Base protocol (interface) for LLM providers.

All providers implement this protocol so consumers can swap models
without changing their call sites.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from src.llm.types import LLMResponse, LLMStreamEvent, LLMToolSchema


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol that every LLM provider must satisfy."""

    provider_name: str
    supports_web_search: bool
    supports_thinking: bool

    async def generate(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[LLMToolSchema] | None = None,
        tool_choice: str | dict[str, str] | None = None,
        max_tokens: int = 4096,
        thinking_budget: int = 0,
    ) -> LLMResponse:
        """Non-streaming generation. Returns the full response."""
        ...

    async def stream(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[LLMToolSchema] | None = None,
        tool_choice: str | dict[str, str] | None = None,
        max_tokens: int = 4096,
        thinking_budget: int = 0,
    ) -> AsyncIterator[LLMStreamEvent | LLMResponse]:
        """
        Streaming generation.

        Yields LLMStreamEvent for incremental content, then a final
        LLMResponse as the last item.
        """
        ...
        # Make this a valid async generator for Protocol purposes
        if False:  # pragma: no cover
            yield LLMStreamEvent(type="text", text="")
