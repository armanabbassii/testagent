import sys
sys.path.insert(0, "/home/dotin/Downloads/agents2/agents")

import httpx  # noqa: E402
from src.config import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY  # noqa: E402

url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
print("POST", url)

resp = httpx.post(
    url,
    headers={
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    },
    json={
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Say hello in one word."},
        ],
        "temperature": 0.1,
        "max_tokens": 10,
    },
    timeout=30,
)

print("Status:", resp.status_code)
print("Content-Type:", resp.headers.get("content-type"))
print("Body (first 1000 chars):")
print(resp.text[:1000])