"""Direct Salesforce REST API SOQL execution (faster than MCP for queries)."""

from __future__ import annotations

import logging
from typing import Any
import httpx

from backend.mcp.salesforce_oauth import get_instance_url, get_valid_access_token

logger = logging.getLogger(__name__)

_API_VERSION = "v59.0"


def execute_soql_via_rest(soql: str) -> tuple[list[str], list[list[Any]]]:
    """Run SOQL via Salesforce REST Query API."""
    clean_soql = soql.strip().rstrip(";")
    if not clean_soql:
        raise ValueError("SOQL query is empty.")

    instance_url = get_instance_url().rstrip("/")
    access_token = get_valid_access_token()
    url = f"{instance_url}/services/data/{_API_VERSION}/query"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    with httpx.Client(timeout=60.0, verify=False) as client:
        response = client.get(url, headers=headers, params={"q": clean_soql})

    if response.status_code == 401:
        raise RuntimeError(
            "Salesforce REST API authentication failed (401). "
            "Please re-authenticate at /auth/salesforce."
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Salesforce REST query failed ({response.status_code}): {response.text[:500]}"
        )

    payload = response.json()
    records = payload.get("records", [])
    if not records:
        return [], []

    columns: list[str] = []
    rows: list[list[Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        flat = {k: v for k, v in record.items() if k != "attributes"}
        if not columns:
            columns = list(flat.keys())
        rows.append([flat.get(col) for col in columns])

    return columns, rows
