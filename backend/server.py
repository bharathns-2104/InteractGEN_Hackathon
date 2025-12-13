# FILE: backend/server.py
import os
import logging
import asyncio
import socket
from io import BytesIO
from ipaddress import ip_address, ip_network
from typing import Optional, List, Set
from contextlib import asynccontextmanager

# Suppress python-dotenv parse warnings
logging.getLogger("dotenv").setLevel(logging.ERROR)
from dotenv import load_dotenv
load_dotenv()

import uvicorn
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, HttpUrl, Field, validator
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Ollama imports
from langchain_community.chat_models import ChatOllama
from langchain_community.embeddings import OllamaEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate
from langchain.docstore.document import Document
import edge_tts

# --- CONFIGURATION ---
class Config:
    PERSIST_DIRECTORY = os.getenv("PERSIST_DIRECTORY", "./notebook_db")
    MODEL_NAME = os.getenv("MODEL_NAME", "llama3")
    OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    MAX_PAGES_PER_CRAWL = int(os.getenv("MAX_PAGES_PER_CRAWL", "10"))
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1500"))
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
    REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))
    CRAWL_TIMEOUT = float(os.getenv("CRAWL_TIMEOUT", "5"))
    MAX_VECTORSTORE_FETCH = int(os.getenv("MAX_VECTORSTORE_FETCH", "1000"))
    
    # Security settings
    BLOCKED_NETWORKS = [
        '127.0.0.0/8',      # Loopback
        '10.0.0.0/8',       # Private
        '172.16.0.0/12',    # Private
        '192.168.0.0/16',   # Private
        '169.254.0.0/16',   # Link-local
        '224.0.0.0/4',      # Multicast
    ]
    ALLOWED_SCHEMES = {'http', 'https'}
    USER_AGENT = "RAGBot/1.0 (+https://yoursite.com/bot)"

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- SECURITY UTILITIES ---
class SecurityValidator:
    @staticmethod
    def is_safe_url(url: str) -> tuple[bool, str]:
        """Validate URL for SSRF protection."""
        try:
            parsed = urlparse(url)
            
            # Check scheme
            if parsed.scheme not in Config.ALLOWED_SCHEMES:
                return False, f"Scheme '{parsed.scheme}' not allowed"
            
            if not parsed.hostname:
                return False, "Invalid hostname"
            
            # Resolve hostname to IP
            try:
                resolved_ip = socket.gethostbyname(parsed.hostname)
                ip = ip_address(resolved_ip)
            except socket.gaierror:
                return False, "Cannot resolve hostname"
            
            # Check if IP is in blocked ranges
            for network_str in Config.BLOCKED_NETWORKS:
                network = ip_network(network_str)
                if ip in network:
                    return False, f"Access to {network_str} is blocked for security"
            
            return True, "OK"
        except Exception as e:
            logger.error(f"URL validation error: {e}")
            return False, f"Invalid URL: {str(e)}"
    
    @staticmethod
    def check_robots_txt(base_url: str, user_agent: str = Config.USER_AGENT) -> bool:
        """Check if crawling is allowed by robots.txt."""
        try:
            parsed = urlparse(base_url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            
            rp = RobotFileParser()
            rp.set_url(robots_url)
            rp.read()
            
            return rp.can_fetch(user_agent, base_url)
        except Exception as e:
            logger.warning(f"Could not check robots.txt: {e}. Proceeding.")
            return True  # Allow if robots.txt is unavailable

# --- CRAWLER ---
class AsyncWebCrawler:
    def __init__(self, max_pages: int = Config.MAX_PAGES_PER_CRAWL):
        self.max_pages = max_pages
        self.visited: Set[str] = set()
        self.documents: List[Document] = []
        self.headers = {"User-Agent": Config.USER_AGENT}
    
    async def crawl(self, base_url: str) -> List[Document]:
        """Crawl website asynchronously."""
        # Security check
        is_safe, msg = SecurityValidator.is_safe_url(base_url)
        if not is_safe:
            raise ValueError(f"URL blocked: {msg}")
        
        # Check robots.txt
        if not SecurityValidator.check_robots_txt(base_url):
            raise ValueError("Crawling disallowed by robots.txt")
        
        domain = urlparse(base_url).netloc
        queue = [base_url]
        
        logger.info(f"🕷️ Starting async crawl: {base_url}")
        
        async with httpx.AsyncClient(timeout=Config.CRAWL_TIMEOUT, headers=self.headers) as client:
            while queue and len(self.visited) < self.max_pages:
                # Process multiple URLs concurrently
                batch = queue[:5]  # Process 5 at a time
                queue = queue[5:]
                
                tasks = [self._fetch_page(client, url, domain) for url in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for result in results:
                    if isinstance(result, Exception):
                        logger.error(f"Crawl error: {result}")
                    elif result:
                        new_urls = result
                        queue.extend([u for u in new_urls if u not in self.visited])
        
        logger.info(f"✅ Crawl finished. Found {len(self.documents)} pages.")
        return self.documents
    
    async def _fetch_page(self, client: httpx.AsyncClient, url: str, domain: str) -> Optional[List[str]]:
        """Fetch and parse a single page."""
        if url in self.visited:
            return None
        
        try:
            logger.info(f"Fetching: {url}")
            response = await client.get(url, follow_redirects=True)
            
            # Check content type
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                logger.warning(f"Skipping non-HTML: {url}")
                return None
            
            soup = BeautifulSoup(response.content, "html.parser")
            
            # Remove unwanted elements
            for element in soup(["script", "style", "nav", "footer", "iframe", "noscript", "header"]):
                element.decompose()
            
            # Extract main content
            text = soup.get_text(separator=' ', strip=True)
            
            # Only save if content is substantial
            if len(text) > 200:
                self.documents.append(
                    Document(
                        page_content=text,
                        metadata={"source": url, "length": len(text)}
                    )
                )
                self.visited.add(url)
                
                # Extract links for further crawling
                links = []
                for link in soup.find_all("a", href=True):
                    full_url = urljoin(url, link["href"])
                    if urlparse(full_url).netloc == domain and full_url not in self.visited:
                        links.append(full_url)
                
                return links
            else:
                logger.warning(f"Page too short: {url}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return None

# --- RAG SERVICE ---
class RAGService:
    def __init__(self):
        self.vectorstore: Optional[Chroma] = None
        self.llm: Optional[ChatOllama] = None
        self.embeddings: Optional[OllamaEmbeddings] = None
    
    def initialize(self):
        """Initialize embeddings and vectorstore."""
        if self.vectorstore is None:
            self.embeddings = OllamaEmbeddings(model=Config.MODEL_NAME)
            self.vectorstore = Chroma(
                persist_directory=Config.PERSIST_DIRECTORY,
                embedding_function=self.embeddings
            )
            logger.info("✅ RAG Service initialized")
    
    def get_llm(self) -> ChatOllama:
        """Get or create LLM instance."""
        if self.llm is None:
            self.llm = ChatOllama(model=Config.MODEL_NAME)
        return self.llm
    
    async def add_documents(self, documents: List[Document]) -> int:
        """Add documents to vectorstore with chunking."""
        self.initialize()
        
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=Config.CHUNK_SIZE,
            chunk_overlap=Config.CHUNK_OVERLAP
        )
        splits = splitter.split_documents(documents)
        
        logger.info(f"⏳ Embedding {len(splits)} chunks...")
        
        # Run blocking operation in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.vectorstore.add_documents, splits)
        
        logger.info(f"✅ Embedded {len(splits)} chunks")
        return len(splits)
    
    async def query(self, question: str, k: int = 4) -> dict:
        """Query the RAG system."""
        self.initialize()
        
        prompt = ChatPromptTemplate.from_template("""
        Answer the question based ONLY on the context below.
        If you cannot find the answer in the context, say so.
        Include relevant source citations in your answer.
        
        <context>
        {context}
        </context>
        
        Question: {input}
        """)
        
        retriever = self.vectorstore.as_retriever(search_kwargs={"k": k})
        chain = create_retrieval_chain(
            retriever,
            create_stuff_documents_chain(self.get_llm(), prompt)
        )
        
        # Run in executor
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: chain.invoke({"input": question})
        )
        
        # Extract unique sources
        sources = set()
        for doc in response.get("context", []):
            sources.add(doc.metadata.get("source", "Unknown"))
        
        return {
            "answer": response["answer"],
            "citations": list(sources)
        }
    
    async def generate_briefing(self) -> str:
        """Generate a briefing document."""
        self.initialize()
        
        retriever = self.vectorstore.as_retriever(search_kwargs={"k": 5})
        loop = asyncio.get_event_loop()
        docs = await loop.run_in_executor(
            None,
            lambda: retriever.invoke("Overview of the content")
        )
        
        if not docs:
            return "No content available to summarize."
        
        text = "\n".join([d.page_content[:1000] for d in docs])  # Limit context
        
        prompt = f"""Create a comprehensive briefing document with:
        1. Executive Summary (2-3 sentences)
        2. Three Key Topics with brief explanations
        3. Three Frequently Asked Questions with answers
        
        Based on this content:
        {text[:5000]}
        """
        
        response = await loop.run_in_executor(
            None,
            lambda: self.get_llm().invoke(prompt)
        )
        
        return response.content
    
    async def generate_podcast_script(self) -> str:
        """Generate a podcast script."""
        self.initialize()
        
        docs = self.vectorstore.similarity_search("Main concepts overview", k=10)
        if not docs:
            raise ValueError("Not enough content for podcast")
        
        context = "\n".join([d.page_content[:500] for d in docs])
        
        prompt = f"""Create a natural, conversational 1-minute podcast script between two hosts.
        
        Format:
        Host A: [greeting and introduction]
        Host B: [response and first point]
        Host A: [discussion of second point]
        Host B: [discussion of third point]
        Host A: [conclusion]
        
        Keep it engaging and informative. Based on:
        {context[:5000]}
        """
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.get_llm().invoke(prompt)
        )
        
        return response.content
    
    def get_sources(self) -> List[str]:
        """Get all unique sources."""
        self.initialize()
        
        try:
            data = self.vectorstore.get(
                limit=Config.MAX_VECTORSTORE_FETCH,
                include=["metadatas"]
            )
            
            sources = set()
            if data and data.get('metadatas'):
                for meta in data['metadatas']:
                    if meta and "source" in meta:
                        sources.add(meta["source"])
            
            return sorted(list(sources))
        except Exception as e:
            logger.error(f"Error fetching sources: {e}")
            return []
    
    def delete_source(self, source_url: str) -> int:
        """Delete all chunks from a specific source."""
        self.initialize()
        
        data = self.vectorstore.get(where={"source": source_url})
        if data and data.get('ids'):
            self.vectorstore.delete(ids=data['ids'])
            return len(data['ids'])
        return 0
    
    def clear_all(self):
        """Clear the entire database."""
        import shutil
        if os.path.exists(Config.PERSIST_DIRECTORY):
            shutil.rmtree(Config.PERSIST_DIRECTORY)
        self.vectorstore = None
        self.llm = None
        logger.info("🗑️ Database cleared")

