"""
Unit tests for src/agent/artifact_store.py

Tests use a real Redis connection OR mock via fakeredis when available.
All tests are async.
"""

from __future__ import annotations

import json
import pytest
import pytest_asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def fake_redis_server():
    """Start a single fakeredis server shared across async tests."""
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()
    return server


@pytest_asyncio.fixture()
async def store(fake_redis_server):
    """Return a fresh ArtifactStore backed by fakeredis."""
    fakeredis = pytest.importorskip("fakeredis")
    from src.agent.artifact_store import ArtifactStore

    class _PatchedStore(ArtifactStore):
        async def _get_redis(self):
            if self._redis is None:
                self._redis = fakeredis.aioredis.FakeRedis(
                    server=fake_redis_server, decode_responses=False
                )
            return self._redis

    s = _PatchedStore(ttl=60)
    yield s
    await s.close()


# ── Helper data ───────────────────────────────────────────────────────────────


_SEARCH_RESULT = json.dumps({
    "query": "JWT validation",
    "results_count": 3,
    "results": [
        {"file": "src/auth/service.py", "score": 0.95},
        {"file": "src/middleware/jwt.py", "score": 0.88},
        {"file": "src/utils/token.py", "score": 0.72},
        {"file": "src/api/auth.py", "score": 0.65},
        {"file": "src/models/user.py", "score": 0.60},
        {"file": "extra/ignored.py", "score": 0.50},
    ],
    "context": "# ... assembled context ...",
    "tokens_used": 2800,
})

_SYMBOL_RESULT = json.dumps({
    "symbols": [
        {
            "name": "validate_jwt",
            "qualified_name": "AuthService.validate_jwt",
            "file": "src/auth/service.py",
            "lines": "42-89",
        }
    ],
    "count": 1,
})

_CALLERS_RESULT = json.dumps({
    "symbol": "validate_jwt",
    "total_callers": 4,
    "hops": [
        {
            "hop": 1,
            "callers": [
                {"file": "src/api/auth.py", "symbol_context": "login_handler", "lines": "10-40", "calls": "validate_jwt"},
                {"file": "src/api/refresh.py", "symbol_context": "refresh_token", "lines": "5-30", "calls": "validate_jwt"},
            ],
        }
    ],
})


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_returns_summary_not_full_result(store):
    """Injected text should be a compressed summary, not the raw JSON blob."""
    artifact_id, summary = await store.save("search_codebase", _SEARCH_RESULT)

    assert artifact_id  # non-empty ID
    # Summary should be much shorter than full result
    assert len(summary) < len(_SEARCH_RESULT)
    # Summary should contain key info
    assert "JWT validation" in summary
    assert "3" in summary  # results count
    # Must NOT be the raw JSON
    assert summary.strip()[0] != "{"


@pytest.mark.asyncio
async def test_save_summary_within_token_limit(store):
    """Compressed summary must not exceed ~200 tokens (≈800 chars at 4 chars/token)."""
    _, summary = await store.save("search_codebase", _SEARCH_RESULT)
    # Rough token estimate: 4 chars per token → 200 tokens = 800 chars
    assert len(summary) <= 1000, f"Summary too long: {len(summary)} chars"


@pytest.mark.asyncio
async def test_load_returns_full_result(store):
    """Round-trip: save then load should return the original full result."""
    artifact_id, _ = await store.save("search_codebase", _SEARCH_RESULT)
    loaded = await store.load(artifact_id)

    assert loaded is not None
    assert loaded == _SEARCH_RESULT


@pytest.mark.asyncio
async def test_load_artifact_missing(store):
    """Loading a non-existent artifact ID returns None."""
    result = await store.load("deadbeef")
    assert result is None


@pytest.mark.asyncio
async def test_working_memory_accumulates(store):
    """Multiple update_working_memory calls should merge lists correctly."""
    await store.update_working_memory("found_files", ["src/auth/service.py"])
    await store.update_working_memory("found_files", ["src/middleware/jwt.py"])
    await store.update_working_memory("found_files", ["src/auth/service.py"])  # duplicate — should be ignored

    wm = await store.get_working_memory()
    assert "src/auth/service.py" in wm["found_files"]
    assert "src/middleware/jwt.py" in wm["found_files"]
    # Deduplication: src/auth/service.py should appear only once
    assert wm["found_files"].count("src/auth/service.py") == 1


