"""Discover live schema context from Snowflake and Salesforce hosted MCP servers."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, str]] = {}
_DEFAULT_TTL_SEC = int(os.getenv("SCHEMA_CACHE_TTL_SEC", "300"))


def _read_cache(key: str) -> str | None:
    entry = _CACHE.get(key)
    if not entry:
        return None
    cached_at, value = entry
    if time.time() - cached_at > _DEFAULT_TTL_SEC:
        return None
    return value


def _write_cache(key: str, value: str) -> None:
    _CACHE[key] = (time.time(), value)


def get_snowflake_schema_hint(force_refresh: bool = False) -> str:
    """Build schema hints by querying INFORMATION_SCHEMA through the Snowflake MCP SQL tool."""
    env_override = settings.table_hints.strip()
    if env_override:
        return env_override

    if not force_refresh:
        cached = _read_cache("snowflake")
        if cached:
            return cached

    from backend.mcp.mcp_client import execute_sql_via_mcp

    schema_predicate = (
        f"TABLE_SCHEMA = '{settings.snowflake_schema.strip().upper()}'"
        if settings.snowflake_schema.strip()
        else "TABLE_SCHEMA = CURRENT_SCHEMA()"
    )

    sql = f"""
SELECT
  TABLE_NAME,
  LISTAGG(COLUMN_NAME || ' ' || DATA_TYPE, ', ')
    WITHIN GROUP (ORDER BY ORDINAL_POSITION) AS COLUMNS
FROM INFORMATION_SCHEMA.COLUMNS
WHERE {schema_predicate}
  AND TABLE_CATALOG = CURRENT_DATABASE()
GROUP BY TABLE_NAME
ORDER BY TABLE_NAME
LIMIT 100
""".strip()

    try:
        _columns, rows = execute_sql_via_mcp(sql)
        if not rows:
            hint = (
                f"No tables found in schema {settings.snowflake_schema or 'CURRENT_SCHEMA()'} "
                "via INFORMATION_SCHEMA. Use only tables that exist in the connected account."
            )
        else:
            parts: list[str] = []
            for row in rows:
                if len(row) >= 2:
                    parts.append(f"{row[0]}({row[1]})")
                elif row:
                    parts.append(str(row[0]))
            hint = "; ".join(parts)
    except Exception as exc:
        logger.warning("Snowflake schema discovery via MCP failed: %s", exc)
        hint = (
            "Schema discovery unavailable. Use INFORMATION_SCHEMA and CURRENT_SCHEMA() "
            "to reference tables and columns that exist in the connected Snowflake account."
        )

    _write_cache("snowflake", hint)
    return hint


def get_salesforce_schema_hint(force_refresh: bool = False) -> str:
    """Build object/field hints using Salesforce hosted MCP getObjectSchema (index mode)."""
    env_override = settings.salesforce_object_hints.strip()
    if env_override:
        return env_override

    if not force_refresh:
        cached = _read_cache("salesforce")
        if cached:
            return cached

    from backend.mcp.salesforce_oauth import is_authenticated

    if not is_authenticated():
        return (
            "Salesforce not connected. Standard objects include Account, Contact, "
            "Opportunity, Lead, Case, Task — use fields from the target object schema."
        )

    from backend.mcp.salesforce_mcp_client import get_sf_schema

    try:
        _columns, rows = get_sf_schema("")
        hint = _format_sf_schema_rows(rows)
    except Exception as exc:
        logger.warning("Salesforce schema discovery via MCP failed: %s", exc)
        hint = (
            "Schema discovery unavailable. Use standard Salesforce objects and fields "
            "per the authenticated user's permissions."
        )

    _write_cache("salesforce", hint)
    return hint


def _format_sf_schema_rows(rows: list[list[Any]]) -> str:
    if not rows:
        return "No Salesforce schema returned from getObjectSchema."

    if len(rows) == 1 and len(rows[0]) == 1:
        text = str(rows[0][0])
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text[:8000] if len(text) > 8000 else text

        if isinstance(data, dict):
            objects = data.get("objects") or data.get("sobjects") or data.get("entities")
            if isinstance(objects, list):
                names = []
                for obj in objects[:40]:
                    if isinstance(obj, dict):
                        names.append(str(obj.get("name") or obj.get("apiName") or obj))
                    else:
                        names.append(str(obj))
                if names:
                    return "Salesforce objects: " + ", ".join(names)
            return json.dumps(data)[:8000]
        if isinstance(data, list):
            return "Salesforce objects: " + ", ".join(str(item) for item in data[:40])

    parts = []
    for row in rows[:40]:
        parts.append(", ".join(str(cell) for cell in row if cell is not None))
    return "; ".join(parts) if parts else "Salesforce schema index retrieved."
