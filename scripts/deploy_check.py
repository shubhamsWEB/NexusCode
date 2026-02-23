"""
Pre-deployment environment check.
Verifies all required env vars are set and services are reachable.

Usage:
    PYTHONPATH=. python scripts/deploy_check.py
    PYTHONPATH=. python scripts/deploy_check.py --full  # includes Voyage AI ping
"""

from __future__ import annotations

import os
import sys

REQUIRED = [
    ("GITHUB_TOKEN", "GitHub Personal Access Token (or use GITHUB_APP_ID)"),
    ("GITHUB_WEBHOOK_SECRET", "HMAC secret for webhook verification"),
    ("DATABASE_URL", "PostgreSQL connection string (postgresql+asyncpg://...)"),
    ("REDIS_URL", "Redis connection string (redis://...)"),
    ("VOYAGE_API_KEY", "Voyage AI API key for embeddings"),
    ("JWT_SECRET", "Secret for signing MCP auth tokens"),
]

OPTIONAL = [
    ("GITHUB_APP_ID", "GitHub App ID (alternative to GITHUB_TOKEN)"),
    ("GITHUB_APP_PRIVATE_KEY_PATH", "Path to GitHub App .pem key"),
    ("ANTHROPIC_API_KEY", "Anthropic API key (optional, for future tools)"),
    ("GITHUB_OAUTH_CLIENT_ID", "GitHub OAuth app client ID"),
    ("GITHUB_OAUTH_CLIENT_SECRET", "GitHub OAuth app client secret"),
    ("EMBEDDING_DIMENSIONS", "Embedding vector size (default: 1536)"),
]

_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_RESET = "\033[0m"
_BOLD = "\033[1m"


def ok(s: str) -> str:
    return f"{_GREEN}✓{_RESET} {s}"


def fail(s: str) -> str:
    return f"{_RED}✗{_RESET} {s}"


def warn(s: str) -> str:
    return f"{_YELLOW}~{_RESET} {s}"


def check_env() -> bool:
    print(f"\n{_BOLD}Required environment variables:{_RESET}")
    missing = []
    for var, desc in REQUIRED:
        val = os.environ.get(var, "")
        if val:
            masked = val[:6] + "***" if len(val) > 6 else "***"
            print(f"  {ok(var):40s} {masked}")
        else:
            print(f"  {fail(var):40s} MISSING — {desc}")
            missing.append(var)

    print(f"\n{_BOLD}Optional environment variables:{_RESET}")
    for var, desc in OPTIONAL:
        val = os.environ.get(var, "")
        if val:
            masked = val[:4] + "***" if len(val) > 4 else "***"
            print(f"  {ok(var):40s} {masked}")
        else:
            print(f"  {warn(var):40s} not set — {desc}")

    if missing:
        print(
            f"\n{_RED}✗ Missing {len(missing)} required variable(s): {', '.join(missing)}{_RESET}"
        )
        return False
    return True


def check_db() -> bool:
    import asyncio

    print(f"\n{_BOLD}Database connectivity:{_RESET}")
    try:
        from sqlalchemy import text

        from src.storage.db import AsyncSessionLocal

        async def _ping():
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("SELECT COUNT(*) FROM chunks WHERE is_deleted = FALSE")
                )
                return result.scalar()

        count = asyncio.run(_ping())
        print(f"  {ok('PostgreSQL'):40s} connected — {count:,} active chunks")
        return True
    except Exception as e:
        print(f"  {fail('PostgreSQL'):40s} {e}")
        return False


def check_redis() -> bool:
    print(f"\n{_BOLD}Redis connectivity:{_RESET}")
    try:
        import redis as redis_lib

        url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        r = redis_lib.from_url(url)
        r.ping()
        info = r.info("server")
        print(f"  {ok('Redis'):40s} connected — v{info.get('redis_version', '?')}")
        return True
    except Exception as e:
        print(f"  {fail('Redis'):40s} {e}")
        return False


def check_voyage() -> bool:
    print(f"\n{_BOLD}Voyage AI API:{_RESET}")
    key = os.environ.get("VOYAGE_API_KEY", "")
    if not key:
        print(f"  {fail('VOYAGE_API_KEY'):40s} not set")
        return False
    try:
        import voyageai

        client = voyageai.Client(api_key=key)
        result = client.embed(["test"], model="voyage-code-2", input_type="query")
        dims = len(result.embeddings[0])
        print(f"  {ok('voyage-code-2'):40s} reachable — {dims} dimensions")
        return True
    except Exception as e:
        print(f"  {fail('voyage-code-2'):40s} {e}")
        return False


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    print(f"{_BOLD}{'=' * 55}")
    print("  Codebase Intelligence — Deployment Check")
    print(f"{'=' * 55}{_RESET}")

    results = []
    results.append(check_env())
    results.append(check_db())
    results.append(check_redis())
    # Voyage check is slow — only run if explicitly requested
    if "--full" in sys.argv:
        results.append(check_voyage())

    all_ok = all(results)
    print(f"\n{'=' * 55}")
    if all_ok:
        print(f"{_GREEN}{_BOLD}✓ All checks passed — system is ready to deploy!{_RESET}")
    else:
        print(f"{_RED}{_BOLD}✗ Some checks failed — fix issues before deploying.{_RESET}")
    print(f"{'=' * 55}\n")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
