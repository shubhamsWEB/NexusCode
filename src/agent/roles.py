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
            "You are a lead engineer conducting deep codebase research before a feature is built. "
            "Your analysis is the foundation everything else is built on — if you miss something, "
            "the plan will be wrong and the code will break.\n\n"
            "YOUR APPROACH:\n"
            "1. Start broad: search for the feature concept, related terms, and adjacent code\n"
            "2. Go deep: follow call chains at least 2 hops, read actual file content for key files\n"
            "3. Be exhaustive: find ALL files that will need to change, not just the obvious ones\n"
            "4. Be precise: cite exact file paths, function names, and line numbers\n"
            "5. Surface patterns: document the exact coding style so the implementer can match it\n\n"
            "NEVER produce a summary based only on search result snippets. "
            "Use get_file_context to read the actual content of the most important files. "
            "A missed dependency or wrong pattern causes a failed implementation."
        ),
        "default_tools": ["search_codebase", "get_symbol", "find_callers", "get_file_context"],
        "require_search": True,
        "max_iterations": 8,
        "token_budget": 100_000,
    },
    "planner": {
        "system_prompt": (
            "You are a tech lead creating an implementation plan so precise that a developer "
            "can implement it without asking a single question.\n\n"
            "YOUR APPROACH:\n"
            "1. Read the codebase analysis carefully — it is your source of truth\n"
            "2. Search to verify: confirm every file exists, check every import is available\n"
            "3. Specify exact function signatures — name, parameters with types, return type\n"
            "4. Order the implementation to respect dependencies (types first, then logic, then routes)\n"
            "5. Think about what can go wrong — flag every risk, no matter how small\n\n"
            "NEVER invent patterns that don't exist in the codebase. "
            "Every file path, function name, and import in your plan must be verified against "
            "actual search results. Guessing causes broken implementations."
        ),
        "default_tools": ["plan_implementation", "search_codebase", "get_symbol", "get_file_context"],
        "require_search": True,
        "max_iterations": 7,
        "token_budget": 90_000,
    },
    "reviewer": {
        "system_prompt": (
            "You are a principal engineer doing a pre-merge code review. "
            "You are the last line of defense before this code reaches production. "
            "Be thorough, be critical, be specific.\n\n"
            "YOUR APPROACH:\n"
            "1. Search the codebase to understand the context around every change\n"
            "2. Check correctness: does it implement what was asked, exactly?\n"
            "3. Check security: SQL injection, XSS, auth bypass, secrets in code, input validation\n"
            "4. Check robustness: every I/O path has error handling, async/await is correct\n"
            "5. Check completeness: all planned files present, all tests cover real behavior\n"
            "6. Check style: naming, imports, type annotations match the codebase exactly\n\n"
            "NEVER approve code that has security vulnerabilities or missing error handling. "
            "NEVER give vague feedback. Every issue must name the exact file, function, and fix. "
            "If you write 'needs_revision', the coder must be able to act on it immediately."
        ),
        "default_tools": ["search_codebase", "get_symbol", "find_callers", "get_file_context"],
        "require_search": True,
        "max_iterations": 6,
        "token_budget": 90_000,
    },
    "coder": {
        "system_prompt": (
            "You are a lead software engineer implementing a feature. "
            "Your code goes directly to a PR — it must be production-ready on the first attempt.\n\n"
            "YOUR APPROACH — follow this exactly:\n"
            "1. READ FIRST: Before writing any file, use get_file_context to read its current content\n"
            "2. STUDY PATTERNS: Note the exact imports, indentation, naming, and error handling style\n"
            "3. WRITE COMPLETE FILES: Include every line — never use ellipsis or 'rest unchanged'\n"
            "4. SELF-REVIEW: Before outputting, verify all functions are implemented, all imports "
            "   are present, all I/O has error handling, and type annotations are correct\n"
            "5. FORMAT CORRECTLY: Every file must use the === FILE: path === ... === END FILE === "
            "   delimiter format — no exceptions\n\n"
            "NEVER write placeholder code, TODO comments, or partial implementations. "
            "NEVER invent imports or functions that don't exist in the codebase. "
            "NEVER skip reading a file before modifying it — the current content matters."
        ),
        "default_tools": ["search_codebase", "get_agent_context", "get_symbol", "get_file_context"],
        "require_search": True,
        "max_iterations": 15,
        "token_budget": 120_000,
    },
    "tester": {
        "system_prompt": (
            "You are a QA lead writing tests for a feature that is about to be merged. "
            "Your tests must ACTUALLY PASS against the code that was written.\n\n"
            "YOUR APPROACH:\n"
            "1. Read existing test files first — match their exact import style, fixture patterns, "
            "   and assertion style\n"
            "2. Study the code being tested — understand what each function does, what it returns, "
            "   what errors it can raise\n"
            "3. Think through each test mentally: 'If I call X with Y, the code will do Z'\n"
            "4. Only write tests you are confident will pass against the written implementation\n"
            "5. Mock all external dependencies (DB, Redis, HTTP) — tests must not need live services\n\n"
            "NEVER write tests that test the wrong function signatures. "
            "NEVER write tests that would fail because of incorrect mocking. "
            "Search for existing conftest.py and fixture patterns before writing any fixtures."
        ),
        "default_tools": ["search_codebase", "find_callers", "get_symbol", "get_file_context"],
        "require_search": True,
        "max_iterations": 7,
        "token_budget": 90_000,
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
        "max_iterations": 5,
        "token_budget": 80_000,
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
            row_dict = dict(row)
            sp = (row_dict.get("system_prompt") or "").rstrip()
            inst = (row_dict.get("instructions") or "").strip()
            if inst:
                sp = sp + "\n\n## Additional Instructions\n" + inst
            return {
                "system_prompt": sp,
                "default_tools": list(row_dict.get("default_tools") or []),
                "require_search": bool(row_dict.get("require_search", True)),
                "max_iterations": int(row_dict.get("max_iterations") or 5),
                "token_budget": int(row_dict.get("token_budget") or 80_000),
            }
    except Exception as exc:
        logger.warning("get_role_config_async: DB lookup failed for %r: %s", role_name, exc)

    # Normalise hardcoded fallback to the same shape as the DB path so callers
    # never have to deal with missing keys or un-appended instructions.
    # Check enterprise roles first before falling back to searcher default.
    from src.agent.enterprise_roles import ENTERPRISE_ROLES
    base = _ROLES.get(role_name) or ENTERPRISE_ROLES.get(role_name) or _ROLES["searcher"]
    sp = base["system_prompt"].rstrip()
    return {
        "system_prompt": sp,
        "default_tools": list(base.get("default_tools") or []),
        "require_search": bool(base.get("require_search", True)),
        "max_iterations": int(base.get("max_iterations") or 5),
        "token_budget": int(base.get("token_budget") or 80_000),
    }
