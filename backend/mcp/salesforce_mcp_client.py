"""
MCP Client for Salesforce's Hosted MCP Server.

Communicates with Salesforce's Hosted MCP Server via JSON-RPC 2.0 over HTTPS,
following the Model Context Protocol specification.

Authentication: Uses OAuth 2.0 Bearer tokens obtained via the PKCE flow
managed by salesforce_oauth.py.

Key difference from Snowflake MCP:
    - Salesforce exposes MULTIPLE tools (soqlQuery, find, createSobjectRecord, etc.)
    - Snowflake exposes a single SYSTEM_EXECUTE_SQL tool
    - Salesforce uses OAuth 2.0; Snowflake uses PAT
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from backend.config import settings
from backend.mcp.mcp_protocol import decode_rpc_response, decode_sse_stream
from backend.mcp.salesforce_oauth import get_valid_access_token

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
SF_CONNECTOR_LABEL = "Salesforce Hosted MCP Server (platform/sobject-all)"

# ---------------------------------------------------------------------------
# Session state (MCP session id is reused; HTTP client is per-request)
# ---------------------------------------------------------------------------

_sf_session_id: str | None = None
_sf_tools_cache: list[dict] | None = None

_DEFAULT_SF_TOOLS: list[dict] = [
    {"name": "soqlQuery"},
    {"name": "getObjectSchema"},
    {"name": "createSobjectRecord"},
    {"name": "updateSobjectRecord"},
    {"name": "deleteSobjectRecord"},
    {"name": "find"},
]


def get_salesforce_connector_label() -> str:
    return SF_CONNECTOR_LABEL


def probe_salesforce_connectivity() -> dict[str, Any]:
    """MCP-only probe for /health/diagnostics (no secrets)."""
    from backend.mcp.salesforce_oauth import auth_status, is_authenticated

    out: dict[str, Any] = {
        "mcp_server_url": settings.salesforce_mcp_server_url.strip(),
        "mcp_timeout_sec": settings.salesforce_mcp_timeout_sec,
        "mcp_init_timeout_sec": settings.salesforce_mcp_init_timeout_sec,
        "soql_row_limit": settings.salesforce_soql_row_limit,
    }
    if not is_authenticated():
        out["mcp_soql_query"] = "skipped (not authenticated)"
        return out
    out["oauth"] = auth_status()
    try:
        cols, rows = execute_soql_via_mcp("SELECT Id FROM Account LIMIT 1")
        out["mcp_soql_query"] = f"ok ({len(rows)} row(s), {len(cols)} col(s))"
    except Exception as exc:
        out["mcp_soql_query"] = f"error: {exc}"
    return out


def _sf_timeout(read_sec: float) -> httpx.Timeout:
    return httpx.Timeout(connect=20.0, read=read_sec, write=30.0, pool=20.0)


def _sf_auth_headers(include_session: bool = True) -> dict[str, str]:
    access_token = get_valid_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "mcp-protocol-version": MCP_PROTOCOL_VERSION,
    }
    if include_session and _sf_session_id:
        headers["mcp-session-id"] = _sf_session_id
    return headers


def _sf_jsonrpc_request(
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


def _decode_http_rpc(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        return decode_sse_stream(response)
    return decode_rpc_response(response)


def _send_initialized(client: httpx.Client, url: str) -> None:
    """MCP lifecycle: client must notify server after initialize (2024-11-05 spec)."""
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    timeout = _sf_timeout(float(settings.salesforce_mcp_init_timeout_sec))
    response = client.post(url, headers=_sf_auth_headers(), json=payload, timeout=timeout)
    if response.status_code not in {200, 202, 204}:
        logger.warning(
            "Salesforce MCP notifications/initialized returned %s: %s",
            response.status_code,
            response.text[:200],
        )


def _reset_sf_session() -> None:
    global _sf_session_id
    _sf_session_id = None


def _ensure_session(client: httpx.Client, url: str) -> None:
    """Initialize Salesforce hosted MCP session + send notifications/initialized."""
    global _sf_session_id
    if _sf_session_id:
        return

    init_timeout = float(settings.salesforce_mcp_init_timeout_sec)
    logger.info("Initializing Salesforce MCP session")
    init_payload = _sf_jsonrpc_request(
        "initialize",
        params={
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "sr-workmind", "version": "1.0.0"},
        },
        request_id=9999,
    )
    headers = _sf_auth_headers(include_session=False)
    response = client.post(
        url,
        headers=headers,
        json=init_payload,
        timeout=_sf_timeout(init_timeout),
    )

    if response.status_code == 401:
        raise RuntimeError(
            "Salesforce MCP authentication failed (401). Reconnect at /auth/salesforce."
        )
    if response.status_code not in {200, 202}:
        raise RuntimeError(
            f"Failed to initialize Salesforce MCP session ({response.status_code}): "
            f"{response.text[:300]}"
        )

    _sf_session_id = response.headers.get("mcp-session-id")
    if not _sf_session_id:
        raise RuntimeError(
            "Salesforce MCP initialize succeeded but no mcp-session-id header was returned."
        )

    init_data = _decode_http_rpc(response)
    if init_data.get("error"):
        err = init_data["error"]
        raise RuntimeError(
            f"Salesforce MCP initialize error ({err.get('code', 'unknown')}): "
            f"{err.get('message', 'Unknown error')}"
        )

    logger.info("Salesforce MCP session id acquired")
    _send_initialized(client, url)


def _sf_send_rpc(
    payload: dict[str, Any],
    *,
    retry: bool = True,
    read_timeout: float | None = None,
) -> dict[str, Any]:
    """POST JSON-RPC to Salesforce hosted MCP (SSE-safe streaming read)."""
    url = settings.salesforce_mcp_server_url.strip()
    if not url:
        raise RuntimeError("SALESFORCE_MCP_SERVER_URL is not configured.")

    read_sec = read_timeout if read_timeout is not None else float(settings.salesforce_mcp_timeout_sec)
    timeout = _sf_timeout(read_sec)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
            _ensure_session(client, url)
            headers = _sf_auth_headers()
            logger.info("SF MCP RPC -> %s method=%s", url, payload.get("method"))
            with client.stream(
                "POST", url, headers=headers, json=payload, timeout=timeout
            ) as response:
                if response.status_code == 401:
                    raise RuntimeError(
                        "Salesforce MCP Server authentication failed (401). "
                        "Reconnect at /auth/salesforce."
                    )
                if response.status_code == 403:
                    raise RuntimeError(
                        "Salesforce MCP Server access denied (403). "
                        "Check External Client App and user permissions."
                    )
                if response.status_code not in {200, 202}:
                    body = response.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Salesforce MCP HTTP {response.status_code}: {body[:500]}"
                    )

                content_type = response.headers.get("content-type", "").lower()
                if "text/event-stream" in content_type:
                    data = decode_sse_stream(response)
                else:
                    body = response.read()
                    if not body.strip():
                        data = {}
                    else:
                        text = body.decode("utf-8", errors="replace")
                        if text.strip().startswith("data:"):
                            data = decode_sse_stream(
                                httpx.Response(200, content=body, headers=response.headers)
                            )
                        else:
                            data = json.loads(text)

    except httpx.ReadTimeout as exc:
        _reset_sf_session()
        raise RuntimeError(
            f"Salesforce MCP request timed out after {read_sec:.0f}s. "
            "Retry once, or reconnect at /auth/salesforce if the session expired."
        ) from exc
    except httpx.TimeoutException as exc:
        _reset_sf_session()
        raise RuntimeError(
            f"Salesforce MCP connection timed out: {exc}. "
            "Check network access to api.salesforce.com."
        ) from exc

    if "error" in data:
        err = data["error"]
        raise RuntimeError(
            f"Salesforce MCP error (code={err.get('code', 'unknown')}): "
            f"{err.get('message', 'Unknown Salesforce MCP error')}"
        )

    result = data.get("result", data)
    if isinstance(result, dict) and result.get("isError"):
        content = result.get("content", [])
        error_text = content[0].get("text", "") if content else "Unknown error"
        if "Unexpected error" in error_text and retry:
            logger.info("Salesforce MCP session expired; retrying once")
            _reset_sf_session()
            new_payload = _sf_jsonrpc_request(
                str(payload.get("method", "")),
                payload.get("params") if isinstance(payload.get("params"), dict) else None,
            )
            return _sf_send_rpc(new_payload, retry=False, read_timeout=read_sec)

    return result if isinstance(result, dict) else {}


def sf_mcp_tools_list(force_refresh: bool = False) -> list[dict]:
    """Retrieve tools from Salesforce hosted MCP server (tools/list)."""
    global _sf_tools_cache
    if _sf_tools_cache is not None and not force_refresh:
        return _sf_tools_cache

    try:
        payload = _sf_jsonrpc_request("tools/list")
        response = _sf_send_rpc(payload, read_timeout=25.0)
        tools: list[dict] = []
        if isinstance(response, dict):
            tools = response.get("tools", [])

        if not tools:
            logger.info(
                "Salesforce tools/list empty; using documented sobject-all tool names."
            )
            tools = list(_DEFAULT_SF_TOOLS)
    except (RuntimeError, httpx.TimeoutException) as exc:
        logger.warning("Salesforce tools/list unavailable (%s); using defaults.", exc)
        tools = list(_DEFAULT_SF_TOOLS)

    _sf_tools_cache = tools
    return tools


def _build_sf_tool_arguments(tool_name: str, value_map: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "soqlQuery" and "query" in value_map:
        return {"query": value_map["query"]}
    if tool_name == "find" and value_map.get("search"):
        return {"search": value_map["search"]}
    return {k: v for k, v in value_map.items() if v is not None}


def sf_mcp_tools_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a tool on the Salesforce Hosted MCP Server (tools/call)."""
    payload = _sf_jsonrpc_request(
        "tools/call",
        params={"name": tool_name, "arguments": arguments},
    )
    result = _sf_send_rpc(payload)

    if result.get("isError"):
        content = result.get("content", [])
        error_text = content[0].get("text", "Unknown error") if content else "Unknown error"
        raise RuntimeError(f"Salesforce MCP tool '{tool_name}' error: {error_text}")

    return result


