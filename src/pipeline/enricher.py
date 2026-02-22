"""
Chunk enricher — injects contextual metadata into each chunk before embedding.

The enriched text is what gets embedded, NOT the raw source.
This is the most impactful retrieval quality improvement:
a function like `def validate_token(self)` has almost no signal alone;
with file path, scope chain, language, and imports it becomes self-describing.

BEFORE:
    def validate_token(self, token: str) -> bool:
        ...

AFTER:
    File: src/auth/service.py
    Scope: AuthService > validate_token
    Language: python
    Key imports:
    import jwt
    from .models import User

    Code:
    def validate_token(self, token: str) -> bool:
        ...
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.pipeline.chunker import RawChunk, count_tokens

logger = logging.getLogger(__name__)

# How many path segments to include in the enriched header (last N segments)
_PATH_SEGMENTS = 3

# Maximum number of import lines to inject (avoid bloating with 50 imports)
_MAX_IMPORTS = 8


@dataclass
class EnrichedChunk:
    # Passthrough from RawChunk
    file_path: str
    language: str
    symbol_name: Optional[str]
    symbol_kind: Optional[str]
    scope_chain: Optional[str]
    start_line: int
    end_line: int
    raw_content: str
    imports: list[str]
    token_count: int         # token count of raw_content

    # Enriched
    enriched_content: str    # what gets embedded
    chunk_id: str            # SHA-256(enriched_content) — used as DB primary key


def enrich_chunk(chunk: RawChunk) -> EnrichedChunk:
    """
    Produce an EnrichedChunk by prepending metadata to the raw source.
    The chunk_id is the SHA-256 of the enriched content — used as a cache key
    to skip re-embedding identical chunks.
    """
    header = _build_header(chunk)
    enriched = f"{header}\nCode:\n{chunk.raw_content}"

    chunk_id = hashlib.sha256(enriched.encode("utf-8")).hexdigest()

    return EnrichedChunk(
        file_path=chunk.file_path,
        language=chunk.language,
        symbol_name=chunk.symbol_name,
        symbol_kind=chunk.symbol_kind,
        scope_chain=chunk.scope_chain,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        raw_content=chunk.raw_content,
        imports=chunk.imports,
        token_count=chunk.token_count,
        enriched_content=enriched,
        chunk_id=chunk_id,
    )


def enrich_chunks(chunks: list[RawChunk]) -> list[EnrichedChunk]:
    return [enrich_chunk(c) for c in chunks]


# ── Header builder ────────────────────────────────────────────────────────────

def _build_header(chunk: RawChunk) -> str:
    """
    Construct the metadata header prepended to each chunk.
    Example output:
        File: src/auth/service.py
        Scope: AuthService > validate_token
        Language: python
        Key imports:
        import jwt
        from .models import User
    """
    lines: list[str] = []

    # File path — shortened to last N segments for brevity
    short_path = _short_path(chunk.file_path)
    lines.append(f"File: {short_path}")

    # Scope chain
    if chunk.scope_chain:
        lines.append(f"Scope: {chunk.scope_chain}")

    # Language
    lines.append(f"Language: {chunk.language}")

    # Relevant imports
    relevant = _filter_relevant_imports(chunk.imports, chunk.raw_content)
    if relevant:
        lines.append("Key imports:")
        lines.extend(relevant[:_MAX_IMPORTS])

    lines.append("")   # blank line before "Code:"
    return "\n".join(lines)


def _short_path(file_path: str) -> str:
    """Return the last _PATH_SEGMENTS segments of the path."""
    parts = Path(file_path).parts
    return "/".join(parts[-_PATH_SEGMENTS:]) if len(parts) >= _PATH_SEGMENTS else file_path


def _filter_relevant_imports(imports: list[str], code: str) -> list[str]:
    """
    Return only imports whose module name or alias appears in the chunk code.
    Falls back to returning all imports if nothing matches (small files).
    """
    if not imports:
        return []

    relevant = []
    for imp in imports:
        # Extract the key identifier from the import line
        # e.g. "import jwt" → "jwt"
        # e.g. "from .models import User, Token" → ["User", "Token"]
        # e.g. "import os.path as osp" → "osp"
        identifiers = _import_identifiers(imp)
        if any(ident in code for ident in identifiers):
            relevant.append(imp)

    # If nothing matched, return first few imports anyway (useful for small chunks)
    return relevant if relevant else imports[:4]


def _import_identifiers(import_line: str) -> list[str]:
    """Extract the usable names from an import statement."""
    line = import_line.strip()
    identifiers: list[str] = []

    if line.startswith("from "):
        # from X import a, b as c
        if " import " in line:
            after_import = line.split(" import ", 1)[1]
            for name in after_import.split(","):
                name = name.strip()
                if " as " in name:
                    name = name.split(" as ")[-1].strip()
                identifiers.append(name)
    elif line.startswith("import "):
        # import X, import X as Y, import X.Y
        after_import = line[len("import "):].strip()
        for part in after_import.split(","):
            part = part.strip()
            if " as " in part:
                identifiers.append(part.split(" as ")[-1].strip())
            else:
                # "os.path" → "os"
                identifiers.append(part.split(".")[0])

    return [i for i in identifiers if i]
