#!/usr/bin/env python3
"""Diagnose Snowflake hosted MCP tools/call responses."""
from __future__ import annotations

import base64
import json
import sys

import httpx

from backend.config import settings
from backend.mcp.mcp_client import _auth_headers, _build_mcp_url, _jsonrpc_request
from backend.mcp.mcp_protocol import decode_rpc_response


def _pat_payload() -> dict:
    token = settings.snowflake_pat.strip()
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def _post(payload: dict, extra_headers: dict | None = None) -> httpx.Response:
    url = _build_mcp_url()
    headers = _auth_headers()
    if extra_headers:
        headers.update(extra_headers)
    with httpx.Client(timeout=120.0, verify=False) as client:
        return client.post(url, headers=headers, json=payload)


def _dump(label: str, response: httpx.Response) -> dict:
    print(f"\n=== {label} HTTP {response.status_code} ===")
    print("content-type:", response.headers.get("content-type"))
    print("body (first 2500 chars):\n", response.text[:2500])
    try:
        data = decode_rpc_response(response)
        print("parsed top-level keys:", list(data.keys()))
        result = data.get("result", data)
        if isinstance(result, dict):
            print("result.isError:", result.get("isError"))
            print("result.content:", json.dumps(result.get("content"), indent=2)[:1200])
            print("structuredContent:", str(result.get("structuredContent"))[:600])
        return data
    except Exception as exc:
        print("decode error:", exc)
        return {}


def main() -> int:
    print("MCP URL:", _build_mcp_url())
    print("PAT payload (no token):", _pat_payload())

    _dump("tools/list", _post(_jsonrpc_request("tools/list")))

    for arg_name in ("sql", "query", "statement"):
        payload = _jsonrpc_request(
            "tools/call",
            {"name": "sql_exec_tool", "arguments": {arg_name: "SELECT 1 AS n"}},
        )
        _dump(f"tools/call arg={arg_name}", _post(payload))

    init = {
        "jsonrpc": "2.0",
        "id": 9999,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "sr-workmind", "version": "1.0.0"},
        },
    }
    init_resp = _post(init)
    _dump("initialize", init_resp)
    session_id = init_resp.headers.get("mcp-session-id")
    extra = {"mcp-session-id": session_id} if session_id else None
    if session_id:
        print("mcp-session-id:", session_id)

    payload = _jsonrpc_request(
        "tools/call",
        {"name": "sql_exec_tool", "arguments": {"sql": "SELECT 1 AS n"}},
    )
    _dump("tools/call after initialize", _post(payload, extra))

    # Headers some clients use
    hdrs = {
        "mcp-protocol-version": "2024-11-05",
        "Accept-Encoding": "identity",
    }
    _dump("tools/call with mcp-protocol-version", _post(payload, hdrs))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
