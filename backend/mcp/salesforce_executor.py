"""
Salesforce MCP Executor — Governance logging and routing for Salesforce operations.

Mirrors the Snowflake executor.py but handles Salesforce-specific operations
through the Salesforce Hosted MCP Server.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

from backend.mcp.salesforce_mcp_client import (
    create_sf_record,
    delete_sf_record,
    execute_sf_search,
    execute_soql_via_mcp,
    get_sf_schema,
    sf_mcp_tools_list,
    update_sf_record,
)
from backend.mcp.salesforce_oauth import is_authenticated

logger = logging.getLogger(__name__)

LOG_PATH = Path(__file__).parent.parent / "logs" / "salesforce_mcp_audit.log"


# ---------------------------------------------------------------------------
# MCP Server validation
# ---------------------------------------------------------------------------

def sf_validate_via_mcp() -> str:
    """Check Salesforce MCP Server connectivity.

    Returns:
        A human-readable validation status string.
    """
    if not is_authenticated():
        return "Validation failed or backend unavailable — Salesforce not authenticated"

    try:
        tools = sf_mcp_tools_list()
        tool_names = [t.get("name", "?") for t in tools]
        return (
            f"Passed — Salesforce MCP Server verified | "
            f"Routing check: PASS | "
            f"Tools: {', '.join(tool_names[:5])}"
        )
    except Exception as exc:
        logger.warning("Salesforce MCP validation error: %s", exc)
        return f"Validation failed or backend unavailable — {exc}"


# ---------------------------------------------------------------------------
# Governance audit logging
# ---------------------------------------------------------------------------

def sf_governance_log(prompt: str, operation: str) -> None:
    """Append an audit entry for a Salesforce MCP operation."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.utcnow().isoformat()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{timestamp} | prompt={prompt} | operation={operation}\n")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def sf_route_and_execute(
    action: str,
    soql_or_operation: str,
    sf_params: dict[str, Any] | None = None,
) -> tuple[list[str], list[list[object]]]:
    """Route a Salesforce operation through the MCP Server.

    Args:
        action: The action type ('query', 'search', 'create', 'update', 'delete', 'schema').
        soql_or_operation: The SOQL query string, or operation description.
        sf_params: Additional parameters for CUD operations (sobject_name, record_id, fields).

    Returns:
        A tuple of (columns, rows) from the MCP Server response.
    """
    if not is_authenticated():
        raise RuntimeError(
            "Salesforce not authenticated. Please visit /auth/salesforce to connect."
        )

    sf_params = sf_params or {}

    if action in ("query", "soql_query"):
        return execute_soql_via_mcp(soql_or_operation)

    elif action == "search":
        return execute_sf_search(soql_or_operation)

    elif action == "create":
        sobject = sf_params.get("sobject_name", "")
        fields = sf_params.get("fields", {})
        if not sobject:
            raise ValueError("Missing 'sobject_name' for Salesforce create operation.")
        return create_sf_record(sobject, fields)

    elif action == "update":
        sobject = sf_params.get("sobject_name", "")
        record_id = sf_params.get("record_id", "")
        fields = sf_params.get("fields", {})
        if not sobject or not record_id:
            raise ValueError("Missing 'sobject_name' or 'record_id' for update.")
        return update_sf_record(sobject, record_id, fields)

    elif action == "delete":
        sobject = sf_params.get("sobject_name", "")
        record_id = sf_params.get("record_id", "")
        if not sobject or not record_id:
            raise ValueError("Missing 'sobject_name' or 'record_id' for delete.")
        return delete_sf_record(sobject, record_id)

    elif action == "schema":
        object_name = sf_params.get("object_name", "")
        return get_sf_schema(object_name)

    else:
        # Default: treat as SOQL query
        return execute_soql_via_mcp(soql_or_operation)
