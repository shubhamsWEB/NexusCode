"""
Call graph traversal functionality for analyzing file and symbol dependencies.

Leverages the kg_edges knowledge graph table to perform BFS traversal
and identify all callers of a given file or symbol, with support for
semantic edges and multi-hop analysis.
"""

import json
from typing import TypedDict

from sqlalchemy import text as sql_text

from src.storage.db import AsyncSessionLocal
from src.utils.logging import get_secure_logger

logger = get_secure_logger(__name__)


class CallGraphResult(TypedDict, total=False):
    """Result structure for call graph traversal."""

    type: str  # "file" | "symbol"
    target: str  # file_path or symbol_name
    total_callers: int
    hops: list[dict]  # List of hop dicts with "hop" and "callers" keys


async def get_call_graph_for_file(
    file_path: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    depth: int = 2,
    include_semantic: bool = True,
) -> CallGraphResult:
    """
    Get all callers of symbols defined in a file.

    Performs BFS traversal on kg_edges to find all symbols that call
    any symbol defined in the given file.

    Args:
        file_path: Path to the file (e.g., 'src/auth/service.py')
        repo_owner: Optional repo owner to scope the search
        repo_name: Optional repo name to scope the search
        depth: Traversal depth (1-3, default 2)
        include_semantic: Include semantic edges in traversal (default True)

    Returns:
        CallGraphResult with structure:
        {
            "type": "file",
            "target": "src/auth/service.py",
            "total_callers": 5,
            "hops": [
                {
                    "hop": 1,
                    "callers": [
                        {
                            "file": "src/api/routes.py",
                            "symbol_context": "login_handler",
                            "lines": "45-67",
                            "calls": "authenticate",
                            "confidence": 0.95,
                            "edge_type": "calls"
                        }
                    ]
                }
            ]
        }
    """
    depth = max(1, min(3, depth))

    try:
        # Step 1: Get all symbols defined in this file
        where_conditions = [
            "file_path = :file_path",
            "is_deleted = FALSE",
            "symbol_name IS NOT NULL",
            "symbol_kind IN ('function', 'method', 'class')"
        ]
        params: dict = {"file_path": file_path}
        
        if repo_owner:
            where_conditions.append("repo_owner = :repo_owner")
            params["repo_owner"] = repo_owner
        if repo_name:
            where_conditions.append("repo_name = :repo_name")
            params["repo_name"] = repo_name

        where_clause = " AND ".join(where_conditions)
        symbols_query = sql_text(
            f"SELECT DISTINCT symbol_name FROM chunks WHERE {where_clause}"
        )

        async with AsyncSessionLocal() as session:
            result = await session.execute(symbols_query, params)
            symbols = [row[0] for row in result.fetchall()]

        if not symbols:
            return {
                "type": "file",
                "target": file_path,
                "total_callers": 0,
                "hops": [],
            }

        # Step 2: Perform BFS traversal starting from these symbols
        edge_types = ["calls"]
        if include_semantic:
            edge_types.append("semantic")

        hops_data = await _bfs_traverse_graph(
            frontier=set(symbols),
            depth=depth,
            repo_owner=repo_owner,
            repo_name=repo_name,
            edge_types=edge_types,
        )

        # Step 3: Aggregate results
        total_callers = sum(len(hop.get("callers", [])) for hop in hops_data)

        return {
            "type": "file",
            "target": file_path,
            "total_callers": total_callers,
            "hops": hops_data,
        }

    except Exception as exc:
        logger.error(
            "get_call_graph_for_file failed for %s: %s",
            file_path,
            str(exc),
            exc_info=True,
        )
        return {
            "type": "file",
            "target": file_path,
            "total_callers": 0,
            "hops": [],
        }


