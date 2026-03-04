"""
Anthropic client — singleton AsyncAnthropic with retry logic.

Exposes:
  get_client()              → shared AsyncAnthropic (Anthropic cloud) instance
  get_ollama_client()       → shared AsyncAnthropic pointed at local Ollama
  get_client_for_model(m)   → routes to the right client based on model name
  is_ollama_model(m)        → True when the model is configured for Ollama
  semaphore                 → asyncio.Semaphore(1) shared across all API calls
"""

from __future__ import annotations

import asyncio

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_client = None
_ollama_client = None
semaphore = asyncio.Semaphore(1)  # one concurrent API call at a time

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
    """Return (or lazily create) the shared Anthropic cloud AsyncAnthropic singleton."""
    global _client
    if _client is None:
        import anthropic

        from src.config import settings

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        _client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
    return _client


def get_ollama_client():
    """Return (or lazily create) the shared Ollama AsyncAnthropic client.

    Ollama v0.14+ exposes the Anthropic Messages API at localhost:11434.
    We reuse the same SDK — only the base_url and a dummy api_key differ.
    Prompt caching is NOT supported by Ollama, so no beta headers are added.
    """
    global _ollama_client
    if _ollama_client is None:
        import anthropic

        from src.config import settings

        if not settings.ollama_base_url:
            raise RuntimeError(
                "OLLAMA_BASE_URL is not configured. Set it in your .env file."
            )
        _ollama_client = anthropic.AsyncAnthropic(
            base_url=settings.ollama_base_url,
            api_key="ollama",  # required by the SDK but ignored by Ollama
        )
        logger.info("ollama: client initialised → %s", settings.ollama_base_url)
    return _ollama_client


def is_ollama_model(model: str) -> bool:
    """Return True if *model* should be routed to the local Ollama instance."""
    from src.config import settings

    if not settings.ollama_base_url or not settings.ollama_models:
        return False
    return model in {m.strip() for m in settings.ollama_models.split(",") if m.strip()}


def get_client_for_model(model: str):
    """Return the appropriate AsyncAnthropic client for *model*.

    Ollama models (configured via OLLAMA_MODELS) → local Ollama client.
    Everything else                                → Anthropic cloud client.
    """
    if is_ollama_model(model):
        return get_ollama_client()
    return get_client()


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
