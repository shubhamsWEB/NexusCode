"""
Multi-LLM provider abstraction layer.

Usage:
    from src.llm import get_provider, list_available_models

    provider = get_provider("gpt-4o")
    response = await provider.generate(model="gpt-4o", system="...", messages=[...])
"""

from src.llm.registry import get_provider, list_available_models, resolve_provider

__all__ = ["get_provider", "list_available_models", "resolve_provider"]
