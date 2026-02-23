"""
Unit tests for the chunker — no network, no DB required.
"""

from src.pipeline.chunker import RawChunk, _greedy_merge, chunk_file, count_tokens
from src.pipeline.parser import ParsedFile

# ── count_tokens ──────────────────────────────────────────────────────────────


def test_count_tokens_basic():
    assert count_tokens("hello world") > 0
    assert count_tokens("") == 0


def test_count_tokens_scales_with_length():
    short = count_tokens("def foo(): pass")
    long = count_tokens("def foo(): pass\n" * 100)
    assert long > short * 50


# ── Chunk size constraint ─────────────────────────────────────────────────────


def _make_parsed(source: str, with_symbols: bool = True) -> ParsedFile:
    from src.pipeline.parser import parse_file

    return parse_file("test.py", source)


def test_no_chunk_exceeds_target():
    """A large class must be split so no chunk exceeds 512 tokens."""
    # Build a large class with many methods
    methods = "\n".join(
        f"    def method_{i}(self, x: int) -> int:\n"
        f"        '''Method {i} docstring.'''\n"
        f"        return x + {i}\n"
        for i in range(40)
    )
    source = f"class BigClass:\n    '''A large class.'''\n{methods}"

    parsed = _make_parsed(source)
    chunks = chunk_file(parsed)

    assert len(chunks) > 1, "Large class should produce multiple chunks"
    for chunk in chunks:
        assert chunk.token_count <= 512, (
            f"Chunk exceeded 512 tokens: {chunk.token_count} tokens\n"
            f"  symbol={chunk.symbol_name} lines={chunk.start_line}-{chunk.end_line}"
        )


def test_tiny_functions_merged():
    """Adjacent tiny functions should be merged into a single chunk."""
    source = "\n".join(f"def tiny_{i}():\n    return {i}\n" for i in range(20))
    parsed = _make_parsed(source)
    chunks = chunk_file(parsed)

    # 20 tiny functions should be merged into far fewer chunks
    assert len(chunks) < 10, (
        f"Expected merging to reduce 20 tiny functions to <10 chunks, got {len(chunks)}"
    )


def test_single_function_single_chunk():
    source = "def greet(name: str) -> str:\n    return f'Hello {name}'\n"
    parsed = _make_parsed(source)
    chunks = chunk_file(parsed)
    assert len(chunks) == 1
    assert "greet" in chunks[0].raw_content


def test_chunk_preserves_line_numbers():
    source = "def first():\n    pass\n\ndef second():\n    pass\n"
    parsed = _make_parsed(source)
    chunks = chunk_file(parsed)
    # After merging, at minimum the lines should be set
    for chunk in chunks:
        assert chunk.start_line >= 1
        assert chunk.end_line >= chunk.start_line


def test_file_with_no_symbols_still_chunked():
    """A file of only constants/assignments should still produce chunks."""
    source = "\n".join(f"CONST_{i} = {i}" for i in range(50))
    parsed = _make_parsed(source)
    chunks = chunk_file(parsed)
    assert len(chunks) >= 1
    assert all(c.token_count > 0 for c in chunks)


# ── Greedy merge ──────────────────────────────────────────────────────────────


def _make_chunk(start: int, end: int, content: str, tok: int | None = None) -> RawChunk:
    return RawChunk(
        file_path="f.py",
        language="python",
        symbol_name=None,
        symbol_kind=None,
        scope_chain=None,
        start_line=start,
        end_line=end,
        raw_content=content,
        token_count=tok or count_tokens(content),
    )


def test_greedy_merge_adjacent_small():
    chunks = [_make_chunk(1, 1, "x = 1", 3), _make_chunk(2, 2, "y = 2", 3)]
    merged = _greedy_merge(chunks, target=512)
    assert len(merged) == 1


def test_greedy_merge_does_not_exceed_target():
    # Each chunk is 300 tokens — should NOT merge (600 > 512)
    big_text = "x " * 150  # ~300 tokens
    chunks = [_make_chunk(1, 5, big_text, 300), _make_chunk(6, 10, big_text, 300)]
    merged = _greedy_merge(chunks, target=512)
    assert len(merged) == 2


def test_greedy_merge_non_adjacent_not_merged():
    chunks = [_make_chunk(1, 2, "a = 1", 3), _make_chunk(100, 101, "b = 2", 3)]
    merged = _greedy_merge(chunks, target=512)
    assert len(merged) == 2
