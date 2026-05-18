"""Discover Salesforce SObject fields via hosted MCP getObjectSchema and build safe SOQL."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

_FIELD_CACHE: dict[str, tuple[float, list[str]]] = {}
_CACHE_TTL_SEC = 300
_MAX_SELECT_COLUMNS = 8
_COLUMN_PRIORITY = (
    "Id",
    "Name",
    "FirstName",
    "LastName",
    "Subject",
    "Email",
    "Status",
    "Type",
    "StageName",
    "Amount",
    "CloseDate",
    "Industry",
    "Phone",
)


def _cache_get(sobject: str) -> list[str] | None:
    entry = _FIELD_CACHE.get(sobject)
    if not entry:
        return None
    cached_at, fields = entry
    if time.time() - cached_at > _CACHE_TTL_SEC:
        return None
    return fields


def _cache_set(sobject: str, fields: list[str]) -> None:
    _FIELD_CACHE[sobject] = (time.time(), fields)


def _field_name_from_entry(entry: Any) -> str | None:
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    if not isinstance(entry, dict):
        return None
    for key in ("name", "apiName", "fieldName", "qualifiedName"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_field_names(payload: Any) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str | None) -> None:
        if not name or name in seen:
            return
        seen.add(name)
        names.append(name)

    if isinstance(payload, list):
        for item in payload:
            add(_field_name_from_entry(item))
        return names

    if not isinstance(payload, dict):
        return names

    for key in ("fields", "fieldDefinitions", "field_definitions", "columns"):
        block = payload.get(key)
        if isinstance(block, list):
            for item in block:
                add(_field_name_from_entry(item))

    for key in ("result", "schema", "sobject", "object"):
        nested = payload.get(key)
        if nested is not payload:
            names.extend(_extract_field_names(nested))

    return names


def parse_object_schema_fields(result: dict[str, Any]) -> list[str]:
    """Parse field API names from a getObjectSchema MCP tools/call result."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        names = _extract_field_names(structured)
        if names:
            return names

    for item in result.get("content", []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        names = _extract_field_names(data)
        if names:
            return names

    return []


def fetch_sobject_field_names(sobject: str, *, force_refresh: bool = False) -> list[str]:
    """Return queryable field API names for an SObject (cached MCP getObjectSchema)."""
    clean = sobject.strip()
    if not clean:
        return []

    if not force_refresh:
        cached = _cache_get(clean)
        if cached:
            return cached

    names: list[str] = []
    try:
        from backend.mcp.salesforce_mcp_client import sf_mcp_tools_call
        from backend.mcp.tool_registry import resolve_salesforce_tool

        tool_name = resolve_salesforce_tool("getObjectSchema", "get_object_schema")
        raw = sf_mcp_tools_call(
            tool_name,
            {"object-name": clean},
            read_timeout=45.0,
        )
        names = parse_object_schema_fields(raw)
    except Exception as exc:
        logger.warning("getObjectSchema field parse failed for %s: %s", clean, exc)

    if not names:
        names = ["Id", "Name"]

    _cache_set(clean, names)
    return names


def pick_query_columns(field_names: list[str], *, max_columns: int = _MAX_SELECT_COLUMNS) -> list[str]:
    """Pick a small, MCP-safe column set from live schema field names."""
    available = [f for f in field_names if f and re.match(r"^[A-Za-z][A-Za-z0-9_]*$", f)]
    if not available:
        return ["Id"]

    picked: list[str] = []
    lower_map = {f.lower(): f for f in available}

    for preferred in _COLUMN_PRIORITY:
        match = lower_map.get(preferred.lower())
        if match and match not in picked:
            picked.append(match)
        if len(picked) >= max_columns:
            return picked

    for name in sorted(available):
        if name not in picked:
            picked.append(name)
        if len(picked) >= max_columns:
            break

    if "Id" not in picked and "Id" in available:
        picked.insert(0, "Id")
    return picked[:max_columns]


def build_query_soql(sobject: str, field_names: list[str] | None = None) -> str:
    """Build a bounded SELECT for hosted MCP soqlQuery (WHERE + LIMIT required)."""
    clean = sobject.strip()
    if not clean:
        raise ValueError("Salesforce object name is required.")

    fields = field_names if field_names is not None else fetch_sobject_field_names(clean)
    columns = pick_query_columns(fields)
    limit = settings.salesforce_soql_row_limit
    return (
        f"SELECT {', '.join(columns)} FROM {clean} "
        f"WHERE Id != null LIMIT {limit}"
    )
