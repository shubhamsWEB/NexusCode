"""
Knowledge Graph builder.

Derives graph edges from existing indexed data (chunks, symbols) and writes
them to the kg_edges table.  All phases are async and read-only against the
main index tables; only kg_edges is mutated.

Edge types
----------
imports   : file  → file    (derived from chunks.imports[])
defines   : file  → symbol  (every symbol belongs to a file)
contains  : symbol → symbol  (class → method, from qualified_name pattern)
calls     : symbol → symbol  (AST-extracted calls, written by pipeline)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

if TYPE_CHECKING:
    from src.pipeline.parser import ParsedFile

logger = get_secure_logger(__name__)


# ── Import resolution helpers ──────────────────────────────────────────────────

_IMPORT_PATTERNS = [
    # Python: from X import Y  /  import X
    re.compile(r"^from\s+([\w./]+)\s+import"),
    re.compile(r"^import\s+([\w./]+)"),
    # TypeScript/JS: import ... from 'path'  or  require('path')
    re.compile(r"""import.*?from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    # Go: import "pkg/path"
    re.compile(r'import\s+"([^"]+)"'),
    # Rust: use crate::mod::thing
    re.compile(r"^use\s+([\w:]+)"),
]


def _extract_module(import_str: str) -> str | None:
    """Return a slash-separated module path from a raw import statement."""
    s = import_str.strip()
    for pat in _IMPORT_PATTERNS:
        m = pat.search(s)
        if m:
            raw = m.group(1)
            # Normalise: dots/colons → slashes, strip leading ./ or ../../
            raw = raw.replace("::", "/").replace(".", "/")
            raw = raw.lstrip("/")
            return raw or None
    return None


def _resolve_import(module_path: str, repo_files: set[str]) -> str | None:
    """Try suffix-matching a module path against known repo file paths."""
    if not module_path:
        return None
    clean = module_path.lstrip("./")
    if not clean:
        return None

    candidates = [
        clean,
        clean + ".py",
        clean + ".ts",
        clean + ".tsx",
        clean + ".js",
        clean + ".jsx",
        clean + ".go",
        clean + ".java",
        clean + ".rs",
        clean + "/__init__.py",
        clean + "/index.ts",
        clean + "/index.js",
        clean + "/mod.rs",
    ]
    for cand in candidates:
        for fp in repo_files:
            if fp == cand or fp.endswith("/" + cand):
                return fp
    return None


# ── Bulk insert helper ─────────────────────────────────────────────────────────


_INSERT_EDGE_SQL = text("""
    INSERT INTO kg_edges
        (source_id, source_type, target_id, target_type, edge_type,
         repo_owner, repo_name, confidence)
    VALUES (:src, :stype, :tgt, :ttype, :etype, :owner, :name, :conf)
    ON CONFLICT (source_id, target_id, edge_type, repo_owner, repo_name) DO NOTHING
""")

_COMMIT_EVERY = 200  # rows per transaction


async def _bulk_insert_edges(
    edges: list[dict[str, Any]],
    repo_owner: str,
    repo_name: str,
) -> int:
    """Insert edges into kg_edges, ignoring duplicates. Returns edges attempted."""
    if not edges:
        return 0

    async with AsyncSessionLocal() as session:
        for i, e in enumerate(edges):
            await session.execute(
                _INSERT_EDGE_SQL,
                {
                    "src": e["source_id"],
                    "stype": e["source_type"],
                    "tgt": e["target_id"],
                    "ttype": e["target_type"],
                    "etype": e["edge_type"],
                    "owner": repo_owner,
                    "name": repo_name,
                    "conf": e.get("confidence", 1.0),
                },
            )
            if (i + 1) % _COMMIT_EVERY == 0:
                await session.commit()
        await session.commit()

    return len(edges)


# ── Phase 1: IMPORTS edges (file → file) ─────────────────────────────────────