async def get_call_graph_for_symbol(
    symbol: str,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    depth: int = 2,
    include_semantic: bool = True,
) -> CallGraphResult:
    """
    Get all callers of a specific symbol.

    Performs BFS traversal on kg_edges to find all symbols that call
    the given symbol.

    Args:
        symbol: Symbol name or qualified name (e.g., 'authenticate')
        repo_owner: Optional repo owner to scope the search
        repo_name: Optional repo name to scope the search
        depth: Traversal depth (1-3, default 2)
        include_semantic: Include semantic edges in traversal (default True)

    Returns:
        CallGraphResult with structure similar to get_call_graph_for_file
    """
    depth = max(1, min(3, depth))

    try:
        edge_types = ["calls"]
        if include_semantic:
            edge_types.append("semantic")

        hops_data = await _bfs_traverse_graph(
            frontier=set([symbol]),
            depth=depth,
            repo_owner=repo_owner,
            repo_name=repo_name,
            edge_types=edge_types,
        )

        total_callers = sum(len(hop.get("callers", [])) for hop in hops_data)

        return {
            "type": "symbol",
            "target": symbol,
            "total_callers": total_callers,
            "hops": hops_data,
        }

    except Exception as exc:
        logger.error(
            "get_call_graph_for_symbol failed for %s: %s",
            symbol,
            str(exc),
            exc_info=True,
        )
        return {
            "type": "symbol",
            "target": symbol,
            "total_callers": 0,
            "hops": [],
        }


async def _bfs_traverse_graph(
    frontier: set[str],
    depth: int,
    repo_owner: str | None,
    repo_name: str | None,
    edge_types: list[str],
) -> list[dict]:
    """
    Internal BFS helper that queries kg_edges with edge_type filtering.

    Args:
        frontier: Set of target symbols to find callers for
        depth: Maximum traversal depth
        repo_owner: Optional repo owner filter
        repo_name: Optional repo name filter
        edge_types: List of edge types to include (e.g., ['calls', 'semantic'])

    Returns:
        List of hop dicts, each containing callers at that hop level
    """
    hops: list[dict] = []
    seen: set[str] = set(frontier)  # Track visited symbols to avoid cycles

    for hop_num in range(1, depth + 1):
        if not frontier:
            break

        # Build WHERE clause for repo filtering
        where_parts = [
            "e.edge_type = ANY(:edge_types)",
            "e.target_id = ANY(:targets)"
        ]
        params: dict = {
            "edge_types": edge_types,
            "targets": list(frontier),
        }

        if repo_owner:
            where_parts.append("e.repo_owner = :repo_owner")
            params["repo_owner"] = repo_owner
        if repo_name:
            where_parts.append("e.repo_name = :repo_name")
            params["repo_name"] = repo_name

        where_clause = " AND ".join(where_parts)

        # Query kg_edges for callers of symbols in frontier
        query = sql_text(
            f"""
            SELECT DISTINCT
                e.source_id,
                e.target_id,
                e.edge_type,
                e.confidence,
                c.file_path,
                c.start_line,
                c.end_line
            FROM kg_edges e
            LEFT JOIN chunks c
                ON c.symbol_name = e.source_id
                AND c.repo_owner = e.repo_owner
                AND c.repo_name = e.repo_name
                AND c.is_deleted = FALSE
            WHERE {where_clause}
            ORDER BY e.confidence DESC
            LIMIT 60
        """
        )

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(query, params)
                rows = result.fetchall()
        except Exception as exc:
            logger.error(
                "BFS query failed at hop %d: %s",
                hop_num,
                str(exc),
                exc_info=True,
            )
            break

        if not rows:
            break

        # Process results for this hop
        callers: list[dict] = []
        next_frontier: set[str] = set()

        for row in rows:
            source_id = row[0]
            target_id = row[1]
            edge_type = row[2]
            confidence = row[3]
            file_path = row[4] or "unknown"
            start_line = row[5]
            end_line = row[6]

            lines = f"{start_line}-{end_line}" if start_line and end_line else "unknown"

            callers.append(
                {
                    "file": file_path,
                    "symbol_context": source_id,
                    "lines": lines,
                    "calls": target_id,
                    "confidence": float(confidence) if confidence is not None else 0.8,
                    "edge_type": edge_type,
                }
            )

            # Add to next frontier if not seen
            if source_id not in seen:
                next_frontier.add(source_id)
                seen.add(source_id)

        if callers:
            hops.append(
                {
                    "hop": hop_num,
                    "callers": callers,
                }
            )

        frontier = next_frontier

    return hops