def _records_to_table(records: list[dict[str, Any]]) -> tuple[list[str], list[list[Any]]]:
    if not records:
        return ["status"], [["Query returned 0 records."]]
    skip_keys = {"attributes"}
    columns = [k for k in records[0].keys() if k not in skip_keys]
    rows: list[list[Any]] = []
    for record in records:
        row: list[Any] = []
        for col in columns:
            val = record.get(col)
            if isinstance(val, dict):
                val = val.get("Name", str(val))
            row.append(val)
        rows.append(row)
    return columns, rows


def execute_soql_via_mcp(soql: str) -> tuple[list[str], list[list[Any]]]:
    """Execute SOQL via hosted MCP soqlQuery tool only."""
    clean_soql = soql.strip().rstrip(";")
    tool_name = "soqlQuery"
    arguments = {"query": clean_soql}
    logger.info("Salesforce MCP tools/call -> %s", tool_name)
    result = sf_mcp_tools_call(tool_name, arguments)
    return _parse_sf_tool_result(result)


def execute_sf_search(search_query: str) -> tuple[list[str], list[list[Any]]]:
    tool_name = "find"
    arguments = _build_sf_tool_arguments(
        tool_name,
        {
            "search": search_query,
            "query": search_query,
            "q": search_query,
            "sosl": search_query,
        },
    )
    result = sf_mcp_tools_call(tool_name, arguments)
    return _parse_sf_tool_result(result)


