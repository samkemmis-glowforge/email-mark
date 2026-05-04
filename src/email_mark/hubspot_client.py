"""HubSpot API client for list management and contact updates."""

from __future__ import annotations

import os
from typing import Any, Dict, List

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

HUBSPOT_BASE_URL = "https://api.hubapi.com"


def _get_token() -> str:
    token = os.environ.get("HUBSPOT_API_KEY")
    if not token:
        raise RuntimeError(
            "HUBSPOT_API_KEY not set. Add it to the .env file in the project root."
        )
    return token


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


def list_contacts(limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch a small page of contacts from HubSpot.

    Used primarily to verify the connection / token / scope are working.
    """
    response = requests.get(
        f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts",
        headers=_headers(),
        params={"limit": limit},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("results", [])
