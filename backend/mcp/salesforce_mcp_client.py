"""
MCP Client for Salesforce's Hosted MCP Server.

Communicates with Salesforce's Hosted MCP Server via JSON-RPC 2.0 over HTTPS,
following the Model Context Protocol specification.

Authentication: Uses OAuth 2.0 Bearer tokens obtained via the PKCE flow
managed by salesforce_oauth.py.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from backend.config import (
    _SF_MCP_PROD_SOBJECT_ALL,
    resolve_salesforce_mcp_server_url,
    settings,
)
from backend.mcp.mcp_protocol import decode_rpc_response, decode_sse_stream
from backend.mcp.salesforce_oauth import get_valid_access_token

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
# Required by Salesforce hosted MCP (HTTP 406 if either is missing).
SF_MCP_ACCEPT = "application/json, text/event-stream"
SF_CONNECTOR_LABEL = "Salesforce Hosted MCP Server (platform/sobject-all)"

_sf_session_id: str | None = None
_sf_session_url: str | None = None
_sf_tools_cache: list[dict] | None = None

_DEFAULT_SF_TOOLS: list[dict] = [
    {"name": "soqlQuery"},
    {"name": "getObjectSchema"},
    {"name": "getUserInfo"},
    {"name": "createSobjectRecord"},
    {"name": "updateSobjectRecord"},
    {"name": "deleteSobjectRecord"},
    {"name": "find"},
]


def get_salesforce_connector_label() -> str:
    return SF_CONNECTOR_LABEL


def ensure_mcp_soql(soql: str) -> str:
    """Salesforce soqlQuery requires WHERE and LIMIT (hosted MCP reference)."""
    clean = soql.strip().rstrip(";")
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


def probe_salesforce_connectivity() -> dict[str, Any]:
    """MCP-only probe for /health/diagnostics."""
    from backend.mcp.salesforce_oauth import auth_status, get_instance_url, is_authenticated

    url = _mcp_url()
    out: dict[str, Any] = {
        "mcp_server_url": url,
        "mcp_server_url_env": settings.salesforce_mcp_server_url.strip(),
        "mcp_timeout_sec": settings.salesforce_mcp_timeout_sec,
        "mcp_init_timeout_sec": settings.salesforce_mcp_init_timeout_sec,
        "soql_row_limit": settings.salesforce_soql_row_limit,
    }
    if not is_authenticated():
        out["mcp_soql_query"] = "skipped (not authenticated)"
        return out

    out["oauth"] = auth_status()
    try:
        from backend.config import resolve_salesforce_oauth_base_url

        inst = get_instance_url()
        out["instance_url"] = inst
        out["oauth_base_url"] = resolve_salesforce_oauth_base_url(inst)
        out["mcp_org_tier"] = "sandbox" if "/sandbox/" in url else "production"
    except RuntimeError:
        out["instance_url"] = None

    try:
        result = sf_mcp_tools_call("getUserInfo", {}, read_timeout=25.0)
        out["mcp_get_user_info"] = "ok"
        content = result.get("content", [])
        if content and content[0].get("text"):
            out["mcp_user_hint"] = content[0]["text"][:120]
    except Exception as exc:
        out["mcp_get_user_info"] = f"error: {exc}"

    try:
        cols, rows = execute_soql_via_mcp(
            "SELECT Id, Name FROM Account WHERE Id != null LIMIT 1"
        )
        out["mcp_soql_query"] = f"ok ({len(rows)} row(s), {len(cols)} col(s))"
    except Exception as exc:
        out["mcp_soql_query"] = f"error: {exc}"

    return out


def _sf_timeout(read_sec: float) -> httpx.Timeout:
    return httpx.Timeout(connect=20.0, read=read_sec, write=30.0, pool=20.0)


def _sf_auth_headers(*, include_session: bool = True) -> dict[str, str]:
    access_token = get_valid_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": SF_MCP_ACCEPT,
        # Avoid gzip/br decode errors on Render (incorrect header check).
        "Accept-Encoding": "identity",
        "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
    }
    if include_session and _sf_session_id:
        headers["Mcp-Session-Id"] = _sf_session_id
    return headers


def _decode_mcp_response(response: httpx.Response) -> dict[str, Any]:
    """Decode Salesforce MCP JSON or SSE response body."""
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


def _send_initialized(client: httpx.Client, url: str) -> None:
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    timeout = _sf_timeout(float(settings.salesforce_mcp_init_timeout_sec))
    response = client.post(
        url,
        headers=_sf_auth_headers(),
        json=payload,
        timeout=timeout,
    )
    if response.status_code not in {200, 202, 204}:
        logger.warning(
            "Salesforce MCP notifications/initialized returned %s: %s",
            response.status_code,
            response.text[:200],
        )


def _reset_sf_session() -> None:
    global _sf_session_id, _sf_session_url
    _sf_session_id = None
    _sf_session_url = None


def _ensure_session(client: httpx.Client, url: str) -> None:
    global _sf_session_id, _sf_session_url
    if _sf_session_id and _sf_session_url == url:
        return

    if _sf_session_url != url:
        _reset_sf_session()

    init_timeout = float(settings.salesforce_mcp_init_timeout_sec)
    logger.info("Initializing Salesforce MCP session at %s", url)
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

    _sf_session_id = response.headers.get("mcp-session-id") or response.headers.get(
        "Mcp-Session-Id"
    )
    if not _sf_session_id:
        raise RuntimeError(
            "Salesforce MCP initialize succeeded but no Mcp-Session-Id header was returned."
        )

    _sf_session_url = url
    init_data = _decode_mcp_response(response)
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
    url: str | None = None,
) -> dict[str, Any]:
    """POST JSON-RPC to Salesforce hosted MCP (Streamable HTTP + SSE-safe read)."""
    mcp_url = (url or _mcp_url()).strip()
    if not mcp_url:
        raise RuntimeError("Salesforce MCP server URL is not configured.")

    read_sec = read_timeout if read_timeout is not None else float(
        settings.salesforce_mcp_timeout_sec
    )
    timeout = _sf_timeout(read_sec)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
            _ensure_session(client, mcp_url)
            headers = _sf_auth_headers()
            logger.info(
                "SF MCP RPC -> %s method=%s params=%s",
                mcp_url,
                payload.get("method"),
                json.dumps(payload.get("params", {}))[:400],
            )
            response = client.post(
                mcp_url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
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
            if response.status_code == 406:
                raise RuntimeError(
                    f"Salesforce MCP HTTP 406: {response.text[:500]}. "
                    f"Accept header must be: {SF_MCP_ACCEPT}"
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
            return _sf_send_rpc(
                new_payload,
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
        tools: list[dict] = []
        if isinstance(response, dict):
            tools = response.get("tools", [])

        if not tools:
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


def _resolve_soql_tool_name() -> str:
    for tool in sf_mcp_tools_list():
        name = str(tool.get("name", ""))
        if name.lower() in ("soqlquery", "soql_query"):
            return name
    return "soqlQuery"


def _soql_argument_variants(soql: str) -> list[dict[str, Any]]:
    """Build argument shapes for soqlQuery (schema-driven + documented fallbacks)."""
    clean = ensure_mcp_soql(soql)
    if not clean.strip():
        raise RuntimeError("SOQL query is empty. Rephrase your Salesforce question.")

    variants: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(args: dict[str, Any]) -> None:
        key = json.dumps(args, sort_keys=True)
        if key not in seen:
            seen.add(key)
            variants.append(args)

    tools = _sf_tools_cache if _sf_tools_cache is not None else sf_mcp_tools_list()
    for tool in tools:
        name = str(tool.get("name", "")).lower()
        if name not in ("soqlquery", "soql_query"):
            continue
        schema = tool.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        for prop in list(required) + list(props.keys()):
            add({prop: clean})
        break

    add({"query": clean})
    add({"soql": clean})
    add({"input": {"query": clean}})
    add({"query": clean, "soql": clean})
    return variants


def _tools_call_param_variants(tool_name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Some gateways expect arguments nested; others accept query alongside name."""
    variants: list[dict[str, Any]] = [{"name": tool_name, "arguments": arguments}]
    if tool_name == "soqlQuery" and arguments.get("query"):
        q = arguments["query"]
        variants.append({"name": tool_name, "arguments": arguments, "query": q})
        variants.append({"name": tool_name, "query": q})
    return variants