def create_sf_record(sobject_name: str, fields: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    from backend.mcp.tool_registry import resolve_salesforce_tool

    tool_name = resolve_salesforce_tool("createSobjectRecord", "create_sobject_record")
    arguments = _build_sf_tool_arguments(
        tool_name,
        {
            "sobject-name": sobject_name,
            "sobject_name": sobject_name,
            "object": sobject_name,
            "body": fields,
            "fields": fields,
        },
    )
    result = sf_mcp_tools_call(tool_name, arguments)
    content = result.get("content", [])
    if content:
        text = content[0].get("text", "")
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return list(data.keys()), [list(data.values())]
        except (json.JSONDecodeError, ValueError):
            return ["status"], [[text]]
    return ["status"], [["Record created successfully."]]


def update_sf_record(
    sobject_name: str, record_id: str, fields: dict[str, Any]
) -> tuple[list[str], list[list[Any]]]:
    from backend.mcp.tool_registry import resolve_salesforce_tool

    tool_name = resolve_salesforce_tool("updateSobjectRecord", "update_sobject_record")
    arguments = _build_sf_tool_arguments(
        tool_name,
        {
            "sobject-name": sobject_name,
            "sobject_name": sobject_name,
            "id": record_id,
            "record_id": record_id,
            "body": fields,
            "fields": fields,
        },
    )
    sf_mcp_tools_call(tool_name, arguments)
    return ["status"], [["Record updated successfully."]]


def delete_sf_record(sobject_name: str, record_id: str) -> tuple[list[str], list[list[Any]]]:
    from backend.mcp.tool_registry import resolve_salesforce_tool

    tool_name = resolve_salesforce_tool("deleteSobjectRecord", "delete_sobject_record")
    arguments = _build_sf_tool_arguments(
        tool_name,
        {
            "sobject-name": sobject_name,
            "sobject_name": sobject_name,
            "id": record_id,
            "record_id": record_id,
        },
    )
    sf_mcp_tools_call(tool_name, arguments)
    return ["status"], [["Record deleted successfully."]]


def get_sf_schema(object_name: str = "") -> tuple[list[str], list[list[Any]]]:
    from backend.mcp.tool_registry import resolve_salesforce_tool

    tool_name = resolve_salesforce_tool("getObjectSchema", "get_object_schema")
    value_map: dict[str, Any] = {}
    if object_name:
        value_map.update(
            {
                "object-name": object_name,
                "object_name": object_name,
                "object": object_name,
                "sobject": object_name,
            }
        )
    arguments = _build_sf_tool_arguments(tool_name, value_map)
    result = sf_mcp_tools_call(tool_name, arguments)
    return _parse_sf_tool_result(result)


def _parse_sf_tool_result(result: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    content_list = result.get("content", [])
    if not content_list:
        return ["status"], [["Operation completed successfully."]]

    for item in content_list:
        item_type = item.get("type", "text")
        if item_type == "text":
            text = item.get("text", "")
            parsed = _try_parse_sf_result(text)
            if parsed:
                return parsed
            return ["result"], [[text]]
        if item_type == "resource":
            resource = item.get("resource", {})
            text = resource.get("text", "")
            parsed = _try_parse_sf_result(text)
            if parsed:
                return parsed

    return ["status"], [["Operation completed."]]


def _try_parse_sf_result(text: str) -> tuple[list[str], list[list[Any]]] | None:
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if isinstance(data, dict) and "records" in data:
        return _records_to_table(data["records"])

    if isinstance(data, list) and data and isinstance(data[0], dict):
        skip_keys = {"attributes"}
        columns = [k for k in data[0].keys() if k not in skip_keys]
        rows = [[row.get(col) for col in columns] for row in data]
        return columns, rows

    if isinstance(data, dict):
        skip_keys = {"attributes"}
        columns = [k for k in data.keys() if k not in skip_keys]
        rows = [[data.get(col) for col in columns]]
        return columns, rows

    return None
