"""
Salesforce OAuth 2.0 Authorization Code flow with PKCE.

Handles token acquisition, storage, and automatic refresh for
the Salesforce Hosted MCP Server.

Flow:
    1. Backend generates a PKCE code_verifier + code_challenge.
    2. User is redirected to Salesforce login page.
    3. Salesforce redirects back to /oauth/salesforce/callback with an auth code.
    4. Backend exchanges the code for access_token + refresh_token.
    5. Tokens are stored locally and auto-refreshed on expiry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token storage (file-based for prototype persistence across restarts)
# ---------------------------------------------------------------------------
_TOKEN_FILE = Path(settings.token_storage_dir) / "salesforce_tokens.json"

# PKCE state for the OAuth redirect flow (persisted for Render/serverless)
_pkce_state: dict[str, str] = {}
_PKCE_FILE = Path(settings.token_storage_dir) / "salesforce_pkce.json"


def _save_pkce_state() -> None:
    _PKCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PKCE_FILE.write_text(json.dumps(_pkce_state), encoding="utf-8")


def _load_pkce_state() -> None:
    global _pkce_state
    if not _PKCE_FILE.exists():
        return
    try:
        loaded = json.loads(_PKCE_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            _pkce_state = {str(k): str(v) for k, v in loaded.items()}
    except (json.JSONDecodeError, OSError):
        _pkce_state = {}


def _clear_pkce_state() -> None:
    global _pkce_state
    _pkce_state = {}
    if _PKCE_FILE.exists():
        try:
            _PKCE_FILE.unlink()
        except OSError:
            pass


def _load_tokens() -> dict[str, Any]:
    """Load stored tokens from disk."""
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_tokens(tokens: dict[str, Any]) -> None:
    """Persist tokens to disk."""
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _generate_code_verifier() -> str:
    """Generate a random PKCE code verifier (43-128 chars, URL-safe)."""
    return secrets.token_urlsafe(64)[:128]


def _generate_code_challenge(verifier: str) -> str:
    """Derive a S256 code challenge from the verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# OAuth URL generation
# ---------------------------------------------------------------------------

