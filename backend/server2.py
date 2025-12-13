"""
Improved Lightweight RAG Server with FastAPI
Features:
- BM25 keyword search
- Nano AI (LaMini-Flan-T5-248M) for generation
- TextRank summarization
- Edge TTS podcast generation
- Proper async handling, security, and error management
"""

import asyncio
import os
import logging
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Set, Tuple
from datetime import datetime
from urllib.parse import urljoin, urlparse

import uvicorn
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, validator
from bs4 import BeautifulSoup

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- IMPORTS WITH GRACEFUL FAILURE ---
try:
    from rank_bm25 import BM25Okapi
except ImportError:
    logger.error("BM25Okapi not installed. Run: pip install rank-bm25")
    raise SystemExit(1)

try:
    from sumy.parsers.plaintext import PlaintextParser
    from sumy.nlp.tokenizers import Tokenizer
    from sumy.summarizers.text_rank import TextRankSummarizer
    from sumy.nlp.stemmers import Stemmer
except ImportError:
    logger.error("Sumy not installed. Run: pip install sumy nltk")
    raise SystemExit(1)

try:
    from transformers import pipeline
except ImportError:
    logger.error("Transformers not installed. Run: pip install transformers sentencepiece torch")
    raise SystemExit(1)

try:
    import edge_tts
except ImportError:
    logger.error("Edge TTS not installed. Run: pip install edge-tts")
    raise SystemExit(1)

# --- CONFIGURATION ---
class Config:
    """Centralized configuration"""
    HOST = "127.0.0.1"
    PORT = 8000
    
    # Security
    ALLOWED_SCHEMES = {"http", "https"}
    BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    MAX_URL_LENGTH = 2048
    
    # Crawling
    MAX_PAGES_PER_CRAWL = 10
    CRAWL_TIMEOUT = 10
    MIN_CHUNK_LENGTH = 50
    MAX_CHUNK_LENGTH = 2000
    
    # Memory limits
    MAX_TOTAL_CHUNKS = 10000
    MAX_CHUNKS_PER_SOURCE = 1000
    
    # Model
    MODEL_NAME = "MBZUAI/LaMini-Flan-T5-248M"
    MAX_MODEL_LENGTH = 512
    
    # Retrieval
    TOP_K_RETRIEVAL = 5
    
    # Summary
    SUMMARY_SENTENCES = 5
    LANGUAGE = "english"
    
    # Threading
    MAX_WORKERS = 3

config = Config()

# --- APP SETUP ---
app = FastAPI(title="Nano RAG Server", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Development: allow all origins for extension/local testing
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# --- GLOBAL STATE ---
class Database:
    """Thread-safe in-memory database"""
    def __init__(self):
        self.sources: Dict[str, List[str]] = {}  # {url: [chunks]}
        self.chunk_metadata: List[Dict] = []  # [{text, source_url, timestamp}]
        self.bm25: BM25Okapi = None
        self.lock = asyncio.Lock()
        self.stemmer = Stemmer(config.LANGUAGE)
    
    def get_stats(self) -> Dict:
        """Get database statistics"""
        return {
            "total_sources": len(self.sources),
            "total_chunks": len(self.chunk_metadata),
            "sources": list(self.sources.keys())
        }
    
    def get_full_text_sample(self, max_chars: int = 5000) -> str:
        """Get a sample of full text for summarization"""
        text_parts = []
        char_count = 0
        
        for meta in self.chunk_metadata:
            if char_count >= max_chars:
                break
            text_parts.append(meta['text'])
            char_count += len(meta['text'])
        
        return " ".join(text_parts)

db = Database()
executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)

# --- UTILITY FUNCTIONS ---
def tokenize(text: str) -> List[str]:
    """Improved tokenization with punctuation handling"""
    # Convert to lowercase and extract words
    tokens = re.findall(r'\b\w+\b', text.lower())
    return tokens

