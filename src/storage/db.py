"""
Async SQLAlchemy engine + all database query methods.
All public methods use async/await and are safe to call concurrently.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.storage.models import Chunk, MerkleNode, Repo, Symbol, WebhookEvent

logger = logging.getLogger(__name__)

# ── Engine ───────────────────────────────────────────────────────────────────

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncSession:
    """Dependency-injectable session factory."""
    async with AsyncSessionLocal() as session:
        yield session


# ── Chunk operations ─────────────────────────────────────────────────────────

async def upsert_chunks(chunks: list[dict[str, Any]]) -> int:
    """
    Insert or update chunks.
    On conflict (same SHA-256 id): update commit metadata, embedding, and un-delete.
    Returns the number of rows inserted or updated.
    """
    if not chunks:
        return 0
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(Chunk).values(chunks)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "is_deleted": False,
                "commit_sha": stmt.excluded.commit_sha,
                "commit_author": stmt.excluded.commit_author,
                "commit_message": stmt.excluded.commit_message,
                "embedding": stmt.excluded.embedding,
                "indexed_at": stmt.excluded.indexed_at,
            },
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount


async def soft_delete_chunks(file_path: str, repo_owner: str, repo_name: str) -> int:
    """Mark all chunks for a given file as deleted (non-destructive)."""
    async with AsyncSessionLocal() as session:
        stmt = (
            update(Chunk)
            .where(
                Chunk.file_path == file_path,
                Chunk.repo_owner == repo_owner,
                Chunk.repo_name == repo_name,
                Chunk.is_deleted.is_(False),
            )
            .values(is_deleted=True)
        )
        result = await session.execute(stmt)
        await session.commit()
        logger.debug(
            "soft_delete_chunks",
            extra={"file": file_path, "repo": f"{repo_owner}/{repo_name}", "rows": result.rowcount},
        )
        return result.rowcount


async def get_chunk_count(repo_owner: str, repo_name: str) -> int:
    """Active (non-deleted) chunk count for a repo."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Chunk)
            .where(
                Chunk.repo_owner == repo_owner,
                Chunk.repo_name == repo_name,
                Chunk.is_deleted.is_(False),
            )
        )
        return len(result.scalars().all())


# ── Symbol operations ────────────────────────────────────────────────────────

async def upsert_symbols(symbols: list[dict[str, Any]]) -> int:
    """
    Upsert symbols — update in place if the id already exists.
    """
    if not symbols:
        return 0
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(Symbol).values(symbols)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "signature": stmt.excluded.signature,
                "docstring": stmt.excluded.docstring,
                "start_line": stmt.excluded.start_line,
                "end_line": stmt.excluded.end_line,
                "indexed_at": stmt.excluded.indexed_at,
            },
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount


async def delete_symbols_for_file(file_path: str, repo_owner: str, repo_name: str) -> int:
    """Hard-delete symbols for a file (re-indexed from scratch on update)."""
    async with AsyncSessionLocal() as session:
        stmt = delete(Symbol).where(
            Symbol.file_path == file_path,
            Symbol.repo_owner == repo_owner,
            Symbol.repo_name == repo_name,
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount


# ── Merkle node operations ───────────────────────────────────────────────────

async def get_merkle_hash(file_path: str, repo_owner: str, repo_name: str) -> Optional[str]:
    """Return the stored GitHub blob SHA for this file, or None if not indexed yet."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MerkleNode.blob_sha).where(
                MerkleNode.file_path == file_path,
                MerkleNode.repo_owner == repo_owner,
                MerkleNode.repo_name == repo_name,
            )
        )
        row = result.scalar_one_or_none()
        return row


async def upsert_merkle_node(file_path: str, repo_owner: str, repo_name: str, blob_sha: str) -> None:
    """Store or update the blob SHA for a file."""
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(MerkleNode).values(
            file_path=file_path,
            repo_owner=repo_owner,
            repo_name=repo_name,
            blob_sha=blob_sha,
            last_indexed=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["file_path", "repo_owner", "repo_name"],
            set_={"blob_sha": stmt.excluded.blob_sha, "last_indexed": stmt.excluded.last_indexed},
        )
        await session.execute(stmt)
        await session.commit()


async def delete_merkle_node(file_path: str, repo_owner: str, repo_name: str) -> None:
    """Remove the merkle node when a file is deleted from the repo."""
    async with AsyncSessionLocal() as session:
        stmt = delete(MerkleNode).where(
            MerkleNode.file_path == file_path,
            MerkleNode.repo_owner == repo_owner,
            MerkleNode.repo_name == repo_name,
        )
        await session.execute(stmt)
        await session.commit()


# ── Repo operations ──────────────────────────────────────────────────────────

async def register_repo(owner: str, name: str, branch: str = "main", description: str = "") -> Repo:
    """Register a new repository (idempotent)."""
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(Repo).values(
            owner=owner,
            name=name,
            branch=branch,
            description=description,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="repos_owner_name_key",
            set_={"branch": stmt.excluded.branch, "description": stmt.excluded.description},
        )
        await session.execute(stmt)
        await session.commit()
        result = await session.execute(select(Repo).where(Repo.owner == owner, Repo.name == name))
        return result.scalar_one()


async def get_repos() -> list[Repo]:
    """List all registered repositories."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Repo).order_by(Repo.registered_at.desc()))
        return list(result.scalars().all())


async def update_repo_status(owner: str, name: str, status: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Repo)
            .where(Repo.owner == owner, Repo.name == name)
            .values(status=status, last_indexed=datetime.now(timezone.utc))
        )
        await session.commit()


# ── Webhook event log ────────────────────────────────────────────────────────

async def log_webhook_event(
    delivery_id: str,
    event_type: str,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
    commit_sha: Optional[str] = None,
    files_changed: int = 0,
) -> None:
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(WebhookEvent).values(
            delivery_id=delivery_id,
            event_type=event_type,
            repo_owner=repo_owner,
            repo_name=repo_name,
            commit_sha=commit_sha,
            files_changed=files_changed,
            status="queued",
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["delivery_id"])
        await session.execute(stmt)
        await session.commit()


async def update_webhook_status(delivery_id: str, status: str, error: Optional[str] = None) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.delivery_id == delivery_id)
            .values(
                status=status,
                error_message=error,
                processed_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()


# ── Health / stats ───────────────────────────────────────────────────────────

async def get_index_stats() -> dict[str, Any]:
    """Return a summary of the index for the health endpoint and dashboard."""
    async with AsyncSessionLocal() as session:
        chunk_count = (
            await session.execute(
                text("SELECT COUNT(*) FROM chunks WHERE is_deleted = FALSE")
            )
        ).scalar()
        symbol_count = (await session.execute(text("SELECT COUNT(*) FROM symbols"))).scalar()
        file_count = (
            await session.execute(text("SELECT COUNT(*) FROM merkle_nodes"))
        ).scalar()
        repo_count = (await session.execute(text("SELECT COUNT(*) FROM repos"))).scalar()
        last_indexed = (
            await session.execute(
                text("SELECT MAX(last_indexed) FROM merkle_nodes")
            )
        ).scalar()

        return {
            "chunks": chunk_count,
            "symbols": symbol_count,
            "files": file_count,
            "repos": repo_count,
            "last_indexed": last_indexed.isoformat() if last_indexed else None,
        }
