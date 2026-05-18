from __future__ import annotations

import json
import time
import httpx

from backend.config import settings


def _extract_text(payload: dict) -> str:
    if "response" in payload:
        return str(payload.get("response", ""))
    if "choices" in payload and payload["choices"]:
        choice = payload["choices"][0]
        if isinstance(choice, dict):
            if "message" in choice and isinstance(choice["message"], dict):
                return str(choice["message"].get("content", ""))
            return str(choice.get("text", ""))
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict) and "generated_text" in first:
            return str(first["generated_text"])
    return ""


def _statement_constraint(action: str) -> str:
    mode = action.lower()
    if mode == "auto":
        return (
            "Infer the correct SQL operation from user intent and generate exactly one valid Snowflake statement "
            "(SELECT, CREATE TABLE, INSERT, UPDATE, or DELETE)."
        )
    if mode == "create":
        return "Generate only one CREATE TABLE statement."
    if mode == "insert":
        return "Generate only one INSERT statement."
    if mode == "update":
        return "Generate only one UPDATE statement and include a WHERE clause."
    if mode == "delete":
        return "Generate only one DELETE statement and include a WHERE clause."
    return "Generate only one Snowflake SELECT query."


def call_llm_for_sql(
    prompt: str,
    schema_hint: str,
    action: str = "query",
    expected_table: str = "",
    insert_columns_hint: list[str] | None = None,
) -> str:
    provider = settings.llm_provider.lower()
    table_constraint = ""
    if expected_table:
        table_constraint = f" Use table name exactly '{expected_table}' for this request."
    column_constraint = ""
    if action.lower() == "insert" and insert_columns_hint:
        cols = ", ".join(str(c).strip() for c in insert_columns_hint if str(c).strip())
        if cols:
            column_constraint = f" Use exactly these INSERT columns: ({cols}). Do not add other columns."
    system_prompt = (
        "You are an enterprise SQL assistant. "
        f"{_statement_constraint(action)} "
        f"{table_constraint}"
        f"{column_constraint}"
        "No markdown, no explanation, no prose. "
        "Use only the provided schema when possible: "
        f"{schema_hint}."
    )

    if provider == "ollama":
        payload = {
            "model": settings.ollama_model,
            "prompt": f"{system_prompt}\nUser request: {prompt}",
            "stream": False,
        }
        with httpx.Client(timeout=30.0, verify=False) as client:
            response = client.post(settings.ollama_url, json=payload)
            response.raise_for_status()
            text = _extract_text(response.json())
            return text.strip()

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is missing")
        payload = {
            "model": settings.groq_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30.0, verify=False) as client:
            response = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                content=json.dumps(payload),
            )
            response.raise_for_status()
            text = _extract_text(response.json())
            return text.strip()

    if provider == "huggingface":
        if not settings.huggingface_api_key:
            raise RuntimeError("HUGGINGFACE_API_KEY is missing")
        headers = {
            "Authorization": f"Bearer {settings.huggingface_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": f"{system_prompt}\nUser request: {prompt}\nSQL:",
            "parameters": {"max_new_tokens": 220, "temperature": 0},
        }
        with httpx.Client(timeout=45.0, verify=False) as client:
            response = client.post(
                f"https://api-inference.huggingface.co/models/{settings.huggingface_model}",
                headers=headers,
                content=json.dumps(payload),
            )
            response.raise_for_status()
            text = _extract_text(response.json())
            return text.strip()

    if provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is missing")
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": f"{system_prompt}\n\nUser request: {prompt}\n\nReturn only SQL."}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 500,
            },
        }
        candidate_models = [
            settings.gemini_model,
            "gemini-1.5-flash-latest",
            "gemini-1.5-flash",
            "gemini-2.0-flash",
        ]
        last_error: Exception | None = None
        with httpx.Client(timeout=30.0, verify=False) as client:
            for model_name in candidate_models:
                for attempt in range(3):
                    try:
                        response = client.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={settings.gemini_api_key}",
                            json=payload,
                        )
                        response.raise_for_status()
                        data = response.json()
                        if "candidates" in data and data["candidates"]:
                            content = data["candidates"][0].get("content", {})
                            if "parts" in content and content["parts"]:
                                text = content["parts"][0].get("text", "")
                                if text.strip():
                                    return text.strip()
                    except httpx.HTTPStatusError as exc:  # pragma: no cover - provider/network path
                        last_error = exc
                        status = exc.response.status_code
                        if status == 429:
                            time.sleep(1 + attempt)
                            continue
                        # 404/403 etc. try next model immediately
                        break
                    except Exception as exc:  # pragma: no cover - provider/network path
                        last_error = exc
                        break

        if last_error:
            raise RuntimeError(f"Gemini SQL generation failed: {last_error}")
        return ""

    raise RuntimeError(f"Unsupported LLM provider: {settings.llm_provider}")


