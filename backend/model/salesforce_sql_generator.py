"""
Salesforce SOQL Generator — Groq NL→SOQL with MCP-safe fallbacks.
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


def _extract_row_limit(prompt: str) -> int | None:
    text = prompt.lower()
    for pattern in (
        r"\b(?:top|first|last)\s+(\d+)\b",
        r"\b(\d+)\s+(?:records|rows|results)\b",
        r"\blimit\s+(\d+)\b",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return max(1, min(int(match.group(1)), settings.salesforce_soql_row_limit))
    return None


def _extract_from_object(soql: str) -> str | None:
    match = re.search(r"\bFROM\s+([A-Za-z0-9_]+)", soql, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _enforce_sobject_in_soql(soql: str, sobject: str) -> str:
    current = _extract_from_object(soql)
    if current and current.lower() == sobject.lower():
        return soql
    if current:
        return re.sub(
            rf"\bFROM\s+{re.escape(current)}\b",
            f"FROM {sobject}",
            soql,
            count=1,
            flags=re.IGNORECASE,
        )
    return f"{soql} FROM {sobject}"


def _enforce_limit_from_prompt(soql: str, prompt: str) -> str:
    requested = _extract_row_limit(prompt)
    if requested is None:
        return soql
    if re.search(r"\bLIMIT\s+\d+", soql, flags=re.IGNORECASE):
        return re.sub(
            r"\bLIMIT\s+\d+",
            f"LIMIT {requested}",
            soql,
            count=1,
            flags=re.IGNORECASE,
        )
    return f"{soql.rstrip()} LIMIT {requested}"


def _is_simple_list_all(prompt: str) -> bool:
    """Plain list-all on one object (no filters, sort, or top-N)."""
    text = prompt.lower()
    if _extract_row_limit(prompt) is not None:
        return False
    if re.search(r"\b(order(?:ed)?\s+by|sort(?:ed)?\s+by)\b", text):
        return False
    complex_filters = (
        "closed",
        "won",
        "lost",
        "open",
        "greater",
        "less",
        "before",
        "after",
        " where ",
        " like ",
        " named ",
        " called ",
        "status",
        "stage",
        "industry",
        "count(",
        "sum(",
        "avg(",
        "group by",
    )
    if any(token in text for token in complex_filters):
        return False
    return bool(
        re.search(
            r"\b(show|list|display|get|fetch)\b.{0,40}\b(all\b|records)\b",
            text,
        )
        or re.search(
            r"\ball\s+(accounts|contacts|opportunities|leads|cases)\b",
            text,
        )
        or re.search(
            r"\b(accounts|contacts|opportunities|leads|cases)\s+in\s+salesforce\b",
            text,
        )
    )


def _resolve_sobject(prompt: str, params: dict) -> str:
    hint = str(params.get("sobject_name", "") or "").strip()
    if hint:
        resolved = call_llm_resolve_sobject(
            f"User request: {prompt}\nRouter hint (verify): {hint}"
        ).strip()
        return resolved or hint
    return call_llm_resolve_sobject(prompt).strip()


def _build_llm_soql_prompt(prompt: str, sobject: str, schema_hints: str) -> str:
    parts = [
        prompt,
        "",
        f"Target SObject (required): {sobject}",
        f"SOQL MUST use: FROM {sobject}",
        "Rules:",
        "- Include WHERE (use Id != null only if no other filter applies).",
        "- Use Salesforce API field names (LastName, StageName, not display labels).",
        "- Include ORDER BY when the user asks for sorting.",
    ]
    row_limit = _extract_row_limit(prompt)
    if row_limit is not None:
        parts.append(f"- Use LIMIT {row_limit} exactly (user requested this row count).")
    else:
        parts.append(f"- Use LIMIT {settings.salesforce_soql_row_limit} at most.")
    if schema_hints:
        parts.append(f"Schema: {schema_hints}")
    parts.append("Return one SOQL SELECT only. No markdown.")
    return "\n".join(parts)


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

    if _is_simple_list_all(prompt):
        return ensure_mcp_soql(build_query_soql(sobject)), f"MCP-schema:{sobject}"

    from backend.mcp.schema_discovery import get_salesforce_schema_hint

    schema_hints = get_salesforce_schema_hint(sobject_name=sobject)
    llm_prompt = _build_llm_soql_prompt(prompt, sobject, schema_hints)
    last_soql = ""

    for attempt in range(2):
        llm_soql = call_llm_for_soql(
            prompt=llm_prompt,
            object_hints=schema_hints,
            action=action,
            sobject_name=sobject,
        )
        sanitized = _sanitize_soql(llm_soql)
        last_soql = sanitized
        if not _is_valid_soql(sanitized):
            llm_prompt = (
                f"{_build_llm_soql_prompt(prompt, sobject, schema_hints)}\n\n"
                f"Previous invalid SOQL: {sanitized}\n"
                f"Return valid SOQL: SELECT fields FROM {sobject} WHERE ... LIMIT n"
            )
            continue

        soql = _enforce_sobject_in_soql(sanitized, sobject)
        soql = _enforce_limit_from_prompt(soql, prompt)
        soql = ensure_mcp_soql(soql)
        from_obj = _extract_from_object(soql)
        if from_obj and from_obj.lower() == sobject.lower():
            return soql, f"LLM:{settings.llm_provider}"

    if last_soql and _is_valid_soql(last_soql):
        soql = ensure_mcp_soql(_enforce_limit_from_prompt(
            _enforce_sobject_in_soql(last_soql, sobject), prompt
        ))
        return soql, f"LLM:{settings.llm_provider}:retry"

    return ensure_mcp_soql(build_query_soql(sobject)), f"MCP-schema:{sobject}:fallback"


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
