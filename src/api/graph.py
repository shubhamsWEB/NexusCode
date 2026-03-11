"""
Knowledge Graph API

GET  /graph/{owner}/{name}
     ?view=files|symbols|all|semantic  (default: files)
     ?max_nodes=200
     Returns nodes, edges, stats, built_at

POST /graph/{owner}/{name}/build
     Rebuilds the full graph for the repo. Returns stats.

GET  /graph/{owner}/{name}/semantic
     Paginated list of semantic edges with relationship + reasoning.

POST /graph/{owner}/{name}/enrich
     Manually trigger semantic enrichment. Returns {edges_inserted, symbols_processed, elapsed_ms}.
"""

from __future__ import annotations

import time
from typing import Literal

import anyio
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

router = APIRouter(prefix="/graph", tags=["graph"])
logger = get_secure_logger(__name__)

# Edge types by view
_VIEW_EDGE_TYPES: dict[str, list[str]] = {
    "files": ["imports"],
    "symbols": ["defines", "contains", "calls"],
    "all": ["imports", "defines", "contains", "calls", "semantic"],
    "semantic": ["semantic"],
}

# Language → color mapping for file nodes
_LANG_COLORS: dict[str, str] = {
    "python": "#3572A5",
    "typescript": "#2B7489",
    "tsx": "#2B7489",
    "javascript": "#F1E05A",
    "java": "#B07219",
    "go": "#00ADD8",
    "rust": "#DEA584",
    "ruby": "#701516",
    "cpp": "#F34B7D",
    "c": "#555555",
}
_LANG_COLOR_DEFAULT = "#888888"

# Symbol kind → color
_KIND_COLORS: dict[str, str] = {
    "class": "#4CAF50",
    "function": "#2196F3",
    "method": "#9C27B0",
}
_KIND_COLOR_DEFAULT = "#888888"

# Edge type → color
_EDGE_COLORS: dict[str, str] = {
    "imports": "#FF6B6B",
    "defines": "#4ECDC4",
    "contains": "#45B7D1",
    "calls": "#FFA07A",
}


async def _get_graph_data(
    owner: str,
    name: str,
    edge_types: list[str],
    max_nodes: int,
) -> dict:
    """Query kg_edges and enrich node metadata from chunks + symbols tables."""
    async with AsyncSessionLocal() as session:
        # Fetch edges (filtered by type)
        edge_rows = (
            await session.execute(
                text("""
                    SELECT source_id, source_type, target_id, target_type, edge_type, confidence, extra
                    FROM kg_edges
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND edge_type = ANY(:types)
                    ORDER BY edge_type, source_id
                """),
                {"owner": owner, "name": name, "types": edge_types},
            )
        ).fetchall()

    if not edge_rows:
        return {"nodes": [], "edges": [], "stats": {"node_count": 0, "edge_count": 0}, "built_at": None}

    # Collect unique node IDs + types
    node_ids: dict[str, str] = {}  # id → type
    for src_id, src_type, tgt_id, tgt_type, *_ in edge_rows:
        node_ids[src_id] = src_type
        node_ids[tgt_id] = tgt_type

    # Degree map for node sizing
    degree: dict[str, int] = {}
    for src_id, _, tgt_id, *_ in edge_rows:
        degree[src_id] = degree.get(src_id, 0) + 1
        degree[tgt_id] = degree.get(tgt_id, 0) + 1

    # If over max_nodes, keep the highest-degree nodes
    if len(node_ids) > max_nodes:
        top_nodes = set(
            sorted(node_ids.keys(), key=lambda x: degree.get(x, 0), reverse=True)[:max_nodes]
        )
        node_ids = {k: v for k, v in node_ids.items() if k in top_nodes}
        edge_rows = [
            r for r in edge_rows if r[0] in top_nodes and r[2] in top_nodes
        ]

    # Separate file and symbol node IDs
    file_ids = [nid for nid, ntype in node_ids.items() if ntype == "file"]
    symbol_ids = [nid for nid, ntype in node_ids.items() if ntype == "symbol"]

    # Fetch file metadata (language) from chunks
    file_meta: dict[str, dict] = {}
    if file_ids:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    text("""
                        SELECT DISTINCT file_path, language
                        FROM chunks
                        WHERE repo_owner = :owner AND repo_name = :name
                          AND file_path = ANY(:files)
                          AND is_deleted = FALSE
                    """),
                    {"owner": owner, "name": name, "files": file_ids},
                )
            ).fetchall()
        for fp, lang in rows:
            file_meta[fp] = {"language": lang or "unknown"}

    # Fetch symbol metadata (kind) from symbols
    sym_meta: dict[str, dict] = {}
    if symbol_ids:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    text("""
                        SELECT qualified_name, kind, file_path
                        FROM symbols
                        WHERE repo_owner = :owner AND repo_name = :name
                          AND qualified_name = ANY(:syms)
                    """),
                    {"owner": owner, "name": name, "syms": symbol_ids},
                )
            ).fetchall()
        for qname, kind, fp in rows:
            sym_meta[qname] = {"kind": kind or "function", "file_path": fp or ""}

    # Fetch built_at from most recent edge
    async with AsyncSessionLocal() as session:
        built_at = (
            await session.execute(
                text("""
                    SELECT MAX(indexed_at) FROM kg_edges
                    WHERE repo_owner = :owner AND repo_name = :name
                """),
                {"owner": owner, "name": name},
            )
        ).scalar()

    # Build node objects
    nodes = []
    for node_id, node_type in node_ids.items():
        deg = degree.get(node_id, 1)
        size = max(10, min(40, 10 + deg * 3))

        if node_type == "file":
            meta = file_meta.get(node_id, {})
            lang = meta.get("language", "unknown")
            color = _LANG_COLORS.get(lang, _LANG_COLOR_DEFAULT)
            label = node_id.split("/")[-1]  # basename
            nodes.append(
                {
                    "id": node_id,
                    "label": label,
                    "type": "file",
                    "language": lang,
                    "color": color,
                    "size": size,
                    "title": node_id,
                }
            )
        else:  # symbol
            meta = sym_meta.get(node_id, {})
            kind = meta.get("kind", "function")
            color = _KIND_COLORS.get(kind, _KIND_COLOR_DEFAULT)
            label = node_id.split(".")[-1]  # short name
            nodes.append(
                {
                    "id": node_id,
                    "label": label,
                    "type": "symbol",
                    "kind": kind,
                    "file_path": meta.get("file_path", ""),
                    "color": color,
                    "size": size,
                    "title": node_id,
                }
            )

    # Build edge objects (semantic edges include extra metadata)
    edges = []
    for r in edge_rows:
        edge: dict = {
            "source": r[0],
            "target": r[2],
            "type": r[4],
            "confidence": float(r[5]) if r[5] else 1.0,
        }
        if r[4] == "semantic" and r[6]:
            extra = r[6] if isinstance(r[6], dict) else {}
            edge["relationship"] = extra.get("relationship", "")
            edge["reasoning"] = extra.get("reasoning", "")
        edges.append(edge)

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {"node_count": len(nodes), "edge_count": len(edges)},
        "built_at": built_at.isoformat() if built_at else None,
    }