def validate_url(url: str) -> bool:
    """Validate URL for security"""
    if len(url) > config.MAX_URL_LENGTH:
        raise ValueError(f"URL too long (max {config.MAX_URL_LENGTH} chars)")
    
    parsed = urlparse(url)
    
    if parsed.scheme not in config.ALLOWED_SCHEMES:
        raise ValueError(f"Only {', '.join(config.ALLOWED_SCHEMES)} schemes allowed")
    
    if parsed.hostname in config.BLOCKED_HOSTS:
        raise ValueError("Cannot crawl local/private resources")
    
    # Block private IP ranges
    if parsed.hostname:
        if parsed.hostname.startswith('192.168.') or \
           parsed.hostname.startswith('10.') or \
           parsed.hostname.startswith('172.'):
            raise ValueError("Cannot crawl private IP ranges")
    
    return True

def clean_text(text: str) -> str:
    """Clean and normalize text"""
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove control characters
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
    return text.strip()

def crawl_website(base_url: str, max_pages: int = None) -> Tuple[List[str], List[str]]:
    """
    Robustly crawls a website for text content.
    Returns: (chunks, visited_urls)
    """
    if max_pages is None:
        max_pages = config.MAX_PAGES_PER_CRAWL
    
    domain = urlparse(base_url).netloc
    visited = set()
    queue = [base_url]
    chunks = []
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    logger.info(f"🕷️ Starting crawl: {base_url} (max {max_pages} pages)")
    
    while queue and len(visited) < max_pages:
        current_url = queue.pop(0)
        
        if current_url in visited:
            continue
        
        try:
            logger.info(f"Visiting: {current_url}")
            resp = requests.get(
                current_url, 
                headers=headers, 
                timeout=config.CRAWL_TIMEOUT,
                allow_redirects=True
            )
            resp.raise_for_status()
            
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                logger.warning(f"Skipping non-HTML: {current_url}")
                continue
            
            soup = BeautifulSoup(resp.content, "html.parser")
            
            # Remove unwanted elements
            for element in soup(["script", "style", "nav", "footer", "iframe", "noscript", "aside"]):
                element.decompose()
            
            # Extract text from meaningful elements
            page_chunks = []
            for tag in soup.find_all(['p', 'h1', 'h2', 'h3', 'article', 'section']):
                text = clean_text(tag.get_text(separator=' ', strip=True))
                
                if config.MIN_CHUNK_LENGTH <= len(text) <= config.MAX_CHUNK_LENGTH:
                    page_chunks.append(text)
            
            if page_chunks:
                chunks.extend(page_chunks)
                visited.add(current_url)
                logger.info(f"✓ Found {len(page_chunks)} chunks on {current_url}")
                
                # Find and queue same-domain links
                for link in soup.find_all("a", href=True):
                    try:
                        full_url = urljoin(current_url, link["href"])
                        parsed = urlparse(full_url)
                        
                        # Only queue same-domain HTTP(S) links
                        if parsed.netloc == domain and \
                           parsed.scheme in config.ALLOWED_SCHEMES and \
                           full_url not in visited and \
                           full_url not in queue:
                            queue.append(full_url)
                    except Exception as e:
                        logger.debug(f"Skipping invalid link: {e}")
            else:
                logger.warning(f"No substantial content found on: {current_url}")
        
        except requests.RequestException as e:
            logger.error(f"Failed to fetch {current_url}: {e}")
        except Exception as e:
            logger.error(f"Error processing {current_url}: {e}")
    
    logger.info(f"✓ Crawl complete: {len(chunks)} chunks from {len(visited)} pages")
    return chunks, list(visited)

async def rebuild_index():
    """Rebuilds BM25 index from current database state"""
    if not db.chunk_metadata:
        db.bm25 = None
        logger.info("Database empty, index cleared")
        return
    
    # Extract texts and tokenize
    corpus_texts = [meta['text'] for meta in db.chunk_metadata]
    tokenized_corpus = [tokenize(text) for text in corpus_texts]
    
    # Build BM25 index
    db.bm25 = BM25Okapi(tokenized_corpus)
    logger.info(f"✓ Rebuilt BM25 index with {len(corpus_texts)} chunks")