def sf_mcp_tools_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    read_timeout: float | None = None,
    mcp_url: str | None = None,
) -> dict[str, Any]:
    last_error: RuntimeError | None = None
    for params in _tools_call_param_variants(tool_name, arguments):
        payload = _sf_jsonrpc_request("tools/call", params=params)
        try:
            result = _sf_send_rpc(payload, read_timeout=read_timeout, url=mcp_url)
        except RuntimeError as exc:
            last_error = exc
            continue

        if result.get("isError"):
            content = result.get("content", [])
            error_text = content[0].get("text", "Unknown error") if content else "Unknown error"
            last_error = RuntimeError(
                f"Salesforce MCP tool '{tool_name}' error: {error_text}"
            )
            if "empty or null" in error_text.lower() or "MALFORMED_QUERY" in error_text:
                continue
            raise last_error
        return result

    if last_error:
        raise last_error
    raise RuntimeError(f"Salesforce MCP tool '{tool_name}' failed with no response.")


def _invoke_soql_query(soql: str) -> dict[str, Any]:
    tool_name = _resolve_soql_tool_name()
    mcp_urls = [_mcp_url()]
    if mcp_urls[0] != _SF_MCP_PROD_SOBJECT_ALL and "/sandbox/" in mcp_urls[0]:
        mcp_urls.append(_SF_MCP_PROD_SOBJECT_ALL)

    last_error: RuntimeError | None = None
    for mcp_url in mcp_urls:
        for args in _soql_argument_variants(soql):
            try:
                return sf_mcp_tools_call(tool_name, args, mcp_url=mcp_url)
            except RuntimeError as exc:
                last_error = exc
                err = str(exc).lower()
                if "empty or null" not in err and "malformed_query" not in err:
                    raise
    if last_error:
        raise last_error
    raise RuntimeError("Salesforce soqlQuery failed.")


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
    clean_soql = ensure_mcp_soql(soql)
    logger.info("Salesforce MCP soqlQuery -> %s", clean_soql[:200])
    result = _invoke_soql_query(clean_soql)
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
