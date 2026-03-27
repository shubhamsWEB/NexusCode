import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch

from src.api.app import app

BASE_URL = "http://test"


@pytest.mark.asyncio
async def test_health_detailed_all_ok():
    """Both DB and Redis healthy → status ok, indexed_repos populated."""
    mock_stats = {"repos": 5, "chunks": 1200}

    with (
        patch("src.api.app.AsyncSessionLocal") as mock_session_cls,
        patch("src.api.app.get_index_stats", new_callable=AsyncMock, return_value=mock_stats),
        patch("redis.asyncio.from_url") as mock_redis_factory,
    ):
        mock_session = AsyncMock()
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_redis = AsyncMock()
        mock_redis_factory.return_value = mock_redis

        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client:
            resp = await client.get("/health/detailed")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["redis"] == "ok"
    assert body["indexed_repos"] == 5
    assert body["detail"] == {}


@pytest.mark.asyncio
async def test_health_detailed_db_error():
    """DB unreachable → status degraded, database error, redis ok."""
    with (
        patch("src.api.app.AsyncSessionLocal", side_effect=Exception("connection refused")),
        patch("redis.asyncio.from_url") as mock_redis_factory,
    ):
        mock_redis = AsyncMock()
        mock_redis_factory.return_value = mock_redis

        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client:
            resp = await client.get("/health/detailed")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"
    assert body["redis"] == "ok"
    assert body["indexed_repos"] == 0
    assert "database" in body["detail"]
    assert "connection refused" in body["detail"]["database"]


@pytest.mark.asyncio
async def test_health_detailed_redis_error():
    """Redis unreachable → status degraded, redis error, database ok."""
    mock_stats = {"repos": 3}

    with (
        patch("src.api.app.AsyncSessionLocal") as mock_session_cls,
        patch("src.api.app.get_index_stats", new_callable=AsyncMock, return_value=mock_stats),
        patch("redis.asyncio.from_url") as mock_redis_factory,
    ):
        mock_session = AsyncMock()
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = Exception("redis timeout")
        mock_redis_factory.return_value = mock_redis

        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client:
            resp = await client.get("/health/detailed")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["database"] == "ok"
    assert body["redis"] == "error"
    assert body["indexed_repos"] == 3
    assert "redis" in body["detail"]


@pytest.mark.asyncio
async def test_health_detailed_both_error():
    """Both DB and Redis down → status error."""
    with (
        patch("src.api.app.AsyncSessionLocal", side_effect=Exception("db down")),
        patch("redis.asyncio.from_url") as mock_redis_factory,
    ):
        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = Exception("redis down")
        mock_redis_factory.return_value = mock_redis

        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client:
            resp = await client.get("/health/detailed")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["database"] == "error"
    assert body["redis"] == "error"
    assert body["indexed_repos"] == 0


@pytest.mark.asyncio
async def test_health_detailed_response_schema():
    """Response always contains all required fields."""
    mock_stats = {"repos": 0}

    with (
        patch("src.api.app.AsyncSessionLocal") as mock_session_cls,
        patch("src.api.app.get_index_stats", new_callable=AsyncMock, return_value=mock_stats),
        patch("redis.asyncio.from_url") as mock_redis_factory,
    ):
        mock_session = AsyncMock()
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_redis_factory.return_value = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client:
            resp = await client.get("/health/detailed")

    body = resp.json()
    required_keys = {"status", "database", "redis", "indexed_repos", "detail"}
    assert required_keys.issubset(body.keys())