def get_salesforce_auth_url(redirect_uri: str) -> str:
    """Build the Salesforce authorization URL for OAuth 2.0 + PKCE.

    Args:
        redirect_uri: The callback URL (e.g. http://localhost:8000/oauth/salesforce/callback)

    Returns:
        The full authorization URL to redirect the user to.
    """
    instance_url = settings.salesforce_instance_url.strip().rstrip("/")
    consumer_key = settings.salesforce_consumer_key.strip()

    if not instance_url or not consumer_key:
        raise RuntimeError(
            "SALESFORCE_INSTANCE_URL and SALESFORCE_CONSUMER_KEY are required in .env"
        )

    code_verifier = _generate_code_verifier()
    code_challenge = _generate_code_challenge(code_verifier)
    state = secrets.token_urlsafe(32)

    # Store PKCE state for the callback (persisted — Render restarts wipe memory)
    _pkce_state["code_verifier"] = code_verifier
    _pkce_state["state"] = state
    _pkce_state["redirect_uri"] = redirect_uri
    _save_pkce_state()

    params = {
        "response_type": "code",
        "client_id": consumer_key,
        "redirect_uri": redirect_uri,
        "scope": "mcp_api refresh_token",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    auth_url = f"{instance_url}/services/oauth2/authorize?{urlencode(params)}"
    logger.info("Salesforce OAuth URL generated: %s", auth_url)
    return auth_url


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

def exchange_code_for_tokens(code: str, state: str) -> dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens.

    Args:
        code: The authorization code from Salesforce callback.
        state: The state parameter to verify against CSRF.

    Returns:
        The token response dict containing access_token, refresh_token, etc.
    """
    _load_pkce_state()
    expected_state = _pkce_state.get("state", "")
    if state != expected_state:
        raise RuntimeError("OAuth state mismatch — possible CSRF attack.")

    code_verifier = _pkce_state.get("code_verifier", "")
    redirect_uri = _pkce_state.get("redirect_uri", "")
    instance_url = settings.salesforce_instance_url.strip().rstrip("/")
    consumer_key = settings.salesforce_consumer_key.strip()

    token_url = f"{instance_url}/services/oauth2/token"

    payload = {
        "grant_type": "authorization_code",
        "client_id": consumer_key,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }

    with httpx.Client(timeout=30.0, verify=False) as client:
        response = client.post(token_url, data=payload)

    print(f"[SF OAUTH] Token exchange status: {response.status_code}")

    if response.status_code != 200:
        raise RuntimeError(
            f"Salesforce token exchange failed ({response.status_code}): {response.text}"
        )

    token_data = response.json()

    # Store tokens with timestamp
    tokens = {
        "access_token": token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "instance_url": token_data.get("instance_url", instance_url),
        "token_type": token_data.get("token_type", "Bearer"),
        "issued_at": token_data.get("issued_at", str(int(time.time() * 1000))),
        "expires_in": 7200,  # Salesforce tokens typically expire in 2 hours
        "obtained_at": time.time(),
    }

    _save_tokens(tokens)
    _clear_pkce_state()

    logger.info("Salesforce OAuth tokens obtained and stored successfully.")
    print("[SF OAUTH] Tokens obtained and stored successfully!")
    return tokens


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Use the refresh token to obtain a new access token."""
    instance_url = settings.salesforce_instance_url.strip().rstrip("/")
    consumer_key = settings.salesforce_consumer_key.strip()

    token_url = f"{instance_url}/services/oauth2/token"

    payload = {
        "grant_type": "refresh_token",
        "client_id": consumer_key,
        "refresh_token": refresh_token,
    }

    with httpx.Client(timeout=30.0, verify=False) as client:
        response = client.post(token_url, data=payload)

    if response.status_code != 200:
        raise RuntimeError(
            f"Salesforce token refresh failed ({response.status_code}): {response.text}. "
            "Please re-authenticate at /auth/salesforce"
        )

    token_data = response.json()

    # Update stored tokens
    tokens = _load_tokens()
    tokens["access_token"] = token_data.get("access_token", "")
    tokens["instance_url"] = token_data.get("instance_url", instance_url)
    tokens["issued_at"] = token_data.get("issued_at", str(int(time.time() * 1000)))
    tokens["obtained_at"] = time.time()

    _save_tokens(tokens)
    logger.info("Salesforce access token refreshed successfully.")
    print("[SF OAUTH] Access token refreshed.")
    return tokens


# ---------------------------------------------------------------------------
# Public API — Get a valid access token
# ---------------------------------------------------------------------------

def get_valid_access_token() -> str:
    """Return a valid Salesforce access token, refreshing if expired.

    Raises:
        RuntimeError: If no tokens are stored (user needs to authenticate first).
    """
    tokens = _load_tokens()

    if not tokens.get("access_token"):
        raise RuntimeError(
            "Salesforce not authenticated. Please visit /auth/salesforce to connect your Salesforce org. "
            "On Render, use https://sr-workmind-backend.onrender.com/auth/salesforce after each deploy."
        )

    # Check if token is expired (Salesforce tokens typically last 2 hours)
    obtained_at = tokens.get("obtained_at", 0)
    expires_in = tokens.get("expires_in", 7200)

    if time.time() > obtained_at + expires_in - 300:  # Refresh 5 min early
        refresh_token = tokens.get("refresh_token", "")
        if not refresh_token:
            raise RuntimeError(
                "Salesforce access token expired and no refresh token available. "
                "Please re-authenticate at /auth/salesforce"
            )
        tokens = _refresh_access_token(refresh_token)

    return tokens["access_token"]


def get_instance_url() -> str:
    """Return the Salesforce instance URL from stored tokens."""
    tokens = _load_tokens()
    return tokens.get("instance_url", settings.salesforce_instance_url.strip().rstrip("/"))


def is_authenticated() -> bool:
    """Check if we have valid Salesforce tokens stored."""
    tokens = _load_tokens()
    return bool(tokens.get("access_token"))
