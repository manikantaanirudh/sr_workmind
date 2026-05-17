#!/usr/bin/env python3
"""Test 'Show all accounts in salesforce' against local or Render backend."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PROMPT = "Show all accounts in salesforce"


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode())


def post_execute(prompt: str) -> tuple[int, dict | str]:
    body = json.dumps({"prompt": prompt}).encode()
    req = urllib.request.Request(
        f"{BASE}/execute",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def main() -> int:
    print(f"Backend: {BASE}\n")
    print("=== /auth/salesforce/status ===")
    try:
        status = get("/auth/salesforce/status")
        print(json.dumps(status, indent=2))
    except Exception as exc:
        print("FAIL:", exc)
        return 1

    if not status.get("authenticated"):
        print(
            "\nNot connected. Open in browser:\n"
            f"  {BASE}/auth/salesforce\n"
            "Then re-run this script."
        )
        return 1

    print("\n=== POST /execute ===")
    code, result = post_execute(PROMPT)
    print("HTTP", code)
    if code == 200 and isinstance(result, dict):
        rows = result.get("result", {}).get("rows", [])
        print("OK | rows:", len(rows))
        print("sql:", (result.get("sql") or "")[:120])
        if rows:
            print("sample:", rows[0])
        return 0

    print(json.dumps(result, indent=2)[:800])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
