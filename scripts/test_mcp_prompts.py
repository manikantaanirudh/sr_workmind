#!/usr/bin/env python3
"""Test example prompts against local backend (MCP-only path)."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

PROMPTS = [
    "What are the top 5 countries that have produced the most Movies on Netflix_table?",
    "Show all accounts in salesforce",
]


def post_execute(prompt: str) -> dict:
    body = json.dumps({"prompt": prompt}).encode()
    req = urllib.request.Request(
        f"{BASE}/execute",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    print(f"Backend: {BASE}\n")
    failed = 0
    for prompt in PROMPTS:
        print("=" * 70)
        print("PROMPT:", prompt)
        try:
            result = post_execute(prompt)
            rows = result.get("result", {}).get("rows", [])
            print("OK | connector:", result.get("connector"))
            print("   model:", result.get("model"))
            print("   mcp:", (result.get("mcp_validation") or "")[:120])
            print("   sql:", (result.get("sql") or "")[:150])
            print("   rows:", len(rows))
            if rows:
                print("   sample:", rows[0])
        except urllib.error.HTTPError as exc:
            failed += 1
            print(f"HTTP {exc.code}:", exc.read().decode()[:500])
        except Exception as exc:
            failed += 1
            print("ERROR:", exc)
    print(f"\nFailed: {failed}/{len(PROMPTS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
