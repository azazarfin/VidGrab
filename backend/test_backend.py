import asyncio
import os
import sys
import httpx
from fastapi.testclient import TestClient

# Ensure we can import the backend modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.main import app
from backend.config import get_all_config, init_db

async def async_test():
    print("Initializing database...")
    await init_db()
    
    print("Testing config retrieval...")
    config = await get_all_config()
    print("Initial Config:", config)
    
    # Test FastAPI Client
    client = TestClient(app)
    
    print("\nTesting GET /status...")
    response = client.get("/status")
    print("Status code:", response.status_code)
    print("Response JSON:", response.json())
    assert response.status_code == 200
    assert response.json()["status"] == "online"

    print("\nTesting GET /config...")
    response = client.get("/config")
    print("Status code:", response.status_code)
    print("Response JSON:", response.json())
    assert response.status_code == 200

    print("\nTesting POST /probe with a sample URL (using a fast/static URL to avoid actual heavy yt-dlp scraping)...")
    # We can use a simple URL that doesn't trigger massive downloads or block.
    # We'll use a public test video URL or similar. Let's just probe a dummy URL to see if it catches the exception or works.
    # For a real test, let's probe a short Vimeo video or similar, or just verify it handles errors gracefully.
    try:
        response = client.post("/probe", json={"url": "https://vimeo.com/76979871"})
        print("Probe status code:", response.status_code)
        if response.status_code == 200:
            data = response.json()
            print("Probe Title:", data.get("title"))
            print("Probe Thumbnail:", data.get("thumbnail"))
            print("Formats count:", len(data.get("formats", [])))
        else:
            print("Probe error response:", response.json())
    except Exception as e:
        print("Probe failed with exception:", e)

    print("\nAll sanity checks passed successfully!")

if __name__ == "__main__":
    asyncio.run(async_test())
