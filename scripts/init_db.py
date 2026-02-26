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


async def run_migrations(conn) -> None:
    """Discover and execute all .sql migration files in order.

    Migration files MUST be idempotent (use IF NOT EXISTS / IF EXISTS guards)
    so they are safe to re-run on every init.
    """
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        print("  No migration files found — skipping.")
        return

    for mf in migration_files:
        sql = mf.read_text()
        print(f"  Applying migration: {mf.name} …")
        # Split on semicolons to handle multi-statement migrations
        for statement in sql.split(";"):
            stmt = statement.strip()
            if stmt:
                await conn.execute(text(stmt))
        print(f"  ✓ {mf.name} applied.")


async def main() -> None:
    print(f"Connecting to: {settings.database_url}")
    engine = create_async_engine(settings.database_url, echo=True)

    async with engine.begin() as conn:
        # Enable required extensions
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
        print("Extensions enabled: vector, pg_trgm")

        # Create all tables from ORM models (skips existing)
        await conn.run_sync(Base.metadata.create_all)
        print("ORM tables created (or already exist).")

        # Run SQL migration files (idempotent — safe to re-run)
        print("\nRunning SQL migrations …")
        await run_migrations(conn)

    await engine.dispose()

    # Verify tables and columns exist
    verify_engine = create_async_engine(settings.database_url)
    async with verify_engine.connect() as conn:
        # Check tables
        result = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;")
        )
        tables = [row[0] for row in result]
        print(f"\n✓ Tables found: {tables}")

        expected = {"chunks", "symbols", "merkle_nodes", "repos", "webhook_events"}
        missing = expected - set(tables)
        if missing:
            print(f"✗ Missing tables: {missing}")
            sys.exit(1)

        # Check that key migration columns exist (e.g. webhook_hook_id from 002)
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'repos' AND column_name = 'webhook_hook_id';"
            )
        )
        if result.fetchone():
            print("✓ repos.webhook_hook_id column present.")
        else:
            print("✗ repos.webhook_hook_id column missing — migration 002 may have failed.")
            sys.exit(1)

        print("✓ All required tables and columns present. DB init complete.")

    await verify_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
