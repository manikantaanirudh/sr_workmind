"""
Standardized MCP Client for Snowflake's Managed MCP Server.

Communicates with the Snowflake MCP Server via JSON-RPC 2.0 over HTTPS,
following the Model Context Protocol specification.

Authentication: Uses the Snowflake Python Connector to authenticate with
username/password and retrieve a valid REST session token. This token is
then used as the Bearer token for the MCP REST API.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
import snowflake.connector

from backend.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token cache (module-level singleton)
# ---------------------------------------------------------------------------
_token_cache: dict[str, Any] = {
    "access_token": None,
    "expires_at": 0.0,
}


# ---------------------------------------------------------------------------
# MCP Endpoint URL builder
# ---------------------------------------------------------------------------

def _get_account_host() -> str:
    """Normalise the Snowflake account identifier to a hostname."""
    account = settings.snowflake_account.strip()
    if "." not in account:
        return f"{account.lower()}.snowflakecomputing.com"
    return account.lower()


def _build_mcp_url() -> str:
    """Build the Snowflake MCP Server REST endpoint URL.

    Format:
        https://<account>.snowflakecomputing.com/api/v2/databases/{db}/schemas/{schema}/mcp-servers/{name}
    """
    host = _get_account_host()
    database = settings.snowflake_database.strip().upper()
    schema = settings.snowflake_schema.strip().upper()
    server_name = settings.mcp_server_name.strip().upper()

    return (
        f"https://{host}/api/v2/databases/{database}"
        f"/schemas/{schema}/mcp-servers/{server_name}"
    )


# ---------------------------------------------------------------------------
# PAT Authentication
# ---------------------------------------------------------------------------

def _auth_headers() -> dict[str, str]:
    """Return authorisation headers using a Programmatic Access Token (PAT)."""
    pat = settings.snowflake_pat.strip()
    
    if not pat:
        raise RuntimeError("SNOWFLAKE_PAT is required in .env for MCP Server authentication.")
        
    return {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
    }


# ---------------------------------------------------------------------------
# Low-level JSON-RPC helpers
# ---------------------------------------------------------------------------

def _jsonrpc_request(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict:
    """Build a JSON-RPC 2.0 request envelope."""
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params:
        payload["params"] = params
    return payload


def _send_rpc(payload: dict, timeout: float = 120.0) -> dict:
    """POST a JSON-RPC request to the Snowflake MCP Server and return the response."""
    url = _build_mcp_url()
    headers = _auth_headers()

    logger.info("MCP RPC -> %s  method=%s", url, payload.get("method"))
    print(f"[MCP DEBUG] POST {url}")
    print(f"[MCP DEBUG] Method: {payload.get('method')}")
    print(f"[MCP DEBUG] Payload: {json.dumps(payload)}")

    with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
        response = client.post(url, headers=headers, json=payload)

    print(f"[MCP DEBUG] Response status: {response.status_code}")
    print(f"[MCP DEBUG] Response body: {response.text}")

    # Surface clear errors for common failure modes
    if response.status_code == 401:
        # Surface the raw response to help debug why the PAT is rejected
        raise RuntimeError(
            f"MCP Server authentication failed (401).\n"
            f"URL: {url}\n"
            f"Raw Response: {response.text}"
        )
    if response.status_code == 403:
        raise RuntimeError(
            "MCP Server access denied (403). "
            "Ensure the OAuth role has USAGE on the MCP server and tools."
        )
    if response.status_code == 404:
        raise RuntimeError(
            f"MCP Server not found (404) at {url}. "
            "Verify MCP_SERVER_NAME, SNOWFLAKE_DATABASE, and SNOWFLAKE_SCHEMA in .env."
        )

    response.raise_for_status()
    data = response.json()

    # Check for JSON-RPC level errors
    if "error" in data:
        err = data["error"]
        code = err.get("code", "unknown")
        message = err.get("message", "Unknown MCP error")
        raise RuntimeError(f"MCP Server error (code={code}): {message}")

    return data


# ---------------------------------------------------------------------------
# Public API — Tool Discovery
# ---------------------------------------------------------------------------

def mcp_tools_list() -> list[dict]:
    """Discover available tools on the Snowflake MCP Server.

    Returns a list of tool descriptors, each with at least 'name' and 'description'.
    """
    payload = _jsonrpc_request("tools/list")
    data = _send_rpc(payload, timeout=30.0)

    result = data.get("result", {})
    tools = result.get("tools", [])
    logger.info("MCP tools/list returned %d tool(s)", len(tools))
    return tools


_sql_tool_schema_cache: dict[str, Any] | None = None


def _get_sql_tool_schema() -> dict[str, Any]:
    """Return the input schema for the configured SQL MCP tool (cached)."""
    global _sql_tool_schema_cache
    if _sql_tool_schema_cache is not None:
        return _sql_tool_schema_cache

    expected_tool = settings.mcp_tool_name.strip()
    for tool in mcp_tools_list():
        if tool.get("name") == expected_tool:
            _sql_tool_schema_cache = tool
            return tool

    _sql_tool_schema_cache = {}
    return _sql_tool_schema_cache


def _build_sql_tool_arguments(clean_sql: str) -> dict[str, Any]:
    """Build tools/call arguments using the MCP tool's declared input schema."""
    tool = _get_sql_tool_schema()
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}

    if "query" in properties:
        return {"query": clean_sql}
    if "sql" in properties:
        return {"sql": clean_sql}

    # Snowflake SYSTEM_EXECUTE_SQL expects `query` (see Snowflake MCP docs).
    return {"query": clean_sql}


