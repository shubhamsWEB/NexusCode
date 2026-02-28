"""
Grok (xAI) LLM provider.

Extends OpenAIProvider since xAI uses an OpenAI-compatible API.
Only overrides client creation to point at the xAI endpoint.
"""

from __future__ import annotations

from src.config import settings
from src.llm.openai_provider import OpenAIProvider


class GrokProvider(OpenAIProvider):
    """LLM provider for xAI Grok models (grok-3, grok-3-mini)."""

    provider_name = "grok"

    def __init__(self):
        super().__init__(
            api_key=settings.grok_api_key,
            base_url="https://api.x.ai/v1",
        )

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            api_key = self._api_key or settings.grok_api_key
            if not api_key:
                raise RuntimeError("GROK_API_KEY is not set. Add it to your .env file.")
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url="https://api.x.ai/v1",
            )
        return self._client
