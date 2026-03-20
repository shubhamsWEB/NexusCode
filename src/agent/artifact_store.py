"""
Redis-backed per-session artifact store for the agent loop.

Each query gets an ArtifactStore instance identified by a UUID session_id.
Full tool results are stored as JSON values; only compressed summaries are
injected into the LLM context. The LLM may call load_artifact(id) to fetch
the original at any time.

Key schema:
  artifact:{session_id}:{artifact_id}   → UTF-8 bytes  (TTL=artifact_ttl_seconds)
  session:wm:{session_id}               → JSON bytes   (TTL=artifact_ttl_seconds+300)

Sessions are scoped to a single query invocation — no cross-query sharing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── Rule-based compressors ────────────────────────────────────────────────────


def _compress_search_codebase(data: dict) -> str:
    """Top-5 file paths + match count + best score."""
    results = data.get("results") or []
    count = data.get("results_count", len(results))
    query = data.get("query", "")
    files = []
    best_score = 0.0
    for r in results[:5]:
        fp = r.get("file", "")
        if fp:
            files.append(fp)
        sc = r.get("score", 0.0)
        if sc > best_score:
            best_score = sc
    files_str = ", ".join(files) if files else "(none)"
    return (
        f"search_codebase({query!r}): {count} results. "
        f"Top files: {files_str}. Best score: {best_score:.3f}."
    )


def _compress_get_agent_context(data: dict) -> str:
    """File path + first 3 symbol names + total lines / tokens."""
    task = data.get("task", "")
    tokens = data.get("tokens_used", 0)
    chunks = data.get("chunks_used", 0)
    focal = data.get("focal_files") or []
    focal_str = ", ".join(focal[:3]) if focal else "(none)"
    return (
        f"get_agent_context({task!r:.60}): focal=[{focal_str}], "
        f"{chunks} chunks, {tokens:,} tokens."
    )


def _compress_get_symbol(data: dict) -> str:
    """Symbol name + file:line + first 3 lines of body."""
    symbols = data.get("symbols") or []
    count = data.get("count", len(symbols))
    parts = []
    for s in symbols[:3]:
        name = s.get("qualified_name") or s.get("name", "?")
        fp = s.get("file", "")
        lines = s.get("lines", "")
        parts.append(f"{name} @ {fp}:{lines}")
    joined = "; ".join(parts) if parts else "(no symbols)"
    return f"get_symbol: {count} matches — {joined}"


def _compress_get_file_context(data: dict) -> str:
    """File path + total lines + first 5 symbol names."""
    fp = data.get("file", "?")
    lang = data.get("language", "")
    symbols = data.get("symbols") or []
    sym_names = [s.get("name", "?") for s in symbols[:5]]
    sym_str = ", ".join(sym_names) if sym_names else "(none)"
    return f"get_file_context({fp!r}, {lang}): {len(symbols)} symbols — {sym_str}"


def _compress_find_callers(data: dict) -> str:
    """Count + first 5 caller locations."""
    total = data.get("total_callers", 0)
    hops = data.get("hops") or []
    locs = []
    for hop in hops:
        for c in (hop.get("callers") or [])[:5]:
            fp = c.get("file", "?")
            sym = c.get("symbol_context", "")
            locs.append(f"{fp}:{sym}" if sym else fp)
            if len(locs) >= 5:
                break
        if len(locs) >= 5:
            break
    locs_str = ", ".join(locs) if locs else "(none)"
    return f"find_callers: {total} callers — {locs_str}"


def _compress_default(result_text: str) -> str:
    """First 200 chars + byte count."""
    byte_count = len(result_text.encode("utf-8"))
    preview = result_text[:200].replace("\n", " ")
    return f"[{byte_count:,} bytes] {preview}…"


def _compress(tool_name: str, result_text: str) -> str:
    """Dispatch to the appropriate rule-based compressor."""
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        data = {}

    if not isinstance(data, dict):
        return _compress_default(result_text)

    if tool_name == "search_codebase":
        return _compress_search_codebase(data)
    if tool_name == "get_agent_context":
        return _compress_get_agent_context(data)
    if tool_name == "get_symbol":
        return _compress_get_symbol(data)
    if tool_name == "get_file_context":
        return _compress_get_file_context(data)
    if tool_name == "find_callers":
        return _compress_find_callers(data)
    return _compress_default(result_text)


# ── Working-memory extraction helpers ─────────────────────────────────────────


def _extract_file_paths(tool_name: str, result_text: str) -> list[str]:
    """Extract file paths from a tool result for working memory."""
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    paths: list[str] = []
    if tool_name == "search_codebase":
        for r in (data.get("results") or []):
            fp = r.get("file")
            if fp:
                paths.append(fp)
    elif tool_name in ("get_agent_context", "get_file_context"):
        fp = data.get("file") or ""
        if fp:
            paths.append(fp)
        for f in (data.get("focal_files") or []):
            if f:
                paths.append(f)
    elif tool_name == "find_callers":
        for hop in (data.get("hops") or []):
            for c in (hop.get("callers") or []):
                fp = c.get("file")
                if fp:
                    paths.append(fp)
    elif tool_name == "get_symbol":
        for s in (data.get("symbols") or []):
            fp = s.get("file")
            if fp:
                paths.append(fp)
    return list(dict.fromkeys(paths))  # dedupe, preserve order


def _extract_symbol_names(tool_name: str, result_text: str) -> list[str]:
    """Extract symbol names from a tool result for working memory."""
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    if tool_name == "get_symbol":
        for s in (data.get("symbols") or []):
            name = s.get("qualified_name") or s.get("name")
            if name:
                names.append(name)
    elif tool_name == "get_file_context":
        for s in (data.get("symbols") or []):
            name = s.get("name")
            if name:
                names.append(name)
    elif tool_name == "find_callers":
        for hop in (data.get("hops") or []):
            for c in (hop.get("callers") or []):
                sym = c.get("symbol_context")
                if sym and sym != "<module>":
                    names.append(sym)
    return list(dict.fromkeys(names))


# ── ArtifactStore ─────────────────────────────────────────────────────────────


class ArtifactStore:
    """
    Redis-backed per-session artifact store for the agent loop.

    Full tool results are stored under artifact:{session_id}:{artifact_id}.
    Only a compressed summary is returned to the caller for injection into
    the LLM context.  The LLM may call load_artifact(artifact_id) to retrieve
    the full result at any point.

    Working memory (found_files, found_symbols, visited_paths, chunks_used,
    iteration_count) accumulates in session:wm:{session_id}.
    """

    def __init__(
        self,
        session_id: str | None = None,
        redis_url: str | None = None,
        ttl: int = 1800,
    ) -> None:
        from src.config import settings as _settings

        self.session_id: str = session_id or str(uuid.uuid4())
        self._redis_url: str = redis_url or _settings.redis_url
        self._ttl: int = ttl
        self._wm_prefix: str = _settings.artifact_working_memory_prefix
        self._redis: Any = None

    async def _get_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=False)
        return self._redis

    def _artifact_key(self, artifact_id: str) -> str:
        return f"artifact:{self.session_id}:{artifact_id}"

    def _wm_key(self) -> str:
        return f"{self._wm_prefix}:{self.session_id}"

    async def save(self, tool_name: str, result_text: str) -> tuple[str, str]:
        """
        Store the full result_text in Redis under a new artifact ID.

        Returns (artifact_id, summary) where summary is the compressed
        representation suitable for injecting into the LLM context.
        """
        artifact_id = str(uuid.uuid4())[:8]  # short 8-char ID keeps summaries readable
        key = self._artifact_key(artifact_id)
        try:
            r = await self._get_redis()
            await r.set(key, result_text.encode("utf-8"), ex=self._ttl)
        except Exception as exc:
            logger.warning("artifact_store.save: Redis error (%s) — returning raw result", exc)
            # Graceful degradation: return identity with a pseudo-ID
            return artifact_id, result_text

        summary = _compress(tool_name, result_text)
        return artifact_id, summary

    async def load(self, artifact_id: str) -> str | None:
        """
        Retrieve the full result for an artifact_id.
        Returns None if the key is missing or expired.
        """
        key = self._artifact_key(artifact_id)
        try:
            r = await self._get_redis()
            value = await r.get(key)
        except Exception as exc:
            logger.warning("artifact_store.load: Redis error (%s)", exc)
            return None
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def update_working_memory(self, key: str, value: Any) -> None:
        """
        Merge a value into the working memory hash for this session.

        For list-valued keys (found_files, found_symbols, visited_paths),
        values are appended and deduplicated.  For scalar keys (chunks_used,
        iteration_count), values are accumulated.
        """
        wm_key = self._wm_key()
        wm_ttl = self._ttl + 300
        try:
            r = await self._get_redis()
            raw = await r.get(wm_key)
            wm: dict = {}
            if raw:
                try:
                    wm = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                except (json.JSONDecodeError, TypeError):
                    wm = {}

            if key in ("found_files", "found_symbols", "visited_paths"):
                existing: list = wm.get(key, [])
                if isinstance(value, list):
                    merged = existing + [v for v in value if v not in existing]
                else:
                    merged = existing + ([value] if value not in existing else [])
                wm[key] = merged
            elif key in ("chunks_used", "iteration_count"):
                wm[key] = wm.get(key, 0) + (value if isinstance(value, (int, float)) else 0)
            else:
                wm[key] = value

            await r.set(wm_key, json.dumps(wm).encode("utf-8"), ex=wm_ttl)
        except Exception as exc:
            logger.warning("artifact_store.update_working_memory: Redis error (%s)", exc)

    async def get_working_memory(self) -> dict:
        """Return the current working memory dict, or an empty dict on miss."""
        wm_key = self._wm_key()
        try:
            r = await self._get_redis()
            raw = await r.get(wm_key)
        except Exception as exc:
            logger.warning("artifact_store.get_working_memory: Redis error (%s)", exc)
            return {}
        if raw is None:
            return {}
        try:
            return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
