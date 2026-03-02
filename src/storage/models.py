"""
SQLAlchemy ORM models matching the 001_init.sql schema.
"""

from __future__ import annotations

import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Chunk(Base):
    __tablename__ = "chunks"

    # Identity
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    repo_owner: Mapped[str] = mapped_column(Text, nullable=False)
    repo_name: Mapped[str] = mapped_column(Text, nullable=False)
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    commit_author: Mapped[str | None] = mapped_column(Text)
    commit_message: Mapped[str | None] = mapped_column(Text)

    # Code structure
    language: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_name: Mapped[str | None] = mapped_column(Text)
    symbol_kind: Mapped[str | None] = mapped_column(Text)
    scope_chain: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_chunk_id: Mapped[str | None] = mapped_column(Text)

    # Content
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    enriched_content: Mapped[str] = mapped_column(Text, nullable=False)
    imports: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    token_count: Mapped[int | None] = mapped_column(Integer)

    # Vector
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))

    # Lifecycle
    indexed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    repo_owner: Mapped[str] = mapped_column(Text, nullable=False)
    repo_name: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)
    docstring: Mapped[str | None] = mapped_column(Text)
    is_exported: Mapped[bool] = mapped_column(Boolean, default=False)
    indexed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MerkleNode(Base):
    __tablename__ = "merkle_nodes"

    file_path: Mapped[str] = mapped_column(Text, primary_key=True)
    repo_owner: Mapped[str] = mapped_column(Text, primary_key=True)
    repo_name: Mapped[str] = mapped_column(Text, primary_key=True)
    blob_sha: Mapped[str] = mapped_column(Text, nullable=False)
    last_indexed: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (UniqueConstraint("owner", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(Text, default="main")
    description: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_indexed: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="pending")
    webhook_hook_id: Mapped[int | None] = mapped_column(Integer, default=None)
    source_type: Mapped[str] = mapped_column(Text, default="github")
    local_path: Mapped[str | None] = mapped_column(Text)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str | None] = mapped_column(Text, unique=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    repo_owner: Mapped[str | None] = mapped_column(Text)
    repo_name: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    files_changed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(Text, default="queued")
    error_message: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    repo_owner: Mapped[str | None] = mapped_column(Text)
    repo_name: Mapped[str | None] = mapped_column(Text)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_active_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (UniqueConstraint("session_id", "turn_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    cited_files: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    follow_up_hints: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    elapsed_ms: Mapped[float | None] = mapped_column(Float)
    context_tokens: Mapped[int | None] = mapped_column(Integer)
    context_files: Mapped[int | None] = mapped_column(Integer)
    query_complexity: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PlanHistoryEntry(Base):
    __tablename__ = "plan_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    response_type: Mapped[str] = mapped_column(Text, nullable=False)
    repo_owner: Mapped[str | None] = mapped_column(Text)
    repo_name: Mapped[str | None] = mapped_column(Text)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    elapsed_ms: Mapped[float | None] = mapped_column(Float)
    context_tokens: Mapped[int | None] = mapped_column(Integer)
    web_research_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
