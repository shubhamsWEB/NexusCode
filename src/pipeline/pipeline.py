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

from src.config import settings
from src.utils.sanitize import sanitize_log

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

    log = logger.bind(
        repo=sanitize_log(f"{owner}/{repo}"),
        commit=sanitize_log(commit_sha[:7]),
        delivery=sanitize_log(delivery_id),
    )
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
    all_parsed_files: list = []
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
            all_parsed_files=all_parsed_files,
        )

    # ── Step 3: Update knowledge graph edges (best-effort, non-fatal) ─────────
    if all_parsed_files or files_to_delete:
        try:
            from src.graph.builder import build_calls_from_parsed, build_file_graph

            await build_calls_from_parsed(owner, repo, all_parsed_files)
            await build_file_graph(owner, repo)
        except Exception as exc:
            log.warning("graph build failed (non-fatal)", error=str(exc))

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

    # Invalidate stale search result cache for this repo
    try:
        from src.retrieval.embed_cache import invalidate_search_cache

        await invalidate_search_cache(owner, repo)
    except Exception:
        pass

    # Update repo summary for cross-repo routing (non-blocking)
    asyncio.create_task(_update_repo_summary(owner, repo))

    # Update webhook event status to "done" (or "error" if all files errored)
    if delivery_id and delivery_id != "manual":
        from src.storage.db import update_webhook_status

        webhook_final = "error" if stats["errors"] > 0 and stats["files_processed"] == 0 else "done"
        err_msg = f"{stats['errors']} error(s)" if stats["errors"] else None
        await update_webhook_status(delivery_id, webhook_final, error=err_msg)

    return stats


# ── Repo summary helper ───────────────────────────────────────────────────────


