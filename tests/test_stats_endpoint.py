"""
Tests for the GET /stats endpoint.
Verifies that the endpoint returns valid aggregated database statistics.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from src.api.app import app

client = TestClient(app)


class TestStatsEndpointSuccess:
    """Test successful /stats endpoint responses."""

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_returns_200(self, mock_stats):
        """Test that GET /stats returns 200 status code."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_returns_valid_json(self, mock_stats):
        """Test that /stats returns valid JSON with correct content-type."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        # Should not raise
        data = response.json()
        assert isinstance(data, dict)

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_no_auth_required(self, mock_stats):
        """Test that /stats endpoint requires no authentication."""
        mock_stats.return_value = {
            "repos": 0,
            "files": 0,
            "chunks": 0,
            "symbols": 0,
            "last_indexed": None,
        }
        # Should succeed without any auth headers
        response = client.get("/stats")
        assert response.status_code == 200

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_contains_required_fields(self, mock_stats):
        """Test that /stats response contains all required fields."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        # Verify all required fields are present
        assert "repos" in data
        assert "files" in data
        assert "chunks" in data
        assert "last_indexed" in data

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_field_types(self, mock_stats):
        """Test that numeric stats are integers and last_indexed is string or null."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        # Verify types
        assert isinstance(data["repos"], int)
        assert isinstance(data["files"], int)
        assert isinstance(data["chunks"], int)
        assert data["last_indexed"] is None or isinstance(data["last_indexed"], str)

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_values_non_negative(self, mock_stats):
        """Test that numeric stats are non-negative."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["repos"] >= 0
        assert data["files"] >= 0
        assert data["chunks"] >= 0
        assert data["symbols"] >= 0

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_last_indexed_null(self, mock_stats):
        """Test that last_indexed can be null when no indexing has occurred."""
        mock_stats.return_value = {
            "repos": 0,
            "files": 0,
            "chunks": 0,
            "symbols": 0,
            "last_indexed": None,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["last_indexed"] is None

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_last_indexed_iso_format(self, mock_stats):
        """Test that last_indexed is ISO format string when present."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        last_indexed = data["last_indexed"]
        assert isinstance(last_indexed, str)
        # ISO format includes T separator
        assert "T" in last_indexed

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_includes_symbols(self, mock_stats):
        """Test that /stats response includes symbols count."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert "symbols" in data
        assert isinstance(data["symbols"], int)
        assert data["symbols"] >= 0

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_response_structure(self, mock_stats):
        """Test complete response structure with all fields."""
        expected_response = {
            "repos": 10,
            "files": 250,
            "chunks": 5000,
            "symbols": 1500,
            "last_indexed": "2024-01-20T10:15:30.456789+00:00",
        }
        mock_stats.return_value = expected_response
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        # Verify exact structure
        assert data == expected_response


class TestStatsEndpointErrorHandling:
    """Test error handling in /stats endpoint."""

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_database_error(self, mock_stats):
        """Test that database errors return 500 with generic error message."""
        mock_stats.side_effect = Exception("Database connection failed")
        response = client.get("/stats")
        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        assert data["error"] == "Failed to fetch statistics"

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_does_not_expose_error_details(self, mock_stats):
        """Verify internal error messages are not leaked to clients."""
        mock_stats.side_effect = Exception("Sensitive DB password: xyz123")
        response = client.get("/stats")
        assert response.status_code == 500
        # Ensure sensitive details are not in response
        assert "xyz123" not in response.text
        assert "password" not in response.text.lower()

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_timeout_error(self, mock_stats):
        """Test that timeout errors are handled gracefully."""
        mock_stats.side_effect = TimeoutError("Query timeout")
        response = client.get("/stats")
        assert response.status_code == 500
        data = response.json()
        assert "error" in data

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_generic_exception(self, mock_stats):
        """Test that generic exceptions are caught and handled."""
        mock_stats.side_effect = RuntimeError("Unexpected error")
        response = client.get("/stats")
        assert response.status_code == 500
        data = response.json()
        assert data["error"] == "Failed to fetch statistics"


class TestStatsEndpointEmptyDatabase:
    """Test /stats endpoint with empty database."""

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_empty_database(self, mock_stats):
        """Test that /stats returns zeros for empty database."""
        mock_stats.return_value = {
            "repos": 0,
            "files": 0,
            "chunks": 0,
            "symbols": 0,
            "last_indexed": None,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["repos"] == 0
        assert data["files"] == 0
        assert data["chunks"] == 0
        assert data["symbols"] == 0
        assert data["last_indexed"] is None

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_large_numbers(self, mock_stats):
        """Test that /stats handles large numbers correctly."""
        mock_stats.return_value = {
            "repos": 1000,
            "files": 50000,
            "chunks": 1000000,
            "symbols": 500000,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["repos"] == 1000
        assert data["files"] == 50000
        assert data["chunks"] == 1000000
        assert data["symbols"] == 500000

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_single_repo(self, mock_stats):
        """Test /stats with minimal data (single repo)."""
        mock_stats.return_value = {
            "repos": 1,
            "files": 5,
            "chunks": 50,
            "symbols": 25,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["repos"] == 1
        assert data["files"] == 5
        assert data["chunks"] == 50
        assert data["symbols"] == 25


class TestStatsEndpointIntegration:
    """Integration tests for /stats endpoint."""

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_multiple_requests(self, mock_stats):
        """Test that multiple requests to /stats work correctly."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        
        # First request
        response1 = client.get("/stats")
        assert response1.status_code == 200
        data1 = response1.json()
        
        # Second request
        response2 = client.get("/stats")
        assert response2.status_code == 200
        data2 = response2.json()
        
        # Both should return same data
        assert data1 == data2

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_changing_data(self, mock_stats):
        """Test that /stats reflects changing database state."""
        # First state
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response1 = client.get("/stats")
        data1 = response1.json()
        assert data1["repos"] == 5
        
        # Database updated
        mock_stats.return_value = {
            "repos": 6,
            "files": 150,
            "chunks": 4000,
            "symbols": 950,
            "last_indexed": "2024-01-16T10:00:00.000000+00:00",
        }
        response2 = client.get("/stats")
        data2 = response2.json()
        assert data2["repos"] == 6
        assert data2["files"] == 150

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_is_public(self, mock_stats):
        """Test that /stats endpoint is accessible without authentication."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        
        # Request without any auth headers
        response = client.get("/stats")
        assert response.status_code == 200
        
        # Request with invalid auth headers should still work (no auth required)
        response = client.get("/stats", headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 200

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_json_serializable(self, mock_stats):
        """Test that response is properly JSON serializable."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        
        # Should be able to parse and re-serialize
        data = response.json()
        import json
        json_str = json.dumps(data)
        assert isinstance(json_str, str)
        assert len(json_str) > 0


class TestStatsEndpointEdgeCases:
    """Test edge cases for /stats endpoint."""

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_zero_values(self, mock_stats):
        """Test /stats with all zero values."""
        mock_stats.return_value = {
            "repos": 0,
            "files": 0,
            "chunks": 0,
            "symbols": 0,
            "last_indexed": None,
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        
        assert all(v == 0 for k, v in data.items() if k != "last_indexed")
        assert data["last_indexed"] is None

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_max_int_values(self, mock_stats):
        """Test /stats with very large integer values."""
        max_int = 2147483647  # 32-bit max
        mock_stats.return_value = {
            "repos": max_int,
            "files": max_int,
            "chunks": max_int,
            "symbols": max_int,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        data = response.json()
        
        assert data["repos"] == max_int
        assert data["files"] == max_int
        assert data["chunks"] == max_int
        assert data["symbols"] == max_int

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_various_iso_formats(self, mock_stats):
        """Test /stats with various ISO timestamp formats."""
        iso_formats = [
            "2024-01-15T14:32:45Z",
            "2024-01-15T14:32:45+00:00",
            "2024-01-15T14:32:45.123456Z",
            "2024-01-15T14:32:45.123456+00:00",
        ]
        
        for iso_format in iso_formats:
            mock_stats.return_value = {
                "repos": 5,
                "files": 142,
                "chunks": 3847,
                "symbols": 892,
                "last_indexed": iso_format,
            }
            response = client.get("/stats")
            assert response.status_code == 200
            data = response.json()
            assert data["last_indexed"] == iso_format

    @patch("src.api.app.get_index_stats", new_callable=AsyncMock)
    def test_stats_endpoint_response_headers(self, mock_stats):
        """Test that response has correct headers."""
        mock_stats.return_value = {
            "repos": 5,
            "files": 142,
            "chunks": 3847,
            "symbols": 892,
            "last_indexed": "2024-01-15T14:32:45.123456+00:00",
        }
        response = client.get("/stats")
        assert response.status_code == 200
        assert "content-type" in response.headers
        assert "application/json" in response.headers["content-type"]
