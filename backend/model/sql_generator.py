from __future__ import annotations

from backend.config import settings
from backend.model.llm_clients import call_llm_for_sql


_RESERVED_IDENTIFIER_NAMES = {"CAST"}


def _quote_reserved_projection_identifiers(sql: str) -> str:
    compact = " ".join(sql.strip().split())
    upper = compact.upper()
    if not upper.startswith("SELECT "):
        return sql

    from_idx = upper.find(" FROM ")
    if from_idx == -1:
        return sql

    projection = compact[len("SELECT "):from_idx]
    tail = compact[from_idx:]
    parts = [p.strip() for p in projection.split(",")]
    rewritten: list[str] = []
    for part in parts:
        if not part:
            continue

        if " " in part or "(" in part or ")" in part:
            rewritten.append(part)
            continue

        candidate = part.strip('"')
        upper_candidate = candidate.upper()
        if "." in candidate:
            segs = candidate.split(".")
            last = segs[-1].strip('"')
            if last.upper() in _RESERVED_IDENTIFIER_NAMES:
                segs[-1] = f'"{last.upper()}"'
                rewritten.append(".".join(segs))
            else:
                rewritten.append(part)
            continue

        if upper_candidate in _RESERVED_IDENTIFIER_NAMES:
            rewritten.append(f'"{upper_candidate}"')
        else:
            rewritten.append(part)

    if not rewritten:
        return sql
    return f"SELECT {', '.join(rewritten)}{tail}"


def _sanitize_llm_sql(sql_text: str) -> str:
    sql = sql_text.strip().strip("`")
    sql = sql.replace("```sql", "").replace("```", "").strip()
    lower_sql = sql.lower()
    if lower_sql.startswith("sql"):
        sql = sql[3:].strip()
    sql = _quote_reserved_projection_identifiers(sql)
    if ";" in sql:
        sql = sql.split(";")[0] + ";"
    elif sql:
        sql = sql + ";"
    return sql


def _sql_head(sql: str) -> str:
    stripped = sql.strip()
    if not stripped:
        return ""
    return stripped.split(None, 1)[0].lower()


def _normalize_identifier(identifier: str | None) -> str:
    if not identifier:
        return ""
    return identifier.strip().strip('"').split(".")[-1].upper()


def _extract_sql_table(sql: str, action: str) -> str:
    compact = " ".join(sql.strip().split())
    if not compact:
        return ""

    tokens = compact.split()
    if not tokens:
        return ""

    if action == "insert":
        if len(tokens) >= 3 and tokens[0].upper() == "INSERT" and tokens[1].upper() == "INTO":
            return _normalize_identifier(tokens[2])
        return ""
    elif action == "update":
        if len(tokens) >= 2 and tokens[0].upper() == "UPDATE":
            return _normalize_identifier(tokens[1])
        return ""
    elif action == "delete":
        if len(tokens) >= 3 and tokens[0].upper() == "DELETE" and tokens[1].upper() == "FROM":
            return _normalize_identifier(tokens[2])
        return ""
    elif action == "create":
        idx = 0
        if len(tokens) > 0 and tokens[idx].upper() == "CREATE":
            idx += 1
        if idx + 1 < len(tokens) and tokens[idx].upper() == "OR" and tokens[idx + 1].upper() == "REPLACE":
            idx += 2
        if idx < len(tokens) and tokens[idx].upper() == "TABLE":
            idx += 1
        if idx + 2 < len(tokens) and tokens[idx].upper() == "IF" and tokens[idx + 1].upper() == "NOT" and tokens[idx + 2].upper() == "EXISTS":
            idx += 3
        if idx < len(tokens):
            return _normalize_identifier(tokens[idx])
        return ""
    else:
        return ""


def _matches_expected_table(sql: str, action: str, expected_table: str) -> bool:
    if not expected_table:
        return True
    
    # We do not strictly validate the table name for SELECT queries because
    # extracting table names from complex FROM/JOIN clauses is error-prone.
    if action == "query":
        return True
        
    sql_table = _extract_sql_table(sql, action)
    if not sql_table:
        return False
    return sql_table == _normalize_identifier(expected_table)


def _extract_insert_columns(sql: str) -> list[str]:
    compact = " ".join(sql.strip().split())
    upper = compact.upper()
    if not upper.startswith("INSERT INTO"):
        return []

    open_paren = compact.find("(")
    close_paren = compact.find(")", open_paren + 1)
    if open_paren == -1 or close_paren == -1 or close_paren <= open_paren:
        return []

    cols_raw = compact[open_paren + 1 : close_paren]
    cols = []
    for col in cols_raw.split(","):
        clean = col.strip().strip('"').lower()
        if clean:
            cols.append(clean)
    return cols


def _matches_insert_columns(sql: str, action: str, insert_columns_hint: list[str]) -> bool:
    if action != "insert" or not insert_columns_hint:
        return True
    sql_cols = _extract_insert_columns(sql)
    if not sql_cols:
        return False
    hinted = {c.strip().lower() for c in insert_columns_hint if c.strip()}
    return hinted.issubset(set(sql_cols))


