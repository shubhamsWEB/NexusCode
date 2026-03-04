"""
LLM client module — Anthropic cloud + local Ollama provider for NexusCode.

Usage:
    from src.llm import get_client_for_model, is_ollama_model
    client = get_client_for_model("glm-4.6:cloud")
"""

from src.llm.client import (
    RateLimitOrOverloadError,
    get_client,
    get_client_for_model,
    get_ollama_client,
    is_ollama_model,
)

__all__ = [
    "get_client",
    "get_client_for_model",
    "get_ollama_client",
    "is_ollama_model",
    "RateLimitOrOverloadError",
]
