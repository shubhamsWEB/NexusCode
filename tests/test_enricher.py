"""
Unit tests for the chunk enricher.
"""
import pytest

from src.pipeline.chunker import RawChunk
from src.pipeline.enricher import (
    _filter_relevant_imports,
    _import_identifiers,
    _short_path,
    enrich_chunk,
)


def _make_chunk(**kwargs) -> RawChunk:
    defaults = dict(
        file_path="src/auth/service.py",
        language="python",
        symbol_name="AuthService.validate_token",
        symbol_kind="method",
        scope_chain="AuthService > validate_token",
        start_line=42,
        end_line=55,
        raw_content=(
            "def validate_token(self, token: str) -> bool:\n"
            "    payload = jwt.decode(token, self.secret)\n"
            "    return payload.get('valid', False)\n"
        ),
        imports=["import jwt", "from .models import User", "import os"],
    )
    defaults.update(kwargs)
    return RawChunk(**defaults)


# ── enrich_chunk ──────────────────────────────────────────────────────────────

def test_enriched_contains_file_path():
    ec = enrich_chunk(_make_chunk())
    assert "auth/service.py" in ec.enriched_content


def test_enriched_contains_scope_chain():
    ec = enrich_chunk(_make_chunk())
    assert "AuthService > validate_token" in ec.enriched_content


def test_enriched_contains_language():
    ec = enrich_chunk(_make_chunk())
    assert "python" in ec.enriched_content


def test_enriched_contains_relevant_imports():
    ec = enrich_chunk(_make_chunk())
    # jwt is used in the code → should appear
    assert "jwt" in ec.enriched_content


def test_enriched_contains_code():
    ec = enrich_chunk(_make_chunk())
    assert "validate_token" in ec.enriched_content
    assert "Code:" in ec.enriched_content


def test_chunk_id_is_deterministic():
    chunk = _make_chunk()
    ec1 = enrich_chunk(chunk)
    ec2 = enrich_chunk(chunk)
    assert ec1.chunk_id == ec2.chunk_id


def test_different_content_different_id():
    ec1 = enrich_chunk(_make_chunk(raw_content="def foo(): pass"))
    ec2 = enrich_chunk(_make_chunk(raw_content="def bar(): pass"))
    assert ec1.chunk_id != ec2.chunk_id


def test_chunk_id_is_64_char_hex():
    ec = enrich_chunk(_make_chunk())
    assert len(ec.chunk_id) == 64
    assert all(c in "0123456789abcdef" for c in ec.chunk_id)


# ── _short_path ───────────────────────────────────────────────────────────────

def test_short_path_long():
    assert _short_path("a/b/c/d/service.py") == "c/d/service.py"


def test_short_path_short():
    result = _short_path("service.py")
    assert "service.py" in result


# ── _import_identifiers ───────────────────────────────────────────────────────

def test_import_identifiers_simple():
    assert "jwt" in _import_identifiers("import jwt")


def test_import_identifiers_alias():
    assert "np" in _import_identifiers("import numpy as np")


def test_import_identifiers_from():
    ids = _import_identifiers("from .models import User, Token")
    assert "User" in ids
    assert "Token" in ids


def test_import_identifiers_from_alias():
    ids = _import_identifiers("from typing import Optional as Opt")
    assert "Opt" in ids


# ── _filter_relevant_imports ──────────────────────────────────────────────────

def test_filter_keeps_used_imports():
    imports = ["import jwt", "import os", "from .db import Session"]
    code = "payload = jwt.decode(token)\nsession = Session()"
    filtered = _filter_relevant_imports(imports, code)
    assert "import jwt" in filtered
    assert "from .db import Session" in filtered


def test_filter_excludes_unused_imports():
    imports = ["import jwt", "import pandas as pd"]
    code = "payload = jwt.decode(token)"
    filtered = _filter_relevant_imports(imports, code)
    assert "import jwt" in filtered
    # pandas not used
    assert "import pandas as pd" not in filtered


def test_filter_fallback_when_nothing_matches():
    """If nothing matches, still return something (first few imports)."""
    imports = ["import pandas", "import numpy"]
    code = "x = 1 + 1"
    filtered = _filter_relevant_imports(imports, code)
    assert len(filtered) > 0