# --- LOAD AI MODEL ---
logger.info(f"⏳ Loading AI model: {config.MODEL_NAME}")
try:
    chatbot = pipeline(
        "text2text-generation",
        model=config.MODEL_NAME,
        max_length=config.MAX_MODEL_LENGTH,
        device=-1  # CPU
    )
    logger.info("✅ AI model loaded successfully")
except Exception as e:
    logger.error(f"❌ Failed to load AI model: {e}")
    logger.error("Server cannot start without the model. Please check your installation.")
    raise SystemExit(1)

# --- REQUEST MODELS ---
class IngestRequest(BaseModel):
    url: str = Field(..., description="URL to crawl and ingest")
    max_pages: int = Field(default=5, ge=1, le=20, description="Maximum pages to crawl")
    
    @validator('url')
    def validate_url_field(cls, v):
        validate_url(v)
        return v

class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    
    @validator('question')
    def validate_question(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Question cannot be empty")
        return v

class DeleteSourceRequest(BaseModel):
    source_url: str

# --- RATE LIMITING (Simple in-memory) ---
rate_limit_store: Dict[str, List[float]] = {}

async def rate_limit_check(request: Request, max_requests: int = 10, window: int = 60):
    """Simple rate limiting: max_requests per window (seconds)"""
    client_ip = request.client.host
    now = datetime.now().timestamp()
    
    if client_ip not in rate_limit_store:
        rate_limit_store[client_ip] = []
    
    # Remove old requests outside window
    rate_limit_store[client_ip] = [
        req_time for req_time in rate_limit_store[client_ip]
        if now - req_time < window
    ]
    
    if len(rate_limit_store[client_ip]) >= max_requests:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Max {max_requests} requests per {window}s"
        )
    
    rate_limit_store[client_ip].append(now)

# --- API ENDPOINTS ---

@app.get("/")
async def root():
    """Health check and API info"""
    return {
        "status": "running",
        "version": "2.0",
        "endpoints": ["/ingest", "/chat", "/briefing", "/podcast", "/sources", "/stats", "/clear"]
    }

@app.get("/stats")
async def get_stats():
    """Get database statistics"""
    return db.get_stats()

@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    """
    Crawl a website and add content to the database.
    Rate limited to prevent abuse.
    """
    await rate_limit_check(request, max_requests=5, window=60)
    
    try:
        # Check if we're at capacity
        if len(db.chunk_metadata) >= config.MAX_TOTAL_CHUNKS:
            raise HTTPException(
                status_code=507,
                detail=f"Database at capacity ({config.MAX_TOTAL_CHUNKS} chunks). Delete some sources first."
            )
        
        # Run crawl in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        chunks, visited_urls = await loop.run_in_executor(
            executor,
            crawl_website,
            req.url,
            req.max_pages
        )
        
        if not chunks:
            raise HTTPException(
                status_code=400,
                detail="No substantial content found. Site may be blocking crawlers or have no text content."
            )
        
        # Limit chunks per source
        if len(chunks) > config.MAX_CHUNKS_PER_SOURCE:
            logger.warning(f"Truncating {len(chunks)} chunks to {config.MAX_CHUNKS_PER_SOURCE}")
            chunks = chunks[:config.MAX_CHUNKS_PER_SOURCE]
        
        # Store with metadata
        async with db.lock:
            timestamp = datetime.now().isoformat()
            
            # Add to sources
            db.sources[req.url] = chunks
            
            # Add to chunk metadata
            for chunk in chunks:
                db.chunk_metadata.append({
                    'text': chunk,
                    'source_url': req.url,
                    'timestamp': timestamp
                })
            
            # Rebuild index
            await rebuild_index()
        
        return {
            "status": "success",
            "source_url": req.url,
            "pages_visited": len(visited_urls),
            "chunks_added": len(chunks),
            "count": len(chunks),
            "total_chunks": len(db.chunk_metadata)
        }
    
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {str(e)}")
    except Exception as e:
        logger.exception("Unexpected error in ingest")
        raise HTTPException(status_code=500, detail="Internal server error during ingestion")