async def _update_repo_summary(owner: str, name: str) -> None:
    """Compute and store repo centroid for cross-repo routing. Non-blocking helper."""
    try:
        from src.storage.db import compute_repo_centroid, upsert_repo_summary

        summary = await compute_repo_centroid(owner, name)
        if summary and summary["chunk_count"] >= settings.cross_repo_summary_update_min_chunks:
            await upsert_repo_summary(
                repo_owner=owner,
                repo_name=name,
                centroid_embedding=summary["centroid"],
                tech_stack_keywords=summary["keywords"],
                language_distribution=summary["language_dist"],
                chunk_count=summary["chunk_count"],
            )
            # Invalidate router cache so next query gets fresh centroids
            try:
                import redis.asyncio as redis_async

                r = redis_async.from_url(settings.redis_url)
                await r.delete("repo_router:summaries")
            except Exception:
                pass
            logger.info(
                "repo_summary.updated",
                repo=f"{owner}/{name}",
                chunks=summary["chunk_count"],
            )
    except Exception as exc:
        logger.warning(
            "repo_summary.update_failed",
            repo=f"{owner}/{name}",
            error=str(exc),
        )


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
    all_parsed_files: list | None = None,
) -> None:
    from src.github.fetcher import fetch_file
    from src.pipeline.chunker import chunk_file
    from src.pipeline.embedder import embed_chunks, get_existing_chunk_ids
    from src.pipeline.enricher import enrich_chunks, link_parent_chunks
    from src.pipeline.parser import parse_file
    from src.storage.db import (
        delete_symbols_for_file,
        get_merkle_hash,
        restore_chunk_ids,
        soft_delete_stale_chunks,
        upsert_chunks,
        upsert_merkle_node,
        upsert_symbols,
    )

    BATCH_SIZE = 50  # Process files in batches for rate-limit resilience + progress
    _sem = asyncio.Semaphore(settings.github_api_concurrency)

    # ── Pre-filter: bulk blob-SHA check avoids content fetches for unchanged files ──
    from src.github.fetcher import fetch_blob_shas_bulk
    from src.storage.db import batch_get_merkle_hashes

    tree_shas = await fetch_blob_shas_bulk(owner, repo, commit_sha, paths)
    stored_shas = await batch_get_merkle_hashes(paths, owner, repo)

    pre_filtered: list[str] = []
    for p in paths:
        tree_sha = tree_shas.get(p)
        if tree_sha and stored_shas.get(p) == tree_sha:
            stats["files_skipped_merkle"] += 1
            log.debug("pipeline.merkle_pre_skip", path=p)
        else:
            pre_filtered.append(p)

    paths = pre_filtered
    total_files = len(paths)

    async def _process_file(path: str, known_blob_sha: str | None = None) -> dict | None:
        async with _sem:
            try:
                result = await fetch_file(owner, repo, path, ref=commit_sha)
                if result is None:
                    log.debug("pipeline.fetch_none", path=path)
                    return None

                content, blob_sha = result

                if known_blob_sha is not None:
                    # Pre-filter confirmed this file changed — skip per-file DB query
                    pass
                else:
                    stored_sha = await get_merkle_hash(path, owner, repo)
                    if stored_sha == blob_sha:
                        log.debug("pipeline.merkle_hit", path=path)
                        return {"type": "merkle_skip"}

                parsed = parse_file(path, content)
                if parsed is None:
                    return None

                chunks = chunk_file(parsed)

                from src.pipeline.summarizer import generate_file_summary

                summary_chunk = await generate_file_summary(path, content)
                if summary_chunk:
                    chunks.insert(0, summary_chunk)

                enriched = enrich_chunks(chunks)
                enriched = link_parent_chunks(enriched)

                if not enriched:
                    return None

                return {
                    "type": "success",
                    "path": path,
                    "blob_sha": blob_sha,
                    "symbols": parsed.all_symbols,
                    "enriched": enriched,
                    "parsed_file": parsed,
                }
            except Exception as exc:
                log.error("pipeline.parse_error", path=path, error=str(exc))
                return {"type": "error"}

    total_batches = (total_files + BATCH_SIZE - 1) // BATCH_SIZE
    files_done = 0

    for batch_idx in range(0, total_files, BATCH_SIZE):
        batch_num = batch_idx // BATCH_SIZE + 1
        batch_paths = paths[batch_idx : batch_idx + BATCH_SIZE]
        log.info(
            "pipeline.batch_start",
            batch=f"{batch_num}/{total_batches}",
            files=f"{files_done}/{total_files}",
            batch_size=len(batch_paths),
        )

        # ── Fetch + parse + chunk + enrich this batch ────────────────────
        results = await asyncio.gather(*[_process_file(p, tree_shas.get(p)) for p in batch_paths])

        batch_enriched = []
        batch_meta = []

        for result in results:
            if result is None:
                continue
            if result["type"] == "merkle_skip":
                stats["files_skipped_merkle"] += 1
            elif result["type"] == "error":
                stats["errors"] += 1
            elif result["type"] == "success":
                batch_enriched.extend(result["enriched"])
                batch_meta.append(result)
                stats["files_processed"] += 1
                if all_parsed_files is not None and result.get("parsed_file"):
                    all_parsed_files.append(result["parsed_file"])

        files_done += len(batch_paths)

        if not batch_enriched:
            log.info(
                "pipeline.batch_skip",
                batch=f"{batch_num}/{total_batches}",
                reason="no enriched chunks",
            )
            continue

        # ── Batch embed ──────────────────────────────────────────────────
        all_ids = [c.chunk_id for c in batch_enriched]
        existing_ids = await get_existing_chunk_ids(all_ids)
        stats["embed_cache_hits"] += len(existing_ids)

        try:
            id_to_vector = await embed_chunks(batch_enriched, existing_ids)
        except Exception as exc:
            log.error("pipeline.embed_error", batch=batch_num, error=str(exc))
            stats["errors"] += 1
            continue

        # ── Store results per file in this batch ─────────────────────────
        for meta in batch_meta:
            path = meta["path"]
            blob_sha = meta["blob_sha"]
            enriched_for_file = meta["enriched"]
            symbols_for_file = meta["symbols"]

            try:
                # Build chunk rows — separate new chunks from cache hits
                chunk_rows = []
                cache_hit_ids = []
                for ec in enriched_for_file:
                    vector = id_to_vector.get(ec.chunk_id)
                    if vector is None:
                        if ec.chunk_id in existing_ids:
                            # Cache hit: chunk already in DB with valid embedding.
                            # Keep it active by including in cache_hit_ids.
                            cache_hit_ids.append(ec.chunk_id)
                        # else: embedding genuinely failed — skip
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
                            "parent_chunk_id": ec.parent_chunk_id,
                            "embedding": vector,
                            "is_deleted": False,
                        }
                    )

                # Step 1: upsert new chunks BEFORE deleting anything.
                # If this fails, old chunks remain active (no data loss).
                if chunk_rows:
                    await upsert_chunks(chunk_rows)
                    stats["chunks_upserted"] += len(chunk_rows)

                # Step 2: ensure cache-hit chunks are active (they may have
                # been soft-deleted by a prior failed run).
                if cache_hit_ids:
                    await restore_chunk_ids(cache_hit_ids)
                    stats["chunks_upserted"] += len(cache_hit_ids)

                # Step 3: NOW remove stale chunks — only IDs not in the new set.
                # This is safe because new/restored chunks are already active above.
                active_ids = {row["id"] for row in chunk_rows} | set(cache_hit_ids)
                await soft_delete_stale_chunks(path, owner, repo, keep_ids=active_ids)
                await delete_symbols_for_file(path, owner, repo)

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

                # Update merkle hash only when we actually stored content.
                # Skipping this when active_ids is empty prevents the file
                # from being permanently excluded by future merkle checks.
                if active_ids:
                    await upsert_merkle_node(path, owner, repo, blob_sha)
                log.debug("pipeline.file_done", path=path, chunks=len(chunk_rows))

            except Exception as exc:
                log.error("pipeline.store_error", path=path, error=str(exc))
                stats["errors"] += 1

        log.info(
            "pipeline.batch_done",
            batch=f"{batch_num}/{total_batches}",
            progress=f"{files_done}/{total_files}",
            chunks=stats["chunks_upserted"],
            errors=stats["errors"],
        )
