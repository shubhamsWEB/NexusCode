"""
GET/PUT/DELETE /agent-roles — CRUD for agent role configurations.

Built-in roles (searcher, planner, reviewer, coder, tester, supervisor) are
defined in src/agent/roles.py and can be overridden via this API.
Custom roles are stored only in the DB.

Endpoints:
  GET    /agent-roles              — list all roles (builtins + DB overrides + custom)
  GET    /agent-roles/{name}       — get one role
  PUT    /agent-roles/{name}       — create or update (upsert)
  DELETE /agent-roles/{name}       — delete custom role OR remove builtin override
  POST   /agent-roles/{name}/reset — reset builtin to hardcoded defaults
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from src.agent.roles import _ROLES
from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)
router = APIRouter(prefix="/agent-roles", tags=["agent-roles"])

_BUILTIN_NAMES: frozenset[str] = frozenset(_ROLES.keys())


# ── Request model ─────────────────────────────────────────────────────────────


class UpsertRoleRequest(BaseModel):
    display_name: str = Field("", max_length=128)
    description: str = Field("", max_length=1000)
    system_prompt: str = Field(..., min_length=1)
    instructions: str = Field("", max_length=8000)
    default_tools: list[str] = Field(default_factory=list)
    require_search: bool = True
    max_iterations: int = Field(5, ge=1, le=20)
    token_budget: int = Field(80_000, ge=1_000, le=500_000)
    is_active: bool = True


# ── Formatters ────────────────────────────────────────────────────────────────


def _builtin_to_dict(name: str) -> dict[str, Any]:
    """Convert a hardcoded builtin entry to the API response shape."""
    r = _ROLES[name]
    return {
        "name": name,
        "display_name": name.replace("-", " ").title(),
        "description": "",
        "system_prompt": r["system_prompt"],
        "instructions": "",
        "default_tools": list(r.get("default_tools", [])),
        "require_search": r.get("require_search", True),
        "max_iterations": 5,
        "token_budget": 80_000,
        "is_builtin": True,
        "is_overridden": False,
        "is_active": True,
        "created_at": None,
        "updated_at": None,
    }


def _row_to_dict(row: dict, is_builtin: bool) -> dict[str, Any]:
    tools = row.get("default_tools") or []
    return {
        "name": row["name"],
        "display_name": row.get("display_name") or row["name"].replace("-", " ").title(),
        "description": row.get("description") or "",
        "system_prompt": row.get("system_prompt") or "",
        "instructions": row.get("instructions") or "",
        "default_tools": list(tools),
        "require_search": bool(row.get("require_search", True)),
        "max_iterations": int(row.get("max_iterations") or 5),
        "token_budget": int(row.get("token_budget") or 80_000),
        "is_builtin": is_builtin,
        "is_overridden": is_builtin,
        "is_active": bool(row.get("is_active", True)),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/tools", response_model=None)
async def list_available_tools() -> JSONResponse:
    """
    Return all tools available for agent roles.
    Split into two groups:
      - internal: hardcoded codebase retrieval tools always available
      - external: tools loaded from connected MCP servers (dynamic, may be empty)
    """
    from src.agent.mcp_bridge import get_external_tools_info
    from src.agent.tool_schemas import ALL_INTERNAL_TOOL_SCHEMAS

    # Build one-liner summaries for the UI tooltip from the first sentence of each schema description
    def _first_sentence(desc: str) -> str:
        """Extract the first sentence (up to first \\n or period+space) for UI display."""
        desc = desc.strip()
        for sep in ("\n", ". "):
            idx = desc.find(sep)
            if 0 < idx < 160:
                return desc[:idx].rstrip(".")
        return desc[:120]

    internal = [
        {
            "name": t["name"],
            "description": _first_sentence(t.get("description", "")),
            "full_description": t.get("description", ""),
            "source": "internal",
            "server_url": None,
        }
        for t in ALL_INTERNAL_TOOL_SCHEMAS
    ]

    external = [
        {
            "name": t["name"],
            "description": t["description"],
            "full_description": t["description"],
            "source": "external",
            "server_url": t["server_url"],
        }
        for t in get_external_tools_info()
    ]

    return JSONResponse({"internal": internal, "external": external})


@router.get("", response_model=None)
async def list_roles() -> JSONResponse:
    """List all agent roles: builtins merged with DB overrides, plus any custom roles."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(text("SELECT * FROM agent_role_overrides ORDER BY name"))
        ).mappings().all()

    db_map = {r["name"]: dict(r) for r in rows}

    result: list[dict] = []

    # Builtins first (in definition order)
    for name in _ROLES:
        if name in db_map:
            result.append(_row_to_dict(db_map[name], is_builtin=True))
        else:
            result.append(_builtin_to_dict(name))

    # Custom roles (not in builtins)
    for name, row in db_map.items():
        if name not in _BUILTIN_NAMES:
            result.append(_row_to_dict(row, is_builtin=False))

    return JSONResponse(result)


