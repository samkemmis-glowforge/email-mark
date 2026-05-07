"""HubSpot CRM API for contact-level queries.

Uses the same Service Key as hubspot_marketing.py. Covers contact search
(used for attribution analysis) and contact property discovery (so the
agent can find the right field names before searching).

Privacy note: callers must enforce the rule that individual customer PII
(emails, names, phones) does not get echoed back to chat — that's the
agent's responsibility per the system prompt.
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
            "HUBSPOT_API_KEY not set. Add the Private App / Service Key to .env."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def search_contacts(
    *,
    filter_groups: Optional[List[Dict[str, Any]]] = None,
    properties: Optional[List[str]] = None,
    query: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Search HubSpot contacts using the v3 search API.

    Args:
      filter_groups: list of HubSpot filter group objects, ANDed within a
        group and ORed across groups. Each filter is a dict like
        {"propertyName": "lifecyclestage", "operator": "EQ", "value": "lead"}.
      properties: list of property names to return per contact. Returns
        only HubSpot defaults if omitted.
      query: free-text search across default searchable properties.
      limit: max contacts to return per page (HubSpot caps at 100; for
        larger result sets we just return the count from `total`).

    Returns the raw HubSpot response: {"total": N, "results": [...], "paging": {...}}.
    """
    body: Dict[str, Any] = {"limit": min(int(limit), 100)}
    if filter_groups:
        body["filterGroups"] = filter_groups
    if properties:
        body["properties"] = properties
    if query:
        body["query"] = query

    response = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_headers(),
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def list_contact_properties(
    name_contains: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all contact properties HubSpot knows about (filterable by name).

    Useful for the agent to discover field names like "original_source",
    "current_subscription_status", or any custom Glowforge property
    before constructing a search query.
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/crm/v3/properties/contacts",
        headers=_headers(),
        timeout=30,
    )
    response.raise_for_status()
    results = response.json().get("results", [])

    if name_contains:
        needle = name_contains.lower()
        results = [
            p
            for p in results
            if needle in (p.get("name") or "").lower()
            or needle in (p.get("label") or "").lower()
        ]

    return [
        {
            "name": p.get("name"),
            "label": p.get("label"),
            "type": p.get("type"),
            "field_type": p.get("fieldType"),
            "description": p.get("description") or "",
            "group_name": p.get("groupName"),
        }
        for p in results
    ]