@router.get("/{owner}/{name}")
async def get_graph(
    owner: str,
    name: str,
    view: Literal["files", "symbols", "all", "semantic"] = Query("files"),
    max_nodes: int = Query(200, ge=10, le=1000),
) -> JSONResponse:
    """Return knowledge graph data for a repo."""
    edge_types = _VIEW_EDGE_TYPES.get(view, ["imports"])
    data = await _get_graph_data(owner, name, edge_types, max_nodes)
    return JSONResponse(data)


@router.post("/{owner}/{name}/build")
async def build_graph_endpoint(owner: str, name: str) -> JSONResponse:
    """Rebuild the full knowledge graph for a repo (synchronous, 30s timeout)."""
    from src.graph.builder import build_graph

    # Verify repo exists
    async with AsyncSessionLocal() as session:
        exists = (
            await session.execute(
                text("SELECT 1 FROM repos WHERE owner = :owner AND name = :name"),
                {"owner": owner, "name": name},
            )
        ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{name} not found")

    try:
        with anyio.fail_after(30):
            result = await build_graph(owner, name)
    except TimeoutError as exc:
        logger.warning("build_graph timed out", extra={"repo": f"{owner}/{name}"})
        raise HTTPException(status_code=504, detail="Graph build timed out after 30s") from exc
    except Exception as exc:
        logger.error("build_graph failed", extra={"repo": f"{owner}/{name}", "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Graph build failed: {exc}") from exc

    return JSONResponse(result)


@router.get("/{owner}/{name}/semantic")
async def get_semantic_edges(
    owner: str,
    name: str,
    limit: int = Query(50, ge=0, le=200),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    """Return a paginated list of semantic edges with relationship + reasoning."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT source_id, target_id, confidence,
                           extra->>'relationship' AS relationship,
                           extra->>'reasoning'    AS reasoning,
                           indexed_at
                    FROM kg_edges
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND edge_type = 'semantic'
                    ORDER BY confidence DESC, indexed_at DESC
                    LIMIT :limit OFFSET :offset
                """),
                {"owner": owner, "name": name, "limit": limit, "offset": offset},
            )
        ).fetchall()

        total = (
            await session.execute(
                text("""
                    SELECT COUNT(*) FROM kg_edges
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND edge_type = 'semantic'
                """),
                {"owner": owner, "name": name},
            )
        ).scalar()

    edges = [
        {
            "source": r[0],
            "target": r[1],
            "confidence": float(r[2]) if r[2] else 1.0,
            "relationship": r[3] or "",
            "reasoning": r[4] or "",
            "indexed_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]
    return JSONResponse({"edges": edges, "total": int(total or 0), "offset": offset, "limit": limit})


@router.post("/{owner}/{name}/enrich")
async def enrich_semantic_graph(owner: str, name: str) -> JSONResponse:
    """Manually trigger semantic enrichment for a repo."""
    from src.graph.semantic_enricher import enrich_repo_semantic_graph

    # Verify repo exists
    async with AsyncSessionLocal() as session:
        exists = (
            await session.execute(
                text("SELECT 1 FROM repos WHERE owner = :owner AND name = :name"),
                {"owner": owner, "name": name},
            )
        ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{name} not found")

    t_start = time.monotonic()
    try:
        edges_inserted, symbols_processed = await enrich_repo_semantic_graph(owner, name)
    except Exception as exc:
        logger.error("enrich_semantic_graph failed", extra={"repo": f"{owner}/{name}", "error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Enrichment failed: {exc}") from exc

    elapsed_ms = round((time.monotonic() - t_start) * 1000, 1)
    return JSONResponse({
        "edges_inserted": edges_inserted,
        "symbols_processed": symbols_processed,
        "elapsed_ms": elapsed_ms,
    })
