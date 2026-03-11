"""
Semantic Graph Enricher

Uses Claude Haiku to extract semantic architectural relationships between
symbols (e.g. "AuthService validates JWTToken") and stores them as
edge_type='semantic' in kg_edges.

Public API
----------
  enrich_repo_semantic_graph(owner, repo) → (edges_inserted, symbols_processed)
  get_semantic_context_for_symbols(symbols, owner, repo, concept, token_budget) → str
  get_enrichment_status(owner, repo) → dict
"""

from __future__ import annotations

import json

from pydantic import BaseModel
from sqlalchemy import text

from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_CONFIDENCE_THRESHOLD = 0.7
_BATCH_SIZE = 25
_MAX_SYMBOLS = 80


# ── Pydantic models ────────────────────────────────────────────────────────────


class SemanticRelation(BaseModel):
    source: str       # symbol qualified_name
    target: str       # symbol qualified_name
    relationship: str  # "validates", "delegates_to", "coordinates", etc.
    confidence: float  # 0.7–1.0
    reasoning: str    # one sentence


class SemanticRelationList(BaseModel):
    relations: list[SemanticRelation]


# ── Tool schema for forced tool call ──────────────────────────────────────────

_EXTRACT_RELATIONS_TOOL = {
    "name": "extract_semantic_relations",
    "description": "Extract semantic architectural relationships between symbols.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                        "relationship": {"type": "string"},
                        "confidence": {"type": "number"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["source", "target", "relationship", "confidence", "reasoning"],
                },
            }
        },
        "required": ["relations"],
    },
}


# ── Enrichment ────────────────────────────────────────────────────────────────


async def enrich_repo_semantic_graph(
    owner: str,
    repo_name: str,
) -> tuple[int, int]:
    """
    Run LLM-based semantic enrichment for a repo.

    1. Fetch top-50 symbols by call in-degree + all exported symbols (cap 80).
    2. Pull qualified_name, signature, docstring[:150] for each.
    3. Batch into groups of 25; per batch call claude-haiku with forced tool schema.
    4. Insert results as edge_type='semantic' into kg_edges.

    Returns (edges_inserted, symbols_processed).
    """
    from src.llm.client import get_client

    symbols = await _fetch_candidate_symbols(owner, repo_name)
    if not symbols:
        logger.info("semantic_enricher: no symbols found", extra={"repo": f"{owner}/{repo_name}"})
        return 0, 0

    client = get_client()
    total_edges = 0
    symbols_processed = len(symbols)

    for batch_start in range(0, len(symbols), _BATCH_SIZE):
        batch = symbols[batch_start : batch_start + _BATCH_SIZE]
        relations = await _extract_relations_for_batch(client, owner, repo_name, batch)
        if relations:
            edges = _relations_to_edges(relations, owner, repo_name)
            inserted = await _insert_semantic_edges(edges, owner, repo_name)
            total_edges += inserted

    logger.info(
        "semantic_enricher: enrichment complete",
        extra={
            "repo": f"{owner}/{repo_name}",
            "edges_inserted": total_edges,
            "symbols_processed": symbols_processed,
        },
    )
    return total_edges, symbols_processed


async def _fetch_candidate_symbols(owner: str, repo_name: str) -> list[dict]:
    """Fetch top in-degree symbols + exported symbols, capped at _MAX_SYMBOLS."""
    async with AsyncSessionLocal() as session:
        # Top 50 by call in-degree
        indegree_rows = (
            await session.execute(
                text("""
                    SELECT s.qualified_name, s.signature, s.docstring
                    FROM symbols s
                    JOIN kg_edges e ON e.target_id = s.qualified_name
                        AND e.repo_owner = s.repo_owner
                        AND e.repo_name = s.repo_name
                        AND e.edge_type = 'calls'
                    WHERE s.repo_owner = :owner AND s.repo_name = :name
                      AND s.qualified_name IS NOT NULL
                    GROUP BY s.qualified_name, s.signature, s.docstring
                    ORDER BY COUNT(*) DESC
                    LIMIT 50
                """),
                {"owner": owner, "name": repo_name},
            )
        ).fetchall()

        # All exported symbols
        exported_rows = (
            await session.execute(
                text("""
                    SELECT qualified_name, signature, docstring
                    FROM symbols
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND is_exported = TRUE
                      AND qualified_name IS NOT NULL
                    LIMIT 60
                """),
                {"owner": owner, "name": repo_name},
            )
        ).fetchall()

    seen: set[str] = set()
    result: list[dict] = []
    for rows in (indegree_rows, exported_rows):
        for qname, sig, doc in rows:
            if qname not in seen:
                seen.add(qname)
                result.append({
                    "qualified_name": qname,
                    "signature": sig or "",
                    "docstring": (doc or "")[:150],
                })
            if len(result) >= _MAX_SYMBOLS:
                break
        if len(result) >= _MAX_SYMBOLS:
            break

    return result


