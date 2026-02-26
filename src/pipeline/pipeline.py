"""
Indexing pipeline orchestrator.

This is the RQ worker entry point — called for every indexing job.
Runs the full pipeline: fetch → parse → chunk → enrich → embed → store.

Job payload schema:
{
    "repo_owner":     str,
    "repo_name":      str,
    "commit_sha":     str,
    "commit_author":  str,
    "commit_message": str,
    "files_to_upsert": list[str],   # paths to index / re-index
    "files_to_delete": list[str],   # paths to soft-delete
    "delivery_id":    str,          # for logging / audit
}
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ── RQ entry point (synchronous wrapper) ─────────────────────────────────────


def run_incremental_index(job_payload: dict[str, Any]) -> dict[str, Any]:
    """
    Synchronous wrapper so RQ can call this function directly.
    Internally runs the async pipeline via asyncio.run().
    """
    return asyncio.run(_async_incremental_index(job_payload))


# ── Async pipeline ────────────────────────────────────────────────────────────


async def _async_incremental_index(payload: dict[str, Any]) -> dict[str, Any]:
    owner = payload["repo_owner"]
    repo = payload["repo_name"]
    commit_sha = payload["commit_sha"]
    commit_author = payload.get("commit_author", "")
    commit_message = payload.get("commit_message", "")
    files_to_upsert = payload.get("files_to_upsert", [])
    files_to_delete = payload.get("files_to_delete", [])
    delivery_id = payload.get("delivery_id", "manual")

    log = logger.bind(repo=f"{owner}/{repo}", commit=commit_sha[:7], delivery=delivery_id)
    log.info("pipeline.start", upsert=len(files_to_upsert), delete=len(files_to_delete))

    start_time = time.monotonic()
    stats = {
        "files_processed": 0,
        "files_skipped_merkle": 0,
        "files_deleted": 0,
        "chunks_upserted": 0,
        "symbols_upserted": 0,
        "embed_cache_hits": 0,
        "errors": 0,
    }

    # ── Step 1: Handle deletions ──────────────────────────────────────────────
    if files_to_delete:
        await _handle_deletions(owner, repo, files_to_delete, stats, log)

    # ── Step 2: Upsert files ──────────────────────────────────────────────────
    if files_to_upsert:
        await _handle_upserts(
            owner,
            repo,
            commit_sha,
            commit_author,
            commit_message,
            files_to_upsert,
            stats,
            log,
        )

    elapsed = time.monotonic() - start_time
    log.info("pipeline.done", elapsed_s=round(elapsed, 2), **stats)

    # Update repo status → "ready" (or "error" if every file failed)
    from src.storage.db import update_repo_status

    repo_final_status = (
        "error"
        if stats["errors"] > 0
        and stats["files_processed"] == 0
        and stats["files_skipped_merkle"] == 0
        else "ready"
    )
    await update_repo_status(owner, repo, repo_final_status)

    # Update webhook event status to "done" (or "error" if all files errored)
    if delivery_id and delivery_id != "manual":
        from src.storage.db import update_webhook_status

        webhook_final = "error" if stats["errors"] > 0 and stats["files_processed"] == 0 else "done"
        err_msg = f"{stats['errors']} error(s)" if stats["errors"] else None
        await update_webhook_status(delivery_id, webhook_final, error=err_msg)

    return stats


# ── Deletion handler ──────────────────────────────────────────────────────────


async def _handle_deletions(
    owner: str,
    repo: str,
    paths: list[str],
    stats: dict,
    log,
) -> None:
    from src.storage.db import (
        delete_merkle_node,
        delete_symbols_for_file,
        soft_delete_chunks,
    )

    for path in paths:
        try:
            await soft_delete_chunks(path, owner, repo)
            await delete_symbols_for_file(path, owner, repo)
            await delete_merkle_node(path, owner, repo)
            stats["files_deleted"] += 1
            log.debug("pipeline.deleted", path=path)
        except Exception as exc:
            log.error("pipeline.delete_error", path=path, error=str(exc))
            stats["errors"] += 1


# ── Upsert handler ────────────────────────────────────────────────────────────


async def _handle_upserts(
    owner: str,
    repo: str,
    commit_sha: str,
    commit_author: str,
    commit_message: str,
    paths: list[str],
    stats: dict,
    log,
) -> None:
    from src.github.fetcher import fetch_file
    from src.pipeline.chunker import chunk_file
    from src.pipeline.embedder import embed_chunks, get_existing_chunk_ids
    from src.pipeline.enricher import enrich_chunks
    from src.pipeline.parser import parse_file
    from src.storage.db import (
        delete_symbols_for_file,
        get_merkle_hash,
        soft_delete_chunks,
        upsert_chunks,
        upsert_merkle_node,
        upsert_symbols,
    )

    all_enriched = []  # accumulate across files before batch-embedding
    file_meta = []  # parallel list: (path, blob_sha, symbols, enriched_slice_range)

    # ── Fetch + parse + chunk + enrich (concurrent with semaphore) ────────
    _sem = asyncio.Semaphore(5)  # max 5 concurrent GitHub API calls

    async def _process_file(path: str) -> dict | None:
        async with _sem:
            try:
                result = await fetch_file(owner, repo, path, ref=commit_sha)
                if result is None:
                    log.debug("pipeline.fetch_none", path=path)
                    return None

                content, blob_sha = result

                stored_sha = await get_merkle_hash(path, owner, repo)
                if stored_sha == blob_sha:
                    log.debug("pipeline.merkle_hit", path=path)
                    return {"type": "merkle_skip"}

                parsed = parse_file(path, content)
                if parsed is None:
                    return None

                chunks = chunk_file(parsed)
                enriched = enrich_chunks(chunks)

                if not enriched:
                    return None

                return {
                    "type": "success",
                    "path": path,
                    "blob_sha": blob_sha,
                    "symbols": parsed.all_symbols,
                    "enriched": enriched,
                }
            except Exception as exc:
                log.error("pipeline.parse_error", path=path, error=str(exc))
                return {"type": "error"}

    results = await asyncio.gather(*[_process_file(p) for p in paths])

    for result in results:
        if result is None:
            continue
        if result["type"] == "merkle_skip":
            stats["files_skipped_merkle"] += 1
        elif result["type"] == "error":
            stats["errors"] += 1
        elif result["type"] == "success":
            all_enriched.extend(result["enriched"])
            file_meta.append(result)
            stats["files_processed"] += 1

    if not all_enriched:
        return

    # ── Batch embed (one API call block for all files) ───────────────────────
    all_ids = [c.chunk_id for c in all_enriched]
    existing_ids = await get_existing_chunk_ids(all_ids)
    stats["embed_cache_hits"] += len(existing_ids)

    try:
        id_to_vector = await embed_chunks(all_enriched, existing_ids)
    except Exception as exc:
        log.error("pipeline.embed_error", error=str(exc))
        stats["errors"] += 1
        return

    # ── Store results per file ────────────────────────────────────────────────
    for meta in file_meta:
        path = meta["path"]
        blob_sha = meta["blob_sha"]
        enriched_for_file = meta["enriched"]
        symbols_for_file = meta["symbols"]

        try:
            # Soft-delete old chunks for this file
            await soft_delete_chunks(path, owner, repo)
            await delete_symbols_for_file(path, owner, repo)

            # Build chunk rows
            chunk_rows = []
            for ec in enriched_for_file:
                vector = id_to_vector.get(ec.chunk_id)
                if vector is None and ec.chunk_id not in existing_ids:
                    # Embedding failed for this chunk — skip it
                    continue
                chunk_rows.append(
                    {
                        "id": ec.chunk_id,
                        "file_path": path,
                        "repo_owner": owner,
                        "repo_name": repo,
                        "commit_sha": commit_sha,
                        "commit_author": commit_author,
                        "commit_message": commit_message,
                        "language": ec.language,
                        "symbol_name": ec.symbol_name,
                        "symbol_kind": ec.symbol_kind,
                        "scope_chain": ec.scope_chain,
                        "start_line": ec.start_line,
                        "end_line": ec.end_line,
                        "raw_content": ec.raw_content,
                        "enriched_content": ec.enriched_content,
                        "imports": ec.imports,
                        "token_count": ec.token_count,
                        "embedding": vector,
                        "is_deleted": False,
                    }
                )

            if chunk_rows:
                await upsert_chunks(chunk_rows)
                stats["chunks_upserted"] += len(chunk_rows)

            # Build symbol rows
            symbol_rows = []
            for sym in symbols_for_file:
                symbol_rows.append(
                    {
                        "id": f"{path}:{sym.qualified_name}",
                        "name": sym.name,
                        "qualified_name": sym.qualified_name,
                        "kind": sym.kind,
                        "file_path": path,
                        "repo_owner": owner,
                        "repo_name": repo,
                        "start_line": sym.start_line,
                        "end_line": sym.end_line,
                        "signature": sym.signature,
                        "docstring": sym.docstring,
                        "is_exported": sym.is_exported,
                    }
                )

            if symbol_rows:
                await upsert_symbols(symbol_rows)
                stats["symbols_upserted"] += len(symbol_rows)

            # Update merkle hash
            await upsert_merkle_node(path, owner, repo, blob_sha)
            log.debug("pipeline.file_done", path=path, chunks=len(chunk_rows))

        except Exception as exc:
            log.error("pipeline.store_error", path=path, error=str(exc))
            stats["errors"] += 1
