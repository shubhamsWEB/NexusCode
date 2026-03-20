"""
Module coverage enforcement for retrieval.

After the primary rerank (Phase 3), checks whether all key modules/directories
mentioned in the query are represented in the candidate set. If a key module
has no candidates, fires a targeted search to fill the gap.

This is a lightweight alternative to full semantic clustering: instead of
pre-computing K-means clusters, it uses the query itself and file paths of
retrieved chunks to detect structural blind spots in real time.

Algorithm:
  1. Extract "key modules" from the query (mentioned path fragments, package
     names, directories) and from the query analysis (mentioned_paths).
  2. Check which modules already have at least one candidate chunk.
  3. For each uncovered module, fire a targeted hybrid search.
  4. Return new candidates to be merged + reranked by the caller.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src.config import settings
from src.utils.logging import get_secure_logger
from src.utils.sanitize import sanitize_log

if TYPE_CHECKING:
    from src.retrieval.searcher import SearchResult

logger = get_secure_logger(__name__)

# Common file/path separators and typical module-name patterns
_PATH_FRAGMENT_RE = re.compile(
    r"(?:^|[\s'\"`(,])([a-zA-Z][a-zA-Z0-9_\-]*/[a-zA-Z0-9_\-/]+)"  # path/like/this
    r"|(?:^|[\s'\"`(,])([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*){1,4})"  # module.path.here
)

# High-signal directory names that map to "subsystems"
_SUBSYSTEM_DIRS = {
    "auth", "authentication", "authorization", "oauth",
    "payment", "billing", "checkout", "subscription",
    "api", "routes", "endpoints", "router",
    "db", "database", "storage", "models", "migrations",
    "cache", "redis", "queue", "worker",
    "email", "notification", "messaging",
    "config", "settings", "env",
    "utils", "helpers", "common", "shared",
    "logging", "monitoring", "metrics",
    "pipeline", "indexing", "retrieval",
    "llm", "ai", "ml", "embedding",
    "webhook", "events",
    "test", "tests",
    "ui", "frontend", "components",
}


def _extract_key_modules(query: str, mentioned_paths: list[str]) -> list[str]:
    """
    Extract candidate module/directory names from the query text.

    Returns a deduplicated list of path fragments or directory names to check.
    """
    modules: list[str] = []

    # Explicitly mentioned paths from query analysis
    for path in mentioned_paths:
        # Normalize: strip leading slash, take up to 2 path segments
        clean = path.lstrip("/")
        parts = clean.split("/")
        if parts:
            modules.append("/".join(parts[:2]))

    # Extract path-like fragments from query text
    for m in _PATH_FRAGMENT_RE.finditer(query):
        frag = m.group(1) or m.group(2)
        if frag:
            modules.append(frag.replace(".", "/"))

    # Extract high-signal directory names from query words
    words = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_\-]+\b", query.lower())
    for word in words:
        if word in _SUBSYSTEM_DIRS:
            modules.append(word)

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for m in modules:
        key = m.lower()
        if key not in seen:
            seen.add(key)
            result.append(m)

    return result


def _module_covered(module: str, candidates: list[SearchResult]) -> bool:
    """Return True if at least one candidate has a file path containing `module`."""
    mod_lower = module.lower()
    for c in candidates:
        if mod_lower in (c.file_path or "").lower():
            return True
    return False


async def enforce_module_coverage(
    query: str,
    mentioned_paths: list[str],
    candidates: list[SearchResult],
    repo_owner: str | None,
    repo_name: str | None,
    top_k_per_module: int = 10,
) -> list[SearchResult]:
    """
    Check module coverage and fire targeted searches for uncovered key modules.

    Args:
        query:           Original user query.
        mentioned_paths: Paths extracted by query analysis.
        candidates:      Current candidate set (post-rerank).
        repo_owner/name: Repo scope.
        top_k_per_module: Max chunks fetched per missing module.

    Returns:
        List of NEW SearchResult objects not already in candidates.
        Caller is responsible for merging + re-reranking.
    """
    if not settings.coverage_enforcement_enabled:
        return []

    key_modules = _extract_key_modules(query, mentioned_paths)
    if not key_modules:
        return []

    uncovered = [m for m in key_modules if not _module_covered(m, candidates)]
    if not uncovered:
        return []

    logger.debug(
        "coverage: %d key modules, %d uncovered: %s",
        len(key_modules),
        len(uncovered),
        sanitize_log(str(uncovered[:5])),
    )

    from src.retrieval.searcher import embed_query, search

    seen_ids = {c.chunk_id for c in candidates}
    new_results: list[SearchResult] = []

    for module in uncovered[:4]:  # cap at 4 targeted searches
        try:
            # Search for the module name specifically to pull in its chunks
            module_query = f"{module} {query[:80]}"
            vec = await embed_query(module_query)
            results = await search(
                query=module_query,
                query_vector=vec,
                top_k=top_k_per_module,
                mode="hybrid",
                repo_owner=repo_owner,
                repo_name=repo_name,
                search_quality="thorough",
            )
            added = 0
            for r in results:
                if r.chunk_id not in seen_ids and module.lower() in (r.file_path or "").lower():
                    seen_ids.add(r.chunk_id)
                    new_results.append(r)
                    added += 1
            if added:
                logger.info(
                    "coverage: module %r filled with %d new chunks", module, added
                )
        except Exception as exc:
            logger.debug(
                "coverage: search for module %r failed: %s", module, sanitize_log(exc)
            )

    return new_results