async def _extract_relations_for_batch(
    client,
    owner: str,
    repo_name: str,
    batch: list[dict],
) -> list[SemanticRelation]:
    """Call Claude Haiku to extract semantic relations for a batch of symbols."""
    symbol_lines = "\n".join(
        f"{s['qualified_name']}: {s['signature']} — {s['docstring']}"
        for s in batch
    )
    prompt = (
        f"Analyze these {owner}/{repo_name} symbols and extract semantic architectural "
        f"relationships.\n\n"
        f"{symbol_lines}\n\n"
        f"Return only relationships that reveal ARCHITECTURE (not trivial call relationships):\n"
        f"  validates, delegates_to, coordinates, produces_data_for, implements, part_of\n"
        f"Confidence: 0.9+ obvious, 0.7–0.9 probable. Skip below {_CONFIDENCE_THRESHOLD}.\n"
        f"Skip: getters/setters, test utilities, internal implementation helpers."
    )

    try:
        response = await client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=2048,
            tools=[_EXTRACT_RELATIONS_TOOL],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.warning(
            "semantic_enricher: haiku call failed",
            extra={"error": str(exc)},
        )
        return []

    relations: list[SemanticRelation] = []
    for block in response.content:
        if block.type == "tool_use" and block.name == "extract_semantic_relations":
            raw_relations = (block.input or {}).get("relations", [])
            for r in raw_relations:
                try:
                    rel = SemanticRelation(**r)
                    if rel.confidence >= _CONFIDENCE_THRESHOLD:
                        relations.append(rel)
                except Exception:
                    pass

    return relations


def _relations_to_edges(
    relations: list[SemanticRelation],
    owner: str,
    repo_name: str,
) -> list[dict]:
    """Convert SemanticRelation objects into kg_edges-compatible dicts."""
    edges = []
    seen: set[tuple] = set()
    for rel in relations:
        key = (rel.source, rel.target, rel.relationship)
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "source_id": rel.source,
            "source_type": "symbol",
            "target_id": rel.target,
            "target_type": "symbol",
            "edge_type": "semantic",
            "confidence": rel.confidence,
            "extra": json.dumps({
                "relationship": rel.relationship,
                "reasoning": rel.reasoning,
            }),
        })
    return edges


_INSERT_SEMANTIC_EDGE_SQL = text("""
    INSERT INTO kg_edges
        (source_id, source_type, target_id, target_type, edge_type,
         repo_owner, repo_name, confidence, extra)
    VALUES (:src, :stype, :tgt, :ttype, :etype, :owner, :name, :conf, CAST(:extra AS JSONB))
    ON CONFLICT (source_id, target_id, edge_type, repo_owner, repo_name) DO NOTHING
""")

_COMMIT_EVERY = 200


async def _insert_semantic_edges(
    edges: list[dict],
    owner: str,
    repo_name: str,
) -> int:
    """Insert semantic edges, ignoring duplicates. Returns rows attempted."""
    if not edges:
        return 0

    async with AsyncSessionLocal() as session:
        for i, e in enumerate(edges):
            await session.execute(
                _INSERT_SEMANTIC_EDGE_SQL,
                {
                    "src": e["source_id"],
                    "stype": e["source_type"],
                    "tgt": e["target_id"],
                    "ttype": e["target_type"],
                    "etype": e["edge_type"],
                    "owner": owner,
                    "name": repo_name,
                    "conf": e.get("confidence", 1.0),
                    "extra": e.get("extra", "{}"),
                },
            )
            if (i + 1) % _COMMIT_EVERY == 0:
                await session.commit()
        await session.commit()

    return len(edges)


# ── Context retrieval ─────────────────────────────────────────────────────────


async def get_semantic_context_for_symbols(
    symbols: list[str],
    owner: str,
    repo_name: str,
    concept: str | None = None,
    token_budget: int = 2000,
) -> str:
    """
    Retrieve semantic edges for a list of symbols and format as markdown.

    Optionally filter by concept (matched against relationship field).
    Returns empty string if no semantic data is available.
    """
    if not symbols:
        return ""

    params: dict = {
        "owner": owner,
        "name": repo_name,
        "symbols": symbols,
    }
    concept_filter = ""
    if concept:
        params["concept"] = f"%{concept}%"
        concept_filter = " AND extra->>'relationship' ILIKE :concept"

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(f"""
                    SELECT
                        source_id, target_id, confidence,
                        extra->>'relationship' AS relationship,
                        extra->>'reasoning'    AS reasoning
                    FROM kg_edges
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND edge_type = 'semantic'
                      AND (source_id = ANY(:symbols) OR target_id = ANY(:symbols))
                      {concept_filter}
                    ORDER BY confidence DESC
                """),
                params,
            )
        ).fetchall()

    if not rows:
        return ""

    lines = ["## Semantic Architecture Context"]
    tokens_used = len(lines[0])

    for source_id, target_id, confidence, relationship, reasoning in rows:
        line = f"{source_id} —[{relationship}]→ {target_id}  (confidence: {confidence:.2f})"
        detail = f'  "{reasoning}"' if reasoning else ""
        entry = f"{line}\n{detail}" if detail else line
        entry_tokens = len(entry.split())
        if tokens_used + entry_tokens > token_budget:
            break
        lines.append(entry)
        tokens_used += entry_tokens

    return "\n".join(lines)


# ── Status ─────────────────────────────────────────────────────────────────────


async def get_enrichment_status(owner: str, repo_name: str) -> dict:
    """Return enrichment status: edge count, symbols covered, last enriched."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT
                        COUNT(*)         AS edges_count,
                        COUNT(DISTINCT source_id) + COUNT(DISTINCT target_id) AS symbols_raw,
                        MAX(indexed_at)  AS last_enriched_at
                    FROM kg_edges
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND edge_type = 'semantic'
                """),
                {"owner": owner, "name": repo_name},
            )
        ).mappings().first()

    if not row:
        return {"edges_count": 0, "symbols_covered": 0, "last_enriched_at": None}

    return {
        "edges_count": int(row["edges_count"] or 0),
        "symbols_covered": int(row["symbols_raw"] or 0),
        "last_enriched_at": row["last_enriched_at"].isoformat() if row["last_enriched_at"] else None,
    }
