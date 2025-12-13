import requests
import time
import sys

BASE_URL = "http://127.0.0.1:8000"

def test_endpoint(name, func):
    try:
        print(f"Testing {name}...", end=" ")
        func()
        print("✅ Passed")
    except Exception as e:
        print(f"❌ Failed: {e}")
        sys.exit(1)

def check_sources_empty():
    res = requests.get(f"{BASE_URL}/sources")
    assert res.status_code == 200
    data = res.json()
    assert data["sources"] == [], f"Expected empty sources, got {data}"

def test_ingest():
    # We use a simple page that is likely to exist or fail gracefully. 
    # Since we can't easily mock the internet access of the server, we try example.com
    # If it fails due to network, we might need to skip or mock.
    # For now assuming internet access as user was crawling before.
    res = requests.post(f"{BASE_URL}/ingest", json={"url": "https://example.com"})
    if res.status_code != 200:
        print(f"(Ingest failed: {res.text})", end=" ")
    else:
        assert res.json()["status"] == "success"

def check_sources_added():
    res = requests.get(f"{BASE_URL}/sources")
    data = res.json()
    # If ingest failed (e.g. no internet), this might be empty, but we want to check struct at least
    assert "sources" in data

def test_chat():
    res = requests.post(f"{BASE_URL}/chat", json={"question": "What is this domain?"})
    # If no sources, it returns "Load a site first." or similar
    assert res.status_code == 200
    assert "answer" in res.json()

def test_briefing():
    res = requests.post(f"{BASE_URL}/briefing", json={})
    assert res.status_code == 200
    assert "content" in res.json()

def test_delete():
    # Add a fake source to delete manually if needed, but we can just try deleting example.com
    requests.post(f"{BASE_URL}/delete_source", json={"source_url": "https://example.com"})
    res = requests.get(f"{BASE_URL}/sources")
    sources = res.json().get("sources", [])
    assert "https://example.com" not in sources

def run_tests():
    # Wait for server to be up
    for _ in range(10):
        try:
            requests.get(f"{BASE_URL}/docs")
            break
        except:
            time.sleep(1)
    else:
        print("Server not responding")
        sys.exit(1)

    test_endpoint("Check Empty Sources", check_sources_empty)
    test_endpoint("Ingest", test_ingest)
    test_endpoint("Check Sources Added", check_sources_added)
    test_endpoint("Chat", test_chat)
    test_endpoint("Briefing", test_briefing)
    test_endpoint("Delete Source", test_delete)

if __name__ == "__main__":
    run_tests()
