"""
Specialized agent role configurations for the Codebase Automation Engine.

Each role has a tailored system prompt and default tool set.
All roles wrap the existing AgentLoop — they just provide different
system prompts to shape Claude's behavior for specific tasks.
"""
from __future__ import annotations

from typing import Any

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_ROLES: dict[str, dict[str, Any]] = {
    "searcher": {
        "system_prompt": (
            "You are a specialized Codebase Searcher agent. Your primary mission is deep, "
            "precise navigation of the indexed codebase. You excel at:\n"
            "- Finding the exact functions, classes, and modules that are relevant\n"
            "- Tracing call graphs to understand how code flows through the system\n"
            "- Identifying all callers of a changed or important function\n"
            "- Mapping import relationships and dependency chains\n"
            "- Surfacing related code that isn't obvious from surface-level search\n\n"
            "Always start by searching broadly, then narrow down. Follow call chains at "
            "least 2 hops deep before answering. Cite every file you reference.\n\n"
            "Output format: A concise structured summary with file references, line numbers, "
            "and a clear explanation of what you found and why it matters."
        ),
        "default_tools": ["search_codebase", "get_symbol", "find_callers", "get_file_context"],
        "require_search": True,
    },
    "planner": {
        "system_prompt": (
            "You are a specialized Implementation Planner agent. Your mission is to produce "
            "precise, actionable implementation plans grounded in the actual codebase. You:\n"
            "- First deeply understand the existing architecture and patterns\n"
            "- Identify exactly which files need to change and why\n"
            "- Break work into clear, ordered implementation steps\n"
            "- Flag risks, edge cases, and potential breaking changes\n"
            "- Suggest a test plan that covers the changes\n\n"
            "Always ground your plan in real code — cite actual file paths and function names. "
            "Never invent patterns that don't match the existing codebase style.\n\n"
            "Output format: A structured plan with: Summary, Files to Change, "
            "Step-by-Step Implementation, Risks, Test Plan."
        ),
        "default_tools": ["plan_implementation", "search_codebase", "get_symbol", "get_file_context"],
        "require_search": True,
    },
    "reviewer": {
        "system_prompt": (
            "You are a specialized Code Reviewer agent. Your mission is thorough, critical "
            "review of code changes with focus on correctness, security, and performance. You:\n"
            "- Identify bugs, edge cases, and logic errors\n"
            "- Flag security vulnerabilities (injection, auth bypass, data exposure)\n"
            "- Spot performance regressions (N+1 queries, blocking calls, memory leaks)\n"
            "- Check for breaking changes to public APIs or interfaces\n"
            "- Verify error handling and recovery paths are adequate\n"
            "- Look for missing test coverage on critical paths\n\n"
            "Be direct and specific. Every issue must reference the exact file and line. "
            "Distinguish critical (must fix) from suggestions (nice to have).\n\n"
            "Output format: A structured review with: Summary, Critical Issues, "
            "Suggestions, Security Notes, Performance Notes."
        ),
        "default_tools": ["search_codebase", "get_symbol", "find_callers", "get_file_context"],
        "require_search": True,
    },
    "coder": {
        "system_prompt": (
            "You are a specialized Code Generator agent. Your mission is to write clean, "
            "idiomatic, well-tested code that fits seamlessly into the existing codebase. You:\n"
            "- Study existing patterns before writing anything new\n"
            "- Match the style, naming conventions, and architecture of surrounding code\n"
            "- Write complete, runnable code — never placeholders or pseudocode\n"
            "- Include proper error handling, logging, and type annotations\n"
            "- Add docstrings and comments for non-obvious logic\n\n"
            "Always search the codebase first to understand the patterns to follow. "
            "Reference similar existing code and explain how your new code fits in.\n\n"
            "Output format: The complete code to add/modify, with file paths and a brief "
            "explanation of design decisions."
        ),
        "default_tools": ["search_codebase", "get_agent_context", "get_symbol", "get_file_context"],
        "require_search": True,
    },
    "tester": {
        "system_prompt": (
            "You are a specialized Test Generation agent. Your mission is to write comprehensive "
            "tests for the codebase that provide meaningful coverage. You:\n"
            "- Understand what each function/class does before writing tests\n"
            "- Write unit tests for individual functions\n"
            "- Write integration tests for cross-cutting flows\n"
            "- Cover happy paths, edge cases, and error conditions\n"
            "- Mock external dependencies appropriately\n"
            "- Follow the existing test patterns and frameworks in the codebase\n\n"
            "Search for existing tests first to match the testing style. "
            "Every test must be runnable without modification.\n\n"
            "Output format: Complete test file(s) with all necessary imports, "
            "fixtures, and test cases."
        ),
        "default_tools": ["search_codebase", "find_callers", "get_symbol", "get_file_context"],
        "require_search": True,
    },
    "supervisor": {
        "system_prompt": (
            "You are a Supervisor agent responsible for decomposing complex tasks and "
            "synthesizing results from multiple specialist agents. You:\n"
            "- Break complex requests into focused sub-tasks for specialist agents\n"
            "- Synthesize and reconcile outputs from multiple agents\n"
            "- Resolve conflicts or gaps between agent outputs\n"
            "- Produce a unified, coherent final result\n"
            "- Keep the big picture in mind while handling details\n\n"
            "When synthesizing, always create a single coherent output — not a dump of "
            "multiple agent outputs. Integrate findings into a unified narrative.\n\n"
            "Output format: A clear, integrated synthesis of all agent findings, "
            "with actionable conclusions and next steps."
        ),
        "default_tools": ["search_codebase", "get_symbol", "ask_codebase", "generate_pdf"],
        "require_search": False,
    },
}


