"""
MCP Client for Salesforce's Hosted MCP Server.

Communicates with Salesforce's Hosted MCP Server via JSON-RPC 2.0 over HTTPS,
following the Model Context Protocol specification.

Authentication: Uses OAuth 2.0 Bearer tokens obtained via the PKCE flow
managed by salesforce_oauth.py.

Key difference from Snowflake MCP:
    - Salesforce exposes MULTIPLE tools (soql_query, find, createSobjectRecord, etc.)
    - Snowflake exposes a single SYSTEM_EXECUTE_SQL tool
    - Salesforce uses OAuth 2.0; Snowflake uses PAT
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from backend.config import settings
from backend.mcp.mcp_protocol import decode_rpc_response
from backend.mcp.salesforce_oauth import get_valid_access_token

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authentication headers
# ---------------------------------------------------------------------------

_sf_session_id: str | None = None


def _sf_auth_headers() -> dict[str, str]:
    """Return OAuth Bearer token headers for Salesforce MCP Server."""
    access_token = get_valid_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _sf_session_id:
        headers["mcp-session-id"] = _sf_session_id
    return headers


# ---------------------------------------------------------------------------
# Low-level JSON-RPC helpers
# ---------------------------------------------------------------------------

def _sf_jsonrpc_request(
    method: str,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
) -> dict:
    """Build a JSON-RPC 2.0 request envelope."""
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params:
        payload["params"] = params
    return payload


def _ensure_session(client: httpx.Client, url: str) -> None:
    """Ensure a valid MCP session exists."""
    global _sf_session_id
    if _sf_session_id:
        return

    logger.info("Initializing Salesforce MCP session")
    init_payload = {
        "jsonrpc": "2.0",
        "id": 9999,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "sr-workmind",
                "version": "1.0.0"
            }
        }
    }
    # Get base headers without session ID
    headers = _sf_auth_headers()
    headers.pop("mcp-session-id", None)
    
    resp = client.post(url, headers=headers, json=init_payload)
    if resp.status_code == 200:
        _sf_session_id = resp.headers.get("mcp-session-id")
        logger.info("Salesforce MCP session id acquired")
    else:
        logger.error("Salesforce MCP initialize failed: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"Failed to initialize Salesforce MCP session: {resp.text}")


def _sf_send_rpc(payload: dict, retry: bool = True, timeout: float = 90.0) -> dict:
    """POST a JSON-RPC request to the Salesforce Hosted MCP Server."""
    url = settings.salesforce_mcp_server_url.strip()

    with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
        # 1. Initialize session if needed
        _ensure_session(client, url)
        
        # 2. Get headers (which will now include mcp-session-id)
        headers = _sf_auth_headers()

        logger.info("SF MCP RPC -> %s method=%s", url, payload.get("method"))

        # 3. Send the actual request
        response = client.post(url, headers=headers, json=payload)

    logger.debug("SF MCP response status=%s", response.status_code)

    if response.status_code == 401:
        raise RuntimeError(
            f"Salesforce MCP Server authentication failed (401). "
            f"Please re-authenticate at /auth/salesforce. "
            f"Raw: {response.text[:300]}"
        )
    if response.status_code == 403:
        raise RuntimeError(
            "Salesforce MCP Server access denied (403). "
            "Ensure the user has the correct permissions and the External Client App is configured."
        )

    # If body is empty (like for tools/list), raise a specific JSON error
    text = response.text.strip()
    if not text:
        return {}

    try:
        data = decode_rpc_response(response)
    except RuntimeError as exc:
        raise RuntimeError(f"Invalid response from Salesforce MCP Server: {exc}") from exc

    # Check for JSON-RPC level errors
    if "error" in data:
        err = data["error"]
        code = err.get("code", "unknown")
        message = err.get("message", "Unknown Salesforce MCP error")
        raise RuntimeError(f"Salesforce MCP error (code={code}): {message}")

    # Check for tool-level errors inside the result object
    if "result" in data and isinstance(data["result"], dict) and data["result"].get("isError"):
        content = data["result"].get("content", [])
        error_text = content[0].get("text", "") if content else "Unknown error"
        
        # Salesforce MCP sessions expire after ~30 minutes, returning "Unexpected error"
        if "Unexpected error" in error_text and retry:
            global _sf_session_id
            if _sf_session_id:
                logger.info("Salesforce MCP session expired; retrying once")
                _sf_session_id = None
                # Generate a fresh payload with a new session id
                if "method" in payload:
                    new_payload = _sf_jsonrpc_request(payload["method"], payload.get("params"))
                    # Recursively retry once
                    return _sf_send_rpc(new_payload, retry=False)
        
        # If it's a different tool error or the retry failed, raise it
        # We don't raise it here to match the existing parser logic, we let the caller handle it.

    return data.get("result", data)


# ---------------------------------------------------------------------------
# Tool Discovery
# ---------------------------------------------------------------------------

_sf_tools_cache: list[dict] | None = None

# Salesforce hosted MCP may return an empty tools/list body (async catalog). These are the
# documented tools for platform/sobject-all per Salesforce Hosted MCP Server reference.
_DEFAULT_SF_TOOLS: list[dict] = [
    {"name": "soqlQuery"},
    {"name": "getObjectSchema"},
    {"name": "createSobjectRecord"},
    {"name": "updateSobjectRecord"},
    {"name": "deleteSobjectRecord"},
    {"name": "find"},
]


def sf_mcp_tools_list(force_refresh: bool = False) -> list[dict]:
    """Retrieve tools from Salesforce hosted MCP server (tools/list).

    See: https://developer.salesforce.com/docs/platform/hosted-mcp-servers/
    """
    global _sf_tools_cache
    if _sf_tools_cache is not None and not force_refresh:
        return _sf_tools_cache

    try:
        payload = _sf_jsonrpc_request("tools/list")
        response = _sf_send_rpc(payload, timeout=20.0)
        tools: list[dict] = []
        if isinstance(response, dict):
            if "tools" in response:
                tools = response.get("tools", [])
            elif "result" in response and isinstance(response["result"], dict):
                tools = response["result"].get("tools", [])

        if not tools:
            logger.info(
                "Salesforce tools/list empty; using documented sobject-all tool names."
            )
            tools = list(_DEFAULT_SF_TOOLS)
    except RuntimeError as exc:
        if "empty" in str(exc).lower() or "invalid" in str(exc).lower():
            logger.warning("Salesforce tools/list unavailable (%s); using defaults.", exc)
            tools = list(_DEFAULT_SF_TOOLS)
        else:
            raise

    _sf_tools_cache = tools
    return tools


def _build_sf_tool_arguments(tool_name: str, value_map: dict[str, Any]) -> dict[str, Any]:
    """Map logical argument names to the tool's declared inputSchema property names."""
    for tool in sf_mcp_tools_list():
        if tool.get("name") != tool_name:
            continue
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if not properties:
            break
        arguments: dict[str, Any] = {}
        for prop in properties:
            if prop in value_map:
                arguments[prop] = value_map[prop]
                continue
            prop_lower = prop.lower().replace("-", "").replace("_", "")
            for key, val in value_map.items():
                key_norm = key.lower().replace("-", "").replace("_", "")
                if prop_lower == key_norm:
                    arguments[prop] = val
                    break
        if arguments:
            return arguments

    return {k: v for k, v in value_map.items() if v is not None}


