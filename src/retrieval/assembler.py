"""
Token-budget-aware context assembler.

Takes a ranked list of SearchResults and assembles them into a
single, formatted context string ready to inject into an LLM prompt.

Respects a token budget — stops adding chunks once the budget is full.
Deduplicates by chunk_id so the same chunk never appears twice.

Output format: file-grouped
  Chunks selected by score rank (greedy), then rendered grouped by file
  so the LLM sees all code from a file consecutively (improves comprehension).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.pipeline.chunker import count_tokens

if TYPE_CHECKING:
    from src.retrieval.searcher import SearchResult

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

_PATH_SEGMENTS = 3

def _estimate_header_tokens(result: SearchResult) -> int:
    """Dynamically estimate the token overhead for this chunk's headers."""
    # We use a string template approximating the real header fields
    from src.pipeline.chunker import count_tokens

    file_hdr = f"File: {result.file_path} ({result.language})  last: {result.commit_author} @ {(result.commit_sha or '')[:7]}"
    chunk_hdr = f"[lines {result.start_line}-{result.end_line}]  Scope: {result.scope_chain}  Score: 0.0000"
    # Provide 15 extra tokens for formatting dividers like ──────
    return count_tokens(file_hdr + "\n" + chunk_hdr) + 15


@dataclass
class AssembledContext:
    context_text: str  # formatted, ready-to-inject string
    chunks_used: list[dict]  # metadata for each included chunk
    tokens_used: int
    retrieval_log: str  # human-readable summary of what was retrieved
    quality_score: float = 0.0  # mean sigmoid-normalized rerank_score of selected chunks (0.0-1.0)


