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
        # Execute the whole file as one string — asyncpg handles multi-statement
        # SQL natively, and this correctly handles dollar-quoted blocks (DO $ … $)
        # which the naive split(";") approach breaks.
        await conn.exec_driver_sql(sql)
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
