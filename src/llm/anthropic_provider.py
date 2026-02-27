"""
Anthropic (Claude) LLM provider.

Wraps AsyncAnthropic with retry logic, rate-limit semaphore, and
streaming event normalization into the unified LLMProvider interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from src.config import settings
from src.llm.base import LLMProvider
from src.llm.tool_converter import to_anthropic_format
from src.llm.types import LLMResponse, LLMStreamEvent, LLMToolCall, LLMToolSchema

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_RETRYABLE_STATUS_CODES = {429, 529}


def _get_retry_after(exc) -> float | None:
    """Extract Retry-After header from an Anthropic API error response."""
    try:
        response = getattr(exc, "response", None)
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                return float(retry_after)
    except (ValueError, AttributeError):
        pass
    return None


class RateLimitOrOverloadError(RuntimeError):
    """Raised when all retries for 429/529 are exhausted."""

    def __init__(self, cause: Exception | None = None):
        status = getattr(cause, "status_code", "unknown") if cause else "unknown"
        if status == 429:
            msg = (
                "Rate limit exceeded - too many concurrent requests. "
                "Please wait a moment and try again, or reduce concurrent usage."
            )
        else:
            msg = "Anthropic API is overloaded. Please try again in a moment."
        super().__init__(msg)
        self.__cause__ = cause
        self.status_code = status


class AnthropicProvider:
    """LLM provider for Anthropic Claude models."""

    provider_name = "anthropic"
    supports_web_search = True
    supports_thinking = True

    def __init__(self):
        self._client = None
        self._semaphore = asyncio.Semaphore(1)

    def _get_client(self):
        if self._client is None:
            import anthropic

            if not settings.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
            self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        return self._client

    def _convert_tool_choice(self, tool_choice, tools):
        """Convert unified tool_choice to Anthropic format."""
        if tool_choice is None or not tools:
            return {"type": "auto"} if tools else None
        if isinstance(tool_choice, str):
            if tool_choice == "auto":
                return {"type": "auto"}
            if tool_choice == "any":
                return {"type": "any"}
            if tool_choice == "none":
                return None
            # Treat as tool name
            return {"type": "tool", "name": tool_choice}
        if isinstance(tool_choice, dict):
            if "name" in tool_choice:
                return {"type": "tool", "name": tool_choice["name"]}
            return tool_choice
        return {"type": "auto"}

    def _build_call_params(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[LLMToolSchema] | None,
        tool_choice,
        max_tokens: int,
        thinking_budget: int,
    ) -> dict:
        params = {
            "model": model,
            "max_tokens": max_tokens + thinking_budget if thinking_budget > 0 else max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            params["tools"] = [to_anthropic_format(t) for t in tools]
            tc = self._convert_tool_choice(tool_choice, tools)
            if tc:
                params["tool_choice"] = tc
        if thinking_budget > 0:
            params["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        return params

    def _parse_response(self, message) -> LLMResponse:
        """Convert an Anthropic message into a unified LLMResponse."""
        tool_calls = []
        text_parts = []
        for block in message.content:
            if block.type == "tool_use":
                tool_calls.append(LLMToolCall(name=block.name, input=block.input))
            elif hasattr(block, "text") and block.text:
                text_parts.append(block.text)
        return LLMResponse(
            tool_calls=tool_calls,
            text_content=" ".join(text_parts),
            stop_reason=message.stop_reason or "",
            raw=message,
        )

    async def _retry_loop(self, coro_factory):
        """Execute with exponential backoff on 429/529."""
        import anthropic

        last_exc = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except anthropic.APIStatusError as exc:
                if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    retry_after = _get_retry_after(exc)
                    if retry_after:
                        wait = min(retry_after, 120)
                    elif exc.status_code == 429:
                        wait = min(5 * (2 ** attempt), 120)
                    else:
                        wait = 2 ** attempt
                    label = "rate-limited (429)" if exc.status_code == 429 else "overloaded (529)"
                    logger.warning(
                        "anthropic: %s, retry %d/%d in %.0fs",
                        label, attempt + 1, _MAX_RETRIES, wait,
                    )
                    last_exc = exc
                    await asyncio.sleep(wait)
                else:
                    raise
        raise RateLimitOrOverloadError(last_exc)

    async def generate(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[LLMToolSchema] | None = None,
        tool_choice=None,
        max_tokens: int = 4096,
        thinking_budget: int = 0,
    ) -> LLMResponse:
        client = self._get_client()
        params = self._build_call_params(
            model, system, messages, tools, tool_choice, max_tokens, thinking_budget,
        )

        async with self._semaphore:
            logger.info("anthropic: acquired semaphore, calling %s…", model)
            if thinking_budget > 0:
                # Use streaming internally to avoid 10-min timeout with thinking
                message = await self._stream_to_message(client, params)
            else:
                message = await self._retry_loop(
                    lambda: client.messages.create(**params)
                )

        return self._parse_response(message)

    async def _stream_to_message(self, client, params):
        """Stream internally and collect the final message (avoids timeout with thinking)."""
        import anthropic

        last_exc = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with client.messages.stream(**params) as stream:
                    async for _ in stream:
                        pass
                    return await stream.get_final_message()
            except anthropic.APIStatusError as exc:
                if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    retry_after = _get_retry_after(exc)
                    if retry_after:
                        wait = min(retry_after, 120)
                    elif exc.status_code == 429:
                        wait = min(5 * (2 ** attempt), 120)
                    else:
                        wait = 2 ** attempt
                    logger.warning(
                        "anthropic: stream %s, retry %d/%d in %.0fs",
                        exc.status_code, attempt + 1, _MAX_RETRIES, wait,
                    )
                    last_exc = exc
                    await asyncio.sleep(wait)
                else:
                    raise
        raise RateLimitOrOverloadError(last_exc)

    async def stream(
        self,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[LLMToolSchema] | None = None,
        tool_choice=None,
        max_tokens: int = 4096,
        thinking_budget: int = 0,
    ) -> AsyncIterator[LLMStreamEvent | LLMResponse]:
        import anthropic

        client = self._get_client()
        params = self._build_call_params(
            model, system, messages, tools, tool_choice, max_tokens, thinking_budget,
        )

        async with self._semaphore:
            logger.info("anthropic: stream acquired semaphore, calling %s…", model)

            last_exc = None
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    async with client.messages.stream(**params) as stream:
                        async for event in stream:
                            event_type = getattr(event, "type", None)

                            if event_type == "thinking":
                                thinking_text = getattr(event, "thinking", None)
                                if thinking_text:
                                    yield LLMStreamEvent(type="thinking", text=thinking_text)

                            elif event_type == "text":
                                text = getattr(event, "text", None)
                                if text:
                                    yield LLMStreamEvent(type="text", text=text)

                            elif event_type == "input_json":
                                partial = getattr(event, "partial_json", None)
                                if partial:
                                    yield LLMStreamEvent(type="input_json", text=partial)

                        final_msg = await stream.get_final_message()

                    yield self._parse_response(final_msg)
                    return

                except anthropic.APIStatusError as exc:
                    if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                        retry_after = _get_retry_after(exc)
                        if retry_after:
                            wait = min(retry_after, 120)
                        elif exc.status_code == 429:
                            wait = min(5 * (2 ** attempt), 120)
                        else:
                            wait = 2 ** attempt
                        logger.warning(
                            "anthropic: stream %s, retry %d/%d in %.0fs",
                            exc.status_code, attempt + 1, _MAX_RETRIES, wait,
                        )
                        last_exc = exc
                        await asyncio.sleep(wait)
                    else:
                        raise

            raise RateLimitOrOverloadError(last_exc)
