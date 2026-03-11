"""
RepoRouter — query-time repo scoring and budget allocation.

Scores repos by semantic similarity (centroid cosine) + keyword Jaccard,
then allocates token budgets proportionally across the top-N repos.

detect_repo_mention() provides a fast short-circuit path: if the query itself
explicitly names a repo (e.g. "search myorg/auth-service for login" or just
"find login in auth-service"), skip the router entirely and go straight to
that single repo.  The same allowed_repos scope gate applies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import numpy as np

from src.config import settings
from src.storage.db import get_all_repo_summaries
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# ── Explicit repo mention detection ──────────────────────────────────────────

_OWNER_SLASH_NAME = re.compile(
    r"(?<![a-zA-Z0-9_])([a-zA-Z0-9][a-zA-Z0-9_.-]*)/"
    r"([a-zA-Z0-9][a-zA-Z0-9_.-]*)(?![a-zA-Z0-9_/])"
)


async def detect_repo_mention(
    query: str,
    allowed_repos: list[str] | None,
) -> tuple[str, str] | None:
    """
    Detect an explicit repo reference in the query by comparing against the
    authoritative list of known repos — no heuristics about naming conventions.

    Candidate list (source of truth, in priority order):
    - ``allowed_repos`` from the API key when present and non-empty: this is
      already the exact scoped set the caller can access, so we use it directly
      without hitting Redis or the DB.
    - Redis-cached ``repo_router:summaries`` when no API key is present: the
      full list of all indexed repos.

    Two match strategies (tried in order):

    Strategy 1 — ``owner/name`` format:
        Scans the query for ``word/word`` tokens and checks each against the
        candidate list.  Fully unambiguous — file paths like ``src/auth`` won't
        match unless there is literally a repo named ``src/auth``.

    Strategy 2 — repo-name-only whole-word match:
        Checks whether any known repo name appears as a standalone word in the
        query.  Returns a match only when exactly one candidate matches
        (zero or multiple → fall through to cross-repo router).

        The candidate list IS the disambiguation layer: if the user says
        "fix bug in authservice" and no indexed repo is named "authservice",
        nothing matches and the router decides instead.  If a repo IS named
        "authservice" and they say that word, we treat it as a repo reference —
        no guessing about PascalCase, file extensions, or context words needed.

    Returns ``(owner, name)`` on an unambiguous match, ``None`` otherwise.
    """
    # ── Build candidate list ──────────────────────────────────────────────────
    if allowed_repos is not None and len(allowed_repos) > 0:
        # API key present — use its scoped list directly; no Redis/DB call needed.
        candidates = [
            {"repo_owner": r.split("/", 1)[0], "repo_name": r.split("/", 1)[1]}
            for r in allowed_repos
            if "/" in r
        ]
    else:
        # No API key — load all indexed repos from Redis cache (or DB fallback).
        router = RepoRouter()
        candidates = await router._load_summaries()

    if not candidates:
        return None

    query_lower = query.lower()

    # ── Strategy 1: 'owner/name' token in query ───────────────────────────────
    for m in _OWNER_SLASH_NAME.finditer(query):
        owner_q, name_q = m.group(1).lower(), m.group(2).lower()
        for c in candidates:
            if c["repo_owner"].lower() == owner_q and c["repo_name"].lower() == name_q:
                logger.debug(
                    "detect_repo_mention: owner/name match → %s/%s",
                    c["repo_owner"], c["repo_name"],
                )
                return (c["repo_owner"], c["repo_name"])

    # ── Strategy 2: repo-name-only whole-word match ───────────────────────────
    # Sort longest name first so "api-gateway" is checked before "api".
    matches: list[tuple[str, str]] = []
    for c in sorted(candidates, key=lambda x: len(x["repo_name"]), reverse=True):
        repo_name_lower = c["repo_name"].lower()
        pattern = re.compile(
            r"(?<![a-zA-Z0-9_])" + re.escape(repo_name_lower) + r"(?![a-zA-Z0-9_])"
        )
        if pattern.search(query_lower):
            matches.append((c["repo_owner"], c["repo_name"]))

    if len(matches) == 1:
        logger.debug(
            "detect_repo_mention: name-only match → %s/%s", matches[0][0], matches[0][1]
        )
        return matches[0]

    # Zero or multiple matches → ambiguous; let the router decide.
    return None


def _get_redis():
    import redis.asyncio as redis

    return redis.from_url(settings.redis_url)


@dataclass
class ScoredRepo:
    owner: str
    name: str
    score: float          # combined [0,1]
    semantic_score: float
    keyword_score: float
    confidence: str       # "high" >0.5, "medium" >0.3, "low"
    chunk_count: int


def _ensure_first(scored: list[ScoredRepo], owner: str, name: str) -> list[ScoredRepo]:
    """Move owner/name to the front with score clamped to 1.0. Add if missing."""
    remaining = [r for r in scored if not (r.owner == owner and r.name == name)]
    existing = next((r for r in scored if r.owner == owner and r.name == name), None)
    if existing:
        existing.score = 1.0
        existing.confidence = "high"
        return [existing, *remaining]
    # Not in scored list — add a synthetic entry at the front
    pinned = ScoredRepo(
        owner=owner, name=name,
        score=1.0, semantic_score=1.0, keyword_score=1.0,
        confidence="high", chunk_count=0,
    )
    return [pinned, *remaining]


class RepoRouter:
    async def score_repos(
        self,
        query: str,
        query_vector: list[float],
        allowed_repos: list[str] | None = None,
        current_repo: tuple[str, str] | None = None,
    ) -> list[ScoredRepo]:
        """
        Score repos against a query and return the top cross_repo_max_repos.

        1. Load repo_summaries (Redis-cached).
        2. Filter to allowed_repos if scope is set.
        3. Score: combined = semantic_weight*cosine + keyword_weight*jaccard.
        4. Filter by cross_repo_min_score.
        5. Sort descending. If current_repo given, ensure it's first.
        6. Return top cross_repo_max_repos.
        """
        summaries = await self._load_summaries()

        # Scope gate — filter before scoring
        if allowed_repos is not None and len(allowed_repos) > 0:
            allowed_set = set(allowed_repos)
            summaries = [
                s for s in summaries
                if f"{s['repo_owner']}/{s['repo_name']}" in allowed_set
            ]

        query_vec = np.array(query_vector, dtype=np.float32)
        query_tokens = set(query.lower().split())
        scored: list[ScoredRepo] = []

        for s in summaries:
            if not s.get("centroid_embedding"):
                continue
            if (s.get("chunk_count") or 0) < settings.cross_repo_summary_update_min_chunks:
                continue

            centroid = np.array(s["centroid_embedding"], dtype=np.float32)
            norm_q = np.linalg.norm(query_vec)
            norm_c = np.linalg.norm(centroid)
            cos_sim = float(
                np.dot(query_vec, centroid) / (norm_q * norm_c + 1e-9)
            )
            cos_sim = max(0.0, cos_sim)

            kw_score = self._keyword_jaccard(query_tokens, s.get("tech_stack_keywords", []))
            combined = (
                settings.cross_repo_semantic_weight * cos_sim
                + settings.cross_repo_keyword_weight * kw_score
            )

            if combined < settings.cross_repo_min_score:
                continue

            confidence = "high" if combined > 0.5 else "medium" if combined > 0.3 else "low"
            scored.append(
                ScoredRepo(
                    owner=s["repo_owner"],
                    name=s["repo_name"],
                    score=combined,
                    semantic_score=cos_sim,
                    keyword_score=kw_score,
                    confidence=confidence,
                    chunk_count=s.get("chunk_count", 0),
                )
            )

        scored.sort(key=lambda r: r.score, reverse=True)

        if current_repo:
            owner, name = current_repo
            scored = _ensure_first(scored, owner, name)

        return scored[: settings.cross_repo_max_repos]

    def allocate_budgets(
        self,
        repos: list[ScoredRepo],
        total_budget: int,
    ) -> dict[tuple[str, str], int]:
        """
        Allocate token budget across repos.
        Floor: each repo gets max(500, total*0.10).
        Remaining distributed proportionally by score.
        """
        if not repos:
            return {}
        floor = max(500, int(total_budget * 0.10))
        remaining = max(0, total_budget - floor * len(repos))
        total_score = sum(r.score for r in repos) or 1.0
        return {
            (r.owner, r.name): floor + int(remaining * r.score / total_score)
            for r in repos
        }

    def _keyword_jaccard(self, query_tokens: set[str], keywords: list[str]) -> float:
        if not keywords:
            return 0.0
        kw_set = {k.lower() for k in keywords}
        inter = len(query_tokens & kw_set)
        union = len(query_tokens | kw_set)
        return inter / union if union else 0.0

    async def _load_summaries(self) -> list[dict]:
        cache_key = "repo_router:summaries"
        try:
            r = _get_redis()
            cached = await r.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        rows = await get_all_repo_summaries()

        try:
            r = _get_redis()
            await r.setex(cache_key, settings.cross_repo_router_cache_ttl, json.dumps(rows))
        except Exception:
            pass

        return rows
