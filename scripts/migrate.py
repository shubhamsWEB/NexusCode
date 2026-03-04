"""
Railway pre-deploy migration runner.

Executes SQL migration files from src/storage/migrations/ in sorted order,
tracking applied migrations in a `_migrations` table to ensure idempotency.

Usage (called automatically via railway.toml preDeployCommand):
    python scripts/migrate.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg2


def _parse_database_url(url: str) -> str:
    """Convert DATABASE_URL to a psycopg2-compatible DSN.

    Railway and the app may use asyncpg:// or postgresql+asyncpg:// prefixes,
    but psycopg2 needs a plain postgresql:// or postgres:// prefix.
    """
    # Strip SQLAlchemy dialect prefixes: postgresql+asyncpg:// -> postgresql://
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    return url


def _ensure_migrations_table(conn) -> None:
    """Create the _migrations tracking table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                filename    TEXT PRIMARY KEY,
                applied_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
    conn.commit()


def _get_applied(conn) -> set[str]:
    """Return set of already-applied migration filenames."""
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM _migrations ORDER BY filename;")
        return {row[0] for row in cur.fetchall()}


def _apply_migration(conn, filepath: Path) -> None:
    """Execute a single migration file and record it."""
    sql = filepath.read_text(encoding="utf-8")
    filename = filepath.name
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO _migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING;",
            (filename,),
        )
    conn.commit()


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    dsn = _parse_database_url(database_url)
    migrations_dir = Path(__file__).resolve().parent.parent / "src" / "storage" / "migrations"

    if not migrations_dir.is_dir():
        print(f"WARNING: Migrations directory not found: {migrations_dir}", file=sys.stderr)
        sys.exit(0)

    # Collect migration files sorted by filename (001_, 002_, ...)
    migration_files = sorted(migrations_dir.glob("*.sql"))
    if not migration_files:
        print("No migration files found. Nothing to do.")
        return

    print(f"Connecting to database...")
    conn = psycopg2.connect(dsn)

    try:
        _ensure_migrations_table(conn)
        applied = _get_applied(conn)

        pending = [f for f in migration_files if f.name not in applied]
        if not pending:
            print(f"All {len(applied)} migrations already applied. Nothing to do.")
            return

        print(f"Found {len(pending)} pending migration(s) ({len(applied)} already applied).")

        for filepath in pending:
            print(f"  Applying {filepath.name} ... ", end="", flush=True)
            try:
                _apply_migration(conn, filepath)
                print("OK")
            except Exception as e:
                conn.rollback()
                print(f"FAILED")
                print(f"  Error: {e}", file=sys.stderr)
                sys.exit(1)

        print(f"All migrations applied successfully.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
