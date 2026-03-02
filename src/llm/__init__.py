"""
Anthropic client module — single LLM provider for NexusCode.

Usage:
    from src.llm import get_client
    client = get_client()
"""

from src.llm.client import RateLimitOrOverloadError, get_client

__all__ = ["get_client", "RateLimitOrOverloadError"]
