"""
SQLAlchemy ORM models matching the 001_init.sql schema.
"""
from __future__ import annotations

import datetime
from typing import List, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Integer,
    String,
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
    commit_author: Mapped[Optional[str]] = mapped_column(Text)
    commit_message: Mapped[Optional[str]] = mapped_column(Text)

    # Code structure
    language: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_name: Mapped[Optional[str]] = mapped_column(Text)
    symbol_kind: Mapped[Optional[str]] = mapped_column(Text)
    scope_chain: Mapped[Optional[str]] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)

    # Content
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    enriched_content: Mapped[str] = mapped_column(Text, nullable=False)
    imports: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text))
    token_count: Mapped[Optional[int]] = mapped_column(Integer)

    # Vector
    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(1536))

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
    signature: Mapped[Optional[str]] = mapped_column(Text)
    docstring: Mapped[Optional[str]] = mapped_column(Text)
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
    description: Mapped[Optional[str]] = mapped_column(Text)
    registered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_indexed: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="pending")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[Optional[str]] = mapped_column(Text, unique=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    repo_owner: Mapped[Optional[str]] = mapped_column(Text)
    repo_name: Mapped[Optional[str]] = mapped_column(Text)
    commit_sha: Mapped[Optional[str]] = mapped_column(Text)
    files_changed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(Text, default="queued")
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True))
