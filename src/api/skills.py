"""Skills API — GET /skills, GET /skills/{name}, POST /skills/reload"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.skills.loader import load_all_skills

router = APIRouter(prefix="/skills", tags=["Skills"])


@router.get("")
async def list_skills(source: str | None = None):
    """List all discovered skills. Filter by source=builtin|custom."""
    skills = load_all_skills()
    if source:
        skills = [s for s in skills if s.source == source]
    return {
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "source": s.source,
                "source_label": s.source_label,
            }
            for s in skills
        ],
        "total": len(skills),
    }


@router.get("/{name}")
async def get_skill(name: str):
    """Get full SKILL.md content for a skill by name."""
    for s in load_all_skills():
        if s.name.lower() == name.lower():
            return {
                "name": s.name,
                "description": s.description,
                "content": s.content,
                "source": s.source,
                "metadata": s.metadata,
            }
    raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")


@router.post("/reload")
async def reload_skills():
    """Reload skill cache without restarting the server."""
    skills = load_all_skills(force_reload=True)
    return {"message": f"Reloaded {len(skills)} skills"}