@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """
    RAG-powered chat: retrieves relevant chunks and generates answer.
    """
    await rate_limit_check(request, max_requests=20, window=60)
    
    logger.info(f"Chat request: {req.question}")
    
    if not db.bm25:
        raise HTTPException(
            status_code=400,
            detail="No content available. Please ingest a website first using /ingest"
        )
    
    try:
        # 1. Retrieve relevant chunks using BM25
        tokenized_query = tokenize(req.question)
        scores = db.bm25.get_scores(tokenized_query)
        
        # Get top K indices
        top_indices = sorted(
            range(len(scores)),
            key=lambda i: scores[i],
            reverse=True
        )[:config.TOP_K_RETRIEVAL]
        
        # Build context and track sources
        context_parts = []
        source_urls = set()
        
        for idx in top_indices:
            if idx < len(db.chunk_metadata):
                meta = db.chunk_metadata[idx]
                context_parts.append(meta['text'])
                source_urls.add(meta['source_url'])
        
        context = "\n\n".join(context_parts)
        
        # 2. Generate answer using Nano AI
        prompt = (
            f"Answer the question based ONLY on the context below. "
            f"Be concise and specific. If the answer isn't in the context, say so.\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {req.question}\n\n"
            f"Answer:"
        )
        
        # Run model inference in thread pool
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            executor,
            lambda: chatbot(prompt, max_length=config.MAX_MODEL_LENGTH, do_sample=False)[0]['generated_text']
        )
        
        return {
            "answer": result.strip(),
            "sources": list(source_urls),
            "citations": list(source_urls),
            "chunks_retrieved": len(context_parts)
        }
    
    except Exception as e:
        logger.exception("Error in chat endpoint")
        raise HTTPException(status_code=500, detail="Failed to generate answer")

@app.post("/briefing")
async def briefing(request: Request):
    """
    Generates a briefing using TextRank summarization and AI-generated FAQs.
    """
    await rate_limit_check(request, max_requests=5, window=60)
    
    if not db.chunk_metadata:
        raise HTTPException(
            status_code=400,
            detail="No content available. Please ingest a website first."
        )
    
    try:
        # Get text sample for summarization
        full_text = db.get_full_text_sample(max_chars=10000)
        
        # Generate extractive summary using TextRank
        parser = PlaintextParser.from_string(full_text, Tokenizer(config.LANGUAGE))
        summarizer = TextRankSummarizer(db.stemmer)
        summary_sentences = summarizer(parser.document, config.SUMMARY_SENTENCES)
        summary = " ".join([str(s) for s in summary_sentences])
        
        # Generate FAQs using AI
        faq_prompt = (
            f"Based on this summary, generate 3 useful questions and answers:\n\n"
            f"{summary}\n\n"
            f"Format as:\nQ1: [question]\nA1: [answer]\n\nQ2:..."
        )
        
        loop = asyncio.get_event_loop()
        faq_content = await loop.run_in_executor(
            executor,
            lambda: chatbot(faq_prompt, max_length=config.MAX_MODEL_LENGTH)[0]['generated_text']
        )
        
        briefing_content = (
            f"# 📝 Content Briefing\n\n"
            f"**Sources:** {len(db.sources)} websites, {len(db.chunk_metadata)} chunks\n\n"
            f"## Summary (TextRank)\n{summary}\n\n"
            f"## Generated FAQs\n{faq_content}"
        )
        
        return {"content": briefing_content}
    
    except Exception as e:
        logger.exception("Error in briefing endpoint")
        raise HTTPException(status_code=500, detail="Failed to generate briefing")