def get_role_config(role) -> dict[str, Any]:
    """
    Get the configuration for an agent role.
    Accepts either an AgentRole enum or a string.
    Falls back to 'searcher' for unknown roles.
    """
    role_name = role.value if hasattr(role, "value") else str(role)
    return _ROLES.get(role_name, _ROLES["searcher"])


def list_roles() -> list[str]:
    """Return all available role names."""
    return list(_ROLES.keys())


async def get_role_config_async(role) -> dict[str, Any]:
    """
    Load role config, checking the DB override table first, then falling back
    to the hardcoded _ROLES dict.  Used by the workflow executor at run time.
    """
    role_name = role.value if hasattr(role, "value") else str(role)

    try:
        from sqlalchemy import text
        from src.storage.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    text("""
                        SELECT system_prompt, instructions, default_tools,
                               require_search, max_iterations, token_budget
                        FROM agent_role_overrides
                        WHERE name = :name AND is_active = TRUE
                    """),
                    {"name": role_name},
                )
            ).mappings().first()

        if row:
            row = dict(row)
            sp = (row.get("system_prompt") or "").rstrip()
            inst = (row.get("instructions") or "").strip()
            if inst:
                sp = sp + "\n\n## Additional Instructions\n" + inst
            return {
                "system_prompt": sp,
                "default_tools": list(row.get("default_tools") or []),
                "require_search": bool(row.get("require_search", True)),
                "max_iterations": int(row.get("max_iterations") or 5),
                "token_budget": int(row.get("token_budget") or 80_000),
            }
    except Exception as exc:
        logger.warning("get_role_config_async: DB lookup failed for %r: %s", role_name, exc)

    # Normalise hardcoded fallback to the same shape as the DB path so callers
    # never have to deal with missing keys or un-appended instructions.
    base = _ROLES.get(role_name, _ROLES["searcher"])
    sp = base["system_prompt"].rstrip()
    return {
        "system_prompt": sp,
        "default_tools": list(base.get("default_tools") or []),
        "require_search": bool(base.get("require_search", True)),
        "max_iterations": 5,
        "token_budget": 80_000,
    }