def _can_use_native_fallback() -> bool:
    return bool(settings.snowflake_user.strip() and settings.snowflake_password.strip())


# ---------------------------------------------------------------------------
# Public API — Tool Invocation
# ---------------------------------------------------------------------------

def mcp_tools_call(tool_name: str, arguments: dict[str, Any]) -> dict:
    """Invoke a tool on the Snowflake MCP Server.

    Args:
        tool_name: The name of the tool to invoke (e.g. "sql_exec_tool").
        arguments: Tool-specific arguments (e.g. {"query": "SELECT ..."}).

    Returns:
        The full JSON-RPC result object.
    """
    payload = _jsonrpc_request(
        method="tools/call",
        params={
            "name": tool_name,
            "arguments": arguments,
        },
    )
    data = _send_rpc(payload, timeout=120.0)
    return data.get("result", {})


# ---------------------------------------------------------------------------
# Public API — High-level SQL Execution
# ---------------------------------------------------------------------------

def execute_sql_via_mcp(sql: str) -> tuple[list[str], list[list[Any]]]:
    """Execute a SQL statement through the Snowflake MCP Server.

    This is the main entry point that replaces the old
    ``snowflake_connector.execute_snowflake_query()`` function.

    Args:
        sql: The SQL statement to execute.

    Returns:
        A tuple of ``(columns, rows)`` matching the existing frontend contract.
    """
    tool_name = settings.mcp_tool_name.strip()
    
    # Strip trailing semicolons as the MCP server's internal parser sometimes chokes on them
    clean_sql = sql.strip()
    if clean_sql.endswith(";"):
        clean_sql = clean_sql[:-1]
        
    arguments = _build_sql_tool_arguments(clean_sql)
    logger.info("MCP tools/call arguments keys: %s", list(arguments.keys()))

    result = mcp_tools_call(tool_name, arguments)

    # Parse the MCP response into columns + rows
    # The SYSTEM_EXECUTE_SQL tool returns results in the "content" array.
    content_list = result.get("content", [])

    if not content_list:
        # DML operations (INSERT, UPDATE, DELETE, CREATE) may return empty content
        return _handle_dml_response(sql)

    # The MCP server returns tool results as content items.
    # For SQL queries, the response typically contains text content.
    columns: list[str] = []
    rows: list[list[Any]] = []

    for item in content_list:
        item_type = item.get("type", "text")

        if item_type == "text":
            text = item.get("text", "")

            if result.get("isError"):
                error_msg = text.strip() or "Unknown MCP Server error"
                if _can_use_native_fallback():
                    logger.warning(
                        "MCP error (%s). Attempting native Snowflake fallback.",
                        error_msg,
                    )
                    return _execute_fallback_sql(clean_sql)
                raise RuntimeError(f"MCP SQL execution failed: {error_msg}")

            parsed = _try_parse_sql_result(text, sql)
            if parsed:
                columns, rows = parsed
                break
            else:
                # If we can't parse it as structured data, return as a status message
                return ["status"], [[text]]

        elif item_type == "resource":
            # Some MCP servers return embedded resources
            resource = item.get("resource", {})
            text = resource.get("text", "")
            parsed = _try_parse_sql_result(text, sql)
            if parsed:
                columns, rows = parsed
                break

    if not columns and not rows:
        return _handle_dml_response(sql)

    return columns, rows


