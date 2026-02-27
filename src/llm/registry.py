"""
LLM provider registry.

Maps model names to provider instances. Providers are lazy singletons
(created on first use, reused thereafter).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.config import settings

if TYPE_CHECKING:
    from src.llm.base import LLMProvider

logger = logging.getLogger(__name__)

# ── Model → provider name mapping ────────────────────────────────────────────

MODEL_REGISTRY: dict[str, str] = {
    # Anthropic
    "claude-sonnet-4-6": "anthropic",
    "claude-opus-4-6": "anthropic",
    "claude-haiku-4-5-20251001": "anthropic",
    # OpenAI
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    "o3": "openai",
    "o3-mini": "openai",
    "o4-mini": "openai",
    # Grok / xAI
    "grok-3": "grok",
    "grok-3-mini": "grok",
}

# ── Provider singletons ──────────────────────────────────────────────────────

_providers: dict[str, LLMProvider] = {}


def _create_provider(provider_name: str) -> LLMProvider:
    """Lazy-create a provider instance."""
    if provider_name == "anthropic":
        from src.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    elif provider_name == "openai":
        from src.llm.openai_provider import OpenAIProvider
        return OpenAIProvider()
    elif provider_name == "grok":
        from src.llm.grok_provider import GrokProvider
        return GrokProvider()
    else:
        raise ValueError(f"Unknown provider: {provider_name}")


def resolve_provider(model: str) -> str:
    """
    Resolve a model name to a provider name.

    Falls back to heuristic matching for unknown model strings.
    """
    if model in MODEL_REGISTRY:
        return MODEL_REGISTRY[model]

    # Heuristic fallback for unknown models
    model_lower = model.lower()
    if "claude" in model_lower:
        return "anthropic"
    if "gpt" in model_lower or model_lower.startswith("o1") or model_lower.startswith("o3") or model_lower.startswith("o4"):
        return "openai"
    if "grok" in model_lower:
        return "grok"

    raise ValueError(
        f"Unknown model '{model}'. Known models: {', '.join(sorted(MODEL_REGISTRY.keys()))}"
    )


def get_provider(model: str | None = None) -> LLMProvider:
    """
    Get a provider instance for the given model.

    If model is None, uses settings.default_model.
    Providers are lazy singletons — created once, reused thereafter.
    """
    if model is None:
        model = settings.default_model

    provider_name = resolve_provider(model)

    if provider_name not in _providers:
        _providers[provider_name] = _create_provider(provider_name)
        logger.info("llm: created %s provider (model=%s)", provider_name, model)

    return _providers[provider_name]


def list_available_models() -> list[dict[str, str]]:
    """
    Return models whose API keys are configured.

    Returns a list of {"model": "...", "provider": "..."} dicts.
    """
    available = []
    for model, provider_name in sorted(MODEL_REGISTRY.items()):
        if _provider_has_key(provider_name):
            available.append({"model": model, "provider": provider_name})
    return available


def _provider_has_key(provider_name: str) -> bool:
    """Check whether the API key for a provider is configured."""
    if provider_name == "anthropic":
        return bool(settings.anthropic_api_key)
    if provider_name == "openai":
        return bool(settings.openai_api_key)
    if provider_name == "grok":
        return bool(settings.grok_api_key)
    return False
