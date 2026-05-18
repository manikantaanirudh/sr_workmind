"""
MCP Client for Salesforce's Hosted MCP Server.

Communicates with Salesforce's Hosted MCP Server via JSON-RPC 2.0 over HTTPS.
OAuth via salesforce_oauth.py; SOQL via soqlQuery tools/call.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from backend.config import resolve_salesforce_mcp_server_url, settings
from backend.mcp.mcp_protocol import decode_rpc_response, decode_sse_stream
from backend.mcp.salesforce_oauth import get_valid_access_token

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
SF_MCP_ACCEPT = "application/json, text/event-stream"
SF_CONNECTOR_LABEL = "Salesforce Hosted MCP Server (platform/sobject-all)"

_sf_session_id: str | None = None
_sf_session_url: str | None = None
_sf_tools_cache: list[dict] | None = None
_soql_tool_name: str = "soqlQuery"
_soql_arg_key: str = "query"

_DEFAULT_SF_TOOLS: list[dict] = [{"name": "soqlQuery"}, {"name": "getUserInfo"}]


def get_salesforce_connector_label() -> str:
    return SF_CONNECTOR_LABEL


def ensure_mcp_soql(soql: str) -> str:
    """Salesforce soqlQuery requires WHERE and LIMIT (hosted MCP reference)."""
    clean = soql.strip().rstrip(";")
    if not clean:
        return ""
    upper = f" {clean.upper()} "
    if not upper.strip().startswith("SELECT"):
        return clean

    if " WHERE " not in upper:
        if re.search(r"\s+LIMIT\s+", clean, flags=re.IGNORECASE):
            clean = re.sub(
                r"(?i)\s+LIMIT\s+",
                " WHERE Id != null LIMIT ",
                clean,
                count=1,
            )
        else:
            clean = f"{clean} WHERE Id != null"

    upper = f" {clean.upper()} "
    if " LIMIT " not in upper:
        clean = f"{clean} LIMIT {settings.salesforce_soql_row_limit}"

    return clean


def _mcp_url() -> str:
    from backend.mcp.salesforce_oauth import get_instance_url, is_authenticated

    instance = ""
    if is_authenticated():
        try:
            instance = get_instance_url()
        except RuntimeError:
            pass
    return resolve_salesforce_mcp_server_url(instance)


def _sf_timeout(read_sec: float) -> httpx.Timeout:
    return httpx.Timeout(connect=20.0, read=read_sec, write=30.0, pool=20.0)


def _sf_auth_headers(*, include_session: bool = True) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {get_valid_access_token()}",
        "Content-Type": "application/json",
        "Accept": SF_MCP_ACCEPT,
        "Accept-Encoding": "identity",
        "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
    }
    if include_session and _sf_session_id:
        headers["Mcp-Session-Id"] = _sf_session_id
    return headers


def _decode_mcp_response(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        return decode_sse_stream(response)
    return decode_rpc_response(response)


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


def _reset_sf_session() -> None:
    global _sf_session_id, _sf_session_url
    _sf_session_id = None
    _sf_session_url = None


def _update_soql_tool_meta(tools: list[dict]) -> None:
    """Cache soqlQuery tool name + argument key from tools/list inputSchema."""
    global _soql_tool_name, _soql_arg_key
    for tool in tools:
        name = str(tool.get("name", ""))
        if "soql" not in name.lower() or "query" not in name.lower():
            continue
        _soql_tool_name = name
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        if not isinstance(schema, dict):
            continue
        required = schema.get("required") or []
        props = schema.get("properties") or {}
        if required and isinstance(required[0], str):
            _soql_arg_key = required[0]
        elif isinstance(props, dict) and props:
            _soql_arg_key = next(iter(props.keys()))
        logger.info(
            "Salesforce soql tool=%s arg_key=%s schema_props=%s",
            _soql_tool_name,
            _soql_arg_key,
            list(props.keys()) if isinstance(props, dict) else [],
        )
        return


def _send_initialized(client: httpx.Client, url: str) -> None:
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    client.post(
        url,
        headers=_sf_auth_headers(),
        json=payload,
        timeout=_sf_timeout(float(settings.salesforce_mcp_init_timeout_sec)),
    )


def _ensure_session(client: httpx.Client, url: str) -> None:
    global _sf_session_id, _sf_session_url, _sf_tools_cache
    if _sf_session_id and _sf_session_url == url:
        return

    if _sf_session_url != url:
        _reset_sf_session()
        _sf_tools_cache = None

    init_payload = _sf_jsonrpc_request(
        "initialize",
        params={
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "sr-workmind", "version": "1.0.0"},
        },
        request_id=9999,
    )
    response = client.post(
        url,
        headers=_sf_auth_headers(include_session=False),
        json=init_payload,
        timeout=_sf_timeout(float(settings.salesforce_mcp_init_timeout_sec)),
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

    _sf_session_id = response.headers.get("mcp-session-id") or response.headers.get(
        "Mcp-Session-Id"
    )
    if not _sf_session_id:
        raise RuntimeError("Salesforce MCP initialize missing Mcp-Session-Id header.")

    _sf_session_url = url
    init_data = _decode_mcp_response(response)
    if init_data.get("error"):
        err = init_data["error"]
        raise RuntimeError(
            f"Salesforce MCP initialize error: {err.get('message', 'Unknown')}"
        )

    _send_initialized(client, url)

    # Refresh tool catalog once per session (learn soqlQuery argument name).
    try:
        list_payload = _sf_jsonrpc_request("tools/list", request_id=2)
        list_resp = client.post(
            url,
            headers=_sf_auth_headers(),
            json=list_payload,
            timeout=_sf_timeout(25.0),
        )
        if list_resp.status_code in {200, 202}:
            list_data = _decode_mcp_response(list_resp)
            result = list_data.get("result", list_data)
            tools = result.get("tools", []) if isinstance(result, dict) else []
            if tools:
                _sf_tools_cache = tools
                _update_soql_tool_meta(tools)
    except Exception as exc:
        logger.warning("Salesforce tools/list during init skipped: %s", exc)


def _sf_send_rpc(
    payload: dict[str, Any],
    *,
    retry: bool = True,
    read_timeout: float | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    mcp_url = (url or _mcp_url()).strip()
    read_sec = read_timeout if read_timeout is not None else float(
        settings.salesforce_mcp_timeout_sec
    )
    timeout = _sf_timeout(read_sec)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
            _ensure_session(client, mcp_url)
            response = client.post(
                mcp_url,
                headers=_sf_auth_headers(),
                json=payload,
                timeout=timeout,
            )
            if response.status_code == 401:
                raise RuntimeError(
                    "Salesforce MCP authentication failed (401). Reconnect at /auth/salesforce."
                )
            if response.status_code not in {200, 202}:
                raise RuntimeError(
                    f"Salesforce MCP HTTP {response.status_code}: {response.text[:500]}"
                )
            data = _decode_mcp_response(response)
    except httpx.ReadTimeout as exc:
        _reset_sf_session()
        raise RuntimeError(
            f"Salesforce MCP request timed out after {read_sec:.0f}s. "
            "Retry once, or reconnect at /auth/salesforce if the session expired."
        ) from exc
    except httpx.TimeoutException as exc:
        _reset_sf_session()
        raise RuntimeError(f"Salesforce MCP connection timed out: {exc}") from exc

    if "error" in data:
        err = data["error"]
        raise RuntimeError(
            f"Salesforce MCP error (code={err.get('code')}): {err.get('message')}"
        )

    result = data.get("result", data)
    if isinstance(result, dict) and result.get("isError") and retry:
        content = result.get("content", [])
        error_text = content[0].get("text", "") if content else ""
        if "Unexpected error" in error_text:
            _reset_sf_session()
            method = str(payload.get("method", ""))
            params = payload.get("params") if isinstance(payload.get("params"), dict) else None
            return _sf_send_rpc(
                _sf_jsonrpc_request(method, params),
                retry=False,
                read_timeout=read_sec,
                url=mcp_url,
            )

    return result if isinstance(result, dict) else {}


def sf_mcp_tools_list(force_refresh: bool = False) -> list[dict]:
    global _sf_tools_cache
    if _sf_tools_cache is not None and not force_refresh:
        return _sf_tools_cache

    try:
        payload = _sf_jsonrpc_request("tools/list")
        response = _sf_send_rpc(payload, read_timeout=25.0)
        tools = response.get("tools", []) if isinstance(response, dict) else []
        if tools:
            _sf_tools_cache = tools
            _update_soql_tool_meta(tools)
            return tools
    except Exception as exc:
        logger.warning("Salesforce tools/list failed: %s", exc)

    _sf_tools_cache = list(_DEFAULT_SF_TOOLS)
    return _sf_tools_cache


def _build_soql_arguments(soql: str) -> dict[str, Any]:
    clean = ensure_mcp_soql(soql)
    if not clean:
        raise RuntimeError("SOQL is empty. Rephrase your Salesforce question.")
    sf_mcp_tools_list()
    return {_soql_arg_key: clean, "query": clean}


def _tool_result_has_payload(result: dict[str, Any]) -> bool:
    """True when MCP returned a parseable tool body (including zero records)."""
    if result.get("isError"):
        return False
    if isinstance(result.get("structuredContent"), dict):
        return True
    for item in result.get("content", []):
        if isinstance(item, dict) and str(item.get("text", "")).strip():
            return True
    return False


def sf_mcp_tools_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    read_timeout: float | None = None,
) -> dict[str, Any]:
    if not arguments and tool_name == _soql_tool_name:
        raise RuntimeError("Refusing soqlQuery with empty arguments.")

    payload = _sf_jsonrpc_request(
        "tools/call",
        params={"name": tool_name, "arguments": arguments},
    )
    logger.info(
        "SF tools/call %s args=%s",
        tool_name,
        json.dumps(arguments)[:300],
    )
    result = _sf_send_rpc(payload, read_timeout=read_timeout)

    if result.get("isError"):
        content = result.get("content", [])
        error_text = content[0].get("text", "Unknown error") if content else "Unknown error"
        raise RuntimeError(f"Salesforce MCP tool '{tool_name}' error: {error_text}")

    if tool_name == _soql_tool_name and not _tool_result_has_payload(result):
        raise RuntimeError(
            "Salesforce soqlQuery returned an empty MCP payload. "
            "SOQL may not have been passed to the server."
        )

    return result


def _invoke_soql_query(soql: str) -> dict[str, Any]:
    args = _build_soql_arguments(soql)
    return sf_mcp_tools_call(_soql_tool_name, args, read_timeout=60.0)


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
    clean = ensure_mcp_soql(soql)
    logger.info("Salesforce soqlQuery: %s", clean[:250])
    result = _invoke_soql_query(clean)
    return _parse_sf_tool_result(result)


def execute_sf_search(search_query: str) -> tuple[list[str], list[list[Any]]]:
    result = sf_mcp_tools_call(
        "find",
        {"search": search_query},
        read_timeout=60.0,
    )
    return _parse_sf_tool_result(result)


def create_sf_record(sobject_name: str, fields: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    from backend.mcp.tool_registry import resolve_salesforce_tool

    tool_name = resolve_salesforce_tool("createSobjectRecord", "create_sobject_record")
    result = sf_mcp_tools_call(
        tool_name,
        {"sobject-name": sobject_name, "body": fields},
        read_timeout=60.0,
    )
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
    sf_mcp_tools_call(
        tool_name,
        {"sobject-name": sobject_name, "id": record_id, "body": fields},
        read_timeout=60.0,
    )
    return ["status"], [["Record updated successfully."]]


def delete_sf_record(sobject_name: str, record_id: str) -> tuple[list[str], list[list[Any]]]:
    from backend.mcp.tool_registry import resolve_salesforce_tool

    tool_name = resolve_salesforce_tool("deleteSobjectRecord", "delete_sobject_record")
    sf_mcp_tools_call(
        tool_name,
        {"sobject-name": sobject_name, "id": record_id},
        read_timeout=60.0,
    )
    return ["status"], [["Record deleted successfully."]]


def get_sf_schema(object_name: str = "") -> tuple[list[str], list[list[Any]]]:
    from backend.mcp.tool_registry import resolve_salesforce_tool

    tool_name = resolve_salesforce_tool("getObjectSchema", "get_object_schema")
    args: dict[str, Any] = {}
    if object_name:
        args["object-name"] = object_name
    result = sf_mcp_tools_call(tool_name, args, read_timeout=45.0)
    return _parse_sf_tool_result(result)


def probe_salesforce_connectivity() -> dict[str, Any]:
    from backend.config import resolve_salesforce_oauth_base_url
    from backend.mcp.salesforce_oauth import auth_status, get_instance_url, is_authenticated

    url = _mcp_url()
    out: dict[str, Any] = {
        "mcp_server_url": url,
        "soql_tool": _soql_tool_name,
        "soql_arg_key": _soql_arg_key,
        "mcp_timeout_sec": settings.salesforce_mcp_timeout_sec,
    }
    if not is_authenticated():
        out["mcp_soql_query"] = "skipped (not authenticated)"
        return out

    out["oauth"] = auth_status()
    try:
        inst = get_instance_url()
        out["instance_url"] = inst
        out["oauth_base_url"] = resolve_salesforce_oauth_base_url(inst)
    except RuntimeError:
        pass

    try:
        sf_mcp_tools_call("getUserInfo", {}, read_timeout=25.0)
        out["mcp_get_user_info"] = "ok"
    except Exception as exc:
        out["mcp_get_user_info"] = f"error: {exc}"

    probe_soql = (
        f"SELECT Id, Name FROM Account WHERE Id != null "
        f"LIMIT {min(5, settings.salesforce_soql_row_limit)}"
    )
    try:
        cols, rows = execute_soql_via_mcp(probe_soql)
        out["mcp_soql_query"] = f"ok ({len(rows)} row(s), cols={cols[:6]})"
        out["mcp_probe_soql"] = probe_soql[:200]
    except Exception as exc:
        out["mcp_soql_query"] = f"error: {exc}"
        out["mcp_probe_soql"] = probe_soql[:200]

    return out


def _parse_sf_tool_result(result: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        records = structured.get("records")
        if isinstance(records, list):
            return _records_to_table(records)
        if "columns" in structured and "rows" in structured:
            return structured["columns"], structured["rows"]
        data = structured.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            columns = list(data[0].keys())
            rows = [[row.get(c) for c in columns] for row in data]
            return columns, rows

    for item in result.get("content", []):
        if item.get("type") == "text":
            text = item.get("text", "")
            parsed = _try_parse_sf_result(text)
            if parsed:
                return parsed
            if text.strip():
                return ["result"], [[text[:2000]]]

    raise RuntimeError(
        "Salesforce MCP returned an empty result. The query may have failed silently."
    )


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

    return None
