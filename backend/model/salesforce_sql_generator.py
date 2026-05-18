"""
Salesforce SOQL Generator — MCP schema discovery + Groq for object/filter resolution.
"""

from __future__ import annotations

import re

from backend.config import settings
from backend.mcp.salesforce_mcp_client import ensure_mcp_soql
from backend.mcp.salesforce_schema import build_query_soql
from backend.model.llm_clients import call_llm_for_soql, call_llm_resolve_sobject


def _sanitize_soql(soql_text: str) -> str:
    soql = soql_text.strip().strip("`")
    soql = soql.replace("```soql", "").replace("```sql", "").replace("```", "").strip()
    lower = soql.lower()
    if lower.startswith("soql"):
        soql = soql[4:].strip()
    if lower.startswith("sql"):
        soql = soql[3:].strip()
    if soql.endswith(";"):
        soql = soql[:-1]
    return soql.strip()


def _is_valid_soql(soql: str) -> bool:
    upper = " ".join(soql.strip().upper().split())
    if not upper.startswith("SELECT"):
        return False
    if " FROM " not in upper:
        return False
    if "FIELDS(" in upper or " SELECT *" in upper:
        return False
    return True


def _resolve_sobject(prompt: str, params: dict) -> str:
    sobject = str(params.get("sobject_name", "") or "").strip()
    if sobject:
        return sobject
    return call_llm_resolve_sobject(prompt).strip()


def _prompt_needs_custom_where(prompt: str) -> bool:
    text = prompt.lower()
    if any(word in text for word in ("show all", "list all", "all ", "every ")):
        return False
    return any(
        word in text
        for word in (
            " where ",
            " in ",
            " with ",
            " named ",
            " called ",
            " from ",
            " before ",
            " after ",
            " greater ",
            " less ",
            " equal ",
            " like ",
            " status",
            " stage",
            " industry",
            " city",
            " state",
            " country",
            " owner",
            " closed",
            " open",
        )
    )


def _merge_where_clause(base_soql: str, custom_soql: str, sobject: str) -> str:
    match = re.search(
        r"\bWHERE\b(.+?)(?:\s+ORDER\s+BY|\s+LIMIT|\s*$)",
        custom_soql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return base_soql
    where_body = match.group(1).strip().rstrip(";")
    if not where_body or where_body.lower().replace(" ", "") == "id!=null":
        return base_soql
    select_match = re.match(
        rf"(?is)\s*SELECT\s+.+?\s+FROM\s+{re.escape(sobject)}\s+",
        base_soql,
    )
    if not select_match:
        return base_soql
    limit_match = re.search(r"\bLIMIT\s+(\d+)\s*$", base_soql, flags=re.IGNORECASE)
    limit_clause = limit_match.group(0) if limit_match else f"LIMIT {settings.salesforce_soql_row_limit}"
    prefix = base_soql[: select_match.end()].rstrip()
    return f"{prefix} WHERE {where_body} {limit_clause}".strip()


def generate_soql(prompt: str, intent: str, params: dict) -> tuple[str, str]:
    action = str(params.get("action_hint", "query")).lower()
    sobject = _resolve_sobject(prompt, params)

    if action in ("create", "update", "delete"):
        return _generate_sf_operation(prompt, action, sobject)

    if action == "schema":
        return f"DESCRIBE {sobject}" if sobject else "DESCRIBE ALL", "Deterministic:schema"

    if not sobject:
        raise ValueError(
            "Could not determine a Salesforce object for this prompt. "
            "Mention the object (e.g. Account, Opportunity) and try again."
        )

    base_soql = build_query_soql(sobject)
    model = f"MCP-schema:{sobject}"

    if _prompt_needs_custom_where(prompt):
        from backend.mcp.schema_discovery import get_salesforce_schema_hint

        hints = get_salesforce_schema_hint(sobject_name=sobject)
        llm_soql = call_llm_for_soql(
            prompt=prompt,
            object_hints=hints,
            action=action,
            sobject_name=sobject,
        )
        sanitized = _sanitize_soql(llm_soql)
        if _is_valid_soql(sanitized):
            merged = _merge_where_clause(base_soql, ensure_mcp_soql(sanitized), sobject)
            return ensure_mcp_soql(merged), f"LLM:{settings.llm_provider}"

    return ensure_mcp_soql(base_soql), model


def _generate_sf_operation(
    prompt: str, action: str, sobject: str
) -> tuple[str, str]:
    from backend.model.llm_clients import call_llm_for_sf_operation
    from backend.mcp.schema_discovery import get_salesforce_schema_hint

    result = call_llm_for_sf_operation(
        prompt=prompt,
        action=action,
        sobject_name=sobject,
        object_hints=get_salesforce_schema_hint(sobject_name=sobject),
    )
    return result, f"LLM:{settings.llm_provider}"
