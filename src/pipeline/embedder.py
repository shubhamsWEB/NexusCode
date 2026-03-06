"""
Voyage AI embedding client (voyage-code-2).

Features:
  - Batched API calls (up to 128 chunks per request)
  - Cache-hit check: if chunk_id already exists in DB, skip the API call
  - Exponential backoff on rate limits (429) and transient errors
  - Returns only the (chunk_id → vector) pairs that were newly embedded
"""

from __future__ import annotations

import asyncio

import voyageai

from src.config import settings
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)

# voyage-code-2 limits (full account)
_MAX_BATCH_SIZE = 128
_MAX_TOKENS_PER_BATCH = 120_000

# Free-tier limits (no payment method): 3 RPM, 10K TPM
# We conservatively cap below 10K tokens/batch and wait 21s between calls
_FREE_TIER_MAX_TOKENS = 8_000
_FREE_TIER_RPM_DELAY = 21.0  # seconds between API calls

# Retry config
_MAX_RETRIES = 5
_BASE_BACKOFF = 2.0  # seconds

# Detected at runtime: True once we see the "payment method" message
_is_free_tier: bool = False


_voyage_client = None


def _make_client() -> voyageai.AsyncClient:
    global _voyage_client
    if _voyage_client is None:
        _voyage_client = voyageai.AsyncClient(api_key=settings.voyage_api_key)
    return _voyage_client


# ── Main public function ──────────────────────────────────────────────────────


async def embed_chunks(
    chunks: list,  # list[EnrichedChunk]
    existing_ids: set[str],  # chunk_ids already in the DB → skip embedding
) -> dict[str, list[float]]:
    """
    Embed a list of EnrichedChunks via the Voyage AI API.

    Returns a dict mapping chunk_id → embedding vector for every chunk
    that was NOT already in existing_ids (i.e. genuinely new content).

    Chunks whose chunk_id is in existing_ids are cache hits — the caller
    can reuse the existing DB embedding without re-calling the API.
    """
    # Split into new vs cache hits
    new_chunks = [c for c in chunks if c.chunk_id not in existing_ids]
    cache_hits = len(chunks) - len(new_chunks)

    if cache_hits:
        logger.info("embed_chunks: %d cache hits skipped, %d to embed", cache_hits, len(new_chunks))

    if not new_chunks:
        return {}

    # Build batches that respect the API token limit
    batches = _build_batches(new_chunks)
    logger.info("embed_chunks: %d chunks → %d batches", len(new_chunks), len(batches))

    embeddings: dict[str, list[float]] = {}
    client = _make_client()

    for i, batch in enumerate(batches):
        texts = [c.enriched_content for c in batch]
        ids = [c.chunk_id for c in batch]

        # On free tier, throttle to 3 RPM between batches
        if i > 0 and _is_free_tier:
            logger.info("Free-tier throttle: waiting %.0fs before next batch", _FREE_TIER_RPM_DELAY)
            await asyncio.sleep(_FREE_TIER_RPM_DELAY)

        vectors = await _embed_with_retry(client, texts, batch_num=i + 1, total=len(batches))

        for chunk_id, vector in zip(ids, vectors):
            embeddings[chunk_id] = vector

    return embeddings


# ── Batching ──────────────────────────────────────────────────────────────────


def _build_batches(chunks: list) -> list[list]:
    """
    Group chunks into batches respecting both count and token limits.
    Uses smaller limits when free-tier mode is detected.
    """
    from src.pipeline.chunker import count_tokens

    max_tok = _FREE_TIER_MAX_TOKENS if _is_free_tier else _MAX_TOKENS_PER_BATCH
    max_size = 32 if _is_free_tier else _MAX_BATCH_SIZE

    batches: list[list] = []
    current_batch: list = []
    current_tokens = 0

    for chunk in chunks:
        tok = count_tokens(chunk.enriched_content)

        if len(current_batch) >= max_size or current_tokens + tok > max_tok:
            if current_batch:
                batches.append(current_batch)
            current_batch = [chunk]
            current_tokens = tok
        else:
            current_batch.append(chunk)
            current_tokens += tok

    if current_batch:
        batches.append(current_batch)

    return batches


# ── API call with retry ───────────────────────────────────────────────────────


async def _embed_with_retry(
    client: voyageai.AsyncClient,
    texts: list[str],
    batch_num: int,
    total: int,
) -> list[list[float]]:
    """
    Call the Voyage AI embed API with exponential backoff on transient errors.
    Uses the native async client to keep the event loop free.
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.debug("Embedding batch %d/%d (%d texts)", batch_num, total, len(texts))

            result = await client.embed(
                texts,
                model=settings.embedding_model,
                input_type="document",
            )
            return result.embeddings

        except Exception as exc:
            err_str = str(exc).lower()
            is_payment = "payment method" in err_str or "billing" in err_str
            is_rate_limit = "rate limit" in err_str or "429" in err_str or is_payment
            is_transient = is_rate_limit or "timeout" in err_str or "503" in err_str

            # Detect free tier and rebuild smaller batches on next call
            if is_payment:
                global _is_free_tier
                if not _is_free_tier:
                    _is_free_tier = True
                    logger.warning(
                        "Voyage AI free tier detected (3 RPM / 10K TPM). "
                        "Switching to smaller batches with 21s delay. "
                        "Add a payment method at dashboard.voyageai.com to unlock full rate limits."
                    )
                # On free tier a payment error means we must wait and retry with
                # smaller batches — but since batches are already built we just
                # wait the RPM window before retrying this same batch
                backoff = _FREE_TIER_RPM_DELAY * attempt
                logger.warning(
                    "Free-tier rate limit on batch %d/%d, attempt %d — waiting %.0fs",
                    batch_num,
                    total,
                    attempt,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue

            if not is_transient or attempt == _MAX_RETRIES:
                logger.error(
                    "Embedding batch %d/%d failed after %d attempts: %s",
                    batch_num,
                    total,
                    attempt,
                    exc,
                )
                raise

            backoff = _BASE_BACKOFF * (2 ** (attempt - 1))
            logger.warning(
                "Embedding batch %d/%d attempt %d failed (%s) — retrying in %.1fs",
                batch_num,
                total,
                attempt,
                exc,
                backoff,
            )
            await asyncio.sleep(backoff)

    # Should never reach here
    raise RuntimeError(f"Embedding batch {batch_num} exhausted all retries")


# ── Utility: check existing IDs in DB ────────────────────────────────────────


async def get_existing_chunk_ids(chunk_ids: list[str]) -> set[str]:
    """
    Query the DB for which chunk_ids already have embeddings stored.
    Used to skip the Voyage API call for unchanged chunks.
    """
    from sqlalchemy import select

    from src.storage.db import AsyncSessionLocal
    from src.storage.models import Chunk

    if not chunk_ids:
        return set()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Chunk.id).where(
                Chunk.id.in_(chunk_ids),
                Chunk.is_deleted.is_(False),
            )
        )
        return {row[0] for row in result}
