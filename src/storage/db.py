"""
Async SQLAlchemy engine + all database query methods.
All public methods use async/await and are safe to call concurrently.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.storage.models import (
    ChatSession,
    ChatTurn,
    Chunk,
    MerkleNode,
    PlanHistoryEntry,
    Repo,
    Symbol,
    WebhookEvent,
)

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
                "start_line": stmt.excluded.start_line,
                "end_line": stmt.excluded.end_line,
                "raw_content": stmt.excluded.raw_content,
                "enriched_content": stmt.excluded.enriched_content,
                "token_count": stmt.excluded.token_count,
                "symbol_name": stmt.excluded.symbol_name,
                "symbol_kind": stmt.excluded.symbol_kind,
                "scope_chain": stmt.excluded.scope_chain,
                "file_path": stmt.excluded.file_path,
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
            select(func.count()).select_from(Chunk).where(
                Chunk.repo_owner == repo_owner,
                Chunk.repo_name == repo_name,
                Chunk.is_deleted.is_(False),
            )
        )
        return result.scalar() or 0


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


async def get_merkle_hash(file_path: str, repo_owner: str, repo_name: str) -> str | None:
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


async def upsert_merkle_node(
    file_path: str, repo_owner: str, repo_name: str, blob_sha: str
) -> None:
    """Store or update the blob SHA for a file."""
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(MerkleNode).values(
            file_path=file_path,
            repo_owner=repo_owner,
            repo_name=repo_name,
            blob_sha=blob_sha,
            last_indexed=datetime.now(UTC),
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
            .values(status=status, last_indexed=datetime.now(UTC))
        )
        await session.commit()


async def update_repo_webhook(owner: str, name: str, hook_id: int | None) -> None:
    """Store or clear the GitHub webhook hook ID for a repo."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Repo)
            .where(Repo.owner == owner, Repo.name == name)
            .values(webhook_hook_id=hook_id)
        )
        await session.commit()


async def delete_repo(owner: str, name: str) -> bool:
    """
    Permanently remove a repository and all its indexed data.
    Deletes chunks (hard), symbols, merkle nodes, and the repo row.
    Returns True if the repo existed, False if not found.
    """
    async with AsyncSessionLocal() as session:
        # Verify repo exists first
        result = await session.execute(select(Repo).where(Repo.owner == owner, Repo.name == name))
        if result.scalar_one_or_none() is None:
            return False

        # Hard-delete all data for this repo
        await session.execute(
            delete(Chunk).where(Chunk.repo_owner == owner, Chunk.repo_name == name)
        )
        await session.execute(
            delete(Symbol).where(Symbol.repo_owner == owner, Symbol.repo_name == name)
        )
        await session.execute(
            delete(MerkleNode).where(MerkleNode.repo_owner == owner, MerkleNode.repo_name == name)
        )
        await session.execute(delete(Repo).where(Repo.owner == owner, Repo.name == name))
        await session.commit()
        return True


async def get_repo_stats() -> list[dict[str, Any]]:
    """Per-repo breakdown: chunk counts, file counts, last indexed."""
    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    text("""
            SELECT
                r.owner,
                r.name,
                r.branch,
                r.status,
                r.registered_at,
                r.last_indexed,
                r.webhook_hook_id,
                COALESCE(c.active_chunks,  0) AS active_chunks,
                COALESCE(c.deleted_chunks, 0) AS deleted_chunks,
                COALESCE(c.files,          0) AS files,
                COALESCE(s.symbols,        0) AS symbols
            FROM repos r
            LEFT JOIN (
                SELECT
                    repo_owner, repo_name,
                    COUNT(*) FILTER (WHERE is_deleted = FALSE) AS active_chunks,
                    COUNT(*) FILTER (WHERE is_deleted = TRUE)  AS deleted_chunks,
                    COUNT(DISTINCT file_path)
                        FILTER (WHERE is_deleted = FALSE)      AS files
                FROM chunks
                GROUP BY repo_owner, repo_name
            ) c ON c.repo_owner = r.owner AND c.repo_name = r.name
            LEFT JOIN (
                SELECT repo_owner, repo_name, COUNT(*) AS symbols
                FROM symbols
                GROUP BY repo_owner, repo_name
            ) s ON s.repo_owner = r.owner AND s.repo_name = r.name
            ORDER BY r.registered_at DESC
        """)
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]


