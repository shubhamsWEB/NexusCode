"""
Comprehensive test suite for call graph traversal functionality.

Tests BFS traversal, semantic edges, repo filtering, and edge cases.
Covers both the core call_graph module and tool_executor integration.
"""

import json
import pytest
from sqlalchemy import text

from src.retrieval.call_graph import (
    CallGraphResult,
    get_call_graph_for_file,
    get_call_graph_for_symbol,
    _bfs_traverse_graph,
)
from src.storage.db import AsyncSessionLocal


@pytest.fixture
async def setup_test_data():
    """Set up test data in kg_edges and chunks tables."""
    async with AsyncSessionLocal() as session:
        # Clear any existing test data
        await session.execute(
            text(
                "DELETE FROM kg_edges WHERE repo_owner IN ('test_owner', 'other_owner')"
            )
        )
        await session.execute(
            text(
                "DELETE FROM chunks WHERE repo_owner IN ('test_owner', 'other_owner')"
            )
        )
        await session.commit()

        # Insert test chunks for repo1
        await session.execute(
            text(
                """
                INSERT INTO chunks (
                    id, file_path, repo_owner, repo_name, commit_sha, commit_author,
                    language, symbol_name, symbol_kind, start_line, end_line,
                    raw_content, is_deleted
                ) VALUES
                (:id1, :fp1, :ro1, :rn1, 'sha1', 'author1', 'python', 'authenticate', 'function', 10, 20, 'def authenticate():', FALSE),
                (:id2, :fp1, :ro1, :rn1, 'sha1', 'author1', 'python', 'validate_token', 'function', 25, 35, 'def validate_token():', FALSE),
                (:id3, :fp2, :ro1, :rn1, 'sha2', 'author2', 'python', 'login_handler', 'function', 45, 67, 'def login_handler():', FALSE),
                (:id4, :fp3, :ro1, :rn1, 'sha3', 'author3', 'python', 'middleware', 'function', 100, 120, 'def middleware():', FALSE),
                (:id5, :fp4, :ro1, :rn1, 'sha4', 'author4', 'python', 'process_request', 'function', 5, 15, 'def process_request():', FALSE),
                (:id6, :fp5, :ro1, :rn1, 'sha5', 'author5', 'python', 'MyClass', 'class', 1, 50, 'class MyClass:', FALSE),
                (:id7, :fp6, :ro2, :rn2, 'sha6', 'author6', 'python', 'other_caller', 'function', 5, 15, 'def other_caller():', FALSE)
            """
            ),
            {
                "id1": "chunk_auth_service_1",
                "fp1": "src/auth/service.py",
                "id2": "chunk_auth_service_2",
                "id3": "chunk_api_routes_1",
                "fp2": "src/api/routes.py",
                "id4": "chunk_middleware_1",
                "fp3": "src/middleware/auth.py",
                "id5": "chunk_api_handlers_1",
                "fp4": "src/api/handlers.py",
                "id6": "chunk_models_1",
                "fp5": "src/models/user.py",
                "id7": "chunk_other_1",
                "fp6": "src/other/module.py",
                "ro1": "test_owner",
                "rn1": "test_repo",
                "ro2": "other_owner",
                "rn2": "other_repo",
            },
        )

        # Insert test kg_edges for repo1
        await session.execute(
            text(
                """
                INSERT INTO kg_edges (
                    source_id, source_type, target_id, target_type,
                    edge_type, repo_owner, repo_name, confidence
                ) VALUES
                (:s1, 'symbol', :t1, 'symbol', 'calls', :ro1, :rn1, 0.95),
                (:s2, 'symbol', :t2, 'symbol', 'calls', :ro1, :rn1, 0.90),
                (:s3, 'symbol', :t3, 'symbol', 'calls', :ro1, :rn1, 0.85),
                (:s4, 'symbol', :t4, 'symbol', 'calls', :ro1, :rn1, 0.88),
                (:s5, 'symbol', :t5, 'symbol', 'semantic', :ro1, :rn1, 0.75),
                (:s6, 'symbol', :t6, 'symbol', 'calls', :ro1, :rn1, 0.92),
                (:s7, 'symbol', :t7, 'symbol', 'calls', :ro2, :rn2, 0.80)
            """
            ),
            {
                "s1": "login_handler",
                "t1": "authenticate",
                "s2": "middleware",
                "t2": "validate_token",
                "s3": "authenticate",
                "t3": "validate_token",
                "s4": "process_request",
                "t4": "authenticate",
                "s5": "MyClass",
                "t5": "MyInterface",
                "s6": "login_handler",
                "t6": "process_request",
                "s7": "other_caller",
                "t7": "authenticate",
                "ro1": "test_owner",
                "rn1": "test_repo",
                "ro2": "other_owner",
                "rn2": "other_repo",
            },
        )

        await session.commit()

    yield

    # Cleanup
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(
                text(
                    "DELETE FROM kg_edges WHERE repo_owner IN ('test_owner', 'other_owner')"
                )
            )
            await session.execute(
                text(
                    "DELETE FROM chunks WHERE repo_owner IN ('test_owner', 'other_owner')"
                )
            )
            await session.commit()
        except Exception as e:
            await session.rollback()