# ---------------------------------------------------------------------------
# Tool Invocation
# ---------------------------------------------------------------------------

def sf_mcp_tools_call(tool_name: str, arguments: dict[str, Any]) -> dict:
    """Invoke a specific tool on the Salesforce Hosted MCP Server.

    Args:
        tool_name: The MCP tool name (e.g. 'soql_query', 'createSobjectRecord').
        arguments: The tool arguments dict.

    Returns:
        The tool result from the MCP server.
    """
    payload = _sf_jsonrpc_request(
        "tools/call",
        params={"name": tool_name, "arguments": arguments},
    )
    result = _sf_send_rpc(payload)

    # Check for tool-level errors
    if result.get("isError"):
        content = result.get("content", [])
        error_text = content[0].get("text", "Unknown error") if content else "Unknown error"
        raise RuntimeError(f"Salesforce MCP tool '{tool_name}' error: {error_text}")

    return result


# ---------------------------------------------------------------------------
# Public API — High-level operations
# ---------------------------------------------------------------------------

def execute_soql_via_mcp(soql: str) -> tuple[list[str], list[list[Any]]]:
    """Execute SOQL via Salesforce hosted MCP server (soqlQuery tool)."""
    clean_soql = soql.strip().rstrip(";")
    # platform/sobject-all always exposes soqlQuery — skip slow tools/list on Render.
    tool_name = "soqlQuery"
    arguments = {"query": clean_soql}
    logger.info("Salesforce MCP tools/call -> %s", tool_name)
    result = sf_mcp_tools_call(tool_name, arguments)
    return _parse_sf_tool_result(result)


