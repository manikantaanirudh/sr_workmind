"""Resolve tool names dynamically from hosted MCP servers (tools/list)."""

from __future__ import annotations

import logging

from backend.config import settings

logger = logging.getLogger(__name__)


def resolve_snowflake_sql_tool(tools: list[dict] | None = None) -> str:
    """Pick the Snowflake SYSTEM_EXECUTE_SQL (or compatible) tool from tools/list."""
    from backend.mcp.mcp_client import mcp_tools_list

    available = tools if tools is not None else mcp_tools_list()
    names = [str(t.get("name", "")) for t in available if t.get("name")]
    configured = settings.mcp_tool_name.strip()

    if configured and configured in names:
        return configured

    for tool in available:
        name = str(tool.get("name", ""))
        tool_type = str(tool.get("type", "")).upper()
        description = str(tool.get("description", "")).lower()
        if tool_type == "SYSTEM_EXECUTE_SQL":
            return name
        if name and ("execute" in name.lower() and "sql" in name.lower()):
            return name
        if "sql execution" in description or "execute sql" in description:
            return name

    if names:
        logger.warning(
            "Configured MCP tool '%s' not found; using '%s' from tools/list.",
            configured,
            names[0],
        )
        return names[0]

    raise RuntimeError(
        "No tools returned from Snowflake hosted MCP server. "
        "Verify MCP_SERVER_NAME and PAT permissions (tools/list)."
    )


def resolve_salesforce_tool(*candidates: str, tools: list[dict] | None = None) -> str:
    """Pick the first matching Salesforce MCP tool name from tools/list."""
    from backend.mcp.salesforce_mcp_client import sf_mcp_tools_list

    available_tools = tools if tools is not None else sf_mcp_tools_list()
    names = {str(t.get("name", "")) for t in available_tools if t.get("name")}

    for candidate in candidates:
        if candidate in names:
            return candidate

    lowered = {n.lower(): n for n in names}
    for candidate in candidates:
        match = lowered.get(candidate.lower())
        if match:
            return match

    if len(candidates) == 1 and names:
        logger.warning(
            "Salesforce MCP tool '%s' not in tools/list; available: %s",
            candidates[0],
            sorted(names),
        )

    raise RuntimeError(
        f"None of {list(candidates)} found on Salesforce hosted MCP server. "
        f"Available tools: {sorted(names)}"
    )
