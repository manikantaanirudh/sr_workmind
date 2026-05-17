"""
Salesforce SOQL Generator — Translates natural language to SOQL via Groq LLM.

Mirrors the Snowflake sql_generator.py but generates SOQL
(Salesforce Object Query Language) instead of Snowflake SQL.
"""

from __future__ import annotations

from backend.config import settings
from backend.mcp.salesforce_mcp_client import ensure_mcp_soql
from backend.mcp.schema_discovery import get_salesforce_schema_hint
from backend.model.llm_clients import call_llm_for_soql


def _sanitize_soql(soql_text: str) -> str:
    """Clean LLM output to extract pure SOQL."""
    soql = soql_text.strip().strip("`")
    soql = soql.replace("```soql", "").replace("```sql", "").replace("```", "").strip()
    lower = soql.lower()
    if lower.startswith("soql"):
        soql = soql[4:].strip()
    if lower.startswith("sql"):
        soql = soql[3:].strip()
    # Remove trailing semicolons (Salesforce SOQL doesn't use them)
    if soql.endswith(";"):
        soql = soql[:-1]
    return soql.strip()


def _ensure_soql_limit(soql: str, limit: int | None = None) -> str:
    """Append LIMIT when missing so hosted MCP soqlQuery returns faster on Render."""
    max_rows = limit if limit is not None else settings.salesforce_soql_row_limit
    upper = f" {soql.strip().upper()} "
    if " LIMIT " in upper:
        return soql.strip()
    return f"{soql.strip()} LIMIT {max_rows}"


def _is_valid_soql(soql: str) -> bool:
    """Basic validation: SOQL must start with SELECT or FIND."""
    upper = " ".join(soql.strip().upper().split())
    return upper.startswith("SELECT") or upper.startswith("FIND")


def generate_soql(prompt: str, intent: str, params: dict) -> tuple[str, str]:
    """Generate a SOQL query from a natural language prompt.

    Args:
        prompt: The user's natural language prompt.
        intent: The classified intent (sf_query, sf_search, etc.).
        params: Additional parameters including sobject_name, action_hint.

    Returns:
        A tuple of (soql_string, model_label).
    """
    action = str(params.get("action_hint", "query")).lower()
    sobject = params.get("sobject_name", "")

    # For non-query actions (create, update, delete), we don't generate SOQL.
    # Instead, the LLM generates a JSON tool call payload.
    if action in ("create", "update", "delete"):
        return _generate_sf_operation(prompt, action, sobject)

    # For schema queries, no LLM needed
    if action == "schema":
        return f"DESCRIBE {sobject}" if sobject else "DESCRIBE ALL", "Deterministic:schema"

    object_hints = get_salesforce_schema_hint(sobject_name=str(sobject or ""))
    llm_prompt = prompt
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

        # Retry with stricter prompt
        llm_prompt = (
            f"{prompt}\n\n"
            f"Previous SOQL was invalid: {sanitized}\n"
            f"Generate a corrected SOQL query. Must start with SELECT. "
            f"Return only SOQL, no markdown."
        )

    raise ValueError(
        f"Model could not generate valid SOQL for the prompt. "
        f"Last attempt: {last_soql}. Please rephrase and try again."
    )


def _generate_sf_operation(
    prompt: str, action: str, sobject: str
) -> tuple[str, str]:
    """Generate a description for CUD (Create/Update/Delete) operations.

    For CUD, the LLM generates a structured operation descriptor instead of SOQL.
    The actual MCP tool call is assembled by the executor from the LLM output.
    """
    import json
    from backend.model.llm_clients import call_llm_for_sf_operation

    result = call_llm_for_sf_operation(
        prompt=prompt,
        action=action,
        sobject_name=sobject,
        object_hints=get_salesforce_schema_hint(sobject_name=sobject),
    )

    # Return the JSON operation as the "SQL" field for display
    return result, f"LLM:{settings.llm_provider}"
