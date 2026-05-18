from typing import Any


def _extract_raw_sql(prompt: str) -> str | None:
    stripped = prompt.strip()
    if not stripped:
        return None
    lowered = " ".join(stripped.lower().split())

    # Accept only SQL-shaped statements, not English instructions.
    if lowered.startswith("select ") or lowered.startswith("with "):
        return stripped
    if lowered.startswith("insert into ") and (" values " in lowered or " select " in lowered):
        return stripped
    if lowered.startswith("update ") and " set " in lowered and " where " in lowered:
        return stripped
    if lowered.startswith("delete from ") and " where " in lowered:
        return stripped
    if lowered.startswith("create table ") or lowered.startswith("create or replace table "):
        return stripped

    return None


def _infer_action_hint(prompt: str) -> str:
    text = prompt.lower()
    if any(word in text for word in ["create", "make", "build"]):
        return "create"
    if any(word in text for word in ["insert", "add", "append"]):
        return "insert"
    if any(word in text for word in ["delete", "remove"]):
        return "delete"
    if any(word in text for word in ["update", "modify", "change"]):
        return "update"
    if any(
        word in text
        for word in [
            "select",
            "show",
            "list",
            "get",
            "find",
            "retrieve",
            "summarize",
            "summary",
            "profile",
            "top",
            "most",
            "least",
            "count",
            "how many",
            "which",
            "what are",
            "oldest",
            "newest",
            "average",
            "total",
        ]
    ):
        return "query"
    return "auto"


def _is_summary_prompt(prompt: str) -> bool:
    text = prompt.lower()
    return any(word in text for word in ["summarize", "summary", "profile"])


def _normalize_prompt_text(prompt: str) -> str:
    chars: list[str] = []
    for ch in prompt:
        if ch.isalnum() or ch in {"_", "$", "."}:
            chars.append(ch)
        else:
            chars.append(" ")
    return " ".join("".join(chars).split())


def _is_identifier_token(token: str) -> bool:
    if not token:
        return False
    if not (token[0].isalpha() or token[0] == "_"):
        return False
    return all(ch.isalnum() or ch in {"_", "$", "."} for ch in token)


def _clean_identifier(token: str) -> str:
    return token.strip().strip("'\"").strip()


def _find_identifier_after(tokens: list[str], anchor: str, skip_words: set[str], max_lookahead: int = 7) -> str | None:
    for i, token in enumerate(tokens):
        if token != anchor:
            continue
        steps = 0
        j = i + 1
        while j < len(tokens) and steps < max_lookahead:
            candidate = _clean_identifier(tokens[j])
            low = candidate.lower()
            if low in skip_words:
                j += 1
                steps += 1
                continue
            if _is_identifier_token(candidate):
                return candidate
            j += 1
            steps += 1
    return None


def _extract_create_table_name(prompt: str) -> str | None:
    """Extract target table name from CREATE TABLE prompts."""
    normalized = _normalize_prompt_text(prompt).lower()
    tokens = normalized.split()
    skip_words = {
        "the",
        "a",
        "an",
        "new",
        "with",
        "columns",
        "column",
        "for",
        "as",
        "integer",
        "int",
        "string",
        "varchar",
        "date",
        "and",
    }
    for index, token in enumerate(tokens):
        if token != "table" or index + 1 >= len(tokens):
            continue
        candidate = _clean_identifier(tokens[index + 1])
        if _is_identifier_token(candidate) and candidate.lower() not in skip_words:
            return candidate.upper()
    return None


def _extract_table_hint(prompt: str, action_hint: str) -> str | None:
    if action_hint == "create":
        created = _extract_create_table_name(prompt)
        if created:
            return created

    normalized = _normalize_prompt_text(prompt).lower()
    tokens = normalized.split()
    if not tokens:
        return None

    skip_words = {
        "the",
        "a",
        "an",
        "table",
        "values",
        "rows",
        "row",
        "record",
        "records",
        "data",
        "these",
        "this",
        "all",
        "new",
        "existing",
    }

    # Common explicit anchor: "... <name> table ..."
    for i, token in enumerate(tokens):
        if token == "table" and i > 0:
            prev = _clean_identifier(tokens[i - 1])
            if _is_identifier_token(prev) and prev not in skip_words:
                return prev

    anchors_by_action: dict[str, list[str]] = {
        "insert": ["into", "in", "table"],
        "delete": ["from", "table"],
        "update": ["update", "in", "table"],
        "create": ["named", "table"],
        "query": ["from", "summarize", "summary", "profile", "table"],
    }

    for anchor in anchors_by_action.get(action_hint, ["table", "into", "from"]):
        candidate = _find_identifier_after(tokens, anchor, skip_words)
        if candidate:
            return candidate

    return None


def _extract_insert_columns_hint(prompt: str) -> list[str]:
    # Supports prompts like:
    # - "Title: Hero, Director: Venky"
    # - "SHOW_ID='s1001', TITLE='The Lost City'"
    # without regex parsing.
    columns: list[str] = []
    stop_words = {
        "with",
        "to",
        "set",
        "where",
        "values",
        "value",
        "and",
        "or",
        "into",
        "in",
        "table",
    }

    for chunk in prompt.split(","):
        part = chunk.strip()
        if not part:
            continue

        delimiter = ""
        if "=" in part:
            delimiter = "="
        elif ":" in part:
            delimiter = ":"
        if not delimiter:
            continue

        left = part.split(delimiter, 1)[0].strip().strip("'\"")
        if not left:
            continue
        parts = _normalize_prompt_text(left).split()
        if not parts:
            continue
        col = parts[-1].lower()
        if _is_identifier_token(col) and col not in stop_words and col not in columns:
            columns.append(col)
    return columns