@app.get("/podcast")
async def podcast(request: Request):
    """
    Generates a 2-host podcast script and converts to MP3.
    """
    await rate_limit_check(request, max_requests=3, window=300)
    
    if not db.chunk_metadata:
        raise HTTPException(
            status_code=400,
            detail="No content available for podcast generation."
        )
    
    try:
        # Get sample text
        sample_text = db.get_full_text_sample(max_chars=3000)
        
        # Generate podcast script
        script_prompt = (
            f"Create a brief 2-host podcast script about this content. "
            f"Format as 'Host A: [text]' and 'Host B: [text]' alternating.\n\n"
            f"Content: {sample_text}"
        )
        
        loop = asyncio.get_event_loop()
        script = await loop.run_in_executor(
            executor,
            lambda: chatbot(script_prompt, max_length=config.MAX_MODEL_LENGTH)[0]['generated_text']
        )
        
        # Generate audio using Edge TTS
        full_audio = b""
        
        for line in script.split('\n'):
            line = line.strip()
            if not line:
                continue
            
            # Detect host and extract text
            if "Host A:" in line or line.startswith("Host A"):
                text = re.sub(r'Host A:?\s*', '', line, flags=re.IGNORECASE)
                voice = "en-US-GuyNeural"
            elif "Host B:" in line or line.startswith("Host B"):
                text = re.sub(r'Host B:?\s*', '', line, flags=re.IGNORECASE)
                voice = "en-US-AriaNeural"
            else:
                text = line
                voice = "en-US-AriaNeural"
            
            if text:
                communicate = edge_tts.Communicate(text, voice)
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        full_audio += chunk["data"]
        
        # Save to temporary file
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        temp_file.write(full_audio)
        temp_file.close()
        
        return FileResponse(
            temp_file.name,
            media_type="audio/mpeg",
            filename="podcast.mp3"
        )
    
    except Exception as e:
        logger.exception("Error in podcast endpoint")
        raise HTTPException(status_code=500, detail="Failed to generate podcast")

@app.get("/sources")
async def get_sources():
    """List all ingested sources as a simple list to match the frontend expectations, and include metadata."""
    urls = list(db.sources.keys())
    # sources_info for backwards compatibility / debugging
    sources_info = [{"url": url, "chunk_count": len(db.sources[url])} for url in urls]
    return {
        "total_sources": len(urls),
        "sources": urls,
        "sources_info": sources_info
    }

@app.post("/delete_source")
async def delete_source(req: DeleteSourceRequest):
    """Delete a specific source and rebuild index"""
    async with db.lock:
        if req.source_url not in db.sources:
            raise HTTPException(status_code=404, detail="Source not found")
        
        # Remove from sources
        del db.sources[req.source_url]
        
        # Remove from chunk metadata
        db.chunk_metadata = [
            meta for meta in db.chunk_metadata
            if meta['source_url'] != req.source_url
        ]
        
        # Rebuild index
        await rebuild_index()
    
    return {
        "status": "success",
        "message": f"Deleted source: {req.source_url}",
        "remaining_chunks": len(db.chunk_metadata)
    }

@app.post("/clear")
async def clear_database():
    """Clear entire database"""
    async with db.lock:
        db.sources.clear()
        db.chunk_metadata.clear()
        db.bm25 = None
    
    logger.info("Database cleared")
    return {
        "status": "success",
        "message": "Database cleared successfully"
    }

# --- STARTUP/SHUTDOWN ---
@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("🚀 Nano RAG Server Starting")
    logger.info(f"📍 Host: {config.HOST}:{config.PORT}")
    logger.info(f"🤖 AI Model: {config.MODEL_NAME}")
    logger.info("=" * 60)

@app.on_event("shutdown")
async def shutdown_event():
    executor.shutdown(wait=True)
    logger.info("Server shutdown complete")

# --- MAIN ---
if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="info"
    )