def _execute_fallback_sql(sql: str) -> tuple[list[str], list[list[Any]]]:
    """Execute SQL using the native Snowflake connector if the MCP server fails."""
    import snowflake.connector

    user = settings.snowflake_user.strip()
    password = settings.snowflake_password.strip()
    account = settings.snowflake_account.strip()

    if not user or not password:
        raise RuntimeError(
            "MCP Server failed and native fallback is not configured. "
            "Set SNOWFLAKE_USER and SNOWFLAKE_PASSWORD, or fix SNOWFLAKE_PAT / MCP server access."
        )

    print("[MCP FALLBACK] Executing SQL directly via python connector...")
    try:
        with snowflake.connector.connect(
            user=user,
            password=password,
            account=account,
            database=settings.snowflake_database,
            schema=settings.snowflake_schema,
            warehouse=settings.snowflake_warehouse or "COMPUTE_WH",
            role=settings.snowflake_role or "ACCOUNTADMIN"
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                
                if cur.description:
                    columns = [col[0] for col in cur.description]
                    rows = cur.fetchall()
                    return columns, [list(row) for row in rows]
                else:
                    return _handle_dml_response(sql)
    except Exception as e:
        raise RuntimeError(f"Fallback native execution failed: {e}")


def _try_parse_sql_result(text: str, sql: str) -> tuple[list[str], list[list[Any]]] | None:
    """Attempt to parse a text response from the MCP server as structured SQL results."""
    if not text.strip():
        return None

    # Try parsing as JSON (the MCP server may return JSON-formatted results)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Not JSON — treat as a plain text status message
        return None

    # Handle array of row objects: [{"COL1": val1, "COL2": val2}, ...]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        columns = list(data[0].keys())
        rows = [[row.get(col) for col in columns] for row in data]
        return columns, rows

    # Handle dict with explicit columns/rows structure
    if isinstance(data, dict):
        if "columns" in data and "rows" in data:
            return data["columns"], data["rows"]
        if "data" in data and isinstance(data["data"], list):
            if data["data"] and isinstance(data["data"][0], dict):
                columns = list(data["data"][0].keys())
                rows = [[row.get(col) for col in columns] for row in data["data"]]
                return columns, rows

    return None


def _handle_dml_response(sql: str) -> tuple[list[str], list[list[Any]]]:
    """Generate a status response for DML statements that don't return rows."""
    upper = sql.strip().upper()
    if upper.startswith("INSERT"):
        return ["status"], [["INSERT executed successfully via MCP Server."]]
    if upper.startswith("UPDATE"):
        return ["status"], [["UPDATE executed successfully via MCP Server."]]
    if upper.startswith("DELETE"):
        return ["status"], [["DELETE executed successfully via MCP Server."]]
    if upper.startswith("CREATE"):
        return ["status"], [["Table created successfully via MCP Server."]]
    return ["status"], [["Statement executed successfully via MCP Server."]]