def execute_sf_search(search_query: str) -> tuple[list[str], list[list[Any]]]:
    """Execute a SOSL search through the Salesforce MCP Server.

    Args:
        search_query: The SOSL search expression.

    Returns:
        A tuple of (columns, rows).
    """
    from backend.mcp.tool_registry import resolve_salesforce_tool

    tool_name = resolve_salesforce_tool("find", "search")
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
    """Create a new Salesforce record.

    Args:
        sobject_name: The SObject type (e.g. 'Account', 'Contact').
        fields: Dict of field names to values.

    Returns:
        A tuple of (columns, rows) with the created record info.
    """
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


def update_sf_record(sobject_name: str, record_id: str, fields: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """Update an existing Salesforce record.

    Args:
        sobject_name: The SObject type.
        record_id: The Salesforce record ID.
        fields: Dict of field names to new values.

    Returns:
        A tuple of (columns, rows) with the update result.
    """
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
    result = sf_mcp_tools_call(tool_name, arguments)
    return ["status"], [["Record updated successfully."]]


def delete_sf_record(sobject_name: str, record_id: str) -> tuple[list[str], list[list[Any]]]:
    """Delete a Salesforce record.

    Args:
        sobject_name: The SObject type.
        record_id: The Salesforce record ID.

    Returns:
        A tuple of (columns, rows) with the delete result.
    """
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
    result = sf_mcp_tools_call(tool_name, arguments)
    return ["status"], [["Record deleted successfully."]]


def get_sf_schema(object_name: str = "") -> tuple[list[str], list[list[Any]]]:
    """Get Salesforce object schema information.

    Args:
        object_name: Optional object name for detailed schema. Empty for index.

    Returns:
        A tuple of (columns, rows).
    """
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


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------

def _parse_sf_tool_result(result: dict) -> tuple[list[str], list[list[Any]]]:
    """Parse the Salesforce MCP tool result into (columns, rows).

    The Salesforce MCP server returns results in the 'content' array,
    typically as JSON text.
    """
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

        elif item_type == "resource":
            resource = item.get("resource", {})
            text = resource.get("text", "")
            parsed = _try_parse_sf_result(text)
            if parsed:
                return parsed

    return ["status"], [["Operation completed."]]


def _try_parse_sf_result(text: str) -> tuple[list[str], list[list[Any]]] | None:
    """Attempt to parse a text response as structured data."""
    if not text.strip():
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    # Handle SOQL result format: {"totalSize": N, "records": [...]}
    if isinstance(data, dict) and "records" in data:
        records = data["records"]
        if not records:
            return ["status"], [["Query returned 0 records."]]
        # Filter out Salesforce metadata keys
        skip_keys = {"attributes"}
        columns = [k for k in records[0].keys() if k not in skip_keys]
        rows = []
        for record in records:
            row = []
            for col in columns:
                val = record.get(col)
                # Handle nested relationship objects
                if isinstance(val, dict):
                    val = val.get("Name", str(val))
                row.append(val)
            rows.append(row)
        return columns, rows

    # Handle array of objects: [{...}, {...}]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        skip_keys = {"attributes"}
        columns = [k for k in data[0].keys() if k not in skip_keys]
        rows = [[row.get(col) for col in columns] for row in data]
        return columns, rows

    # Handle single object: {...}
    if isinstance(data, dict):
        skip_keys = {"attributes"}
        columns = [k for k in data.keys() if k not in skip_keys]
        rows = [[data.get(col) for col in columns]]
        return columns, rows

    return None