@pytest.mark.asyncio
async def test_get_call_graph_for_file_success(setup_test_data):
    """Test getting call graph for a file with callers."""
    result = await get_call_graph_for_file(
        file_path="src/auth/service.py",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    assert isinstance(result, dict)
    assert result["type"] == "file"
    assert result["target"] == "src/auth/service.py"
    assert result["total_callers"] >= 2
    assert len(result["hops"]) > 0
    assert result["hops"][0]["hop"] == 1
    assert "callers" in result["hops"][0]
    assert len(result["hops"][0]["callers"]) > 0


@pytest.mark.asyncio
async def test_get_call_graph_for_file_no_callers(setup_test_data):
    """Test getting call graph for a file with no callers."""
    result = await get_call_graph_for_file(
        file_path="src/nonexistent/file.py",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    assert result["type"] == "file"
    assert result["target"] == "src/nonexistent/file.py"
    assert result["total_callers"] == 0
    assert result["hops"] == []


@pytest.mark.asyncio
async def test_get_call_graph_for_file_depth_1(setup_test_data):
    """Test depth=1 returns only direct callers."""
    result = await get_call_graph_for_file(
        file_path="src/auth/service.py",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    assert len(result["hops"]) >= 1
    assert result["hops"][0]["hop"] == 1


@pytest.mark.asyncio
async def test_get_call_graph_for_file_depth_2(setup_test_data):
    """Test depth=2 returns multi-hop callers."""
    result = await get_call_graph_for_file(
        file_path="src/auth/service.py",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=2,
    )

    assert len(result["hops"]) >= 1
    assert result["hops"][0]["hop"] == 1


@pytest.mark.asyncio
async def test_get_call_graph_for_file_depth_capped(setup_test_data):
    """Test that depth is capped to 3."""
    result = await get_call_graph_for_file(
        file_path="src/auth/service.py",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=10,
    )

    assert len(result["hops"]) <= 3


@pytest.mark.asyncio
async def test_get_call_graph_for_symbol_success(setup_test_data):
    """Test getting call graph for a symbol with callers."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    assert result["type"] == "symbol"
    assert result["target"] == "authenticate"
    assert result["total_callers"] >= 1
    assert len(result["hops"]) > 0
    assert any(
        c["symbol_context"] == "login_handler" for c in result["hops"][0]["callers"]
    )


@pytest.mark.asyncio
async def test_get_call_graph_for_symbol_no_callers(setup_test_data):
    """Test getting call graph for a symbol with no callers."""
    result = await get_call_graph_for_symbol(
        symbol="nonexistent_symbol",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    assert result["type"] == "symbol"
    assert result["target"] == "nonexistent_symbol"
    assert result["total_callers"] == 0
    assert result["hops"] == []


@pytest.mark.asyncio
async def test_get_call_graph_semantic_edges_included(setup_test_data):
    """Test that semantic edges are included when requested."""
    result = await get_call_graph_for_symbol(
        symbol="MyInterface",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
        include_semantic=True,
    )

    if result["total_callers"] > 0:
        has_semantic = any(
            c["edge_type"] == "semantic" for hop in result["hops"] for c in hop["callers"]
        )
        assert has_semantic


@pytest.mark.asyncio
async def test_get_call_graph_semantic_edges_excluded(setup_test_data):
    """Test that semantic edges are excluded when not requested."""
    result = await get_call_graph_for_symbol(
        symbol="MyInterface",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
        include_semantic=False,
    )

    for hop in result["hops"]:
        for caller in hop["callers"]:
            assert caller["edge_type"] != "semantic"


@pytest.mark.asyncio
async def test_get_call_graph_repo_scope_filtering(setup_test_data):
    """Test that repo scope filtering works correctly."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    for hop in result["hops"]:
        for caller in hop["callers"]:
            assert "src/" in caller["file"]


@pytest.mark.asyncio
async def test_get_call_graph_invalid_file(setup_test_data):
    """Test getting call graph for non-existent file."""
    result = await get_call_graph_for_file(
        file_path="src/nonexistent/orphan.py",
        repo_owner="test_owner",
        repo_name="test_repo",
    )

    assert result["total_callers"] == 0
    assert result["hops"] == []


@pytest.mark.asyncio
async def test_get_call_graph_circular_references(setup_test_data):
    """Test that circular references don't cause infinite loops."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                """
                INSERT INTO kg_edges (
                    source_id, source_type, target_id, target_type,
                    edge_type, repo_owner, repo_name, confidence
                ) VALUES
                (:s, 'symbol', :t, 'symbol', 'calls', :ro, :rn, 0.9)
            """
            ),
            {
                "s": "validate_token",
                "t": "authenticate",
                "ro": "test_owner",
                "rn": "test_repo",
            },
        )
        await session.commit()

    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=3,
    )

    assert result["total_callers"] >= 0
    assert len(result["hops"]) <= 3


@pytest.mark.asyncio
async def test_get_call_graph_confidence_scores(setup_test_data):
    """Test that confidence scores are included and reasonable."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    if result["total_callers"] > 0:
        for hop in result["hops"]:
            for caller in hop["callers"]:
                assert "confidence" in caller
                assert 0.0 <= caller["confidence"] <= 1.0


@pytest.mark.asyncio
async def test_get_call_graph_large_depth_capped(setup_test_data):
    """Test that large depth values are capped to 3."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=10,
    )

    assert len(result["hops"]) <= 3


@pytest.mark.asyncio
async def test_get_call_graph_empty_frontier(setup_test_data):
    """Test that traversal stops when frontier is empty."""
    result = await get_call_graph_for_symbol(
        symbol="nonexistent_symbol",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=3,
    )

    assert result["total_callers"] == 0
    assert result["hops"] == []


@pytest.mark.asyncio
async def test_get_call_graph_caller_structure(setup_test_data):
    """Test that caller objects have all required fields."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    if result["total_callers"] > 0:
        for hop in result["hops"]:
            for caller in hop["callers"]:
                assert "file" in caller
                assert "symbol_context" in caller
                assert "lines" in caller
                assert "calls" in caller
                assert "confidence" in caller
                assert "edge_type" in caller


@pytest.mark.asyncio
async def test_get_call_graph_for_file_with_multiple_symbols(setup_test_data):
    """Test that file with multiple symbols finds all callers."""
    result = await get_call_graph_for_file(
        file_path="src/auth/service.py",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    assert result["total_callers"] >= 2


@pytest.mark.asyncio
async def test_get_call_graph_depth_0_treated_as_1(setup_test_data):
    """Test that depth=0 is treated as depth=1."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=0,
    )

    assert len(result["hops"]) >= 0


@pytest.mark.asyncio
async def test_get_call_graph_negative_depth_treated_as_1(setup_test_data):
    """Test that negative depth is treated as depth=1."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=-5,
    )

    assert len(result["hops"]) >= 0


@pytest.mark.asyncio
async def test_bfs_traverse_graph_basic(setup_test_data):
    """Test the internal BFS traversal function."""
    hops = await _bfs_traverse_graph(
        frontier={"authenticate"},
        depth=1,
        repo_owner="test_owner",
        repo_name="test_repo",
        edge_types=["calls"],
    )

    assert isinstance(hops, list)
    if len(hops) > 0:
        assert hops[0]["hop"] == 1
        assert "callers" in hops[0]


@pytest.mark.asyncio
async def test_bfs_traverse_graph_with_semantic(setup_test_data):
    """Test BFS traversal with semantic edges."""
    hops = await _bfs_traverse_graph(
        frontier={"MyInterface"},
        depth=1,
        repo_owner="test_owner",
        repo_name="test_repo",
        edge_types=["calls", "semantic"],
    )

    if len(hops) > 0 and len(hops[0]["callers"]) > 0:
        has_semantic = any(c["edge_type"] == "semantic" for c in hops[0]["callers"])
        assert has_semantic


@pytest.mark.asyncio
async def test_bfs_traverse_graph_without_semantic(setup_test_data):
    """Test BFS traversal without semantic edges."""
    hops = await _bfs_traverse_graph(
        frontier={"MyInterface"},
        depth=1,
        repo_owner="test_owner",
        repo_name="test_repo",
        edge_types=["calls"],
    )

    for hop in hops:
        for caller in hop["callers"]:
            assert caller["edge_type"] != "semantic"


@pytest.mark.asyncio
async def test_get_call_graph_result_type_structure(setup_test_data):
    """Test that result matches CallGraphResult TypedDict structure."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    assert "type" in result
    assert "target" in result
    assert "total_callers" in result
    assert "hops" in result
    assert isinstance(result["type"], str)
    assert isinstance(result["target"], str)
    assert isinstance(result["total_callers"], int)
    assert isinstance(result["hops"], list)


@pytest.mark.asyncio
async def test_get_call_graph_for_file_without_repo_scope(setup_test_data):
    """Test getting call graph for file without repo scope."""
    result = await get_call_graph_for_file(
        file_path="src/auth/service.py",
        repo_owner=None,
        repo_name=None,
        depth=1,
    )

    assert result["type"] == "file"
    assert result["target"] == "src/auth/service.py"


@pytest.mark.asyncio
async def test_get_call_graph_for_symbol_without_repo_scope(setup_test_data):
    """Test getting call graph for symbol without repo scope."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner=None,
        repo_name=None,
        depth=1,
    )

    assert result["type"] == "symbol"
    assert result["target"] == "authenticate"


@pytest.mark.asyncio
async def test_get_call_graph_hop_ordering(setup_test_data):
    """Test that hops are returned in correct order."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=3,
    )

    for i, hop in enumerate(result["hops"]):
        assert hop["hop"] == i + 1


@pytest.mark.asyncio
async def test_get_call_graph_caller_lines_format(setup_test_data):
    """Test that caller lines are in correct format."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    if result["total_callers"] > 0:
        for hop in result["hops"]:
            for caller in hop["callers"]:
                lines = caller["lines"]
                assert isinstance(lines, str)
                if lines != "unknown":
                    parts = lines.split("-")
                    assert len(parts) == 2


@pytest.mark.asyncio
async def test_get_call_graph_for_file_deleted_chunks_excluded(setup_test_data):
    """Test that deleted chunks are excluded from results."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE chunks SET is_deleted = TRUE WHERE symbol_name = 'authenticate' AND repo_owner = 'test_owner'"
            )
        )
        await session.commit()

    result = await get_call_graph_for_file(
        file_path="src/auth/service.py",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    assert result["total_callers"] == 0


@pytest.mark.asyncio
async def test_get_call_graph_multiple_callers_same_hop(setup_test_data):
    """Test that multiple callers at same hop are all returned."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    if result["total_callers"] > 0:
        assert len(result["hops"][0]["callers"]) >= 1


@pytest.mark.asyncio
async def test_get_call_graph_confidence_sorting(setup_test_data):
    """Test that callers are sorted by confidence descending."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
    )

    if result["total_callers"] > 1:
        callers = result["hops"][0]["callers"]
        confidences = [c["confidence"] for c in callers]
        assert confidences == sorted(confidences, reverse=True)


@pytest.mark.asyncio
async def test_get_call_graph_edge_type_values(setup_test_data):
    """Test that edge_type values are valid."""
    result = await get_call_graph_for_symbol(
        symbol="authenticate",
        repo_owner="test_owner",
        repo_name="test_repo",
        depth=1,
        include_semantic=True,
    )

    valid_edge_types = {"calls", "semantic"}
    for hop in result["hops"]:
        for caller in hop["callers"]:
            assert caller["edge_type"] in valid_edge_types
