import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# Replicating the exact logic from server2.py for testing
def crawl_website(base_url, max_pages=5):
    print(f"Crawling: {base_url}")
    domain = urlparse(base_url).netloc
    visited = set()
    queue = [base_url]
    texts = []
    
    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited: continue
        try:
            print(f"  Fetching {url}...", end=" ")
            resp = requests.get(url, timeout=2) # Potential issue: User-Agent, Timeout
            print(f"Status: {resp.status_code}")
            
            if "text/html" not in resp.headers.get("Content-Type", ""): 
                print("    Skipping: Not HTML")
                continue
                
            soup = BeautifulSoup(resp.content, "html.parser")
            for s in soup(["script", "style", "nav", "footer"]): s.decompose()
            
            # Strict filtering here
            paras = [p.get_text().strip() for p in soup.find_all('p') if len(p.get_text()) > 50]
            print(f"    Found {len(paras)} valid paragraphs")
            
            texts.extend(paras)
            visited.add(url)
            
            for link in soup.find_all("a", href=True):
                full = urljoin(url, link["href"])
                if urlparse(full).netloc == domain and full not in visited: queue.append(full)
        except Exception as e:
            print(f"    Error: {e}")
            pass
    return texts

if __name__ == "__main__":
    # Test cases:
    # 1. Wikipedia (Usually works well with standard requests, has lots of <p>)
    # 2. Google (Often blocks or is complex structure)
    # 3. A dev blog (Simple HTML)
    
    urls = [
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        "https://example.com", 
    ]
    
    for u in urls:
        print("\n---")
        chunks = crawl_website(u, max_pages=1)
        print(f"Total Chunks for {u}: {len(chunks)}")
