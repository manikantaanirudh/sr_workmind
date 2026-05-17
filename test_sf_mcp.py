import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv("backend/.env")

url = os.getenv("SALESFORCE_MCP_SERVER_URL")
token = "" # We need the token. Let's read it from the cache file.

token_file = "backend/logs/salesforce_tokens.json"
if os.path.exists(token_file):
    with open(token_file, "r") as f:
        data = json.load(f)
        token = data.get("access_token")

if not token:
    print("No token found")
    exit(1)

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

# Test 1: Just tools/list
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list"
}

print(f"Testing POST {url}")
resp = httpx.post(verify=False, url, headers=headers, json=payload)
print(f"Status: {resp.status_code}")
print(f"Body: {resp.text}")

print(f"Testing GET {url}")
resp_sse = httpx.get(verify=False, url, headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}, timeout=10.0)
print(f"Status: {resp_sse.status_code}")
print(f"Headers: {resp_sse.headers}")
print(f"Body: {resp_sse.text}")
