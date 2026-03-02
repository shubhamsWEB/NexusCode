"""
Anthropic client — singleton AsyncAnthropic with retry logic.

The only LLM provider for NexusCode. Exposes:
  get_client()  → shared AsyncAnthropic instance
  semaphore     → asyncio.Semaphore(1) shared across all API calls
"""

from __future__ import annotations

import asyncio

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_client = None
semaphore = asyncio.Semaphore(1)  # one concurrent Anthropic API call at a time

MAX_RETRIES = 5
RETRYABLE_CODES = {429, 529}


class RateLimitOrOverloadError(RuntimeError):
    """Raised when all retries for 429/529 are exhausted."""

    def __init__(self, cause: Exception | None = None):
        status = getattr(cause, "status_code", "unknown") if cause else "unknown"
        if status == 429:
            msg = (
                "Rate limit exceeded — too many concurrent requests. "
                "Please wait a moment and try again."
            )
        else:
            msg = "Anthropic API is overloaded. Please try again in a moment."
        super().__init__(msg)
        self.__cause__ = cause
        self.status_code = status


def get_client():
    """Return (or lazily create) the shared AsyncAnthropic singleton."""
    global _client
    if _client is None:
        import anthropic

        from src.config import settings

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def get_retry_after(exc) -> float | None:
    """Extract Retry-After header value from an Anthropic APIStatusError."""
    try:
        response = getattr(exc, "response", None)
        if response is not None:
            ra = response.headers.get("retry-after")
            if ra:
                return float(ra)
    except (ValueError, AttributeError):
        pass
    return None
