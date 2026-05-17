#!/usr/bin/env python3
from backend.mcp.mcp_client import execute_sql_via_mcp

for sql in ["SELECT 1", "SELECT COUNT(*) FROM NETFLIX_TABLE"]:
    print("SQL:", sql)
    try:
        cols, rows = execute_sql_via_mcp(sql)
        print("  OK", cols, rows[:2])
    except Exception as exc:
        print("  FAIL", exc)
