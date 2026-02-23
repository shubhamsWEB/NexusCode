#!/usr/bin/env python3
"""
CLI tool to test search queries against the indexed codebase.
Embeds the query with voyage-code-2 and runs a pgvector cosine similarity search.

Usage:
    python scripts/test_query.py "authentication logic"
    python scripts/test_query.py "JWT token validation" --top-k 10
    python scripts/test_query.py "payment processing" --repo myorg/my-backend
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import logging

logging.basicConfig(level=logging.WARNING)

from sqlalchemy import text

from src.config import settings
from src.storage.db import AsyncSessionLocal, get_index_stats


async def embed_query(query: str) -> list[float]:
    """Embed the query string using voyage-code-2."""
    import voyageai

    client = voyageai.Client(api_key=settings.voyage_api_key)
    result = client.embed([query], model=settings.embedding_model, input_type="query")
    return result.embeddings[0]


async def search(
    query: str,
    top_k: int = 5,
    repo: str | None = None,
    language: str | None = None,
) -> list[dict]:
    """Run a vector similarity search and return top-k results."""
    vector = await embed_query(query)
    vector_str = "[" + ",".join(str(v) for v in vector) + "]"

    filters = ["is_deleted = FALSE"]
    if repo:
        parts = repo.split("/", 1)
        if len(parts) == 2:
            filters.append(f"repo_owner = '{parts[0]}' AND repo_name = '{parts[1]}'")
    if language:
        filters.append(f"language = '{language}'")

    where_clause = " AND ".join(filters)

    sql = text(f"""
        SELECT
            id,
            file_path,
            repo_owner,
            repo_name,
            symbol_name,
            symbol_kind,
            scope_chain,
            start_line,
            end_line,
            language,
            raw_content,
            commit_sha,
            commit_author,
            1 - (embedding <=> '{vector_str}'::vector) AS score
        FROM chunks
        WHERE {where_clause}
          AND embedding IS NOT NULL
        ORDER BY embedding <=> '{vector_str}'::vector
        LIMIT {top_k}
    """)

    async with AsyncSessionLocal() as session:
        result = await session.execute(sql)
        rows = result.mappings().all()
        return [dict(r) for r in rows]


def _print_results(results: list[dict], query: str) -> None:
    print(f'\nQuery: "{query}"')
    print(f"Results: {len(results)}\n")

    for i, r in enumerate(results, 1):
        score = r.get("score", 0)
        sym = r.get("symbol_name") or "<module-level>"
        loc = f"{r['file_path']}:{r['start_line']}-{r['end_line']}"
        repo = f"{r['repo_owner']}/{r['repo_name']}"
        lang = r.get("language", "")
        author = r.get("commit_author", "")
        commit = (r.get("commit_sha") or "")[:7]

        print(f"  [{i}] score={score:.4f}  [{lang}]  {sym}")
        print(f"       {loc}  ({repo})")
        if author:
            print(f"       last changed by {author} @ {commit}")

        # Print a short preview of the code
        content = r.get("raw_content", "")
        preview_lines = content.splitlines()[:6]
        preview = "\n       ".join(preview_lines)
        print("       ┌─────")
        print(f"       {preview}")
        if len(content.splitlines()) > 6:
            print(f"       ... ({len(content.splitlines())} lines total)")
        print("       └─────\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test search queries against the index")
    parser.add_argument("query", help="Natural language or identifier query")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results (default 5)")
    parser.add_argument("--repo", help="Scope to a specific repo: owner/name")
    parser.add_argument("--language", help="Filter by language: python, typescript, etc.")
    parser.add_argument("--stats", action="store_true", help="Print index stats and exit")
    args = parser.parse_args()

    # Print stats if requested
    if args.stats:
        stats = await get_index_stats()
        print("\nIndex stats:")
        for k, v in stats.items():
            print(f"  {k:20s}: {v}")
        return

    # Check index is non-empty
    stats = await get_index_stats()
    if stats["chunks"] == 0:
        print("Index is empty. Run scripts/full_index.py first.")
        sys.exit(1)

    print(
        f"Index has {stats['chunks']} chunks across {stats['files']} files in {stats['repos']} repo(s)."
    )

    results = await search(args.query, top_k=args.top_k, repo=args.repo, language=args.language)

    if not results:
        print("No results found.")
        sys.exit(0)

    _print_results(results, args.query)


if __name__ == "__main__":
    asyncio.run(main())