async def build_import_edges(repo_owner: str, repo_name: str) -> int:
    """Derive IMPORTS edges from chunks.imports[] for this repo."""
    # Collect all repo file paths for resolution
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT DISTINCT file_path
                    FROM chunks
                    WHERE repo_owner = :owner AND repo_name = :name AND is_deleted = FALSE
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).fetchall()

    repo_files: set[str] = {r[0] for r in rows}

    # Fetch all (file_path, import_str) pairs
    async with AsyncSessionLocal() as session:
        pairs = (
            await session.execute(
                text("""
                    SELECT DISTINCT file_path, imp
                    FROM chunks, unnest(imports) AS imp
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND is_deleted = FALSE
                      AND array_length(imports, 1) > 0
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).fetchall()

    edges: list[dict] = []
    seen: set[tuple] = set()
    for file_path, imp_str in pairs:
        module = _extract_module(imp_str)
        if not module:
            continue
        target = _resolve_import(module, repo_files)
        if not target or target == file_path:
            continue
        key = (file_path, target)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            {
                "source_id": file_path,
                "source_type": "file",
                "target_id": target,
                "target_type": "file",
                "edge_type": "imports",
                "confidence": 1.0,
            }
        )

    inserted = await _bulk_insert_edges(edges, repo_owner, repo_name)
    logger.debug("build_import_edges", extra={"repo": f"{repo_owner}/{repo_name}", "edges": inserted})
    return inserted


# ── Phase 2: DEFINES edges (file → symbol) ────────────────────────────────────


async def build_defines_edges(repo_owner: str, repo_name: str) -> int:
    """Derive DEFINES edges from the symbols table."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT qualified_name, file_path
                    FROM symbols
                    WHERE repo_owner = :owner AND repo_name = :name
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).fetchall()

    edges = [
        {
            "source_id": file_path,
            "source_type": "file",
            "target_id": qname,
            "target_type": "symbol",
            "edge_type": "defines",
            "confidence": 1.0,
        }
        for qname, file_path in rows
        if qname and file_path
    ]

    inserted = await _bulk_insert_edges(edges, repo_owner, repo_name)
    logger.debug("build_defines_edges", extra={"repo": f"{repo_owner}/{repo_name}", "edges": inserted})
    return inserted


# ── Phase 3: CONTAINS edges (class → method) ─────────────────────────────────


async def build_contains_edges(repo_owner: str, repo_name: str) -> int:
    """Derive CONTAINS edges from method qualified_names (ClassName.method)."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT qualified_name
                    FROM symbols
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND kind = 'method'
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).fetchall()

    edges: list[dict] = []
    for (qname,) in rows:
        if not qname or "." not in qname:
            continue
        parent = qname.rsplit(".", 1)[0]
        edges.append(
            {
                "source_id": parent,
                "source_type": "symbol",
                "target_id": qname,
                "target_type": "symbol",
                "edge_type": "contains",
                "confidence": 1.0,
            }
        )

    inserted = await _bulk_insert_edges(edges, repo_owner, repo_name)
    logger.debug("build_contains_edges", extra={"repo": f"{repo_owner}/{repo_name}", "edges": inserted})
    return inserted


# ── Phase 4: CALLS edges (symbol → symbol, from AST) ────────────────────────


async def build_calls_from_parsed(
    repo_owner: str,
    repo_name: str,
    parsed_files: list[ParsedFile],
) -> int:
    """
    Write CALLS edges for symbols in parsed_files.

    For each symbol with a non-empty `calls` list, look up matching target
    symbols in the same repo and insert CALLS edges.  Only deletes/replaces
    edges for the specific source symbols being processed (partial update).
    """
    if not parsed_files:
        return 0

    # Collect (qualified_name → calls[]) for all parsed symbols
    sym_calls: dict[str, list[str]] = {}
    for pf in parsed_files:
        for sym in pf.all_symbols:
            if sym.calls:
                sym_calls[sym.qualified_name] = sym.calls

    if not sym_calls:
        return 0

    # Fetch all known symbol names in the repo (for target resolution)
    async with AsyncSessionLocal() as session:
        known_rows = (
            await session.execute(
                text("""
                    SELECT name, qualified_name
                    FROM symbols
                    WHERE repo_owner = :owner AND repo_name = :name
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).fetchall()

    # Build name → set of qualified names (one short name may map to several qualified)
    name_to_qnames: dict[str, list[str]] = {}
    for short_name, qname in known_rows:
        name_to_qnames.setdefault(short_name, []).append(qname)

    # Delete existing CALLS edges for our source symbols (partial rebuild)
    source_names = list(sym_calls.keys())
    if source_names:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    DELETE FROM kg_edges
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND edge_type = 'calls'
                      AND source_id = ANY(:sources)
                """),
                {"owner": repo_owner, "name": repo_name, "sources": source_names},
            )
            await session.commit()

    # Build new CALLS edges
    edges: list[dict] = []
    seen: set[tuple] = set()
    for source_qname, callee_names in sym_calls.items():
        for callee in callee_names:
            targets = name_to_qnames.get(callee, [])
            for target_qname in targets:
                if target_qname == source_qname:
                    continue
                key = (source_qname, target_qname)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    {
                        "source_id": source_qname,
                        "source_type": "symbol",
                        "target_id": target_qname,
                        "target_type": "symbol",
                        "edge_type": "calls",
                        "confidence": 0.8,  # AST-based, not 100% precise
                    }
                )

    inserted = await _bulk_insert_edges(edges, repo_owner, repo_name)
    logger.debug("build_calls_from_parsed", extra={"repo": f"{repo_owner}/{repo_name}", "edges": inserted})
    return inserted


# ── build_file_graph: IMPORTS + DEFINES + CONTAINS ───────────────────────────


async def build_file_graph(repo_owner: str, repo_name: str) -> dict[str, int]:
    """
    Rebuild IMPORTS, DEFINES, and CONTAINS edges for a repo from current DB.
    Deletes only those edge types first, then re-inserts.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                DELETE FROM kg_edges
                WHERE repo_owner = :owner AND repo_name = :name
                  AND edge_type IN ('imports', 'defines', 'contains')
            """),
            {"owner": repo_owner, "name": repo_name},
        )
        await session.commit()

    imports = await build_import_edges(repo_owner, repo_name)
    defines = await build_defines_edges(repo_owner, repo_name)
    contains = await build_contains_edges(repo_owner, repo_name)

    return {"imports": imports, "defines": defines, "contains": contains}


# ── build_graph: full rebuild ─────────────────────────────────────────────────


async def build_graph(repo_owner: str, repo_name: str) -> dict[str, Any]:
    """
    Full knowledge-graph rebuild for a repo.

    Deletes ALL existing edges for the repo, then rebuilds IMPORTS, DEFINES,
    CONTAINS from DB data.  CALLS edges are rebuilt separately by the pipeline
    on each index run; this function only rebuilds them from existing symbols
    (best-effort, without original AST call data).
    """
    t_start = datetime.now(UTC)

    # Delete all existing edges for this repo
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM kg_edges WHERE repo_owner = :owner AND repo_name = :name"),
            {"owner": repo_owner, "name": repo_name},
        )
        await session.commit()

    # Rebuild structural edges
    stats = await build_file_graph(repo_owner, repo_name)

    # Best-effort CALLS: rebuild from symbols using caller heuristic
    calls_inserted = await _build_calls_from_db(repo_owner, repo_name)
    stats["calls"] = calls_inserted

    # Compute node/edge counts
    async with AsyncSessionLocal() as session:
        edge_count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM kg_edges "
                    "WHERE repo_owner = :owner AND repo_name = :name"
                ),
                {"owner": repo_owner, "name": repo_name},
            )
        ).scalar()
        node_count = (
            await session.execute(
                text("""
                    SELECT COUNT(DISTINCT id) FROM (
                        SELECT source_id AS id FROM kg_edges
                        WHERE repo_owner = :owner AND repo_name = :name
                        UNION
                        SELECT target_id FROM kg_edges
                        WHERE repo_owner = :owner AND repo_name = :name
                    ) sub
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).scalar()

    elapsed_ms = (datetime.now(UTC) - t_start).total_seconds() * 1000

    return {
        "nodes": node_count or 0,
        "edges": edge_count or 0,
        "built_at": t_start.isoformat(),
        "elapsed_ms": round(elapsed_ms, 1),
        "stats": stats,
    }


