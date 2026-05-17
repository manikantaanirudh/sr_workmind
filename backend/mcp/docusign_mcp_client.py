"""
Docusign managed MCP client.

Uses JSON-RPC 2.0 over HTTP with support for SSE-formatted responses. The
server URL is beta/config-driven because Docusign can change the hosted MCP
endpoint while the program is in beta.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from backend.config import settings
from backend.mcp.docusign_oauth import get_granted_scopes, get_valid_access_token

logger = logging.getLogger(__name__)

_ds_session_id: str | None = None
_tools_cache: list[dict[str, Any]] | None = None


def _auth_headers(include_session: bool = True) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {get_valid_access_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if include_session and _ds_session_id:
        headers["mcp-session-id"] = _ds_session_id
    return headers


def _jsonrpc_request(
    method: str,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    return payload


def _parse_sse_response(text: str) -> dict[str, Any]:
    """Parse the first JSON data event from an SSE response body."""
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
        raise RuntimeError("Docusign MCP returned an SSE response with no JSON data event.")

    return json.loads("\n".join(data_lines))


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    text = response.text.strip()
    content_type = response.headers.get("content-type", "").lower()
    if not text:
        return {}
    if "text/event-stream" in content_type or text.startswith("event:") or text.startswith("data:"):
        return _parse_sse_response(text)
    return response.json()


def _ensure_session(client: httpx.Client, url: str) -> None:
    global _ds_session_id
    if _ds_session_id:
        return

    payload = _jsonrpc_request(
        "initialize",
        params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "sr-workmind-docusign",
                "version": "1.0.0",
            },
        },
        request_id=1000,
    )
    response = client.post(url, headers=_auth_headers(include_session=False), json=payload)
    if response.status_code in {401, 403}:
        if response.status_code == 403:
            granted = ", ".join(sorted(get_granted_scopes())) or "none"
            raise RuntimeError(
                "Docusign MCP access denied (403). The OAuth token is valid, but this "
                "Docusign user/app/account is not authorized for the managed MCP server. "
                f"Granted scopes: {granted}. Required beta/IAM access usually includes "
                "Docusign MCP plus Navigator agreement read permissions "
                "(agreement_object_model_read) for agreement queries."
            )
        raise RuntimeError(
            f"Docusign MCP authentication/access failed ({response.status_code}): "
            f"{response.text[:500]}"
        )
    if response.status_code == 404:
        raise RuntimeError(
            f"Docusign MCP endpoint not found at {url}. "
            "Set DOCUSIGN_MCP_SERVER_URL to the beta endpoint from Docusign."
        )

    response.raise_for_status()
    _ds_session_id = response.headers.get("mcp-session-id")
    data = _decode_response(response)
    if data.get("error"):
        err = data["error"]
        raise RuntimeError(
            f"Docusign MCP initialize error ({err.get('code', 'unknown')}): "
            f"{err.get('message', 'Unknown error')}"
        )


def _send_rpc(payload: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    url = settings.docusign_mcp_server_url.strip()
    if not url:
        raise RuntimeError("DOCUSIGN_MCP_SERVER_URL is required in backend/.env")

    with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
        _ensure_session(client, url)
        logger.info("Docusign MCP RPC -> %s method=%s", url, payload.get("method"))
        response = client.post(url, headers=_auth_headers(), json=payload)

    if response.status_code == 401:
        raise RuntimeError(
            "Docusign MCP authentication failed (401). Please re-authenticate at /auth/docusign."
        )
    if response.status_code == 403:
        granted = ", ".join(sorted(get_granted_scopes())) or "none"
        raise RuntimeError(
            "Docusign MCP access denied (403). The OAuth token is valid, but this "
            "Docusign user/app/account is not authorized for the managed MCP server. "
            f"Granted scopes: {granted}. Required beta/IAM access usually includes "
            "Docusign MCP plus Navigator agreement read permissions "
            "(agreement_object_model_read) for agreement queries."
        )
    if response.status_code == 404:
        raise RuntimeError(
            f"Docusign MCP endpoint not found at {url}. Verify DOCUSIGN_MCP_SERVER_URL."
        )

    response.raise_for_status()
    data = _decode_response(response)
    if data.get("error"):
        err = data["error"]
        raise RuntimeError(
            f"Docusign MCP error ({err.get('code', 'unknown')}): "
            f"{err.get('message', 'Unknown error')}"
        )
    return data.get("result", data)


def ds_mcp_tools_list(force_refresh: bool = False) -> list[dict[str, Any]]:
    global _tools_cache
    if _tools_cache is not None and not force_refresh:
        return _tools_cache

    result = _send_rpc(_jsonrpc_request("tools/list"), timeout=30.0)
    tools = result.get("tools", []) if isinstance(result, dict) else []
    _tools_cache = tools
    return tools


def ds_mcp_tools_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = _send_rpc(
        _jsonrpc_request(
            "tools/call",
            params={
                "name": tool_name,
                "arguments": arguments,
            },
        ),
        timeout=120.0,
    )
    if isinstance(result, dict) and result.get("isError"):
        content = result.get("content", [])
        error_text = content[0].get("text", "Unknown error") if content else "Unknown error"
        raise RuntimeError(f"Docusign MCP tool '{tool_name}' error: {error_text}")
    return result
