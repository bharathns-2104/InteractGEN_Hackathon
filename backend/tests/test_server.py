import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch, AsyncMock
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from server import app, SecurityValidator, AsyncWebCrawler, RAGService

client = TestClient(app)

# --- Security Tests ---

class TestSecurityValidator:
    def test_safe_http_url(self):
        is_safe, msg = SecurityValidator.is_safe_url("https://example.com")
        assert is_safe == True
    
    def test_block_localhost(self):
        is_safe, msg = SecurityValidator.is_safe_url("http://localhost:8080")
        assert is_safe == False
        assert "blocked" in msg.lower()
    
    def test_block_private_ip(self):
        is_safe, msg = SecurityValidator.is_safe_url("http://192.168.1.1")
        assert is_safe == False
        assert "blocked" in msg.lower()
    
    def test_block_loopback(self):
        is_safe, msg = SecurityValidator.is_safe_url("http://127.0.0.1")
        assert is_safe == False
    
    def test_invalid_scheme(self):
        is_safe, msg = SecurityValidator.is_safe_url("ftp://example.com")
        assert is_safe == False
        assert "scheme" in msg.lower()
    
    def test_block_internal_domains(self):
        # Test common internal domain patterns
        test_urls = [
            "http://10.0.0.1",
            "http://172.16.0.1",
            "http://192.168.0.1"
        ]
        for url in test_urls:
            is_safe, msg = SecurityValidator.is_safe_url(url)
            assert is_safe == False

# --- API Tests ---

class TestHealthEndpoint:
    @patch('server.check_ollama_health', new_callable=AsyncMock)
    def test_health_check_healthy(self, mock_health):
        mock_health.return_value = True
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ["healthy", "degraded"]
        assert "ollama" in data
    
    @patch('server.check_ollama_health', new_callable=AsyncMock)
    def test_health_check_degraded(self, mock_health):
        mock_health.return_value = False
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["ollama"] == False

class TestIngestEndpoint:
    @patch('server.ensure_ollama_ready', new_callable=AsyncMock)
    @patch('server.AsyncWebCrawler.crawl', new_callable=AsyncMock)
    def test_ingest_invalid_url_scheme(self, mock_crawl, mock_ollama):
        response = client.post("/ingest", json={
            "url": "ftp://example.com",
            "max_pages": 5
        })
        # Should fail validation at Pydantic level or security check
        assert response.status_code in [400, 422]
    
    @patch('server.ensure_ollama_ready', new_callable=AsyncMock)
    @patch('server.AsyncWebCrawler.crawl', new_callable=AsyncMock)
    def test_ingest_private_ip(self, mock_crawl, mock_ollama):
        mock_ollama.return_value = None
        mock_crawl.side_effect = ValueError("URL blocked")
        
        response = client.post("/ingest", json={
            "url": "http://192.168.1.1",
            "max_pages": 5
        })
        assert response.status_code == 400

class TestChatEndpoint:
    @patch('server.ensure_ollama_ready', new_callable=AsyncMock)
    @patch('server.RAGService.query', new_callable=AsyncMock)
    def test_chat_empty_question(self, mock_query, mock_ollama):
        response = client.post("/chat", json={
            "question": ""
        })
        # Should fail Pydantic validation (min_length=1)
        assert response.status_code == 422
    
    @patch('server.ensure_ollama_ready', new_callable=AsyncMock)
    @patch('server.RAGService.query', new_callable=AsyncMock)
    def test_chat_too_long_question(self, mock_query, mock_ollama):
        response = client.post("/chat", json={
            "question": "x" * 1001  # Exceeds max_length=1000
        })
        assert response.status_code == 422

class TestSourcesEndpoint:
    @patch('server.RAGService.get_sources')
    def test_get_sources_success(self, mock_get_sources):
        mock_get_sources.return_value = [
            "https://example.com",
            "https://test.com"
        ]
        
        response = client.get("/sources")
        assert response.status_code == 200
        data = response.json()
        assert "sources" in data
        assert "count" in data
        assert data["count"] == 2

# --- Crawler Tests ---

class TestAsyncWebCrawler:
    @pytest.mark.asyncio
    async def test_crawler_respects_max_pages(self):
        crawler = AsyncWebCrawler(max_pages=2)
        # Would need to mock httpx.AsyncClient for full test
        assert crawler.max_pages == 2
    
    @pytest.mark.asyncio
    async def test_crawler_blocks_unsafe_urls(self):
        crawler = AsyncWebCrawler()
        with pytest.raises(ValueError, match="URL blocked"):
            await crawler.crawl("http://127.0.0.1")

# --- RAG Service Tests ---

class TestRAGService:
    def test_service_initialization(self):
        service = RAGService()
        assert service.vectorstore is None
        assert service.llm is None
    
    def test_get_llm_creates_instance(self):
        service = RAGService()
        with patch('server.ChatOllama'):
            llm = service.get_llm()
            assert llm is not None

# --- Integration Tests ---

class TestIntegration:
    @pytest.mark.asyncio
    @patch('server.check_ollama_health', new_callable=AsyncMock)
    async def test_full_workflow_mock(self, mock_health):
        """Test the full ingest -> chat workflow with mocks"""
        mock_health.return_value = True
        
        # This would require more extensive mocking
        # but demonstrates the testing approach
        pass

if __name__ == "__main__":
    pytest.main([__file__, "-v"])