#!/usr/bin/env python3
"""Probe Snowflake MCP tools/call argument shapes."""
from backend.mcp.mcp_client import mcp_tools_call, mcp_tools_list

tools = mcp_tools_list()
print("tools:", [t.get("name") for t in tools])

sql = "SELECT CURRENT_VERSION()"
for args in [
    {"sql": sql},
    {"query": sql},
    {"statement": sql},
    {"sql": sql, "warehouse": "COMPUTE_WH"},
]:
    print("try", args)
    try:
        result = mcp_tools_call("sql_exec_tool", args)
        print("  result isError=", result.get("isError"), "content=", str(result)[:200])
    except Exception as exc:
        print("  exc", exc)