def assemble(
    results: list[SearchResult],
    token_budget: int = 8000,
    query: str | None = None,
    expand_parents: bool = False,
) -> AssembledContext:
    """
    Greedily fill the token budget with the highest-ranked chunks,
    then render the selected chunks grouped by file (file-grouped format).

    Selection phase: iterate by score rank, stop when budget is full.
    Rendering phase: group selected chunks by file_path, sort by start_line,
    so the LLM sees consecutive code within each file.

    Format of each file group in the output:
    ══════════════════════════════════════════════════════════
    File: src/auth/service.py  (python)  · last: alice @ a1b2c3d
    ──────────────────────────────────────────────────────────
    [lines 42-55]  Scope: AuthService > validate_token  Score: 0.847

    <raw source code>
    ──────────────────────────────────────────────────────────
    [lines 80-110]  Scope: AuthService > refresh_token  Score: 0.732

    <raw source code>
    ══════════════════════════════════════════════════════════
    """
    # ── Phase 1: greedy selection by score rank ───────────────────────────────
    seen_ids: set[str] = set()
    selected: list[SearchResult] = []
    chunks_used: list[dict] = []
    tokens_used = 0

    for result in results:
        if result.chunk_id in seen_ids:
            continue

        section_tokens = count_tokens(result.raw_content) + _estimate_header_tokens(result)
        if tokens_used + section_tokens > token_budget:
            # Try to keep going — maybe a later chunk is shorter
            continue

        seen_ids.add(result.chunk_id)
        selected.append(result)
        tokens_used += section_tokens
        chunks_used.append(
            {
                "file": result.file_path,
                "lines": f"{result.start_line}-{result.end_line}",
                "symbol": result.symbol_name,
                "score": round(result.rerank_score or result.score, 4),
                "tokens": section_tokens,
            }
        )

    # ── Phase 1.5: Expand parents ─────────────────────────────────────────────
    if expand_parents and selected:
        from src.storage.db import get_parent_chunks_sync

        # Find which chunks want a parent we don't already have
        parent_ids_needed = set()
        for r in selected:
            if getattr(r, "parent_chunk_id", None) and r.parent_chunk_id not in seen_ids:
                parent_ids_needed.add(r.parent_chunk_id)

        if parent_ids_needed:
            # Note: get_parent_chunks_sync must be synchronous or we need to await here.
            # We'll need a DB helper. Let's assume it returns dict[str, SearchResult]
            parents_map = get_parent_chunks_sync(list(parent_ids_needed))

            for pid, p_result in parents_map.items():
                section_tokens = count_tokens(p_result.raw_content) + _estimate_header_tokens(p_result)
                if tokens_used + section_tokens > token_budget:
                    continue  # Stop if budget full

                seen_ids.add(pid)
                # Parent score inherited from highest scoring child? For rendering order,
                # we don't care about score, the sorting step handles line numbers.
                selected.append(p_result)
                tokens_used += section_tokens
                chunks_used.append(
                    {
                        "file": p_result.file_path,
                        "lines": f"{p_result.start_line}-{p_result.end_line}",
                        "symbol": p_result.symbol_name,
                        "score": 0.0, # Indicates it was pulled via parent expansion
                        "tokens": section_tokens,
                    }
                )

    # ── Phase 2: group selected chunks by file, sort by start_line ───────────
    file_order: list[str] = []  # preserve first-seen file order (by top score)
    file_chunks: dict[str, list[SearchResult]] = defaultdict(list)

    for r in selected:
        if r.file_path not in file_chunks:
            file_order.append(r.file_path)
        file_chunks[r.file_path].append(r)

    # Sort each file's chunks by start_line for narrative reading order, with summaries first
    for fp in file_order:
        file_chunks[fp].sort(key=lambda r: (r.symbol_kind != "file_summary", r.start_line))

    # ── Phase 3: render file-grouped output ───────────────────────────────────
    sections: list[str] = []
    divider_heavy = "═" * 60
    divider_light = "─" * 60

    for fp in file_order:
        chunks = file_chunks[fp]
        if not chunks:
            continue

        # File-level header (shown once per file)
        first = chunks[0]
        short_path = _short_path(fp)
        lang = first.language or ""
        file_header_parts = [f"File: {short_path}"]
        if lang:
            file_header_parts.append(f"({lang})")

        # Commit info (from first chunk — all chunks in a file share the same commit)
        commit = first.commit_sha[:7] if first.commit_sha else ""
        if first.commit_author or commit:
            commit_parts = []
            if first.commit_author:
                commit_parts.append(first.commit_author)
            if commit:
                commit_parts.append(f"@ {commit}")
            file_header_parts.append("· last: " + " ".join(commit_parts))

        file_header = "  ".join(file_header_parts)
        sections.append(f"{divider_heavy}\n{file_header}")

        # Per-chunk content (each chunk gets a lightweight sub-header)
        for r in chunks:
            score = r.rerank_score or r.score
            chunk_header_parts = [f"[lines {r.start_line}-{r.end_line}]"]
            if r.scope_chain:
                chunk_header_parts.append(f"Scope: {r.scope_chain}")
            chunk_header_parts.append(f"Score: {score:.4f}")
            chunk_header = "  ".join(chunk_header_parts)
            sections.append(f"{divider_light}\n{chunk_header}\n\n{r.raw_content}")

    context_text = "\n".join(sections)
    if sections:
        context_text += f"\n{divider_heavy}"

    retrieval_log = _build_log(query, chunks_used, tokens_used, token_budget)
    logger.info(
        "assembler: %d/%d chunks included across %d files, %d tokens used",
        len(chunks_used),
        len(results),
        len(file_order),
        tokens_used,
    )

    # Aggregate quality score: mean of selected chunks' sigmoid-normalized scores
    quality_score = (
        sum(r.quality_score for r in selected) / len(selected)
        if selected else 0.0
    )

    return AssembledContext(
        context_text=context_text,
        chunks_used=chunks_used,
        tokens_used=tokens_used,
        retrieval_log=retrieval_log,
        quality_score=quality_score,
    )


# ── Formatting helpers ────────────────────────────────────────────────────────


def _short_path(file_path: str) -> str:
    parts = Path(file_path).parts
    return "/".join(parts[-_PATH_SEGMENTS:]) if len(parts) >= _PATH_SEGMENTS else file_path


def _build_log(
    query: str | None,
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