# --- HEALTH CHECK ---
async def check_ollama_health() -> bool:
    """Check if Ollama is reachable."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{Config.OLLAMA_HOST}/api/version")
            return response.status_code == 200
    except Exception:
        return False

async def ensure_ollama_ready(retries: int = 6, base_delay: float = 1.0):
    """Wait for Ollama to become available."""
    delay = base_delay
    for attempt in range(1, retries + 1):
        if await check_ollama_health():
            logger.info("✅ Ollama is ready")
            return
        
        logger.warning(f"⏳ Waiting for Ollama (attempt {attempt}/{retries})...")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 10)
    
    raise HTTPException(
        status_code=503,
        detail=f"Ollama at {Config.OLLAMA_HOST} is unavailable. Start with 'ollama serve'"
    )

# --- API MODELS ---
class IngestRequest(BaseModel):
    url: HttpUrl
    max_pages: int = Field(default=5, ge=1, le=50, description="Max pages to crawl")

class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)

class DeleteSourceRequest(BaseModel):
    source_url: HttpUrl

class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None

# --- FASTAPI APP ---
limiter = Limiter(key_func=get_remote_address)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    logger.info(f"🚀 Starting RAG Server")
    logger.info(f"📍 Ollama: {Config.OLLAMA_HOST}")
    logger.info(f"💾 Database: {Config.PERSIST_DIRECTORY}")
    
    try:
        await ensure_ollama_ready()
        app.state.rag_service = RAGService()
        app.state.rag_service.initialize()
    except Exception as e:
        logger.error(f"❌ Startup failed: {e}")
        raise
    
    yield
    
    # Shutdown
    logger.info("👋 Shutting down")

app = FastAPI(
    title="RAG API",
    description="Retrieval-Augmented Generation API with Ollama",
    version="2.0.0",
    lifespan=lifespan
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ENDPOINTS ---

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    ollama_status = await check_ollama_health()
    return {
        "status": "healthy" if ollama_status else "degraded",
        "ollama": ollama_status,
        "database": os.path.exists(Config.PERSIST_DIRECTORY)
    }

@app.post("/ingest", response_model=dict)
@limiter.limit("10/hour")
async def ingest_website(request: Request, req: IngestRequest):
    """Crawl and ingest a website into the knowledge base."""
    try:
        await ensure_ollama_ready()
        
        # Crawl website
        crawler = AsyncWebCrawler(max_pages=req.max_pages)
        documents = await crawler.crawl(str(req.url))
        
        if not documents:
            raise HTTPException(400, "No content found or crawling blocked")
        
        # Add to vectorstore
        rag_service: RAGService = request.app.state.rag_service
        chunks_added = await rag_service.add_documents(documents)
        
        return {
            "status": "success",
            "pages_crawled": len(documents),
            "chunks_added": chunks_added,
            "message": f"Successfully ingested {len(documents)} pages"
        }
        
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Ingest error: {e}")
        raise HTTPException(500, f"Ingestion failed: {str(e)}")

@app.post("/chat", response_model=dict)
@limiter.limit("30/minute")
async def chat_endpoint(request: Request, req: ChatRequest):
    """Ask questions about ingested content."""
    try:
        await ensure_ollama_ready()
        
        rag_service: RAGService = request.app.state.rag_service
        result = await rag_service.query(req.question)
        
        return result
        
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(500, f"Query failed: {str(e)}")

@app.post("/briefing", response_model=dict)
@limiter.limit("5/hour")
async def generate_briefing(request: Request):
    """Generate a briefing document from ingested content."""
    try:
        await ensure_ollama_ready()
        
        rag_service: RAGService = request.app.state.rag_service
        content = await rag_service.generate_briefing()
        
        return {"content": content}
        
    except Exception as e:
        logger.error(f"Briefing error: {e}")
        raise HTTPException(500, f"Briefing generation failed: {str(e)}")

@app.get("/podcast")
@limiter.limit("3/hour")
async def generate_podcast(request: Request):
    """Generate a podcast from ingested content."""
    try:
        await ensure_ollama_ready()
        
        rag_service: RAGService = request.app.state.rag_service
        script = await rag_service.generate_podcast_script()
        
        # Generate audio
        audio_buffer = BytesIO()
        
        for line in script.split('\n'):
            line = line.strip()
            if line.startswith("Host A:"):
                text = line.replace("Host A:", "").strip()
                if text:
                    comm = edge_tts.Communicate(text, "en-US-GuyNeural")
                    async for chunk in comm.stream():
                        if chunk["type"] == "audio":
                            audio_buffer.write(chunk["data"])
                        
            elif line.startswith("Host B:"):
                text = line.replace("Host B:", "").strip()
                if text:
                    comm = edge_tts.Communicate(text, "en-US-AriaNeural")
                    async for chunk in comm.stream():
                        if chunk["type"] == "audio":
                            audio_buffer.write(chunk["data"])
        
        audio_buffer.seek(0)
        
        return StreamingResponse(
            audio_buffer,
            media_type="audio/mpeg",
            headers={"Content-Disposition": "attachment; filename=podcast.mp3"}
        )
        
    except Exception as e:
        logger.error(f"Podcast error: {e}")
        raise HTTPException(500, f"Podcast generation failed: {str(e)}")

@app.get("/sources", response_model=dict)
async def list_sources(request: Request):
    """List all ingested sources."""
    try:
        rag_service: RAGService = request.app.state.rag_service
        sources = rag_service.get_sources()
        return {"sources": sources, "count": len(sources)}
    except Exception as e:
        logger.error(f"List sources error: {e}")
        return {"sources": [], "count": 0}

@app.post("/delete_source", response_model=dict)
@limiter.limit("20/hour")
async def delete_source(request: Request, req: DeleteSourceRequest):
    """Delete a specific source from the knowledge base."""
    try:
        rag_service: RAGService = request.app.state.rag_service
        deleted_count = rag_service.delete_source(str(req.source_url))
        
        if deleted_count == 0:
            raise HTTPException(404, "Source not found")
        
        return {
            "status": "success",
            "deleted_chunks": deleted_count,
            "message": f"Deleted {deleted_count} chunks"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete source error: {e}")
        raise HTTPException(500, f"Deletion failed: {str(e)}")

@app.post("/clear", response_model=dict)
@limiter.limit("5/hour")
async def clear_database(request: Request):
    """Clear the entire knowledge base."""
    try:
        rag_service: RAGService = request.app.state.rag_service
        rag_service.clear_all()
        
        # Reinitialize
        request.app.state.rag_service = RAGService()
        request.app.state.rag_service.initialize()
        
        return {"status": "success", "message": "Database cleared"}
        
    except Exception as e:
        logger.error(f"Clear error: {e}")
        raise HTTPException(500, f"Clear failed: {str(e)}")

# --- MAIN ---
if __name__ == "__main__":
    logger.info(f"Starting server; Ollama host: {Config.OLLAMA_HOST}")
    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000"))
    )