def _detect_platform_explicit(prompt: str) -> str | None:
    """Only match when the user explicitly names a platform."""
    text = prompt.lower()
    if "docusign" in text or "docu sign" in text:
        return "docusign"
    if "salesforce" in text or "sfdc" in text:
        return "salesforce"
    if "snowflake" in text:
        return "snowflake"
    return None


def _infer_sf_action(prompt: str) -> str:
    """Infer the Salesforce-specific action from the prompt."""
    text = prompt.lower()
    if any(word in text for word in ["create", "add", "insert", "new"]):
        return "create"
    if any(word in text for word in ["update", "modify", "change", "edit"]):
        return "update"
    if any(word in text for word in ["delete", "remove"]):
        return "delete"
    if any(word in text for word in ["search", "find", "look up", "lookup"]):
        return "search"
    if any(word in text for word in ["describe", "schema", "fields", "structure"]):
        return "schema"
    return "query"


def _infer_ds_action(prompt: str) -> str:
    """Infer a Docusign MCP action family from a natural-language prompt."""
    text = prompt.lower()
    if "trigger" in text and "workflow" in text:
        return "trigger_workflow"
    if "requirement" in text and "workflow" in text:
        return "workflow_requirements"
    if any(word in text for word in ["detail", "details", "metadata", "specific"]):
        return "agreement_details"
    return "query_agreements"


def _intent_label(platform: str, action: str) -> str:
    if platform == "docusign":
        if action == "agreement_details":
            return "ds_agreement_details"
        if action == "workflow_requirements":
            return "ds_workflow_requirements"
        if action == "trigger_workflow":
            return "ds_trigger_workflow"
        return "ds_query_agreements"
    if platform == "salesforce":
        mapping = {
            "create": "sf_record_created",
            "update": "sf_record_updated",
            "delete": "sf_record_deleted",
            "search": "sf_search",
            "schema": "sf_schema",
        }
        return mapping.get(action, "sf_query")
    mapping = {
        "create": "create_table",
        "insert": "insert_rows",
        "update": "update_rows",
        "delete": "delete_rows",
    }
    return mapping.get(action, "nl_request")


def _build_intent_payload(
    platform: str,
    action: str,
    prompt: str,
    *,
    sobject_name: str = "",
    expected_table: str = "",
) -> dict[str, Any]:
    action_hint = action if action in {"create", "insert", "update", "delete", "query", "search", "schema", "auto"} else "auto"

    if platform == "docusign":
        ds_action = action if action in {"trigger_workflow", "workflow_requirements", "agreement_details", "query_agreements"} else _infer_ds_action(prompt)
        return {
            "platform": "docusign",
            "action": ds_action,
            "intent": _intent_label("docusign", ds_action),
            "parameters": {"action_hint": ds_action},
        }

    if platform == "salesforce":
        sf_action = action_hint if action_hint != "auto" else _infer_sf_action(prompt)
        params: dict[str, Any] = {"action_hint": sf_action}
        if sobject_name.strip():
            params["sobject_name"] = sobject_name.strip()
        return {
            "platform": "salesforce",
            "action": sf_action,
            "intent": _intent_label("salesforce", sf_action),
            "parameters": params,
        }

    # Snowflake
    if action_hint == "auto":
        action_hint = _infer_action_hint(prompt)
    intent = _intent_label("snowflake", action_hint)
    if action_hint == "query" and _is_summary_prompt(prompt):
        intent = "summarize_table"

    params_sf: dict[str, Any] = {"action_hint": action_hint}
    table_hint = expected_table.strip() or (_extract_table_hint(prompt, action_hint) or "")
    if table_hint:
        params_sf["expected_table"] = table_hint
    if action_hint == "insert":
        insert_cols = _extract_insert_columns_hint(prompt)
        if insert_cols:
            params_sf["insert_columns_hint"] = insert_cols

    return {
        "platform": "snowflake",
        "action": action_hint,
        "intent": intent,
        "parameters": params_sf,
    }


def _classify_intent_heuristic(prompt: str) -> dict[str, Any]:
    platform = _detect_platform_explicit(prompt) or "snowflake"
    action = _infer_action_hint(prompt)
    if platform == "salesforce":
        action = _infer_sf_action(prompt)
    elif platform == "docusign":
        action = _infer_ds_action(prompt)
    return _build_intent_payload(platform, action, prompt)


def classify_intent(prompt: str) -> dict[str, Any]:
    raw_sql = _extract_raw_sql(prompt)
    if raw_sql:
        first_word = raw_sql.strip().split(None, 1)[0].lower()
        action = "query"
        intent = "ad_hoc_query"
        if first_word == "create":
            action = "create"
            intent = "create_table"
        elif first_word == "insert":
            action = "insert"
            intent = "insert_rows"
        elif first_word == "update":
            action = "update"
            intent = "update_rows"
        elif first_word == "delete":
            action = "delete"
            intent = "delete_rows"
        return {
            "platform": "snowflake",
            "action": action,
            "intent": intent,
            "parameters": {"raw_sql": raw_sql},
        }

    try:
        from backend.model.llm_clients import call_llm_classify_intent

        llm = call_llm_classify_intent(prompt)
        platform = str(llm.get("platform", "snowflake")).lower()
        if platform not in {"snowflake", "salesforce", "docusign"}:
            platform = "snowflake"
        action = str(llm.get("action", "auto")).lower()
        return _build_intent_payload(
            platform,
            action,
            prompt,
            sobject_name=str(llm.get("sobject_name", "") or ""),
            expected_table=str(llm.get("expected_table", "") or ""),
        )
    except Exception:
        return _classify_intent_heuristic(prompt)

