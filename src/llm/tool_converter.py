"""
Tool schema conversion utilities.

Converts between the existing Anthropic-format tool schemas (used in
claude_planner.py and ask_agent.py) and the provider-agnostic LLMToolSchema.
"""

from __future__ import annotations

from src.llm.types import LLMToolSchema


def from_anthropic_schema(schema: dict) -> LLMToolSchema:
    """
    Convert an Anthropic-format tool schema dict to an LLMToolSchema.

    Anthropic format:
        {"name": "...", "description": "...", "input_schema": {...}}

    LLMToolSchema:
        LLMToolSchema(name="...", description="...", parameters={...})
    """
    return LLMToolSchema(
        name=schema["name"],
        description=schema.get("description", ""),
        parameters=schema.get("input_schema", {}),
    )


def to_anthropic_format(tool: LLMToolSchema) -> dict:
    """Convert LLMToolSchema back to Anthropic format."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def to_openai_format(tool: LLMToolSchema) -> dict:
    """
    Convert LLMToolSchema to OpenAI function-calling format.

    OpenAI format:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
