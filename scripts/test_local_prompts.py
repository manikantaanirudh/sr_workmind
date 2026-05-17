#!/usr/bin/env python3
"""Run example prompts against a local backend (default http://127.0.0.1:8000)."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

PROMPTS = [
    "Show me the titles and release years of the 5 oldest movies in netflix_table",
    "Show top 5 customers by revenue",
    "List last 10 orders",
]


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as resp:
        return json.loads(resp.read().decode())


def post_execute(prompt: str) -> dict:
    body = json.dumps({"prompt": prompt}).encode()
    req = urllib.request.Request(
        f"{BASE}/execute",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    print(f"Backend: {BASE}\n")
    try:
        health = get("/health")
        print("health:", health)
        oauth = get("/config/oauth")
        print("oauth callbacks:")
        print("  SF:", oauth.get("salesforce_callback_url"))
        print("  DS:", oauth.get("docusign_callback_url"))
        print()
    except urllib.error.URLError as exc:
        print(f"Backend not reachable: {exc}")
        print("Start: uvicorn backend.main:app --host 0.0.0.0 --port 8000")
        return 1

    failed = 0
    for prompt in PROMPTS:
        print("=" * 60)
        print("PROMPT:", prompt)
        try:
            result = post_execute(prompt)
            print("SQL:", result.get("sql", "")[:200])
            print("Model:", result.get("model"))
            print("Connector:", result.get("connector"))
            rows = result.get("result", {}).get("rows", [])
            print("Rows:", len(rows))
            if rows:
                print("Sample:", rows[0])
            print("OK")
        except urllib.error.HTTPError as exc:
            failed += 1
            body = exc.read().decode()
            print(f"HTTP {exc.code}: {body[:500]}")
        except Exception as exc:
            failed += 1
            print(f"ERROR: {exc}")

    print()
    print(f"Done. Failed: {failed}/{len(PROMPTS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
