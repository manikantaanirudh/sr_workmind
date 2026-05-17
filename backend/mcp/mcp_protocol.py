"""Shared MCP JSON-RPC response decoding (JSON and SSE)."""

from __future__ import annotations

import json
from typing import Any

import httpx


def parse_sse_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object from an SSE-style MCP response body."""
    data_lines: list[str] = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            if data_lines:
                break
            continue
        if clean.startswith("data:"):
            data = clean.split(":", 1)[1].strip()
            if data and data != "[DONE]":
                data_lines.append(data)

    if not data_lines:
        raise RuntimeError("MCP returned SSE without a JSON data event.")

    return json.loads("\n".join(data_lines))


def decode_sse_stream(response: httpx.Response) -> dict[str, Any]:
    """Read an SSE MCP stream until the first JSON-RPC result or error event."""
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line is None:
            continue
        clean = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
        clean = clean.strip()
        if not clean:
            if data_lines:
                break
            continue
        if clean.startswith("data:"):
            data = clean.split(":", 1)[1].strip()
            if not data or data == "[DONE]":
                continue
            data_lines.append(data)
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                return obj

    if data_lines:
        return json.loads("\n".join(data_lines))

    raise RuntimeError("MCP SSE stream ended without a JSON-RPC result.")


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
