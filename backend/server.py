# FILE: backend/server.py
import os
import logging
from dotenv import load_dotenv
load_dotenv()
import uvicorn
import requests
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import List

# --- OLLAMA IMPORTS (Compatibility Mode) ---
from langchain_community.chat_models import ChatOllama
from langchain_community.embeddings import OllamaEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate
from langchain.docstore.document import Document
import edge_tts

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# CONFIG: Save database to a folder named "notebook_db"
PERSIST_DIRECTORY = "./notebook_db"
# CONFIG: Model Name
MODEL_NAME = "llama3"

# GLOBAL STATE
DB = {"vectorstore": None}

def get_vectorstore():
    if DB["vectorstore"] is None:
        # Use Ollama Embeddings
        embeddings = OllamaEmbeddings(model=MODEL_NAME)
        # Load existing DB or create new one on disk
        DB["vectorstore"] = Chroma(
            persist_directory=PERSIST_DIRECTORY, 
            embedding_function=embeddings
        )
    return DB["vectorstore"]

# --- 1. CRAWLER V2 (Robust) ---
def crawl_website(base_url, max_pages=1):
    domain = urlparse(base_url).netloc
    visited = set()
    queue = [base_url]
    documents = []
    
    # Headers to mimic a real browser
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    logger.info(f"🕷️ Starting Crawl: {base_url}")
    while queue and len(visited) < max_pages:
        current_url = queue.pop(0)
        if current_url in visited: continue
        try:
            logger.info(f"Visiting: {current_url}")
            resp = requests.get(current_url, headers=headers, timeout=5)
            
            # Check content type
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                logger.warning(f"Skipping non-HTML: {current_url} ({content_type})")
                continue
                
            soup = BeautifulSoup(resp.content, "html.parser")
            
            # Remove junk
            for s in soup(["script", "style", "nav", "footer", "iframe", "noscript"]): 
                s.decompose()
            
            text = soup.get_text(separator=' ', strip=True)
            
            if len(text) > 200:
                documents.append(Document(page_content=text, metadata={"source": current_url}))
                visited.add(current_url)
                
                # Add found links to queue
                for link in soup.find_all("a", href=True):
                    full = urljoin(current_url, link["href"])
                    if urlparse(full).netloc == domain and full not in visited: 
                        queue.append(full)
            else:
                logger.warning(f"Page too short, skipping: {current_url}")
                
        except Exception as e:
            logger.error(f"Failed to crawl {current_url}: {e}")
            
    logger.info(f"Crawl finished. Found {len(documents)} pages.")
    return documents

# --- API MODELS ---
class IngestRequest(BaseModel):
    url: str
    # API key is no longer strictly needed for Ollama, but kept for compatibility
    api_key: str = "" 

class ChatRequest(BaseModel):
    question: str
    api_key: str = ""

class DeleteSourceRequest(BaseModel):
    source_url: str

# --- ENDPOINTS ---

@app.post("/ingest")
async def ingest(req: IngestRequest):
    # No API Key validation needed for Ollama
    try:
        docs = crawl_website(req.url)
        if not docs: raise HTTPException(400, "No content found on this page (or blocked).")
        
        # Chunking
        splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=200)
        splits = splitter.split_documents(docs)
        
        # Add to Persistent DB
        logger.info(f"⏳ Hitting Ollama to embed {len(splits)} chunks... this might take a while.")
        vectorstore = get_vectorstore()
        vectorstore.add_documents(splits)
        logger.info(f"✅ Finished embedding! Added to DB.")
        
        return {"status": "success", "count": len(docs), "message": f"Added {len(docs)} pages to memory."}
    except Exception as e:
        logger.error(f"Ingest error: {e}")
        raise HTTPException(500, str(e))

