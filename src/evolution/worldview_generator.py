"""
Pillar 2 — The Memory: Worldview generator.

Builds and stores a per-repo semantic understanding (worldview) by:
1. Sampling representative code chunks and symbols
2. Fetching repo_summaries metadata
3. Calling an LLM with a structured JSON output prompt
4. Persisting the result in repo_worldviews (auto-incremented version)

The worldview is injected into Ask and Plan system prompts, making every
response progressively smarter as the system learns more about each repo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from src.config import settings
from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_WORLDVIEW_SYSTEM = """\
You are analyzing a software repository to build a compact semantic understanding
of its architecture, patterns, and conventions. You will receive:

1. Representative code chunks (classes, functions, key entry points)
2. Repository metadata (tech stack, language distribution)
3. Recent interaction history (what developers asked, where retrieval struggled)

Your task: produce a structured JSON document that captures the codebase worldview.
Be concise and actionable — every observation should help an AI agent better
answer questions or plan changes to this repo.

Respond with ONLY valid JSON, no preamble:
{
  "architecture_summary": "2-3 paragraph plain-English description of what this codebase does and how it is structured",
  "key_patterns": ["list of 3-7 dominant design patterns, e.g. 'event-driven', 'factory pattern', 'CQRS'"],
  "difficult_zones": ["list of 2-5 areas where questions or searches tend to struggle, e.g. 'async error propagation', 'auth token refresh flow'"],
  "conventions": ["list of 3-6 notable coding conventions or style rules visible in this codebase"],
  "recent_changes": "1 paragraph describing the most recent indexed changes and their likely impact",
  "full_worldview": "a 5-8 paragraph narrative document that an AI agent could read to orient itself quickly, covering: purpose, architecture, key entry points, data flow, notable constraints"
}
"""


@dataclass
class WorldviewDoc:
    repo_owner: str
    repo_name: str
    version: int
    architecture_summary: str
    key_patterns: list[str]
    difficult_zones: list[str]
    conventions: list[str]
    recent_changes: str
    full_worldview: str
    chunks_sampled: int
    interactions_analyzed: int
    model_used: str
    generated_at: datetime


async def _get_next_version(repo_owner: str, repo_name: str) -> int:
    """Return version number to use for a new worldview (max + 1 or 1)."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT MAX(version) AS max_v FROM repo_worldviews
                    WHERE repo_owner = :owner AND repo_name = :name
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).mappings().first()
    return int(row["max_v"] or 0) + 1


async def _sample_chunks(repo_owner: str, repo_name: str, n: int = 50) -> str:
    """
    Sample representative chunks for worldview generation.
    Prioritises: classes > functions > entry-point files > other.
    Returns a formatted string ready to embed in the prompt.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT file_path, symbol_name, symbol_kind, scope_chain,
                           raw_content, language
                    FROM chunks
                    WHERE repo_owner = :owner AND repo_name = :name
                      AND is_deleted = FALSE
                    ORDER BY
                        CASE symbol_kind
                            WHEN 'class'    THEN 1
                            WHEN 'function' THEN 2
                            WHEN 'method'   THEN 3
                            ELSE 4
                        END,
                        token_count DESC NULLS LAST
                    LIMIT :n
                """),
                {"owner": repo_owner, "name": repo_name, "n": n},
            )
        ).mappings().all()

    if not rows:
        return "(No indexed code chunks found.)"

    parts = []
    for r in rows:
        header = f"### {r['file_path']}"
        if r["symbol_name"]:
            header += f" — {r['symbol_kind'] or 'symbol'}: {r['symbol_name']}"
        if r["scope_chain"]:
            header += f" (in {r['scope_chain']})"
        content = (r["raw_content"] or "")[:600]  # cap per chunk
        parts.append(f"{header}\n```{r['language'] or ''}\n{content}\n```")

    return "\n\n".join(parts)


