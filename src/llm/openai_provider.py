"""
OpenAI LLM provider.

Wraps AsyncOpenAI with tool schema conversion, streaming event
normalization, retry logic, and rate-limit semaphore.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from src.config import settings
from src.llm.tool_converter import to_openai_format
from src.llm.types import LLMResponse, LLMStreamEvent, LLMToolCall, LLMToolSchema
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_MAX_RETRIES = 5
# OpenAI uses 429 for rate limits and 500/502/503 for overload
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}

# Models that support reasoning_effort parameter
_O_SERIES_MODELS = {"o3", "o4-mini", "o3-mini"}


class OpenAIProvider:
    """LLM provider for OpenAI models (gpt-4o, o3, etc.)."""

    provider_name = "openai"
    supports_web_search = False
    supports_thinking = False

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self._api_key = api_key
        self._base_url = base_url
        self._client: Any | None = None
        self._semaphore = asyncio.Semaphore(1)

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            api_key = self._api_key or settings.openai_api_key
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not set. Add it to your .env file.")
            kwargs: dict[str, Any] = {"api_key": api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    def _convert_tool_choice(self, tool_choice, tools):
        """Convert unified tool_choice to OpenAI format."""
        if tool_choice is None or not tools:
            return "auto" if tools else None
        if isinstance(tool_choice, str):
            if tool_choice in ("auto", "none", "required"):
                return tool_choice
            # Treat as tool name
            return {"type": "function", "function": {"name": tool_choice}}
        if isinstance(tool_choice, dict):
            if "name" in tool_choice:
                return {"type": "function", "function": {"name": tool_choice["name"]}}
            # Pass through if already in OpenAI format
            if "type" in tool_choice and tool_choice.get("type") == "function":
                return tool_choice
            return "auto"
        return "auto"

    def _build_messages(self, system: str, messages: list[dict]) -> list[dict]:
        """Prepend system prompt as first message (OpenAI style)."""
        result = [{"role": "system", "content": system}]
        result.extend(messages)
        return result

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
        params: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(system, messages),
        }

        # o-series models use max_completion_tokens, not max_tokens
        if model in _O_SERIES_MODELS:
            params["max_completion_tokens"] = max_tokens
            if thinking_budget > 0:
                params["reasoning_effort"] = "medium"
        else:
            params["max_tokens"] = max_tokens

        if tools:
            params["tools"] = [to_openai_format(t) for t in tools]
            tc = self._convert_tool_choice(tool_choice, tools)
            if tc:
                params["tool_choice"] = tc

        return params

    def _parse_response(self, message) -> LLMResponse:
        """Convert an OpenAI ChatCompletion message into a unified LLMResponse."""
        choice = message.choices[0] if message.choices else None
        if not choice:
            return LLMResponse(text_content="", stop_reason="unknown", raw=message)

        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    # Attempt JSON repair for malformed output
                    args = _repair_json(tc.function.arguments)
                tool_calls.append(LLMToolCall(name=tc.function.name, input=args))

        return LLMResponse(
            tool_calls=tool_calls,
            text_content=msg.content or "",
            stop_reason=choice.finish_reason or "",
            raw=message,
        )

    async def _retry_loop(self, coro_factory):
        """Execute with exponential backoff on retryable errors."""
        from openai import APIStatusError

        last_exc = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except APIStatusError as exc:
                if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    if exc.status_code == 429:
                        wait = min(5 * (2**attempt), 120)
                    else:
                        wait = 2**attempt
                    logger.warning(
                        "openai: HTTP %d, retry %d/%d in %.0fs",
                        exc.status_code,
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                    )
                    last_exc = exc
                    await asyncio.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"OpenAI API retries exhausted: {last_exc}")

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
            model,
            system,
            messages,
            tools,
            tool_choice,
            max_tokens,
            thinking_budget,
        )

        async with self._semaphore:
            logger.info("openai: acquired semaphore, calling %s…", model)
            message = await self._retry_loop(lambda: client.chat.completions.create(**params))

        return self._parse_response(message)

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
        from openai import APIStatusError

        client = self._get_client()
        params = self._build_call_params(
            model,
            system,
            messages,
            tools,
            tool_choice,
            max_tokens,
            thinking_budget,
        )
        params["stream"] = True

        async with self._semaphore:
            logger.info("openai: stream acquired semaphore, calling %s…", model)

            last_exc = None
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    # Accumulate tool call arguments for final response parsing
                    tool_call_accumulators: dict[int, dict] = {}
                    text_content = ""
                    finish_reason = ""

                    response = await client.chat.completions.create(**params)
                    async for chunk in response:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        fr = chunk.choices[0].finish_reason

                        if fr:
                            finish_reason = fr

                        # Text content
                        if delta.content:
                            text_content += delta.content
                            yield LLMStreamEvent(type="text", text=delta.content)

                        # Tool call deltas
                        if delta.tool_calls:
                            for tc_delta in delta.tool_calls:
                                idx = tc_delta.index
                                if idx not in tool_call_accumulators:
                                    tool_call_accumulators[idx] = {
                                        "name": "",
                                        "arguments": "",
                                    }
                                acc = tool_call_accumulators[idx]
                                if tc_delta.function:
                                    if tc_delta.function.name:
                                        acc["name"] = tc_delta.function.name
                                    if tc_delta.function.arguments:
                                        acc["arguments"] += tc_delta.function.arguments
                                        yield LLMStreamEvent(
                                            type="input_json",
                                            text=tc_delta.function.arguments,
                                        )

                    # Build final response
                    tool_calls = []
                    for idx in sorted(tool_call_accumulators.keys()):
                        acc = tool_call_accumulators[idx]
                        try:
                            args = json.loads(acc["arguments"])
                        except (json.JSONDecodeError, TypeError):
                            args = _repair_json(acc["arguments"])
                        tool_calls.append(LLMToolCall(name=acc["name"], input=args))

                    yield LLMResponse(
                        tool_calls=tool_calls,
                        text_content=text_content,
                        stop_reason=finish_reason,
                        raw=None,
                    )
                    return

                except APIStatusError as exc:
                    if exc.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                        if exc.status_code == 429:
                            wait = min(5 * (2**attempt), 120)
                        else:
                            wait = 2**attempt
                        logger.warning(
                            "openai: stream HTTP %d, retry %d/%d in %.0fs",
                            exc.status_code,
                            attempt + 1,
                            _MAX_RETRIES,
                            wait,
                        )
                        last_exc = exc
                        await asyncio.sleep(wait)
                    else:
                        raise

            raise RuntimeError(f"OpenAI API retries exhausted: {last_exc}")


def _repair_json(raw: str) -> dict:
    """Best-effort JSON repair for malformed tool call arguments."""
    if not raw or not raw.strip():
        return {}
    # Try adding closing brace
    for suffix in ["", "}", '"}', '"}']:
        try:
            return json.loads(raw + suffix)
        except json.JSONDecodeError:
            continue
    logger.warning("openai: could not repair JSON tool arguments: %s", raw[:200])
    return {"_raw_arguments": raw}
