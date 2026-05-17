"""
MCP Executor — Governance logging and routing via Snowflake's Managed MCP Server.

All manual SQL validation (keyword blocking, statement checking) has been removed.
Validation and governance are now handled server-side by the Snowflake MCP Server's
built-in RBAC and tool configuration.

This module retains:
  - governance_log()  — local audit trail of prompt + SQL
  - validate_via_mcp() — verifies the MCP server is reachable and the tool exists
  - route_and_execute() — routes SQL execution through the MCP client
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from backend.mcp.mcp_client import execute_sql_via_mcp, mcp_tools_list

logger = logging.getLogger(__name__)

LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "mcp_audit.log"


def validate_via_mcp() -> str:
    """Verify that the Snowflake MCP Server is reachable and the SQL tool is available.

    Returns:
        A status string ("Passed — MCP Server verified" or an error message).

    Raises:
        RuntimeError: If the MCP server is unreachable or the tool is missing.
    """
    try:
        tools = mcp_tools_list()
        tool_names = [t.get("name", "") for t in tools]
        logger.info("MCP Server tools discovered: %s", tool_names)

        from backend.config import settings
        expected_tool = settings.mcp_tool_name.strip()

        if expected_tool not in tool_names:
            raise RuntimeError(
                f"MCP tool '{expected_tool}' not found on server. "
                f"Available tools: {tool_names}. "
                "Verify the MCP server specification in Snowflake."
            )

        return "Passed — MCP Server verified"
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"MCP Server health check failed: {exc}") from exc


def governance_log(prompt: str, sql: str) -> None:
    """Append an audit entry to the local governance log file."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.utcnow().isoformat()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{timestamp} | prompt={prompt} | sql={sql}\n")


def route_and_execute(platform: str, sql: str) -> tuple[list[str], list[list[object]]]:
    """Route SQL execution through the Snowflake MCP Server.

    Args:
        platform: Target platform identifier (must be "snowflake").
        sql: The SQL statement to execute.

    Returns:
        A tuple of (columns, rows) from the MCP Server response.
    """
    if platform != "snowflake":
        raise ValueError(f"Unsupported platform for Snowflake Edition: {platform}")
    return execute_sql_via_mcp(sql)
