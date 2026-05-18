"""
Salesforce SOQL Generator — Groq LLM + deterministic fallbacks for hosted MCP soqlQuery.
"""

from __future__ import annotations

import re

from backend.config import settings
from backend.mcp.salesforce_mcp_client import ensure_mcp_soql
from backend.mcp.schema_discovery import get_salesforce_schema_hint
from backend.model.llm_clients import call_llm_for_soql

# Safe templates when Groq output is invalid (always include WHERE + LIMIT for MCP).
_SOQL_TEMPLATES: dict[str, str] = {
    "Account": (
        "SELECT Id, Name, Industry, Type, Phone, Website FROM Account "
        "WHERE Id != null LIMIT {limit}"
    ),
    "Opportunity": (
        "SELECT Id, Name, StageName, Amount, CloseDate, AccountId FROM Opportunity "
        "WHERE Id != null LIMIT {limit}"
    ),
    "Contact": (
        "SELECT Id, FirstName, LastName, Email, Phone, AccountId FROM Contact "
        "WHERE Id != null LIMIT {limit}"
    ),
    "Lead": (
        "SELECT Id, FirstName, LastName, Company, Status, Email FROM Lead "
        "WHERE Id != null LIMIT {limit}"
    ),
    "Case": (
        "SELECT Id, Subject, Status, Priority, AccountId FROM Case "
        "WHERE Id != null LIMIT {limit}"
    ),
}


def infer_sobject_from_prompt(prompt: str) -> str:
    """Heuristic SObject name from natural language (used when classifier omits it)."""
    text = prompt.lower()
    if "opportunit" in text:
        return "Opportunity"
    if "contact" in text and "account" not in text:
        return "Contact"
    if "lead" in text:
        return "Lead"
    if "case" in text:
        return "Case"
    if "account" in text:
        return "Account"
    return ""


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


def _ensure_soql_limit(soql: str, limit: int | None = None) -> str:
    max_rows = limit if limit is not None else settings.salesforce_soql_row_limit
    upper = f" {soql.strip().upper()} "
    if " LIMIT " in upper:
        return soql.strip()
    return f"{soql.strip()} LIMIT {max_rows}"


def _is_valid_soql(soql: str) -> bool:
    upper = " ".join(soql.strip().upper().split())
    return upper.startswith("SELECT") and " FROM " in upper


def _template_soql(sobject: str) -> str | None:
    template = _SOQL_TEMPLATES.get(sobject)
    if not template:
        return None
    return template.format(limit=settings.salesforce_soql_row_limit)


def _extract_from_object(soql: str) -> str | None:
    match = re.search(r"\bFROM\s+([A-Za-z0-9_]+)", soql, flags=re.IGNORECASE)
    return match.group(1) if match else None


def generate_soql(prompt: str, intent: str, params: dict) -> tuple[str, str]:
    action = str(params.get("action_hint", "query")).lower()
    sobject = str(params.get("sobject_name", "") or "").strip() or infer_sobject_from_prompt(
        prompt
    )

    if action in ("create", "update", "delete"):
        return _generate_sf_operation(prompt, action, sobject)

    if action == "schema":
        return f"DESCRIBE {sobject}" if sobject else "DESCRIBE ALL", "Deterministic:schema"

    limit = settings.salesforce_soql_row_limit
    object_hints = get_salesforce_schema_hint(sobject_name=sobject)
    llm_prompt = (
        f"{prompt}\n\n"
        f"Target object: {sobject or 'infer from prompt'}.\n"
        "Return one SOQL SELECT only. Must include WHERE and LIMIT."
    )

    last_soql = ""
    for attempt in range(3):
        llm_soql = call_llm_for_soql(
            prompt=llm_prompt,
            object_hints=object_hints,
            action=action,
            sobject_name=sobject,
        )
        sanitized = _sanitize_soql(llm_soql)
        last_soql = sanitized

        if _is_valid_soql(sanitized):
            limited = _ensure_soql_limit(sanitized)
            return ensure_mcp_soql(limited), f"LLM:{settings.llm_provider}"

        llm_prompt = (
            f"{prompt}\n\nPrevious invalid SOQL: {sanitized}\n"
            f"Return valid SOQL: SELECT fields FROM {sobject or 'Object'} "
            f"WHERE Id != null LIMIT {limit}. No markdown."
        )

    if sobject:
        fallback = _template_soql(sobject)
        if fallback:
            return ensure_mcp_soql(fallback), f"Template:{sobject}"

    if last_soql and _extract_from_object(last_soql):
        return ensure_mcp_soql(_ensure_soql_limit(last_soql)), f"LLM:{settings.llm_provider}:retry"

    raise ValueError(
        f"Could not generate valid SOQL. Last attempt: {last_soql}. "
        f"Try: 'Show {sobject or 'Account'} records in Salesforce'."
    )


def _generate_sf_operation(
    prompt: str, action: str, sobject: str
) -> tuple[str, str]:
    from backend.model.llm_clients import call_llm_for_sf_operation

    result = call_llm_for_sf_operation(
        prompt=prompt,
        action=action,
        sobject_name=sobject,
        object_hints=get_salesforce_schema_hint(sobject_name=sobject),
    )
    return result, f"LLM:{settings.llm_provider}"
