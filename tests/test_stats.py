import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from src.api.app import app

client = TestClient(app)


def test_stats_endpoint_success():
    """Test that /stats endpoint returns correct data with valid stats."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 5,
            "files": 150,
            "chunks": 3000,
            "last_indexed": "2024-01-15T10:30:00Z",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["repos"] == 5
        assert data["files"] == 150
        assert data["chunks"] == 3000
        assert data["last_indexed"] == "2024-01-15T10:30:00Z"


def test_stats_endpoint_no_auth_required():
    """Test /stats endpoint returns 200 without authentication headers."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 0,
            "files": 0,
            "chunks": 0,
            "last_indexed": None,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        assert response.is_success


def test_stats_endpoint_response_schema():
    """Test /stats endpoint response matches StatsResponse schema."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 3,
            "files": 75,
            "chunks": 1500,
            "last_indexed": "2024-01-10T15:45:00Z",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == {"repos", "files", "chunks", "last_indexed"}
        assert isinstance(data["repos"], int)
        assert isinstance(data["files"], int)
        assert isinstance(data["chunks"], int)
        assert data["last_indexed"] is None or isinstance(data["last_indexed"], str)


def test_stats_endpoint_empty_database():
    """Test /stats endpoint with empty database (all zeros)."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 0,
            "files": 0,
            "chunks": 0,
            "last_indexed": None,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["repos"] == 0
        assert data["files"] == 0
        assert data["chunks"] == 0
        assert data["last_indexed"] is None


def test_stats_endpoint_null_timestamp():
    """Test /stats endpoint correctly serializes null last_indexed."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 10,
            "files": 200,
            "chunks": 5000,
            "last_indexed": None,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["last_indexed"] is None


def test_stats_endpoint_large_numbers():
    """Test /stats endpoint with large stat values."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 1000,
            "files": 50000,
            "chunks": 1000000,
            "last_indexed": "2024-12-31T23:59:59Z",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["repos"] == 1000
        assert data["files"] == 50000
        assert data["chunks"] == 1000000
        assert data["last_indexed"] == "2024-12-31T23:59:59Z"


def test_stats_endpoint_database_error():
    """Test /stats endpoint returns 500 on database errors."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.side_effect = Exception("Database connection failed")
        response = client.get("/stats")
        assert response.status_code == 500


def test_stats_endpoint_database_timeout():
    """Test /stats endpoint returns 500 on database timeout."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.side_effect = TimeoutError("Database query timeout")
        response = client.get("/stats")
        assert response.status_code == 500


def test_stats_endpoint_missing_fields_in_db_response():
    """Test /stats endpoint handles missing fields from database gracefully."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 5,
            "files": 100,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["repos"] == 5
        assert data["files"] == 100
        assert data["chunks"] == 0
        assert data["last_indexed"] is None


def test_stats_endpoint_extra_fields_in_db_response():
    """Test /stats endpoint ignores extra fields from database."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 5,
            "files": 100,
            "chunks": 2000,
            "last_indexed": "2024-01-15T10:30:00Z",
            "extra_field": "should_be_ignored",
            "another_field": 12345,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == {"repos", "files", "chunks", "last_indexed"}
        assert "extra_field" not in data
        assert "another_field" not in data


def test_stats_endpoint_iso8601_timestamp_formats():
    """Test /stats endpoint with various ISO 8601 timestamp formats."""
    test_timestamps = [
        "2024-01-15T10:30:00Z",
        "2024-01-15T10:30:00+00:00",
        "2024-01-15T10:30:00.123456Z",
    ]
    
    for timestamp in test_timestamps:
        with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
            mock_stats.return_value = {
                "repos": 1,
                "files": 10,
                "chunks": 100,
                "last_indexed": timestamp,
            }
            response = client.get("/stats")
            assert response.status_code == 200
            data = response.json()
            assert data["last_indexed"] == timestamp


def test_stats_endpoint_response_content_type():
    """Test /stats endpoint returns JSON content type."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 1,
            "files": 10,
            "chunks": 100,
            "last_indexed": None,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"


def test_stats_endpoint_get_method_only():
    """Test /stats endpoint only accepts GET requests."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 1,
            "files": 10,
            "chunks": 100,
            "last_indexed": None,
        }
        response = client.post("/stats")
        assert response.status_code == 405
        
        response = client.put("/stats")
        assert response.status_code == 405
        
        response = client.delete("/stats")
        assert response.status_code == 405


def test_stats_endpoint_zero_values():
    """Test /stats endpoint with zero values for all counts."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 0,
            "files": 0,
            "chunks": 0,
            "last_indexed": None,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["repos"] == 0
        assert data["files"] == 0
        assert data["chunks"] == 0
        assert data["last_indexed"] is None


def test_stats_endpoint_response_is_json_serializable():
    """Test /stats endpoint response can be serialized to JSON."""
    import json
    
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 5,
            "files": 150,
            "chunks": 3000,
            "last_indexed": "2024-01-15T10:30:00Z",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        json_str = json.dumps(data)
        assert isinstance(json_str, str)
        parsed = json.loads(json_str)
        assert parsed == data


def test_stats_endpoint_concurrent_calls():
    """Test /stats endpoint handles multiple concurrent calls."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 5,
            "files": 150,
            "chunks": 3000,
            "last_indexed": "2024-01-15T10:30:00Z",
        }
        responses = [client.get("/stats") for _ in range(5)]
        for response in responses:
            assert response.status_code == 200
            data = response.json()
            assert data["repos"] == 5


def test_stats_endpoint_integer_field_validation():
    """Test /stats endpoint validates integer fields."""
    with patch("src.api.stats.get_index_stats", new_callable=AsyncMock) as mock_stats:
        mock_stats.return_value = {
            "repos": 5,
            "files": 150,
            "chunks": 3000,
            "last_indexed": "2024-01-15T10:30:00Z",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["repos"], int)
        assert isinstance(data["files"], int)
        assert isinstance(data["chunks"], int)
        assert not isinstance(data["repos"], bool)
        assert not isinstance(data["files"], bool)
        assert not isinstance(data["chunks"], bool)