# ── Webhook event log ────────────────────────────────────────────────────────


async def log_webhook_event(
    delivery_id: str,
    event_type: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    commit_sha: str | None = None,
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


async def update_webhook_status(delivery_id: str, status: str, error: str | None = None) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.delivery_id == delivery_id)
            .values(
                status=status,
                error_message=error,
                processed_at=datetime.now(UTC),
            )
        )
        await session.commit()


# ── Health / stats ───────────────────────────────────────────────────────────


async def get_index_stats() -> dict[str, Any]:
    """Return a summary of the index for the health endpoint and dashboard."""
    async with AsyncSessionLocal() as session:
        chunk_count = (
            await session.execute(text("SELECT COUNT(*) FROM chunks WHERE is_deleted = FALSE"))
        ).scalar()
        symbol_count = (await session.execute(text("SELECT COUNT(*) FROM symbols"))).scalar()
        file_count = (await session.execute(text("SELECT COUNT(*) FROM merkle_nodes"))).scalar()
        repo_count = (await session.execute(text("SELECT COUNT(*) FROM repos"))).scalar()
        last_indexed = (
            await session.execute(text("SELECT MAX(last_indexed) FROM merkle_nodes"))
        ).scalar()

        return {
            "chunks": chunk_count,
            "symbols": symbol_count,
            "files": file_count,
            "repos": repo_count,
            "last_indexed": last_indexed.isoformat() if last_indexed else None,
        }


# ── Chat history ──────────────────────────────────────────────────────────────


async def ensure_chat_session(
    session_id: str,
    first_query: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> None:
    """Idempotent — creates the session row only if it doesn't exist yet."""
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(ChatSession).values(
            id=session_id,
            title=first_query[:120].strip(),
            repo_owner=repo_owner,
            repo_name=repo_name,
            turn_count=0,
        ).on_conflict_do_nothing(index_elements=["id"])
        await session.execute(stmt)
        await session.commit()


async def append_chat_turn(
    session_id: str,
    user_query: str,
    answer: str,
    cited_files: list[str] | None = None,
    follow_up_hints: list[str] | None = None,
    elapsed_ms: float | None = None,
    context_tokens: int | None = None,
    context_files: int | None = None,
    query_complexity: str | None = None,
) -> None:
    """Append one turn to a session, atomically incrementing turn_count."""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )).scalar_one_or_none()
        if not row:
            return  # session not found — skip silently
        turn_index = row.turn_count
        stmt = pg_insert(ChatTurn).values(
            session_id=session_id,
            turn_index=turn_index,
            user_query=user_query,
            answer=answer,
            cited_files=cited_files or [],
            follow_up_hints=follow_up_hints or [],
            elapsed_ms=elapsed_ms,
            context_tokens=context_tokens,
            context_files=context_files,
            query_complexity=query_complexity,
        ).on_conflict_do_nothing(constraint="chat_turns_session_id_turn_index_key")
        await session.execute(stmt)
        await session.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(turn_count=turn_index + 1, last_active_at=datetime.now(UTC))
        )
        await session.commit()


async def save_plan_history(
    plan_id: str,
    query: str,
    response_type: str,
    plan_json_str: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    elapsed_ms: float | None = None,
    context_tokens: int | None = None,
    web_research_used: bool = False,
) -> None:
    """Persist a completed plan. on_conflict_do_nothing makes it retry-safe."""
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(PlanHistoryEntry).values(
            plan_id=plan_id,
            query=query,
            response_type=response_type,
            plan_json=plan_json_str,
            repo_owner=repo_owner,
            repo_name=repo_name,
            elapsed_ms=elapsed_ms,
            context_tokens=context_tokens,
            web_research_used=web_research_used,
        ).on_conflict_do_nothing(index_elements=["plan_id"])
        await session.execute(stmt)
        await session.commit()