# ---------------------------------------------------------------------------
# Salesforce SOQL generation
# ---------------------------------------------------------------------------

def call_llm_resolve_sobject(prompt: str) -> str:
    """Return a single Salesforce SObject API name (PascalCase) for the user prompt."""
    system_prompt = (
        "You map natural language to exactly one Salesforce SObject API name (PascalCase). "
        "Examples: accounts->Account, contacts->Contact, opportunities/opps->Opportunity, "
        "leads->Lead, cases->Case, campaigns->Campaign. "
        "Pick the object the user is asking about, not a related lookup field. "
        "Reply with only the API name. No markdown, no quotes."
    )
    provider = settings.llm_provider.lower()

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is missing")
        payload = {
            "model": settings.groq_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 32,
        }
        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=20.0, verify=False) as client:
            response = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                content=json.dumps(payload),
            )
            response.raise_for_status()
            text = _extract_text(response.json()).strip()
    elif provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is missing")
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                f"{system_prompt}\n\nUser request: {prompt}\n\n"
                                "Return only the SObject API name."
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 32},
        }
        with httpx.Client(timeout=20.0, verify=False) as client:
            response = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{settings.gemini_model}:generateContent?key={settings.gemini_api_key}",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            text = ""
            if "candidates" in data and data["candidates"]:
                parts = data["candidates"][0].get("content", {}).get("parts", [])
                if parts:
                    text = str(parts[0].get("text", "")).strip()
    else:
        raise RuntimeError(f"Unsupported LLM provider for SObject resolution: {provider}")

    token = text.split()[0].strip("`'\".,;") if text else ""
    if token and token[0].isalpha():
        return token[0].upper() + token[1:]
    raise ValueError("Could not resolve a Salesforce object name from the prompt.")


def _soql_statement_constraint(action: str, sobject_name: str = "") -> str:
    """Build the SOQL-specific constraint text for the system prompt."""
    obj_hint = f" from the {sobject_name} object" if sobject_name else ""
    if action == "search":
        return f"Generate exactly one valid SOSL FIND expression{obj_hint}."
    return f"Generate exactly one valid Salesforce SOQL SELECT query{obj_hint}."


def call_llm_for_soql(
    prompt: str,
    object_hints: str,
    action: str = "query",
    sobject_name: str = "",
) -> str:
    """Use the configured LLM to generate a SOQL query for Salesforce.

    Uses the same provider infrastructure as call_llm_for_sql but with
    a Salesforce-specific system prompt.
    """
    object_lock = ""
    if sobject_name:
        object_lock = (
            f"The query MUST use FROM {sobject_name} only. "
            f"Do not query Contact unless the user asked for contacts. "
        )
    system_prompt = (
        "You are an enterprise Salesforce SOQL assistant. "
        f"{_soql_statement_constraint(action, sobject_name)} "
        f"{object_lock}"
        "SOQL syntax: SELECT fields FROM Object WHERE conditions ORDER BY field ASC|DESC LIMIT n. "
        "Important SOQL rules: "
        "- Use API field names (LastName, StageName, CloseDate), not labels. "
        "- When the user says 'top N' or 'first N', set LIMIT to exactly N. "
        "- When the user asks to sort/order, include ORDER BY with the correct field. "
        "- Opportunity closed won: StageName = 'Closed Won'. Closed lost: StageName = 'Closed Lost'. "
        "- Use single quotes for string literals. "
        "- Always include WHERE and LIMIT (required by Salesforce hosted MCP). "
        "- If no filter is specified, use WHERE Id != null. "
        "No markdown, no explanation, no prose. "
        f"{object_hints}"
    )

    provider = settings.llm_provider.lower()

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is missing")
        payload = {
            "model": settings.groq_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30.0, verify=False) as client:
            response = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                content=json.dumps(payload),
            )
            response.raise_for_status()
            text = _extract_text(response.json())
            return text.strip()

    if provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is missing")
        payload = {
            "contents": [
                {"parts": [{"text": f"{system_prompt}\n\nUser request: {prompt}\n\nReturn only SOQL."}]}
            ],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 500},
        }
        with httpx.Client(timeout=30.0, verify=False) as client:
            response = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent?key={settings.gemini_api_key}",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            if "candidates" in data and data["candidates"]:
                content = data["candidates"][0].get("content", {})
                if "parts" in content and content["parts"]:
                    return content["parts"][0].get("text", "").strip()

    # Fallback: use the SQL generator with adapted prompt
    return call_llm_for_sql(
        prompt=f"Generate a Salesforce SOQL query for: {prompt}",
        schema_hint=object_hints,
        action="query",
    )


