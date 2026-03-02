"""Load scoped agent rules from the rules/ directory.

Rules are injected into the system prompt so Claude gets repo-specific guidance
(tech stack, conventions, forbidden patterns, important files) that it cannot
derive from code alone.

Load order: global default.md → {owner}/{name}.md (per-repo, user-managed)
"""

from __future__ import annotations

from pathlib import Path

from src.utils.logging import get_secure_logger

_RULES_DIR = Path(__file__).parent.parent.parent / "rules"
logger = get_secure_logger(__name__)


def load_rules(repo_owner: str | None = None, repo_name: str | None = None) -> str:
    """
    Load and concatenate applicable rule files.

    Order: default.md → {owner}/{name}.md
    Returns empty string if no rule files exist.
    """
    candidates = [_RULES_DIR / "default.md"]
    if repo_owner and repo_name:
        candidates.append(_RULES_DIR / repo_owner / f"{repo_name}.md")

    parts: list[str] = []
    for path in candidates:
        if path.exists():
            try:
                parts.append(path.read_text())
                logger.debug("agent_rules: loaded %s", path)
            except Exception as exc:
                logger.warning("agent_rules: failed to load %s: %s", path, exc)

    return "\n\n---\n\n".join(parts) if parts else ""
