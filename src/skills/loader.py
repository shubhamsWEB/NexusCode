"""
Skill discovery and loading.
Scans built-in skills/ dir + CUSTOM_SKILLS_DIRS env paths.
Caches results in memory; use force_reload=True to refresh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


@dataclass
class SkillInfo:
    name: str
    description: str
    content: str         # full SKILL.md text
    source: str          # "builtin" | "custom"
    source_label: str    # e.g. "skills/" or "/path/to/custom"
    metadata: dict = field(default_factory=dict)


_cache: list[SkillInfo] = []
_loaded = False


def load_all_skills(force_reload: bool = False) -> list[SkillInfo]:
    global _cache, _loaded
    if _loaded and not force_reload:
        return _cache

    from src.config import settings

    skills: list[SkillInfo] = []

    # 1. Built-in skills (relative to project root)
    builtin = Path("skills")
    if builtin.exists():
        found = _load_from_dir(builtin, "builtin", "skills/")
        skills.extend(found)
        logger.info("skills: loaded %d built-in skills", len(found))

    # 2. Custom dirs from CUSTOM_SKILLS_DIRS env var
    for dir_str in settings.custom_skills_dirs_list:
        p = Path(dir_str)
        if p.exists():
            found = _load_from_dir(p, "custom", str(p))
            skills.extend(found)
            logger.info("skills: loaded %d custom skills from %s", len(found), dir_str)
        else:
            logger.warning("skills: custom dir not found: %s", dir_str)

    _cache = skills
    _loaded = True
    return skills


def _load_from_dir(base: Path, source: str, label: str) -> list[SkillInfo]:
    results = []
    for skill_md in sorted(base.rglob("SKILL.md")):
        info = _parse_skill(skill_md, source, label)
        if info:
            results.append(info)
    return results


def _parse_skill(path: Path, source: str, label: str) -> Optional[SkillInfo]:
    try:
        content = path.read_text(encoding="utf-8")
        name, description, metadata = _extract_frontmatter(content)
        if not name:
            name = path.parent.name  # fallback to directory name
        return SkillInfo(
            name=name,
            description=description,
            content=content,
            source=source,
            source_label=label,
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning("skills: failed to parse %s: %s", path, exc)
        return None


def _extract_frontmatter(content: str) -> tuple[str, str, dict]:
    """Extract the FIRST YAML frontmatter block from a SKILL.md."""
    try:
        import yaml
    except ImportError:
        return "", "", {}

    m = _FRONTMATTER_RE.match(content)
    if not m:
        return "", "", {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
        return str(data.get("name", "")), str(data.get("description", "")), data
    except Exception:
        return "", "", {}