@pytest.mark.asyncio
async def test_working_memory_scalar_accumulates(store):
    """Scalar working memory keys (iteration_count, chunks_used) accumulate."""
    await store.update_working_memory("iteration_count", 1)
    await store.update_working_memory("iteration_count", 1)
    await store.update_working_memory("iteration_count", 1)

    wm = await store.get_working_memory()
    assert wm["iteration_count"] == 3


@pytest.mark.asyncio
async def test_compressor_search_codebase(store):
    """search_codebase compressor: top-5 file format and best score present."""
    _, summary = await store.save("search_codebase", _SEARCH_RESULT)

    # Should mention the top-5 files but NOT the 6th (extra/ignored.py)
    assert "src/auth/service.py" in summary
    assert "src/middleware/jwt.py" in summary
    assert "src/utils/token.py" in summary
    assert "src/api/auth.py" in summary
    assert "src/models/user.py" in summary
    assert "extra/ignored.py" not in summary
    # Best score
    assert "0.950" in summary or "0.95" in summary


@pytest.mark.asyncio
async def test_compressor_get_symbol(store):
    """get_symbol compressor: symbol name and file:line in summary."""
    _, summary = await store.save("get_symbol", _SYMBOL_RESULT)

    assert "validate_jwt" in summary or "AuthService.validate_jwt" in summary
    assert "src/auth/service.py" in summary


@pytest.mark.asyncio
async def test_compressor_find_callers(store):
    """find_callers compressor: total count and first caller locations."""
    _, summary = await store.save("find_callers", _CALLERS_RESULT)

    assert "4" in summary  # total callers
    assert "src/api/auth.py" in summary


@pytest.mark.asyncio
async def test_artifact_ttl_set(store):
    """After save(), the key should have a TTL set in Redis."""
    artifact_id, _ = await store.save("search_codebase", _SEARCH_RESULT)

    r = await store._get_redis()
    key = store._artifact_key(artifact_id)
    ttl = await r.ttl(key)
    # TTL should be positive and ≤ store._ttl
    assert 0 < ttl <= store._ttl


@pytest.mark.asyncio
async def test_working_memory_ttl_set(store):
    """After update_working_memory(), the WM key should have a TTL set."""
    await store.update_working_memory("found_files", ["src/foo.py"])

    r = await store._get_redis()
    wm_key = store._wm_key()
    ttl = await r.ttl(wm_key)
    # WM TTL should be ttl+300
    assert 0 < ttl <= store._ttl + 300


@pytest.mark.asyncio
async def test_get_working_memory_empty(store):
    """get_working_memory on a fresh store returns empty dict."""
    wm = await store.get_working_memory()
    assert wm == {}


@pytest.mark.asyncio
async def test_multiple_saves_independent(store):
    """Multiple save() calls produce independent artifact IDs."""
    id1, _ = await store.save("search_codebase", _SEARCH_RESULT)
    id2, _ = await store.save("get_symbol", _SYMBOL_RESULT)

    assert id1 != id2
    assert await store.load(id1) == _SEARCH_RESULT
    assert await store.load(id2) == _SYMBOL_RESULT


@pytest.mark.asyncio
async def test_session_isolation(fake_redis_server):
    """Two stores with different session IDs don't share artifacts."""
    fakeredis = pytest.importorskip("fakeredis")
    from src.agent.artifact_store import ArtifactStore

    class _PatchedStore(ArtifactStore):
        async def _get_redis(self):
            if self._redis is None:
                self._redis = fakeredis.aioredis.FakeRedis(
                    server=fake_redis_server, decode_responses=False
                )
            return self._redis

    store_a = _PatchedStore(session_id="session-AAA", ttl=60)
    store_b = _PatchedStore(session_id="session-BBB", ttl=60)

    try:
        id_a, _ = await store_a.save("search_codebase", _SEARCH_RESULT)
        # Store B should NOT be able to load store A's artifact by the same short ID
        result_b = await store_b.load(id_a)
        assert result_b is None
    finally:
        await store_a.close()
        await store_b.close()