def call_llm_for_sf_operation(
    prompt: str,
    action: str,
    sobject_name: str,
    object_hints: str,
) -> str:
    """Use the LLM to generate a JSON operation descriptor for Salesforce CUD.

    Returns a JSON string describing the operation (fields, values, etc.).
    """
    system_prompt = (
        "You are a Salesforce operation assistant. "
        f"Generate a JSON object describing a '{action}' operation on the {sobject_name} object. "
        "The JSON should have the structure: "
        '{"sobject_name": "<Object>", "fields": {"FieldName": "value", ...}} for create, '
        '{"sobject_name": "<Object>", "record_id": "<id>", "fields": {"FieldName": "value", ...}} for update, '
        '{"sobject_name": "<Object>", "record_id": "<id>"} for delete. '
        "Use only valid Salesforce field API names from this schema: "
        f"{object_hints}. "
        "Return only valid JSON, no markdown, no explanation."
    )

    provider = settings.llm_provider.lower()

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is missing")
        payload = {
            "model": settings.groq_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30.0, verify=False) as client:
            response = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                content=json.dumps(payload),
            )
            response.raise_for_status()
            text = _extract_text(response.json())
            return text.strip()

    # Fallback for other providers
    return f'{{"sobject_name": "{sobject_name}", "fields": {{}}}}'


def call_llm_for_ds_operation(prompt: str, tools_schema: str) -> str:
    from backend.config import settings
    import httpx, json
    
    system_prompt = (
        "You are a Docusign MCP tool routing assistant. "
        "Given the user request, decide which tool to call and extract its arguments. "
        "CRITICAL RULES:\n"
        "1. Never provide an argument if its value is not explicitly required or logically inferred.\n"
        "2. NEVER use the value 'all' for any 'status' parameter.\n"
        "Return ONLY valid JSON matching this schema: "
        '{"tool_name": "<ToolName>", "arguments": {"arg1": "val1"}} '
        "Here are the available tools and their schemas: "
        f"{tools_schema}"
    )

    if not settings.groq_api_key:
        return '{"tool_name": "getTemplates", "arguments": {}}'

    payload = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"}
    }
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }
    # Safety preflight: avoid sending oversized tool schemas that will exceed
    # Groq tokens-per-minute (TPM) limits. If the combined tools_schema is
    # large, skip the LLM and return a safe fallback to avoid 429 errors.
    try:
        if tools_schema and len(tools_schema) > 18000:
            print(f"GROQ SKIP: tools_schema size {len(tools_schema)} exceeds threshold; using fallback")
            return '{"tool_name": "getTemplates", "arguments": {}}'

    except Exception:
        pass

    try:
        with httpx.Client(timeout=30.0, verify=False) as client:
            response = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                content=json.dumps(payload),
            )
            if response.status_code != 200:
                print(f"GROQ HTTP ERROR {response.status_code}: {response.text}")
            response.raise_for_status()
            data = response.json()
            if "choices" in data and data["choices"]:
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Groq Error: {e}")

    return '{"tool_name": "getTemplates", "arguments": {}}'


def call_llm_classify_intent(prompt: str) -> dict:
    """Classify platform and action dynamically (no hardcoded table/object lists)."""
    system_prompt = (
        "You route natural-language requests to the correct data platform. "
        "Return ONLY valid JSON with this shape: "
        '{"platform":"snowflake|salesforce|docusign",'
        '"action":"query|create|insert|update|delete|search|schema|auto",'
        '"sobject_name":"",'
        '"expected_table":""}. '
        "Use platform=snowflake for SQL/warehouse/table/analytics requests. "
        "Use platform=salesforce for CRM/Account/Contact/Opportunity/SOQL requests. "
        "Use platform=docusign for envelopes, agreements, contracts, or signing. "
        "Set expected_table only when the user names a specific Snowflake table. "
        "Set sobject_name only when the user names a Salesforce object (e.g. Account). "
        "Infer action from verbs (show/list/query vs create vs update vs delete)."
    )

    provider = settings.llm_provider.lower()
    text = ""

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is missing")
        payload = {
            "model": settings.groq_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30.0, verify=False) as client:
            response = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                content=json.dumps(payload),
            )
            response.raise_for_status()
            text = _extract_text(response.json()).strip()
    else:
        text = call_llm_for_sql(
            prompt=f"User request: {prompt}",
            schema_hint=system_prompt,
            action="auto",
        )

    cleaned = text.strip().strip("`").replace("```json", "").replace("```", "").strip()
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("LLM classification did not return a JSON object.")
    return data