@router.get("/{name}", response_model=None)
async def get_role(name: str) -> JSONResponse:
    """Get a single agent role by name."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT * FROM agent_role_overrides WHERE name = :name"),
                {"name": name},
            )
        ).mappings().first()

    if row:
        return JSONResponse(_row_to_dict(dict(row), is_builtin=(name in _BUILTIN_NAMES)))
    if name in _BUILTIN_NAMES:
        return JSONResponse(_builtin_to_dict(name))
    raise HTTPException(status_code=404, detail=f"Role {name!r} not found")


@router.put("/{name}", response_model=None)
async def upsert_role(name: str, req: UpsertRoleRequest) -> JSONResponse:
    """Create or update an agent role. Built-in roles are overridden, not replaced."""
    # Validate tool names against all currently-known tools (internal + external MCP).
    # Unknown names cause silent misconfiguration — agents silently get no tools.
    if req.default_tools:
        from src.agent.mcp_bridge import get_external_tool_schemas
        from src.agent.tool_schemas import ALL_INTERNAL_TOOL_SCHEMAS

        valid_tools: set[str] = {t["name"] for t in ALL_INTERNAL_TOOL_SCHEMAS}
        valid_tools |= {t["name"] for t in get_external_tool_schemas()}
        unknown = [t for t in req.default_tools if t not in valid_tools]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": f"Unknown tool name(s): {unknown}",
                    "valid_tools": sorted(valid_tools),
                },
            )

    is_builtin = name in _BUILTIN_NAMES

    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                text("SELECT name FROM agent_role_overrides WHERE name = :name"),
                {"name": name},
            )
        ).first()

        params: dict = {
            "name": name,
            "display_name": req.display_name or name.replace("-", " ").title(),
            "description": req.description,
            "system_prompt": req.system_prompt,
            "instructions": req.instructions,
            "tools": req.default_tools,
            "require_search": req.require_search,
            "max_iterations": req.max_iterations,
            "token_budget": req.token_budget,
            "is_builtin": is_builtin,
            "is_active": req.is_active,
        }

        if existing:
            await session.execute(
                text("""
                    UPDATE agent_role_overrides
                    SET display_name   = :display_name,
                        description    = :description,
                        system_prompt  = :system_prompt,
                        instructions   = :instructions,
                        default_tools  = :tools,
                        require_search = :require_search,
                        max_iterations = :max_iterations,
                        token_budget   = :token_budget,
                        is_active      = :is_active,
                        updated_at     = NOW()
                    WHERE name = :name
                """),
                params,
            )
        else:
            await session.execute(
                text("""
                    INSERT INTO agent_role_overrides
                        (name, display_name, description, system_prompt, instructions,
                         default_tools, require_search, max_iterations, token_budget,
                         is_builtin, is_active)
                    VALUES
                        (:name, :display_name, :description, :system_prompt, :instructions,
                         :tools, :require_search, :max_iterations, :token_budget,
                         :is_builtin, :is_active)
                """),
                params,
            )
        await session.commit()

    return await get_role(name)


@router.delete("/{name}", response_model=None)
async def delete_role(name: str) -> JSONResponse:
    """
    Delete a custom role, or remove the DB override for a built-in role
    (restoring it to hardcoded defaults).
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("DELETE FROM agent_role_overrides WHERE name = :name"),
            {"name": name},
        )
        await session.commit()

    if int(getattr(result, "rowcount", 0) or 0) == 0 and name not in _BUILTIN_NAMES:
        raise HTTPException(status_code=404, detail=f"Role {name!r} not found")

    if name in _BUILTIN_NAMES:
        return JSONResponse({"reset": True, "name": name,
                             "message": "Override removed — restored to built-in defaults."})
    return JSONResponse({"deleted": True, "name": name})


@router.post("/{name}/reset", response_model=None)
async def reset_role(name: str) -> JSONResponse:
    """Reset a built-in role to its hardcoded defaults by removing any DB override."""
    if name not in _BUILTIN_NAMES:
        raise HTTPException(status_code=400, detail=f"{name!r} is not a built-in role")

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM agent_role_overrides WHERE name = :name"),
            {"name": name},
        )
        await session.commit()

    return JSONResponse({"reset": True, "name": name, **_builtin_to_dict(name)})