async def _fetch_repo_metadata(repo_owner: str, repo_name: str) -> str:
    """Fetch repo_summaries metadata as a formatted string."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT tech_stack_keywords, language_distribution, chunk_count
                    FROM repo_summaries
                    WHERE repo_owner = :owner AND repo_name = :name
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).mappings().first()

    if not row:
        return "(No repo summary metadata available.)"

    lines = [f"Chunk count: {row['chunk_count']}"]
    if row["tech_stack_keywords"]:
        lines.append(f"Tech stack keywords: {', '.join(row['tech_stack_keywords'][:20])}")
    if row["language_distribution"]:
        top_langs = sorted(
            row["language_distribution"].items(), key=lambda x: -x[1]
        )[:5]
        lines.append(f"Languages: {', '.join(f'{l} ({p:.0%})' for l, p in top_langs)}")

    return "\n".join(lines)


async def _fetch_recent_interactions(repo_owner: str, repo_name: str, n: int = 30) -> tuple[str, int]:
    """Return a summary of recent interactions and the count."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT query, implicit_quality_score, query_complexity
                    FROM interaction_metrics
                    WHERE repo_owner = :owner AND repo_name = :name
                    ORDER BY created_at DESC
                    LIMIT :n
                """),
                {"owner": repo_owner, "name": repo_name, "n": n},
            )
        ).mappings().all()

    if not rows:
        return "(No interaction history yet.)", 0

    weak = [r for r in rows if r["implicit_quality_score"] is not None and r["implicit_quality_score"] < 0.6]
    lines = [f"Recent queries sampled: {len(rows)}"]
    if weak:
        lines.append(f"Low-quality retrievals ({len(weak)}):")
        for r in weak[:5]:
            lines.append(f"  - [{r['query_complexity']}] {r['query'][:120]}")
    return "\n".join(lines), len(rows)


async def _call_llm_for_worldview(prompt: str, model: str) -> dict:
    """Call the LLM and parse the JSON worldview response."""
    from src.llm.client import get_client

    client = get_client()
    response = await client.messages.create(
        model=model,
        max_tokens=4096,
        system=_WORLDVIEW_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]

    return json.loads(raw)


async def generate_worldview(repo_owner: str, repo_name: str) -> WorldviewDoc | None:
    """
    Generate and persist a new worldview for a repository.

    Returns the WorldviewDoc on success, or None if the repo has no chunks.
    """
    logger.info("Generating worldview for %s/%s", repo_owner, repo_name)

    chunks_text = await _sample_chunks(repo_owner, repo_name)
    if chunks_text.startswith("(No indexed"):
        logger.info("No chunks found for %s/%s, skipping worldview", repo_owner, repo_name)
        return None

    metadata = await _fetch_repo_metadata(repo_owner, repo_name)
    interactions_text, interaction_count = await _fetch_recent_interactions(repo_owner, repo_name)
    version = await _get_next_version(repo_owner, repo_name)
    model = settings.default_model

    prompt = (
        f"## Repository: {repo_owner}/{repo_name}\n\n"
        f"## Metadata\n{metadata}\n\n"
        f"## Recent Developer Interactions\n{interactions_text}\n\n"
        f"## Representative Code Chunks\n\n{chunks_text}"
    )

    try:
        wv_data = await _call_llm_for_worldview(prompt, model)
    except Exception:
        logger.exception("LLM worldview generation failed for %s/%s", repo_owner, repo_name)
        return None

    doc = WorldviewDoc(
        repo_owner=repo_owner,
        repo_name=repo_name,
        version=version,
        architecture_summary=wv_data.get("architecture_summary", ""),
        key_patterns=wv_data.get("key_patterns", []),
        difficult_zones=wv_data.get("difficult_zones", []),
        conventions=wv_data.get("conventions", []),
        recent_changes=wv_data.get("recent_changes", ""),
        full_worldview=wv_data.get("full_worldview", ""),
        chunks_sampled=len(chunks_text.split("###")) - 1,
        interactions_analyzed=interaction_count,
        model_used=model,
        generated_at=datetime.now(timezone.utc),
    )

    await _persist_worldview(doc)
    logger.info(
        "Worldview v%d generated for %s/%s (%d patterns, %d difficult zones)",
        version,
        repo_owner,
        repo_name,
        len(doc.key_patterns),
        len(doc.difficult_zones),
    )
    return doc


async def _persist_worldview(doc: WorldviewDoc) -> None:
    """Insert a worldview row into repo_worldviews."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO repo_worldviews (
                    repo_owner, repo_name, version,
                    architecture_summary, key_patterns, difficult_zones,
                    conventions, recent_changes, full_worldview,
                    chunks_sampled, interactions_analyzed, model_used, generated_at
                ) VALUES (
                    :owner, :name, :version,
                    :arch, :patterns, :difficult,
                    :conventions, :recent, :full,
                    :chunks, :interactions, :model, :generated
                )
                ON CONFLICT (repo_owner, repo_name, version) DO UPDATE
                    SET full_worldview = EXCLUDED.full_worldview,
                        architecture_summary = EXCLUDED.architecture_summary,
                        key_patterns = EXCLUDED.key_patterns,
                        difficult_zones = EXCLUDED.difficult_zones,
                        conventions = EXCLUDED.conventions,
                        recent_changes = EXCLUDED.recent_changes,
                        generated_at = EXCLUDED.generated_at
            """),
            {
                "owner": doc.repo_owner,
                "name": doc.repo_name,
                "version": doc.version,
                "arch": doc.architecture_summary,
                "patterns": doc.key_patterns,
                "difficult": doc.difficult_zones,
                "conventions": doc.conventions,
                "recent": doc.recent_changes,
                "full": doc.full_worldview,
                "chunks": doc.chunks_sampled,
                "interactions": doc.interactions_analyzed,
                "model": doc.model_used,
                "generated": doc.generated_at,
            },
        )
        await session.commit()


async def get_latest_worldview_text(repo_owner: str, repo_name: str) -> str:
    """
    Return the full_worldview text for the latest version, or "" if none exists.
    Used to inject into system prompts (graceful degradation on miss).
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text("""
                    SELECT full_worldview FROM repo_worldviews
                    WHERE repo_owner = :owner AND repo_name = :name
                    ORDER BY version DESC LIMIT 1
                """),
                {"owner": repo_owner, "name": repo_name},
            )
        ).mappings().first()

    return (row["full_worldview"] or "") if row else ""
