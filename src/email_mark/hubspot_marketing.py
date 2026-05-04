"""HubSpot Marketing Email API client.

Fills the gap left by the official HubSpot MCP, which doesn't expose
marketing emails or A/B test results. Uses the Private App token.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

HUBSPOT_BASE = "https://api.hubapi.com"


def _headers() -> Dict[str, str]:
    token = os.environ.get("HUBSPOT_API_KEY")
    if not token:
        raise RuntimeError(
            "HUBSPOT_API_KEY not set. Add the Private App token to the .env file."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def list_marketing_emails(
    *,
    name_contains: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List marketing emails, optionally filtering by name substring.

    The HubSpot list endpoint doesn't take a name-contains filter directly,
    so we pull a page and filter client-side.
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/marketing/v3/emails/",
        headers=_headers(),
        params={"limit": limit, "includeStats": True},
        timeout=30,
    )
    response.raise_for_status()
    results = response.json().get("results", [])

    if name_contains:
        needle = name_contains.lower()
        results = [e for e in results if needle in (e.get("name") or "").lower()]

    return results


def get_email_statistics(email_id: str) -> Dict[str, Any]:
    """Stats for a single marketing email.

    HubSpot has moved these endpoints around; the per-email object with
    `includeStats=true` is the path that works on current Service Keys.
    Returns the full email object — stats live nested inside.
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}",
        headers=_headers(),
        params={"includeStats": "true"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_ab_test_variations(email_id: str) -> List[Dict[str, Any]]:
    """List the A/B variants of an email, if it has any."""
    response = requests.get(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}/ab-test/variations",
        headers=_headers(),
        timeout=30,
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return response.json().get("results", [])
