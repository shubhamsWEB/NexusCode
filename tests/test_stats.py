import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from src.api.app import app


@pytest.fixture
def client():
    """Create a TestClient for the FastAPI app."""
    return TestClient(app)


class TestStatsEndpoint:
    """Test suite for GET /stats endpoint."""

    def test_stats_endpoint_success(self, client):
        """Test that GET /stats returns valid stats dict with expected keys."""
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        # Verify all required fields are present
        assert "repos" in data
        assert "files" in data
        assert "chunks" in data
        assert "last_indexed" in data

        # Verify types
        assert isinstance(data["repos"], int)
        assert isinstance(data["files"], int)
        assert isinstance(data["chunks"], int)
        assert data["last_indexed"] is None or isinstance(data["last_indexed"], str)

    def test_stats_endpoint_no_auth_required(self, client):
        """Test that GET /stats does not require authentication."""
        # Should succeed without any auth headers
        response = client.get("/stats")
        assert response.status_code == 200

    def test_stats_endpoint_values_non_negative(self, client):
        """Test that all count values are non-negative."""
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        # All counts should be >= 0
        assert data["repos"] >= 0
        assert data["files"] >= 0
        assert data["chunks"] >= 0

    def test_stats_endpoint_last_indexed_format(self, client):
        """Test that last_indexed is either null or valid ISO 8601 format."""
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        if data["last_indexed"] is not None:
            # Should be parseable as ISO 8601
            datetime.fromisoformat(data["last_indexed"].replace("Z", "+00:00"))

    def test_stats_endpoint_no_symbols_field(self, client):
        """Test that the response does not include the symbols field."""
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        # Symbols field should NOT be in the response (filtered out)
        assert "symbols" not in data

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_error_handling(self, mock_get_stats, client):
        """Test that errors are handled gracefully with 500 response."""
        mock_get_stats.side_effect = Exception("Database connection failed")

        response = client.get("/stats")
        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        assert "Database connection failed" in data["error"]

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_timeout_error(self, mock_get_stats, client):
        """Test that timeout errors are handled gracefully."""
        mock_get_stats.side_effect = TimeoutError("Query timeout")

        response = client.get("/stats")
        assert response.status_code == 500
        data = response.json()
        assert "error" in data

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_with_populated_database(self, mock_get_stats, client):
        """Test that GET /stats returns correct values with populated database."""
        mock_get_stats.return_value = {
            "chunks": 1234,
            "symbols": 567,
            "files": 89,
            "repos": 12,
            "last_indexed": "2025-01-15T10:30:45.123456+00:00",
        }

        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["repos"] == 12
        assert data["files"] == 89
        assert data["chunks"] == 1234
        assert data["last_indexed"] == "2025-01-15T10:30:45.123456+00:00"
        # Symbols should be filtered out
        assert "symbols" not in data

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_with_empty_database(self, mock_get_stats, client):
        """Test that GET /stats returns zeros when database is empty."""
        mock_get_stats.return_value = {
            "chunks": 0,
            "symbols": 0,
            "files": 0,
            "repos": 0,
            "last_indexed": None,
        }

        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["repos"] == 0
        assert data["files"] == 0
        assert data["chunks"] == 0
        assert data["last_indexed"] is None
        # Symbols should be filtered out
        assert "symbols" not in data

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_filters_symbols_field(self, mock_get_stats, client):
        """Test that symbols field is always filtered from response."""
        mock_get_stats.return_value = {
            "chunks": 100,
            "symbols": 999,
            "files": 50,
            "repos": 5,
            "last_indexed": "2025-01-15T10:30:45.123456+00:00",
        }

        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        # Verify symbols is not in response even though it was in the mock
        assert "symbols" not in data
        # Verify other fields are present
        assert data["chunks"] == 100
        assert data["files"] == 50
        assert data["repos"] == 5

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_response_structure(self, mock_get_stats, client):
        """Test that response has exactly the required fields."""
        mock_get_stats.return_value = {
            "chunks": 42,
            "symbols": 10,
            "files": 20,
            "repos": 3,
            "last_indexed": "2025-01-15T10:30:45.123456+00:00",
        }

        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        # Response should have exactly 4 keys
        assert len(data) == 4
        assert set(data.keys()) == {"repos", "files", "chunks", "last_indexed"}

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_large_numbers(self, mock_get_stats, client):
        """Test that endpoint handles large integer values correctly."""
        mock_get_stats.return_value = {
            "chunks": 999999999,
            "symbols": 888888888,
            "files": 777777777,
            "repos": 666666666,
            "last_indexed": "2025-01-15T10:30:45.123456+00:00",
        }

        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["chunks"] == 999999999
        assert data["files"] == 777777777
        assert data["repos"] == 666666666

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_null_last_indexed(self, mock_get_stats, client):
        """Test that endpoint correctly handles null last_indexed."""
        mock_get_stats.return_value = {
            "chunks": 10,
            "symbols": 5,
            "files": 3,
            "repos": 1,
            "last_indexed": None,
        }

        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["last_indexed"] is None

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_database_error(self, mock_get_stats, client):
        """Test that database errors return 500 with error message."""
        mock_get_stats.side_effect = RuntimeError("Database unavailable")

        response = client.get("/stats")
        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        assert isinstance(data["error"], str)

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_missing_field_in_stats(self, mock_get_stats, client):
        """Test that missing fields in get_index_stats result in error."""
        # Return incomplete stats dict
        mock_get_stats.return_value = {
            "chunks": 10,
            "files": 5,
            # Missing repos and last_indexed
        }

        response = client.get("/stats")
        assert response.status_code == 500
        data = response.json()
        assert "error" in data

    def test_stats_endpoint_http_method_get(self, client):
        """Test that only GET method is allowed."""
        # POST should not be allowed
        response = client.post("/stats")
        assert response.status_code == 405

        # PUT should not be allowed
        response = client.put("/stats")
        assert response.status_code == 405

        # DELETE should not be allowed
        response = client.delete("/stats")
        assert response.status_code == 405

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_logging_on_success(self, mock_get_stats, client):
        """Test that successful calls are logged."""
        mock_get_stats.return_value = {
            "chunks": 10,
            "symbols": 5,
            "files": 3,
            "repos": 1,
            "last_indexed": None,
        }

        with patch("src.api.stats.logger") as mock_logger:
            response = client.get("/stats")
            assert response.status_code == 200
            # Verify debug logging was called
            mock_logger.debug.assert_called_once()

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_logging_on_error(self, mock_get_stats, client):
        """Test that errors are logged with exception details."""
        mock_get_stats.side_effect = Exception("Test error")

        with patch("src.api.stats.logger") as mock_logger:
            response = client.get("/stats")
            assert response.status_code == 500
            # Verify exception logging was called
            mock_logger.exception.assert_called_once()

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_content_type(self, mock_get_stats, client):
        """Test that response has correct content type."""
        mock_get_stats.return_value = {
            "chunks": 10,
            "symbols": 5,
            "files": 3,
            "repos": 1,
            "last_indexed": None,
        }

        response = client.get("/stats")
        assert response.status_code == 200
        assert "application/json" in response.headers.get("content-type", "")

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_zero_values(self, mock_get_stats, client):
        """Test that endpoint correctly handles all zero values."""
        mock_get_stats.return_value = {
            "chunks": 0,
            "symbols": 0,
            "files": 0,
            "repos": 0,
            "last_indexed": None,
        }

        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["chunks"] == 0
        assert data["files"] == 0
        assert data["repos"] == 0
        assert data["last_indexed"] is None

    @patch("src.api.stats.get_index_stats")
    def test_stats_endpoint_iso8601_timestamp_variations(self, mock_get_stats, client):
        """Test that endpoint handles various ISO 8601 timestamp formats."""
        test_timestamps = [
            "2025-01-15T10:30:45.123456+00:00",
            "2025-01-15T10:30:45Z",
            "2025-01-15T10:30:45",
        ]

        for timestamp in test_timestamps:
            mock_get_stats.return_value = {
                "chunks": 10,
                "symbols": 5,
                "files": 3,
                "repos": 1,
                "last_indexed": timestamp,
            }

            response = client.get("/stats")
            assert response.status_code == 200
            data = response.json()
            assert data["last_indexed"] == timestamp
