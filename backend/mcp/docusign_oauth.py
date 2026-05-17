"""
Docusign OAuth 2.0 Authorization Code flow.

The Docusign managed MCP server requires a user-scoped bearer token before
JSON-RPC requests can be sent. This module owns token acquisition, local
prototype persistence, and refresh.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

_TOKEN_FILE = Path(settings.token_storage_dir) / "docusign_tokens.json"
_oauth_state: dict[str, str] = {}


def _load_tokens() -> dict[str, Any]:
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_tokens(tokens: dict[str, Any]) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def _basic_auth_header() -> str:
    client_id = settings.docusign_client_id.strip()
    client_secret = settings.docusign_client_secret.strip()
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def get_docusign_auth_url(redirect_uri: str) -> str:
    """Build the Docusign user consent URL."""
    client_id = settings.docusign_client_id.strip()
    oauth_base = settings.docusign_oauth_base_url.strip().rstrip("/")
    scopes = settings.docusign_scopes.strip()
    resource = settings.docusign_mcp_resource.strip()

    if not client_id:
        raise RuntimeError("DOCUSIGN_CLIENT_ID is required in backend/.env")

    state = secrets.token_urlsafe(32)
    _oauth_state["state"] = state
    _oauth_state["redirect_uri"] = redirect_uri

    params = {
        "response_type": "code",
        "scope": scopes,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if resource:
        params["resource"] = resource
    return f"{oauth_base}/oauth/auth?{urlencode(params)}"


def exchange_code_for_tokens(code: str, state: str) -> dict[str, Any]:
    """Exchange an authorization code for access and refresh tokens."""
    expected_state = _oauth_state.get("state", "")
    if state != expected_state:
        raise RuntimeError("OAuth state mismatch; possible CSRF attempt.")

    client_id = settings.docusign_client_id.strip()
    client_secret = settings.docusign_client_secret.strip()
    oauth_base = settings.docusign_oauth_base_url.strip().rstrip("/")
    redirect_uri = _oauth_state.get("redirect_uri", "")

    if not client_id or not client_secret:
        raise RuntimeError("DOCUSIGN_CLIENT_ID and DOCUSIGN_CLIENT_SECRET are required.")

    headers = {
        "Authorization": f"Basic {_basic_auth_header()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    resource = settings.docusign_mcp_resource.strip()
    if resource:
        payload["resource"] = resource

    with httpx.Client(timeout=30.0) as client:
        response = client.post(f"{oauth_base}/oauth/token", headers=headers, data=payload)

    if response.status_code != 200:
        raise RuntimeError(
            f"Docusign token exchange failed ({response.status_code}): {response.text}"
        )

    token_data = response.json()
    tokens = {
        "access_token": token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "token_type": token_data.get("token_type", "Bearer"),
        "scope": token_data.get("scope", settings.docusign_scopes),
        "expires_in": int(token_data.get("expires_in", 28800)),
        "obtained_at": time.time(),
    }
    _save_tokens(tokens)
    _oauth_state.clear()
    logger.info("Docusign OAuth tokens obtained and stored.")
    return tokens


def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    oauth_base = settings.docusign_oauth_base_url.strip().rstrip("/")
    headers = {
        "Authorization": f"Basic {_basic_auth_header()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    resource = settings.docusign_mcp_resource.strip()
    if resource:
        payload["resource"] = resource

    with httpx.Client(timeout=30.0) as client:
        response = client.post(f"{oauth_base}/oauth/token", headers=headers, data=payload)

    if response.status_code != 200:
        raise RuntimeError(
            f"Docusign token refresh failed ({response.status_code}): {response.text}. "
            "Please re-authenticate at /auth/docusign."
        )

    token_data = response.json()
    tokens = _load_tokens()
    tokens["access_token"] = token_data.get("access_token", "")
    if token_data.get("refresh_token"):
        tokens["refresh_token"] = token_data["refresh_token"]
    tokens["expires_in"] = int(token_data.get("expires_in", tokens.get("expires_in", 28800)))
    tokens["scope"] = token_data.get("scope", tokens.get("scope", settings.docusign_scopes))
    tokens["obtained_at"] = time.time()
    _save_tokens(tokens)
    return tokens


def get_valid_access_token() -> str:
    tokens = _load_tokens()
    access_token = tokens.get("access_token", "")
    if not access_token:
        raise RuntimeError(
            "Docusign is not authenticated. Please visit /auth/docusign to connect."
        )

    obtained_at = float(tokens.get("obtained_at", 0))
    expires_in = int(tokens.get("expires_in", 28800))
    if time.time() > obtained_at + expires_in - 300:
        refresh_token = tokens.get("refresh_token", "")
        if not refresh_token:
            raise RuntimeError(
                "Docusign access token expired and no refresh token is available. "
                "Please re-authenticate at /auth/docusign."
            )
        tokens = _refresh_access_token(refresh_token)

    return str(tokens["access_token"])


def is_authenticated() -> bool:
    return bool(_load_tokens().get("access_token"))


def get_granted_scopes() -> set[str]:
    """Return scopes DocuSign actually granted for the stored token."""
    scope_text = str(_load_tokens().get("scope", ""))
    return {scope.strip() for scope in scope_text.split() if scope.strip()}