def _matches_action(sql: str, action: str) -> bool:
    upper = sql.strip().upper()
    normalized = " ".join(upper.split())
    if action == "create":
        return normalized.startswith("CREATE TABLE")
    if action == "insert":
        return normalized.startswith("INSERT INTO") and " VALUES " in f" {normalized} "
    if action == "update":
        return (
            normalized.startswith("UPDATE ")
            and " SET " in f" {normalized} "
            and " WHERE " in f" {normalized} "
        )
    if action == "delete":
        return normalized.startswith("DELETE FROM") and " WHERE " in f" {normalized} "
    if action == "query":
        return normalized.startswith("SELECT") or normalized.startswith("WITH")
    return _sql_head(sql) in {"select", "with", "create", "insert", "update", "delete"}


def _strict_retry_prompt(
    base_prompt: str,
    action: str,
    previous_sql: str,
    expected_table: str,
    insert_columns_hint: list[str],
) -> str:
    extra = ""
    if action == "insert":
        extra = (
            "Use this exact pattern: INSERT INTO <table_name> (<col1>, <col2>, ...) "
            "VALUES (<val1>, <val2>, ...); Do not use SELECT, CREATE, UPDATE, or DELETE."
        )
    elif action == "create":
        extra = "Use this exact pattern: CREATE TABLE <table_name> (<col definitions>);"
    elif action == "delete":
        extra = "Use this exact pattern: DELETE FROM <table_name> WHERE <condition>;"
    elif action == "update":
        extra = "Use this exact pattern: UPDATE <table_name> SET <assignments> WHERE <condition>;"

    table_extra = ""
    if expected_table:
        table_extra = f"The table name must be exactly: {expected_table}. Do not use any other table."

    column_extra = ""
    if action == "insert" and insert_columns_hint:
        cols = ", ".join(insert_columns_hint)
        column_extra = (
            f"Use exactly these INSERT columns: ({cols}). "
            "Do not invent extra columns."
        )

    return (
        f"{base_prompt}\n\n"
        f"Previous SQL was invalid for requested action '{action}': {previous_sql}\n"
        f"{extra}\n"
        f"{table_extra}\n"
        f"{column_extra}\n"
        "Generate a corrected SQL statement now. Return only SQL."
    )


def _build_summary_sql(expected_table: str) -> str:
    table = expected_table.strip()
    if not table:
        raise ValueError("Please specify a table name to summarize.")

    parts = [p.strip().strip('"') for p in table.split(".") if p.strip()]
    if not parts:
        raise ValueError("Please specify a valid table name to summarize.")

    table_name = parts[-1].upper()
    schema_name = ""
    info_schema_source = "INFORMATION_SCHEMA.COLUMNS"

    if len(parts) >= 2:
        schema_name = parts[-2].upper()
    if len(parts) >= 3:
        database_name = parts[-3].upper()
        info_schema_source = f"{database_name}.INFORMATION_SCHEMA.COLUMNS"

    if schema_name:
        schema_filter = f"TABLE_SCHEMA = '{schema_name}'"
    else:
        schema_filter = "TABLE_SCHEMA = CURRENT_SCHEMA()"

    return (
        "SELECT "
        f"(SELECT COUNT(*) FROM {table}) AS total_rows, "
        f"(SELECT COUNT(*) FROM {info_schema_source} "
        f"WHERE {schema_filter} AND TABLE_NAME = '{table_name}') AS total_columns"
    )


def generate_sql(prompt: str, intent: str, params: dict) -> tuple[str, str]:
    raw_sql = params.get("raw_sql")
    if raw_sql:
        return _sanitize_llm_sql(str(raw_sql)), "DirectSQL"

    # Dynamic-by-default path: let the configured LLM derive SQL from natural-language intent.
    action = str(params.get("action_hint", "auto")).lower()
    if action not in {"create", "insert", "update", "delete", "query", "auto"}:
        action = "auto"
    expected_table = str(params.get("expected_table", "")).strip()
    insert_columns_hint = params.get("insert_columns_hint", [])
    if not isinstance(insert_columns_hint, list):
        insert_columns_hint = []

    if intent == "summarize_table":
        summary_sql = _build_summary_sql(expected_table)
        return _sanitize_llm_sql(summary_sql), "Deterministic:summary"

    llm_prompt = prompt
    last_sql = ""
    for _ in range(3):
        llm_sql = call_llm_for_sql(
            prompt=llm_prompt,
            schema_hint=settings.table_hints,
            action=action,
            expected_table=expected_table,
            insert_columns_hint=insert_columns_hint,
        )
        sanitized = _sanitize_llm_sql(llm_sql)
        last_sql = sanitized
        if (
            _matches_action(sanitized, action)
            and _matches_expected_table(sanitized, action, expected_table)
            and _matches_insert_columns(sanitized, action, insert_columns_hint)
        ):
            return sanitized, f"LLM:{settings.llm_provider}"
        llm_prompt = _strict_retry_prompt(
            prompt,
            action,
            sanitized,
            expected_table,
            insert_columns_hint,
        )

    raise ValueError(
        (
            f"Model could not generate a valid '{action}' statement for the prompt. "
            f"Last SQL: {last_sql}. Please rephrase and try again."
        )
    )