async def _build_calls_from_db(repo_owner: str, repo_name: str) -> int:
    """
    Heuristic CALLS edges from DB: use a single JOIN query to find which
    symbol chunks contain calls to other known symbols.
    Only symbols with names >= 5 chars are considered to reduce noise.
    """
    async with AsyncSessionLocal() as session:
        # One query: for each symbol, find chunk-symbols that call it
        # (chunk raw_content contains "name(")
        # This replaces the N-query-per-symbol loop.
        rows = (
            await session.execute(
                text("""
                    SELECT DISTINCT
                        c.symbol_name   AS caller,
                        s.qualified_name AS callee
                    FROM symbols s
                    JOIN chunks c
                      ON c.repo_owner = s.repo_owner
                     AND c.repo_name  = s.repo_name
                     AND c.file_path != s.file_path
                     AND c.is_deleted = FALSE
                     AND c.symbol_name IS NOT NULL
                     AND c.symbol_kind IN ('function', 'method')
                     AND c.raw_content ILIKE '%' || s.name || '(%'
                    WHERE s.repo_owner = :owner
                      AND s.repo_name  = :name
                      AND LENGTH(s.name) >= 5
                    LIMIT 500
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).fetchall()

    if not rows:
        return 0

    edges: list[dict] = []
    seen: set[tuple] = set()
    for caller_sym, target_qname in rows:
        if not caller_sym or not target_qname:
            continue
        key = (caller_sym, target_qname)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            {
                "source_id": caller_sym,
                "source_type": "symbol",
                "target_id": target_qname,
                "target_type": "symbol",
                "edge_type": "calls",
                "confidence": 0.5,
            }
        )

    return await _bulk_insert_edges(edges, repo_owner, repo_name)
