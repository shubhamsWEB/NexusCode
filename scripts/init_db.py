#!/usr/bin/env python3
"""
Run once to create all tables, extensions, and apply SQL migrations.
Usage: PYTHONPATH=. python scripts/init_db.py
"""

import asyncio
import sys
from pathlib import Path

# Make sure src/ is importable when running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.config import settings
from src.storage.models import Base

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "storage" / "migrations"


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL file into individual statements.

    Correctly handles:
    - Dollar-quoted blocks: DO $$ BEGIN...END $$ and DO $tag$ BEGIN...END $tag$
    - Single-quoted strings with escaped quotes ('')
    - Line comments (--)
    """
    statements: list[str] = []
    buf: list[str] = []
    pos = 0
    in_single_quote = False
    dollar_tag: str | None = None

    while pos < len(sql):
        c = sql[pos]

        if in_single_quote:
            buf.append(c)
            if c == "'" and pos + 1 < len(sql) and sql[pos + 1] == "'":
                buf.append(sql[pos + 1])  # escaped ''
                pos += 2
            elif c == "'":
                in_single_quote = False
                pos += 1
            else:
                pos += 1
            continue

        if dollar_tag is not None:
            if sql[pos : pos + len(dollar_tag)] == dollar_tag:
                buf.append(dollar_tag)
                pos += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(c)
                pos += 1
            continue

        if c == "'":
            in_single_quote = True
            buf.append(c)
            pos += 1
            continue

        if c == "$":
            end = pos + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            if end < len(sql) and sql[end] == "$":
                tag = sql[pos : end + 1]
                dollar_tag = tag
                buf.append(tag)
                pos = end + 1
                continue

        if c == "-" and pos + 1 < len(sql) and sql[pos + 1] == "-":
            newline = sql.find("\n", pos)
            line = sql[pos : newline + 1] if newline != -1 else sql[pos:]
            buf.append(line)
            pos = newline + 1 if newline != -1 else len(sql)
            continue

        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            pos += 1
            continue

        buf.append(c)
        pos += 1

    remaining = "".join(buf).strip()
    if remaining:
        statements.append(remaining)

    return [s for s in statements if s.strip()]


async def run_migrations(conn) -> int:
    """Discover and execute all .sql migration files in order.

    Migration files MUST be idempotent (use IF NOT EXISTS / IF EXISTS guards)
    so they are safe to re-run on every init.

    Returns the number of migration files applied.
    """
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        print("  No migration files found — skipping.")
        return 0

    for mf in migration_files:
        sql = mf.read_text()
        for stmt in _split_sql_statements(sql):
            await conn.execute(text(stmt))
    return len(migration_files)


async def main() -> None:
    print(f"DB init starting …")
    engine = create_async_engine(settings.database_url, echo=False)

    async with engine.begin() as conn:
        # Enable required extensions
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))

        # Create all tables from ORM models (skips existing)
        await conn.run_sync(Base.metadata.create_all)

        await conn.execute(text("SET maintenance_work_mem = '256MB';"))
        # Run SQL migration files (idempotent — safe to re-run)
        n = await run_migrations(conn)
        print(f"DB init complete — {n} migration(s) applied.")

    await engine.dispose()

    # Verify key tables and columns exist
    verify_engine = create_async_engine(settings.database_url, echo=False)
    async with verify_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;")
        )
        tables = {row[0] for row in result}

        expected = {"chunks", "symbols", "merkle_nodes", "repos", "webhook_events", "chat_sessions", "chat_turns", "plan_history"}
        missing = expected - tables
        if missing:
            print(f"✗ Missing tables: {missing}")
            sys.exit(1)

        # Spot-check key migration columns
        checks = [
            ("repos", "webhook_hook_id", "002"),
            ("chat_sessions", "turn_count", "003"),
        ]
        for table, column, migration in checks:
            result = await conn.execute(
                text("SELECT 1 FROM information_schema.columns WHERE table_name=:t AND column_name=:c"),
                {"t": table, "c": column},
            )
            if not result.fetchone():
                print(f"✗ {table}.{column} missing — migration {migration} may have failed.")
                sys.exit(1)

        print(f"✓ Schema verified ({len(tables)} tables present).")

    await verify_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
