from __future__ import annotations

import json
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse

from backend.config import settings
from backend.intent.classifier import classify_intent
from backend.mcp.executor import governance_log, route_and_execute, validate_via_mcp
from backend.mcp.docusign_executor import ds_governance_log, ds_route_and_execute, ds_validate_via_mcp
from backend.mcp.docusign_mcp_client import ds_mcp_tools_list
from backend.mcp.docusign_oauth import (
    exchange_code_for_tokens as exchange_docusign_code_for_tokens,
    get_docusign_auth_url,
    is_authenticated as ds_is_authenticated,
)
from backend.mcp.salesforce_executor import sf_governance_log, sf_route_and_execute, sf_validate_via_mcp
from backend.mcp.salesforce_oauth import (
    exchange_code_for_tokens,
    get_salesforce_auth_url,
    is_authenticated as sf_is_authenticated,
)
from backend.model.docusign_tool_planner import plan_docusign_operation
from backend.model.sql_generator import generate_sql
from backend.model.salesforce_sql_generator import generate_soql
from backend.json_utils import json_safe_rows
from backend.response.formatter import build_user_message
from backend.schemas import (
    ExecuteRequest,
    ExecuteResponse,
    IntentPayload,
    QueryResultPayload,
)

app = FastAPI(title="SR WorkMind — Multi-Platform MCP Edition", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _oauth_redirect_base(request: Request) -> str:
    """Base URL for OAuth callbacks (must match Connected App / DocuSign redirect URIs)."""
    for candidate in (
        settings.oauth_redirect_base_url.strip(),
        settings.public_backend_url.strip(),
    ):
        if candidate:
            return candidate.rstrip("/")
    return str(request.base_url).rstrip("/")


def _external_base_url(request: Request) -> str:
    """Return the public backend URL when configured, otherwise the request origin."""
    return _oauth_redirect_base(request)


@app.get("/config/oauth")
def oauth_config(request: Request) -> dict[str, str | list[str]]:
    """Return exact OAuth redirect URIs to register in Salesforce and DocuSign."""
    base = _oauth_redirect_base(request)
    return {
        "backend_base_url": base,
        "salesforce_auth_url": f"{base}/auth/salesforce",
        "salesforce_callback_url": f"{base}/oauth/salesforce/callback",
        "docusign_auth_url": f"{base}/auth/docusign",
        "docusign_callback_url": f"{base}/oauth/docusign/callback",
        "register_in_salesforce": (
            "Salesforce Setup → App Manager → Connected App → "
            "Callback URL must include the salesforce_callback_url exactly."
        ),
        "register_in_docusign": (
            "DocuSign Admin → Apps and Keys → your app → "
            "Redirect URI must include the docusign_callback_url exactly."
        ),
        "local_dev_callbacks": [
            "http://127.0.0.1:8000/oauth/salesforce/callback",
            "http://127.0.0.1:8000/oauth/docusign/callback",
        ],
        "render_callbacks": [
            "https://sr-workmind-backend.onrender.com/oauth/salesforce/callback",
            "https://sr-workmind-backend.onrender.com/oauth/docusign/callback",
        ],
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Salesforce OAuth endpoints
# ---------------------------------------------------------------------------

@app.get("/auth/salesforce")
def salesforce_auth_init(request: Request):
    """Redirect the user to Salesforce login page for OAuth authorization."""
    try:
        callback_url = _external_base_url(request) + "/oauth/salesforce/callback"
        auth_url = get_salesforce_auth_url(redirect_uri=callback_url)
        return RedirectResponse(url=auth_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/oauth/salesforce/callback")
def salesforce_oauth_callback(code: str, state: str):
    """Handle the Salesforce OAuth callback after user authorization."""
    try:
        tokens = exchange_code_for_tokens(code=code, state=state)
        return HTMLResponse(
            content="""
            <html>
            <head><title>SR WorkMind — Salesforce Connected</title></head>
            <body style="background:#0a1628;color:#4ade80;font-family:system-ui;display:flex;
                         align-items:center;justify-content:center;height:100vh;flex-direction:column;">
                <h1>✅ Salesforce Connected Successfully!</h1>
                <p style="color:#94a3b8;">You can now close this window and return to SR WorkMind.</p>
                <p style="color:#64748b;font-size:0.9em;">Your Salesforce org is now linked via OAuth 2.0.</p>
                <script>setTimeout(() => window.close(), 3000);</script>
            </body>
            </html>
            """,
            status_code=200,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OAuth callback error: {exc}") from exc


@app.get("/auth/salesforce/status")
def salesforce_auth_status():
    """Check if Salesforce is currently authenticated."""
    return {"authenticated": sf_is_authenticated()}


# ---------------------------------------------------------------------------
# Docusign OAuth endpoints
# ---------------------------------------------------------------------------

@app.get("/auth/docusign")
def docusign_auth_init(request: Request):
    """Redirect the user to Docusign login page for OAuth authorization."""
    try:
        callback_url = _external_base_url(request) + "/oauth/docusign/callback"
        auth_url = get_docusign_auth_url(redirect_uri=callback_url)
        return RedirectResponse(url=auth_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/oauth/docusign/callback")
def docusign_oauth_callback(code: str, state: str):
    """Handle the Docusign OAuth callback after user authorization."""
    try:
        exchange_docusign_code_for_tokens(code=code, state=state)
        return HTMLResponse(
            content="""
            <html>
            <head><title>SR WorkMind - Docusign Connected</title></head>
            <body style="background:#0a1628;color:#4ade80;font-family:system-ui;display:flex;
                         align-items:center;justify-content:center;height:100vh;flex-direction:column;">
                <h1>Docusign Connected Successfully</h1>
                <p style="color:#94a3b8;">You can now close this window and return to SR WorkMind.</p>
                <p style="color:#64748b;font-size:0.9em;">Your Docusign account is linked via OAuth 2.0.</p>
                <script>setTimeout(() => window.close(), 3000);</script>
            </body>
            </html>
            """,
            status_code=200,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Docusign OAuth callback error: {exc}") from exc


@app.get("/auth/docusign/status")
def docusign_auth_status():
    """Check if Docusign is currently authenticated."""
    from backend.mcp.docusign_oauth import is_authenticated as ds_is_authenticated
    return {"authenticated": ds_is_authenticated()}


# ---------------------------------------------------------------------------
# Debug endpoints for Docusign OAuth troubleshooting
# ---------------------------------------------------------------------------

@app.get("/debug/docusign/scopes")
def debug_docusign_scopes():
    """Return requested vs granted Docusign OAuth scopes."""
    from backend.mcp.docusign_oauth import get_granted_scopes
    
    requested = set(settings.docusign_scopes.split()) if settings.docusign_scopes else set()
    granted = get_granted_scopes()
    missing = requested - granted
    
    return {
        "requested_scopes": sorted(list(requested)),
        "granted_scopes": sorted(list(granted)),
        "missing_scopes": sorted(list(missing)),
        "issue": "Contact DocuSign support to enable missing scopes for your Integration Key." if missing else "All scopes granted ✓"
    }


@app.get("/debug/docusign/auth-url")
def debug_docusign_auth_url(request: Request):
    """Return the OAuth authorization URL that would be sent (for debugging)."""
    callback_url = _external_base_url(request) + "/oauth/docusign/callback"
    from backend.mcp.docusign_oauth import get_docusign_auth_url
    try:
        auth_url = get_docusign_auth_url(redirect_uri=callback_url)
        return {
            "auth_url": auth_url,
            "client_id": settings.docusign_client_id,
            "redirect_uri": callback_url,
            "requested_scopes": settings.docusign_scopes.split(),
            "oauth_base": settings.docusign_oauth_base_url,
        }
    except RuntimeError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Unified execution endpoint — routes between Snowflake and Salesforce
# ---------------------------------------------------------------------------

@app.post("/execute", response_model=ExecuteResponse)
def execute(request: ExecuteRequest) -> ExecuteResponse:
    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    try:
        started = time.perf_counter()

        # --- Step 1: Classify intent from the user prompt ---
        intent_payload = classify_intent(prompt)
        platform = intent_payload.get("platform", "snowflake")

        # --- Route to the correct platform ---
        if platform == "salesforce":
            return _execute_salesforce(prompt, intent_payload, started)
        elif platform == "docusign":
            return _execute_docusign(prompt, intent_payload, started)
        else:
            return _execute_snowflake(prompt, intent_payload, started)

    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        detail = str(exc)
        if "Too Many Requests" in detail or "429" in detail:
            raise HTTPException(
                status_code=429,
                detail=f"{settings.llm_provider.upper()} rate limit reached. Please retry in a moment or use a key with available quota.",
            ) from exc
        raise HTTPException(status_code=500, detail=detail) from exc


# ---------------------------------------------------------------------------
# Snowflake execution (existing logic, completely untouched)
# ---------------------------------------------------------------------------

def _execute_snowflake(prompt: str, intent_payload: dict, started: float) -> ExecuteResponse:
    """Execute a Snowflake query through the Snowflake MCP Server."""
    # --- Step 2: Generate SQL via LLM ---
    sql, selected_model = generate_sql(
        prompt=prompt,
        intent=intent_payload["intent"],
        params=intent_payload["parameters"],
    )

    # --- Step 3: Determine the SQL action from the generated query ---
    sql_action = "query"
    sql_head = sql.strip().split(None, 1)[0].lower() if sql.strip() else ""
    if sql_head == "create":
        sql_action = "create"
    elif sql_head == "insert":
        sql_action = "insert"
    elif sql_head == "update":
        sql_action = "update"
    elif sql_head == "delete":
        sql_action = "delete"

    requested_action = str(intent_payload.get("action", "auto")).lower()
    if requested_action in {"create", "insert", "update", "delete", "query"} and sql_action != requested_action:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Operation mismatch: prompt requested '{requested_action}' but model generated '{sql_action}'. "
                "Please retry with a clearer prompt."
            ),
        )

    intent_payload["action"] = sql_action

    # --- Step 4: Validate MCP Server is reachable ---
    mcp_status = validate_via_mcp()

    # --- Step 5: Log to local governance audit trail ---
    governance_log(prompt, sql)

    # --- Step 6: Execute SQL through Snowflake MCP Server ---
    columns, rows = route_and_execute(intent_payload["platform"], sql)
    safe_rows = json_safe_rows(rows)
    message = build_user_message(intent_payload["intent"], safe_rows)
    elapsed = round(time.perf_counter() - started, 2)

    return ExecuteResponse(
        intent=IntentPayload(**intent_payload),
        sql=sql,
        result=QueryResultPayload(columns=columns, rows=safe_rows),
        message=message,
        model=selected_model,
        connector=(
            "Snowflake SQL API (PAT)"
            if settings.snowflake_use_sql_api and settings.snowflake_pat.strip()
            else "Snowflake MCP Server (Standardized)"
        ),
        mcp_validation=mcp_status,
        execution_time_sec=elapsed,
        security_checks=["MCP Server RBAC", "OAuth/PAT Auth", "Audit Logged"],
    )


# ---------------------------------------------------------------------------
# Salesforce execution (new platform)
# ---------------------------------------------------------------------------

def _execute_salesforce(prompt: str, intent_payload: dict, started: float) -> ExecuteResponse:
    """Execute a Salesforce operation through the Salesforce Hosted MCP Server."""
    action = intent_payload.get("action", "query")
    params = intent_payload.get("parameters", {})

    # --- Step 2: Generate SOQL or operation descriptor via LLM ---
    soql_or_op, selected_model = generate_soql(
        prompt=prompt,
        intent=intent_payload["intent"],
        params=params,
    )

    # --- Step 3: Validate Salesforce MCP Server ---
    mcp_status = sf_validate_via_mcp()

    # --- Step 4: Log to Salesforce audit trail ---
    sf_governance_log(prompt, soql_or_op)

    # --- Step 5: Execute through Salesforce MCP Server ---
    sf_params = None
    if action in ("create", "update", "delete"):
        # Parse the LLM's JSON operation descriptor
        try:
            op_data = json.loads(soql_or_op)
            sf_params = {
                "sobject_name": op_data.get("sobject_name", params.get("sobject_name", "")),
                "fields": op_data.get("fields", {}),
                "record_id": op_data.get("record_id", ""),
            }
        except (json.JSONDecodeError, ValueError):
            sf_params = {
                "sobject_name": params.get("sobject_name", ""),
                "fields": {},
            }

    columns, rows = sf_route_and_execute(
        action=action,
        soql_or_operation=soql_or_op,
        sf_params=sf_params,
    )
    safe_rows = json_safe_rows(rows)

    message = build_user_message(intent_payload["intent"], safe_rows)
    elapsed = round(time.perf_counter() - started, 2)

    return ExecuteResponse(
        intent=IntentPayload(**intent_payload),
        sql=soql_or_op,
        result=QueryResultPayload(columns=columns, rows=safe_rows),
        message=message,
        model=selected_model,
        connector="Salesforce Hosted MCP Server",
        mcp_validation=mcp_status,
        execution_time_sec=elapsed,
        security_checks=["MCP Server RBAC", "OAuth 2.0 PKCE", "Audit Logged"],
    )


# ---------------------------------------------------------------------------
# Docusign execution
# ---------------------------------------------------------------------------

def _execute_docusign(prompt: str, intent_payload: dict, started: float) -> ExecuteResponse:
    """Execute a Docusign operation through Docusign's managed MCP Server."""
    if not ds_is_authenticated():
        raise HTTPException(
            status_code=401,
            detail="Docusign not authenticated. Please connect at /auth/docusign.",
        )

    mcp_status = ds_validate_via_mcp()
    tools = ds_mcp_tools_list()
    operation, selected_model = plan_docusign_operation(
        prompt=prompt,
        intent=intent_payload["intent"],
        params=intent_payload["parameters"],
        tools=tools,
    )

    ds_governance_log(prompt, operation.tool_name, operation.arguments)
    columns, rows = ds_route_and_execute(operation)
    safe_rows = json_safe_rows(rows)
    message = build_user_message(intent_payload["intent"], safe_rows)
    elapsed = round(time.perf_counter() - started, 2)

    return ExecuteResponse(
        intent=IntentPayload(**intent_payload),
        sql=operation.display,
        result=QueryResultPayload(columns=columns, rows=safe_rows),
        message=message,
        model=selected_model,
        connector="Docusign Managed MCP Server (Beta)",
        mcp_validation=mcp_status,
        execution_time_sec=elapsed,
        security_checks=["OAuth 2.0 Bearer Token", "MCP Tool Discovery", "Audit Logged"],
    )