@app.post("/chat")
async def chat(req: ChatRequest):
    logger.info(f"Chat request: {req.question}")
    try:
        vectorstore = get_vectorstore()
        # Use ChatOllama
        llm = ChatOllama(model=MODEL_NAME)
        
        prompt = ChatPromptTemplate.from_template("""
        Answer the question based ONLY on the context below.
        If you find the answer, refer to the source.
        
        <context>
        {context}
        </context>
        
        Question: {input}
        """)
        
        chain = create_retrieval_chain(vectorstore.as_retriever(), create_stuff_documents_chain(llm, prompt))
        response = chain.invoke({"input": req.question})
        
        sources = set()
        for doc in response["context"]:
            sources.add(doc.metadata.get("source", "Unknown"))
        
        return {
            "answer": response["answer"],
            "citations": list(sources)
        }
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(500, "Failed to generate answer. Is Ollama running?")

@app.post("/briefing")
async def briefing(req: IngestRequest):
    """Generates the 'Source Guide' (Summary/FAQ)"""
    try:
        retriever = get_vectorstore().as_retriever(search_kwargs={"k": 5})
        docs = retriever.invoke("Overview of the content")
        if not docs: return {"content": "No content available to summarize."}
        
        text = "\n".join([d.page_content for d in docs])
        llm = ChatOllama(model=MODEL_NAME)
        summary = llm.invoke(f"Create a Briefing Doc (Summary, 3 Key Topics, 3 FAQ) for: {text[:10000]}").content
        return {"content": summary}
    except Exception as e:
        logger.error(f"Briefing error: {e}")
        raise HTTPException(500, "Failed to generate briefing. Is Ollama running?")

@app.get("/podcast")
async def podcast():
    try:
        vectorstore = get_vectorstore()
        docs = vectorstore.similarity_search("Main concepts overview", k=10)
        if not docs: raise HTTPException(400, "Not enough content for a podcast.")
        
        context = "\n".join([d.page_content for d in docs])
        
        llm = ChatOllama(model=MODEL_NAME)
        script = llm.invoke(f"Create a 1-minute 2-host podcast script about: {context[:10000]}").content
        
        full_audio = b""
        for line in script.split('\n'):
            if "Host A:" in line:
                comm = edge_tts.Communicate(line.replace("Host A:",""), "en-US-GuyNeural")
                async for chunk in comm.stream(): 
                    if chunk["type"] == "audio": full_audio += chunk["data"]
            elif "Host B:" in line:
                comm = edge_tts.Communicate(line.replace("Host B:",""), "en-US-AriaNeural")
                async for chunk in comm.stream(): 
                    if chunk["type"] == "audio": full_audio += chunk["data"]
                    
        with open("podcast.mp3", "wb") as f: f.write(full_audio)
        return FileResponse("podcast.mp3")
    except Exception as e:
        logger.error(f"Podcast error: {e}")
        raise HTTPException(500, "Failed to generate podcast. Is Ollama running?")

@app.get("/sources")
async def get_sources():
    """List all unique sources in the DB"""
    try:
        vectorstore = get_vectorstore()
        # Fetch metadata only, limit 1000
        data = vectorstore.get(limit=1000, include=["metadatas"])
        sources = set()
        if data and data['metadatas']:
            for meta in data['metadatas']:
                if meta and "source" in meta:
                    sources.add(meta["source"])
        return {"sources": list(sources)}
    except Exception as e:
        logger.error(f"Get sources error: {e}")
        return {"sources": []}

@app.post("/delete_source")
async def delete_source(req: DeleteSourceRequest):
    """Deletes all chunks associated with a specific URL"""
    try:
        vectorstore = get_vectorstore()
        data = vectorstore.get(where={"source": req.source_url})
        if data and data['ids']:
            vectorstore.delete(ids=data['ids'])
            return {"status": "success", "message": f"Deleted {len(data['ids'])} chunks."}
        return {"status": "not_found", "message": "Source not found."}
    except Exception as e:
        logger.error(f"Delete source error: {e}")
        raise HTTPException(500, str(e))

@app.post("/clear")
async def clear_db():
    """Wipes the memory"""
    global DB
    if os.path.exists(PERSIST_DIRECTORY):
        import shutil
        shutil.rmtree(PERSIST_DIRECTORY)
    DB["vectorstore"] = None
    return {"status": "cleared"}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
