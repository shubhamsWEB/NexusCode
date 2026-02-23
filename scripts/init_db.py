#!/usr/bin/env python3
"""
Run once to create all tables and extensions.
Usage: python scripts/init_db.py
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

    await engine.dispose()

    # Verify tables exist
    verify_engine = create_async_engine(settings.database_url)
    async with verify_engine.connect() as conn:
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
        else:
            print("✓ All required tables present. DB init complete.")

    await verify_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