async def list_chat_sessions(
    limit: int = 20,
    offset: int = 0,
    repo_owner: str | None = None,
    repo_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return chat sessions ordered by last_active_at DESC."""
    async with AsyncSessionLocal() as session:
        q = select(ChatSession).order_by(ChatSession.last_active_at.desc())
        if repo_owner:
            q = q.where(ChatSession.repo_owner == repo_owner)
        if repo_name:
            q = q.where(ChatSession.repo_name == repo_name)
        q = q.limit(limit).offset(offset)
        rows = (await session.execute(q)).scalars().all()
        return [
            {
                "session_id": r.id,
                "title": r.title,
                "repo_owner": r.repo_owner,
                "repo_name": r.repo_name,
                "turn_count": r.turn_count,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_active_at": r.last_active_at.isoformat() if r.last_active_at else None,
            }
            for r in rows
        ]


async def get_chat_session_with_turns(session_id: str) -> dict[str, Any] | None:
    """Return a session and all its turns ordered by turn_index."""
    async with AsyncSessionLocal() as session:
        sess_row = (await session.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )).scalar_one_or_none()
        if not sess_row:
            return None
        turns = (await session.execute(
            select(ChatTurn)
            .where(ChatTurn.session_id == session_id)
            .order_by(ChatTurn.turn_index)
        )).scalars().all()
        return {
            "session_id": sess_row.id,
            "title": sess_row.title,
            "repo_owner": sess_row.repo_owner,
            "repo_name": sess_row.repo_name,
            "turn_count": sess_row.turn_count,
            "created_at": sess_row.created_at.isoformat() if sess_row.created_at else None,
            "last_active_at": sess_row.last_active_at.isoformat() if sess_row.last_active_at else None,
            "turns": [
                {
                    "turn_index": t.turn_index,
                    "user_query": t.user_query,
                    "answer": t.answer,
                    "cited_files": t.cited_files or [],
                    "follow_up_hints": t.follow_up_hints or [],
                    "elapsed_ms": t.elapsed_ms,
                    "context_tokens": t.context_tokens,
                    "context_files": t.context_files,
                    "query_complexity": t.query_complexity,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in turns
            ],
        }


async def list_plan_history(
    limit: int = 20,
    offset: int = 0,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    response_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return plan history entries ordered by created_at DESC."""
    async with AsyncSessionLocal() as session:
        q = select(PlanHistoryEntry).order_by(PlanHistoryEntry.created_at.desc())
        if repo_owner:
            q = q.where(PlanHistoryEntry.repo_owner == repo_owner)
        if repo_name:
            q = q.where(PlanHistoryEntry.repo_name == repo_name)
        if response_type:
            q = q.where(PlanHistoryEntry.response_type == response_type)
        q = q.limit(limit).offset(offset)
        rows = (await session.execute(q)).scalars().all()
        return [
            {
                "plan_id": r.plan_id,
                "query": r.query,
                "response_type": r.response_type,
                "repo_owner": r.repo_owner,
                "repo_name": r.repo_name,
                "elapsed_ms": r.elapsed_ms,
                "context_tokens": r.context_tokens,
                "web_research_used": r.web_research_used,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


async def get_plan_history_entry(plan_id: str) -> dict[str, Any] | None:
    """Return a single plan history entry with plan_json parsed back to dict."""
    import json as _json

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(PlanHistoryEntry).where(PlanHistoryEntry.plan_id == plan_id)
        )).scalar_one_or_none()
        if not row:
            return None
        try:
            plan_data = _json.loads(row.plan_json)
        except Exception:
            plan_data = {}
        return {
            "plan_id": row.plan_id,
            "query": row.query,
            "response_type": row.response_type,
            "repo_owner": row.repo_owner,
            "repo_name": row.repo_name,
            "elapsed_ms": row.elapsed_ms,
            "context_tokens": row.context_tokens,
            "web_research_used": row.web_research_used,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "plan": plan_data,
        }
