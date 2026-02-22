"""
Token-budget-aware context assembler.

Takes a ranked list of SearchResults and assembles them into a
single, formatted context string ready to inject into an LLM prompt.

Respects a token budget — stops adding chunks once the budget is full.
Deduplicates by chunk_id so the same chunk never appears twice.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from src.pipeline.chunker import count_tokens

if TYPE_CHECKING:
    from src.retrieval.searcher import SearchResult

logger = logging.getLogger(__name__)

_PATH_SEGMENTS = 3


@dataclass
class AssembledContext:
    context_text: str              # formatted, ready-to-inject string
    chunks_used: list[dict]        # metadata for each included chunk
    tokens_used: int
    retrieval_log: str             # human-readable summary of what was retrieved


def assemble(
    results: list["SearchResult"],
    token_budget: int = 8000,
    query: Optional[str] = None,
) -> AssembledContext:
    """
    Greedily fill the token budget with the highest-ranked chunks.

    Format of each chunk in the output:
    ─────────────────────────────────────
    File: src/auth/service.py  [lines 42-55]  (typescript)
    Scope: AuthService > validate_token
    Score: 0.847

    <raw source code>
    ─────────────────────────────────────
    """
    seen_ids: set[str] = set()
    sections: list[str] = []
    chunks_used: list[dict] = []
    tokens_used = 0

    for result in results:
        if result.chunk_id in seen_ids:
            continue

        section = _format_chunk(result)
        section_tokens = count_tokens(section)

        if tokens_used + section_tokens > token_budget:
            # Try to keep going — maybe a later chunk is shorter
            continue

        seen_ids.add(result.chunk_id)
        sections.append(section)
        tokens_used += section_tokens
        chunks_used.append({
            "file": result.file_path,
            "lines": f"{result.start_line}-{result.end_line}",
            "symbol": result.symbol_name,
            "score": round(result.rerank_score or result.score, 4),
            "tokens": section_tokens,
        })

    context_text = "\n".join(sections)

    retrieval_log = _build_log(query, chunks_used, tokens_used, token_budget)
    logger.info(
        "assembler: %d/%d chunks fit in %d/%d token budget",
        len(chunks_used), len(results), tokens_used, token_budget,
    )

    return AssembledContext(
        context_text=context_text,
        chunks_used=chunks_used,
        tokens_used=tokens_used,
        retrieval_log=retrieval_log,
    )


# ── Formatting helpers ────────────────────────────────────────────────────────

def _format_chunk(result: "SearchResult") -> str:
    short_path = _short_path(result.file_path)
    header_parts = [
        f"File: {short_path}  [lines {result.start_line}-{result.end_line}]"
        f"  ({result.language})"
    ]
    if result.scope_chain:
        header_parts.append(f"Scope: {result.scope_chain}")

    score = result.rerank_score or result.score
    header_parts.append(f"Score: {score:.4f}")

    repo = f"{result.repo_owner}/{result.repo_name}"
    commit = result.commit_sha[:7] if result.commit_sha else ""
    if result.commit_author or commit:
        parts = []
        if result.commit_author:
            parts.append(result.commit_author)
        if commit:
            parts.append(f"@ {commit}")
        header_parts.append(f"Last changed: {' '.join(parts)}")

    header = "\n".join(header_parts)
    divider = "─" * 60

    return f"{divider}\n{header}\n\n{result.raw_content}\n"


def _short_path(file_path: str) -> str:
    parts = Path(file_path).parts
    return "/".join(parts[-_PATH_SEGMENTS:]) if len(parts) >= _PATH_SEGMENTS else file_path


def _build_log(
    query: Optional[str],
    chunks_used: list[dict],
    tokens_used: int,
    token_budget: int,
) -> str:
    lines = []
    if query:
        lines.append(f"Query: {query!r}")
    lines.append(f"Chunks included: {len(chunks_used)}, tokens: {tokens_used}/{token_budget}")
    for c in chunks_used:
        sym = c.get("symbol") or "<module>"
        lines.append(f"  [{c['score']:.4f}] {c['file']}:{c['lines']}  {sym}")
    return "\n".join(lines)
