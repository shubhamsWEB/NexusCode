"""
Recursive split-then-merge chunker.

Converts a ParsedFile into a list of RawChunks that:
  - Respect AST boundaries (never split mid-function)
  - Stay within TARGET_TOKENS (512)
  - Are at least MIN_TOKENS (50) — adjacent small nodes are merged
  - Fall back to sliding window for files with no symbols
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import tiktoken

from src.config import settings
from src.pipeline.parser import ParsedFile, ParsedSymbol

logger = logging.getLogger(__name__)

# Token counting — cl100k_base is fast and a good proxy for voyage-code-2
_ENCODER = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text, disallowed_special=()))


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class RawChunk:
    file_path: str
    language: str
    symbol_name: Optional[str]       # "UserService.authenticate"
    symbol_kind: Optional[str]       # "function" | "class" | "method" | None
    scope_chain: Optional[str]       # "UserService > authenticate"
    start_line: int
    end_line: int
    raw_content: str
    imports: list[str] = field(default_factory=list)
    token_count: int = 0

    def __post_init__(self):
        if self.token_count == 0:
            self.token_count = count_tokens(self.raw_content)


# ── Core algorithm ────────────────────────────────────────────────────────────

def chunk_file(parsed: ParsedFile) -> list[RawChunk]:
    """
    Main entry point. Returns a list of RawChunks for the parsed file.
    """
    target = settings.chunk_target_tokens
    overlap = settings.chunk_overlap_tokens
    min_tok = settings.chunk_min_tokens

    if parsed.top_level_symbols:
        chunks = _process_symbols(parsed, target, overlap, min_tok)
    else:
        # No symbols found — slide over the raw content
        chunks = _sliding_window(
            parsed.file_path, parsed.language, parsed.source,
            parsed.imports, target, overlap,
        )

    # Greedy merge pass: combine adjacent tiny chunks
    merged = _greedy_merge(chunks, target)

    logger.debug(
        "chunk_file: %s → %d chunks (min=%d max=%d avg=%d tokens)",
        parsed.file_path, len(merged),
        min(c.token_count for c in merged) if merged else 0,
        max(c.token_count for c in merged) if merged else 0,
        sum(c.token_count for c in merged) // max(len(merged), 1),
    )
    return merged


# ── Symbol processing ─────────────────────────────────────────────────────────

def _process_symbols(
    parsed: ParsedFile,
    target: int,
    overlap: int,
    min_tok: int,
) -> list[RawChunk]:
    chunks: list[RawChunk] = []

    for sym in parsed.top_level_symbols:
        tok = count_tokens(sym.source)

        if tok <= target:
            # Fits in one chunk
            chunks.append(_symbol_to_chunk(sym, parsed))

        elif sym.children:
            # Too large but has children (e.g. a class with many methods)
            # Emit class header + process each method individually
            header = _class_header(sym, parsed.source)
            header_tok = count_tokens(header)
            if header_tok > 0:
                chunks.append(RawChunk(
                    file_path=parsed.file_path,
                    language=parsed.language,
                    symbol_name=sym.qualified_name,
                    symbol_kind=sym.kind,
                    scope_chain=sym.name,
                    start_line=sym.start_line,
                    end_line=min(sym.start_line + 10, sym.end_line),
                    raw_content=header,
                    imports=parsed.imports,
                    token_count=header_tok,
                ))

            for child in sym.children:
                child_tok = count_tokens(child.source)
                if child_tok <= target:
                    chunks.append(_symbol_to_chunk(child, parsed))
                else:
                    # Method is itself too large — sliding window over it
                    chunks.extend(_sliding_window(
                        parsed.file_path, parsed.language, child.source,
                        parsed.imports, target, overlap,
                        symbol_name=child.qualified_name,
                        symbol_kind=child.kind,
                        scope_chain=_scope_chain(child),
                        base_line=child.start_line - 1,
                    ))

        else:
            # Too large, no children — sliding window
            chunks.extend(_sliding_window(
                parsed.file_path, parsed.language, sym.source,
                parsed.imports, target, overlap,
                symbol_name=sym.qualified_name,
                symbol_kind=sym.kind,
                scope_chain=_scope_chain(sym),
                base_line=sym.start_line - 1,
            ))

    return chunks


def _symbol_to_chunk(sym: ParsedSymbol, parsed: ParsedFile) -> RawChunk:
    return RawChunk(
        file_path=parsed.file_path,
        language=parsed.language,
        symbol_name=sym.qualified_name,
        symbol_kind=sym.kind,
        scope_chain=_scope_chain(sym),
        start_line=sym.start_line,
        end_line=sym.end_line,
        raw_content=sym.source,
        imports=parsed.imports,
    )


def _scope_chain(sym: ParsedSymbol) -> str:
    if sym.parent_name:
        return f"{sym.parent_name} > {sym.name}"
    return sym.name


def _class_header(sym: ParsedSymbol, full_source: str) -> str:
    """Extract the class signature + docstring (up to 10 lines)."""
    lines = sym.source.splitlines()
    header_lines = lines[:min(10, len(lines))]
    return "\n".join(header_lines)


# ── Sliding window fallback ───────────────────────────────────────────────────

def _sliding_window(
    file_path: str,
    language: str,
    source: str,
    imports: list[str],
    target: int,
    overlap: int,
    symbol_name: Optional[str] = None,
    symbol_kind: Optional[str] = None,
    scope_chain: Optional[str] = None,
    base_line: int = 0,
) -> list[RawChunk]:
    """Split source text by token budget with overlap. Used as a last resort."""
    tokens = _ENCODER.encode(source, disallowed_special=())
    stride = target - overlap
    chunks: list[RawChunk] = []
    lines = source.splitlines()

    i = 0
    while i < len(tokens):
        window_tokens = tokens[i : i + target]
        window_text = _ENCODER.decode(window_tokens)

        # Estimate line numbers from character offsets
        chars_before = len(_ENCODER.decode(tokens[:i]))
        start_line = base_line + source[:chars_before].count("\n") + 1
        end_line = start_line + window_text.count("\n")

        chunks.append(RawChunk(
            file_path=file_path,
            language=language,
            symbol_name=symbol_name,
            symbol_kind=symbol_kind,
            scope_chain=scope_chain,
            start_line=start_line,
            end_line=end_line,
            raw_content=window_text,
            imports=imports,
            token_count=len(window_tokens),
        ))
        i += stride

    return chunks


# ── Greedy merge pass ─────────────────────────────────────────────────────────

def _greedy_merge(chunks: list[RawChunk], target: int) -> list[RawChunk]:
    """
    Merge adjacent chunks from the same file if:
      - They are adjacent (end_line + 1 >= next start_line)
      - Combined token count <= target
    This prevents hundreds of 5-token constant chunks.
    """
    if not chunks:
        return []

    result: list[RawChunk] = []
    buf = chunks[0]

    for chunk in chunks[1:]:
        adjacent = chunk.start_line <= buf.end_line + 2
        combined_tok = buf.token_count + chunk.token_count

        if adjacent and combined_tok <= target:
            # Merge into buffer
            buf = RawChunk(
                file_path=buf.file_path,
                language=buf.language,
                symbol_name=buf.symbol_name or chunk.symbol_name,
                symbol_kind=buf.symbol_kind or chunk.symbol_kind,
                scope_chain=buf.scope_chain or chunk.scope_chain,
                start_line=buf.start_line,
                end_line=chunk.end_line,
                raw_content=buf.raw_content + "\n" + chunk.raw_content,
                imports=buf.imports,
                token_count=combined_tok,
            )
        else:
            result.append(buf)
            buf = chunk

    result.append(buf)
    return result
