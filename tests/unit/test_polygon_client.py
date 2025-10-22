"""Unit tests for Polygon.io client"""

import pytest
from unittest.mock import Mock, patch
from data.ingestion.polygon.client import PolygonClient, PolygonAPIError


class TestPolygonClient:
    """Test suite for PolygonClient"""
    
    def test_client_initialization(self):
        """Test client initializes correctly"""
        client = PolygonClient("test_api_key")
        assert client.api_key == "test_api_key"
        assert client.BASE_URL == "https://api.polygon.io"
    
    def test_client_requires_api_key(self):
        """Test client raises error without API key"""
        with pytest.raises(ValueError):
            PolygonClient("")
    
    @patch('data.ingestion.polygon.client.requests.Session.get')
    def test_get_request_success(self, mock_get):
        """Test successful GET request"""
        # Mock response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "OK", "results": []}
        mock_get.return_value = mock_response
        
        client = PolygonClient("test_key")
        result = client._get("/test/endpoint")
        
        assert result == {"status": "OK", "results": []}
    
    @patch('data.ingestion.polygon.client.requests.Session.get')
    def test_get_request_api_error(self, mock_get):
        """Test API error handling"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "ERROR", "error": "Test error"}
        mock_get.return_value = mock_response
        
        client = PolygonClient("test_key")
        
        with pytest.raises(PolygonAPIError):
            client._get("/test/endpoint")
    
    def test_context_manager(self):
        """Test client works as context manager"""
        with PolygonClient("test_key") as client:
            assert client.api_key == "test_key"
        
        # Session should be closed after context exit
        assert client.session is not None  # Session object still exists


class TestAggregatesClient:
    """Test suite for AggregatesClient"""
    
    @pytest.fixture
    def polygon_client(self):
        """Create a mock Polygon client"""
        return Mock(spec=PolygonClient)
    
    def test_chunk_date_range(self, polygon_client):
        """Test date range chunking"""
        from data.ingestion.polygon.aggregates import AggregatesClient
        
        agg_client = AggregatesClient(polygon_client)
        chunks = agg_client.chunk_date_range("2024-01-01", "2024-01-31", chunk_days=7)
        
        # Should create 5 chunks (31 days / 7 days per chunk)
        assert len(chunks) >= 4
        assert len(chunks) <= 5
        
        # First chunk should start at 2024-01-01
        assert chunks[0][0] == "2024-01-01"


class TestReferenceClient:
    """Test suite for ReferenceClient"""
    
    @pytest.fixture
    def polygon_client(self):
        """Create a mock Polygon client"""
        return Mock(spec=PolygonClient)
    
    def test_search_tickers(self, polygon_client):
        """Test ticker search functionality"""
        from data.ingestion.polygon.reference import ReferenceClient
        
        ref_client = ReferenceClient(polygon_client)
        
        # Mock the response
        polygon_client.get_with_pagination.return_value = [
            {"ticker": "AAPL", "name": "Apple Inc."},
            {"ticker": "GOOGL", "name": "Alphabet Inc."}
        ]
        
        results = ref_client.search_tickers("Apple")
        
        assert len(results) == 2
        assert results[0]["ticker"] == "AAPL"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
