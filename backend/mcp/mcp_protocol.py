"""Shared MCP JSON-RPC response decoding (JSON and SSE)."""

from __future__ import annotations

import json
from typing import Any

import httpx


def parse_sse_json(text: str) -> dict[str, Any]:
    """Parse JSON-RPC objects from an SSE-style MCP response body (uses last result/error)."""
    last_obj: dict[str, Any] | None = None
    data_lines: list[str] = []

    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        if not clean.startswith("data:"):
            continue
        data = clean.split(":", 1)[1].strip()
        if not data or data == "[DONE]":
            continue
        data_lines.append(data)
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
            last_obj = obj

    if last_obj is not None:
        return last_obj

    if data_lines:
        return json.loads(data_lines[-1])

    raise RuntimeError("MCP returned SSE without a JSON data event.")


def decode_sse_stream(response: httpx.Response) -> dict[str, Any]:
    """Decode a buffered SSE MCP body (do not use iter_lines — it can block until timeout)."""
    text = response.text
    if not text.strip():
        return {}
    return parse_sse_json(text)


def decode_rpc_response(response: httpx.Response) -> dict[str, Any]:
    """Decode Snowflake/Salesforce MCP HTTP responses (JSON or SSE)."""
    text = response.text.strip()
    if not text:
        return {}

    content_type = response.headers.get("content-type", "").lower()
    if (
        "text/event-stream" in content_type
        or text.startswith("event:")
        or text.startswith("data:")
    ):
        return parse_sse_json(text)

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid MCP response (status {response.status_code}): {text[:500]}"
        ) from exc
