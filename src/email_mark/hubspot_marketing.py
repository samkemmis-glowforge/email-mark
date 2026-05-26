"""HubSpot Marketing Email API client.

Fills the gap left by the official HubSpot MCP, which doesn't expose
marketing emails or A/B test results. Uses the Private App token.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

HUBSPOT_BASE = "https://api.hubapi.com"

# Glowforge-specific business constant: HubSpot's "engaged" cutoff for
# marketing email targeting. A contact is "engaged" if they have fewer
# than this many marketing sends since their last open/click. This
# matches the value configured in the HubSpot UI under marketing email
# engagement settings. If you change this in HubSpot, change it here too.
GLOWFORGE_ENGAGEMENT_SENDS_CUTOFF = 11


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
    state: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List marketing emails, optionally filtering by name and/or state.

    Common HubSpot email states:
      DRAFT, PUBLISHED, AUTOMATED, AUTOMATED_DRAFT,
      AUTOMATED_AB, AUTOMATED_DRAFT_AB, AUTOMATED_AB_VARIANT, AB_EMAIL.

    Without `state`, HubSpot may exclude some draft variants from the
    default response — pass state="DRAFT" or "AUTOMATED_DRAFT" to find
    drafts specifically.
    """
    params: Dict[str, Any] = {"limit": limit, "includeStats": "true"}
    if state:
        params["state"] = state
    response = requests.get(
        f"{HUBSPOT_BASE}/marketing/v3/emails/",
        headers=_headers(),
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    results = response.json().get("results", [])

    if name_contains:
        needle = name_contains.lower()
        results = [e for e in results if needle in (e.get("name") or "").lower()]

    return results


def get_email_body_text(email_id: str) -> Dict[str, Any]:
    """Extract the readable body text of a marketing email by ID.

    Returns subject, preview text, and the concatenated text content from all
    text-bearing widgets (with HTML stripped). Useful when the bot needs to
    review or quote actual draft copy rather than just metadata.
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}",
        headers=_headers(),
        params={"includeStats": "true"},
        timeout=30,
    )
    response.raise_for_status()
    email = response.json()

    content = email.get("content") if isinstance(email.get("content"), dict) else {}
    widgets = (content or {}).get("widgets") if isinstance(content, dict) else {}

    body_parts: List[str] = []
    if isinstance(widgets, dict):
        # Sort by widget id roughly preserves the visual order most templates use.
        for _wid, widget in sorted(widgets.items()):
            if not isinstance(widget, dict):
                continue
            body = widget.get("body")
            if not isinstance(body, dict):
                continue
            for field in ("html", "text", "value", "rich_text"):
                raw = body.get(field)
                if raw and isinstance(raw, str):
                    stripped = re.sub(r"<[^>]+>", "", raw).strip()
                    # Collapse repeated whitespace.
                    stripped = re.sub(r"\s+", " ", stripped)
                    if stripped:
                        body_parts.append(stripped)
                    break

    return {
        "id": email.get("id"),
        "name": email.get("name"),
        "subject": email.get("subject"),
        "preview_text": email.get("previewText") or "",
        "state": email.get("state"),
        "campaign_name": email.get("campaignName"),
        "body_text": "\n\n".join(body_parts),
    }


def get_email_widget_structure(email_id: str) -> Dict[str, Any]:
    """Return the widget layout of a marketing email — for diagnosing
    template structure before trying to populate it programmatically.

    For each widget we return its id, type, length of HTML text content,
    and a short text preview (first ~200 chars, HTML-stripped). Sorted by
    widget id so the order roughly matches visual order in the email.
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}",
        headers=_headers(),
        params={"includeStats": "true"},
        timeout=30,
    )
    response.raise_for_status()
    email = response.json()

    content = email.get("content") if isinstance(email.get("content"), dict) else {}
    widgets = (content or {}).get("widgets") if isinstance(content, dict) else {}

    rows: List[Dict[str, Any]] = []
    if isinstance(widgets, dict):
        for wid, widget in sorted(widgets.items()):
            if not isinstance(widget, dict):
                continue
            body = widget.get("body") if isinstance(widget.get("body"), dict) else {}
            html = body.get("html") if isinstance(body.get("html"), str) else ""
            text_only = re.sub(r"<[^>]+>", " ", html or "")
            text_only = re.sub(r"\s+", " ", text_only).strip()

            row: Dict[str, Any] = {
                "widget_id": wid,
                "widget_type": widget.get("type") or widget.get("name") or "",
                "label": widget.get("label") or "",
                "html_text_length": len(text_only),
                "text_preview": text_only[:200],
                "has_html_field": bool(html),
            }

            # For non-text widgets (images, buttons, dividers, etc.) dump
            # the body field shape so we can see what we're dealing with —
            # field names like image_url / src / url tell us how to patch.
            if not html and body:
                body_summary: Dict[str, Any] = {}
                for k, v in body.items():
                    if isinstance(v, str):
                        body_summary[k] = (
                            v if len(v) <= 200
                            else v[:200] + f"...[{len(v)} chars total]"
                        )
                    elif isinstance(v, (int, float, bool)) or v is None:
                        body_summary[k] = v
                    elif isinstance(v, dict):
                        body_summary[k] = (
                            f"<dict {len(v)} keys: {list(v.keys())[:8]}>"
                        )
                    elif isinstance(v, list):
                        body_summary[k] = f"<list of {len(v)}>"
                    else:
                        body_summary[k] = f"<{type(v).__name__}>"
                row["body_fields"] = body_summary

            rows.append(row)

    return {
        "email_id": email.get("id"),
        "email_name": email.get("name"),
        "subject": email.get("subject"),
        "widget_count": len(rows),
        "widgets": rows,
    }


def get_email_widget_html(email_id: str, widget_id: str) -> Dict[str, Any]:
    """Return the RAW HTML of a single widget — for debugging cases where
    the rendered email doesn't look right and we need to see the actual
    tag structure (which the text-stripped preview from
    get_email_widget_structure hides).

    Returns the raw html string, the list of all body fields, and the
    widget type, so we can inspect both text widgets and image widgets.
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}",
        headers=_headers(),
        params={"includeStats": "true"},
        timeout=30,
    )
    response.raise_for_status()
    email = response.json()

    content = email.get("content") if isinstance(email.get("content"), dict) else {}
    widgets = (content or {}).get("widgets") if isinstance(content, dict) else {}
    if not isinstance(widgets, dict):
        return {"error": "Email has no widgets dict."}

    widget = widgets.get(widget_id)
    if not isinstance(widget, dict):
        return {"error": f"Widget {widget_id} not found in email {email_id}."}

    body = widget.get("body") if isinstance(widget.get("body"), dict) else {}
    return {
        "email_id": email_id,
        "widget_id": widget_id,
        "widget_type": widget.get("type") or "",
        "body_keys": sorted(list(body.keys())),
        "raw_html": body.get("html") or "",
        "raw_html_length": len(body.get("html") or ""),
    }


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


def list_workflows(limit: int = 100) -> Dict[str, Any]:
    """List workflows accessible to the Service Key.

    Returns whatever the v3 workflows API exposes. If empty or 403, the
    automation scope isn't doing what we need (or modern flows aren't
    accessible via this API).
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/automation/v3/workflows",
        headers=_headers(),
        params={"limit": int(limit)},
        timeout=30,
    )
    if response.status_code == 403:
        return {
            "error": (
                "Access denied. Service Key likely missing automation scope, "
                "or scope not yet active (try regenerating the key)."
            )
        }
    response.raise_for_status()
    return response.json()


def get_workflow_details(workflow_id: str) -> Dict[str, Any]:
    """Get metadata for a HubSpot workflow / flow.

    Tries the v4 Flows API first (modern workflows created in the UI),
    falls back to the v3 Workflows API (legacy).
    """
    # Try v4 flows first — the modern API.
    v4_response = requests.get(
        f"{HUBSPOT_BASE}/automation/v4/flows/{workflow_id}",
        headers=_headers(),
        timeout=30,
    )
    if v4_response.status_code == 200:
        data = v4_response.json()
        data["_api_version"] = "v4"
        return data
    if v4_response.status_code == 403:
        return {
            "error": "Access denied to v4 flows API. Check 'automation' scope on Service Key."
        }

    # Fall back to v3 workflows.
    v3_response = requests.get(
        f"{HUBSPOT_BASE}/automation/v3/workflows/{workflow_id}",
        headers=_headers(),
        timeout=30,
    )
    if v3_response.status_code == 200:
        data = v3_response.json()
        data["_api_version"] = "v3"
        return data
    if v3_response.status_code == 403:
        return {
            "error": "Access denied to v3 workflows API. Check 'automation' scope on Service Key."
        }

    return {
        "error": (
            f"Workflow {workflow_id} not found in either v3 or v4 APIs. "
            "The ID may be wrong, or the Service Key may not have access "
            "to this specific workflow. Check the workflow URL in HubSpot — "
            "if it contains '/platform/flow/' it's a v4 flow; if it contains "
            "'/main/edit/' it's a v3 workflow."
        )
    }


def get_workflow_enrollments(workflow_id: str, limit: int = 250) -> Dict[str, Any]:
    """List currently enrolled contacts in a HubSpot workflow.

    Returns contact IDs (vids) currently enrolled, plus pagination info.
    Note: this returns ACTIVELY enrolled contacts. For a workflow that
    fires once per contact and ends, completed enrollments may not be
    returned — historical enrollment data may need a different approach
    (e.g., reading per-contact workflow properties).
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/automation/v3/workflows/{workflow_id}/enrollments",
        headers=_headers(),
        params={"limit": min(int(limit), 250)},
        timeout=30,
    )
    if response.status_code == 403:
        return {
            "error": (
                "Access denied. The Service Key likely needs the "
                "'automation' scope."
            )
        }
    if response.status_code == 404:
        return {"error": f"Workflow {workflow_id} not found."}
    response.raise_for_status()
    return response.json()


def get_contact_email_events(
    contact_email: str,
    email_ids: Optional[List[str]] = None,
    event_types: Optional[List[str]] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Get email engagement events for a specific contact (queried by their email).

    Used for the REVERSE attribution query: instead of "which contacts got
    email X?" (which doesn't work for automated emails), we ask "what emails
    did this contact receive?" and filter to the campaign's email IDs.

    Args:
      contact_email: The contact's email address.
      email_ids: Optional list of marketing email IDs. If provided, only
        events matching these emailIds are returned.
      event_types: Optional list of event types (DELIVERED, OPEN, CLICK,
        BOUNCE, UNSUBSCRIBE). HubSpot v1 events API only takes one at a time;
        if multiple are passed, we filter client-side.
      limit: Max events returned (HubSpot caps at 1000).

    Returns dict with event_count, matched_email_ids (the unique email IDs
    the contact engaged with from the filter list), and the events themselves.
    """
    params: Dict[str, Any] = {"recipient": contact_email, "limit": min(int(limit), 1000)}
    if event_types and len(event_types) == 1:
        params["eventType"] = event_types[0]

    response = requests.get(
        f"{HUBSPOT_BASE}/email/public/v1/events",
        headers=_headers(),
        params=params,
        timeout=30,
    )
    if response.status_code == 403:
        return {"error": "Access denied to Email Events API."}
    response.raise_for_status()
    data = response.json()
    events = data.get("events", []) or []

    # Apply client-side filters not handled by the API.
    if email_ids:
        target = {str(e) for e in email_ids}
        events = [e for e in events if str(e.get("emailId")) in target]
    if event_types and len(event_types) > 1:
        target_types = {t.upper() for t in event_types}
        events = [e for e in events if str(e.get("type", "")).upper() in target_types]

    matched_ids = sorted({str(e.get("emailId")) for e in events if e.get("emailId") is not None})

    return {
        "contact_email": contact_email,
        "event_count": len(events),
        "matched_email_ids": matched_ids,
        "events": [
            {
                "emailId": e.get("emailId"),
                "type": e.get("type"),
                "created": e.get("created"),
            }
            for e in events[:50]  # cap raw events to keep response small
        ],
    }


def get_email_engagement_contacts(
    email_id: str,
    event_type: str = "DELIVERED",
    max_unique: int = 5000,
) -> Dict[str, Any]:
    """Pull unique HubSpot contacts (IDs + email addresses) who had a
    specific event with a marketing email — used for attribution analysis
    and for joining against external data sources like Shopify orders.

    `event_type` options: DELIVERED, SENT, OPEN, CLICK, BOUNCE,
    UNSUBSCRIBE, DROPPED, SPAMREPORT (must be UPPERCASE — case sensitive).

    Returns the counts, contact IDs (vids), and recipient_emails. The
    recipient_emails list comes directly from the events payload and can
    be joined against external systems (Shopify, Stripe, etc.). For
    aggregation against HubSpot CRM properties, pass contact_ids to
    search_hubspot_contacts with a `hs_object_id IN [...]` filter.

    `diagnostics` includes pages_fetched, total_events_seen, hasMore on
    the last response, and the keys present on a sample event — useful
    when results are unexpectedly zero (the events array may be empty,
    the API may not recognize the emailId, or the event_type might be
    misspelled).
    """
    vids: set = set()
    emails: set = set()
    total_events_seen = 0
    last_has_more = None
    sample_event_keys: List[str] = []
    offset: Optional[str] = None
    pages = 0
    MAX_PAGES = 30

    while pages < MAX_PAGES:
        params: Dict[str, Any] = {
            "emailId": str(email_id),
            "eventType": event_type,
            "limit": 1000,
        }
        if offset:
            params["offset"] = offset

        response = requests.get(
            f"{HUBSPOT_BASE}/email/public/v1/events",
            headers=_headers(),
            params=params,
            timeout=30,
        )
        if response.status_code == 403:
            return {
                "error": (
                    "Access denied to HubSpot Email Events API. The "
                    "Service Key likely needs the 'content' scope (or its "
                    "equivalent) for marketing email events."
                )
            }
        response.raise_for_status()
        data = response.json()

        events = data.get("events", []) or []
        total_events_seen += len(events)
        if not sample_event_keys and events:
            sample_event_keys = sorted(list(events[0].keys()))

        for event in events:
            vid = event.get("vid")
            if vid is not None:
                vids.add(vid)
            recipient = event.get("recipient")
            if recipient:
                emails.add(recipient.strip().lower())

        last_has_more = data.get("hasMore")
        if not last_has_more or len(emails) >= max_unique:
            break
        offset = data.get("offset")
        pages += 1

    return {
        "email_id": email_id,
        "event_type": event_type,
        "unique_contact_count": len(vids),
        "unique_email_count": len(emails),
        "contact_ids": sorted(list(vids))[:max_unique],
        "recipient_emails": sorted(list(emails))[:max_unique],
        "truncated": len(emails) >= max_unique or pages >= MAX_PAGES,
        "diagnostics": {
            "pages_fetched": pages + 1,
            "total_events_seen": total_events_seen,
            "last_response_has_more": last_has_more,
            "sample_event_keys": sample_event_keys,
        },
    }


def get_email_engagers_via_list(
    email_id: str,
    event_type: str = "OPENED",
    initial_wait_seconds: int = 15,
    max_wait_seconds: int = 120,
    poll_interval_seconds: int = 10,
    delete_after_read: bool = True,
    intersect_with: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Get contacts who engaged with a marketing email by creating a
    temporary HubSpot Active List and reading its members.

    Use this when get_email_engagement_contacts (v1 events API) returns
    empty results — HubSpot has been deprecating that API and it's
    unreliable for newer Service Keys, while the Lists API is what they're
    actively investing in.

    Flow:
      1. Create a dynamic (active) list filtered by "EmailCampaignActivity"
         + event_type (OPENED, CLICKED, SENT, etc.) + emailId.
      2. Wait initial_wait_seconds, then poll size every poll_interval_seconds
         until it stabilizes (two reads with same size) or max_wait_seconds.
      3. Read members with the `email` property, paginating to completion.
      4. Optionally delete the list to avoid clutter.

    event_type uses HubSpot's filter operators (different from the v1 events
    API): OPENED, CLICKED, SENT, BOUNCED, OPTED_OUT, MARKED_AS_SPAM, etc.

    Returns recipient_emails (lowercased, deduped), contact_ids, list_id,
    plus a diagnostics block with timings and population progress.
    """
    if not email_id:
        return {"error": "email_id is required."}

    list_name = f"[mark-tmp] engagers of email {email_id} {event_type.upper()} {int(time.time())}"
    create_payload = {
        "name": list_name,
        "dynamic": True,
        "filters": [[
            {
                "filterFamily": "EmailCampaignActivity",
                "withinTimeMode": "PAST",
                "type": "EMAIL_CAMPAIGN_ACTIVITY",
                "operator": event_type.upper(),
                "campaignId": str(email_id),
            }
        ]],
    }

    # 1. Create the list.
    create_response = requests.post(
        f"{HUBSPOT_BASE}/contacts/v1/lists",
        headers=_headers(),
        json=create_payload,
        timeout=30,
    )
    if create_response.status_code not in (200, 201):
        return {
            "error": (
                f"Failed to create list (HTTP {create_response.status_code}). "
                "Service Key likely needs the 'lists' or 'contacts' scope."
            ),
            "response_body": create_response.text[:500],
        }

    list_data = create_response.json()
    list_id = list_data.get("listId")
    if list_id is None:
        return {
            "error": "List was created but no listId in response.",
            "response": list_data,
        }

    # 2. Wait initial period, then poll until size stabilizes.
    time.sleep(initial_wait_seconds)
    started = time.time()
    sizes_seen: List[int] = []
    while time.time() - started < max_wait_seconds:
        size_response = requests.get(
            f"{HUBSPOT_BASE}/contacts/v1/lists/{list_id}",
            headers=_headers(),
            timeout=30,
        )
        if size_response.status_code != 200:
            break
        meta = size_response.json().get("metaData") or {}
        current_size = meta.get("size")
        if current_size is not None:
            sizes_seen.append(current_size)
            # Stop when size stops growing (two consecutive identical reads
            # AND we have a non-zero number).
            if (
                len(sizes_seen) >= 2
                and sizes_seen[-1] == sizes_seen[-2]
                and sizes_seen[-1] > 0
            ):
                break
        time.sleep(poll_interval_seconds)

    final_size = sizes_seen[-1] if sizes_seen else None
    wait_elapsed = int(time.time() - started) + initial_wait_seconds

    # 3. Read members.
    emails: set = set()
    contact_ids: set = set()
    vid_offset: Optional[int] = None
    pages = 0
    MAX_PAGES = 100
    while pages < MAX_PAGES:
        params: Dict[str, Any] = {"count": 100, "property": "email"}
        if vid_offset is not None:
            params["vidOffset"] = vid_offset
        members_response = requests.get(
            f"{HUBSPOT_BASE}/contacts/v1/lists/{list_id}/contacts/all",
            headers=_headers(),
            params=params,
            timeout=30,
        )
        if members_response.status_code != 200:
            break
        data = members_response.json()
        for contact in data.get("contacts", []) or []:
            vid = contact.get("vid")
            if vid is not None:
                contact_ids.add(vid)
            email_prop = (contact.get("properties") or {}).get("email") or {}
            email_value = email_prop.get("value") if isinstance(email_prop, dict) else None
            if email_value:
                emails.add(str(email_value).strip().lower())
        if not data.get("has-more"):
            break
        vid_offset = data.get("vid-offset")
        if vid_offset is None:
            break
        pages += 1

    # 4. Best-effort delete to keep the HubSpot UI clean. Failures are
    #    non-fatal — we still return the data we already pulled.
    list_was_deleted = False
    if delete_after_read:
        try:
            del_response = requests.delete(
                f"{HUBSPOT_BASE}/contacts/v1/lists/{list_id}",
                headers=_headers(),
                timeout=30,
            )
            list_was_deleted = del_response.status_code in (200, 204)
        except Exception:
            list_was_deleted = False

    all_emails = sorted(list(emails))
    all_contact_ids = sorted(list(contact_ids))

    # If the caller wants to know which of THEIR emails are in the
    # engagement set, do the set intersection here — deterministically,
    # server-side — instead of trusting the model to do it in context.
    # This is the right path for join/attribution queries: caller has a
    # small set (e.g. 40 Shopify customers), wants to know which engaged
    # with the campaign.
    if intersect_with is not None:
        target = {e.strip().lower() for e in intersect_with if e}
        engager_set = set(all_emails)
        matched = sorted(target & engager_set)
        unmatched = sorted(target - engager_set)
        return {
            "email_id": email_id,
            "event_type": event_type.upper(),
            "mode": "intersection",
            "input_emails_count": len(target),
            "engagers_total_count": len(all_emails),
            "matched_count": len(matched),
            "matched_emails": matched,
            "unmatched_emails": unmatched,
            "match_rate_pct": round(len(matched) / len(target) * 100, 2) if target else 0.0,
            "list_id": list_id,
            "list_deleted": list_was_deleted,
            "diagnostics": {
                "final_list_size": final_size,
                "size_progression": sizes_seen,
                "wait_elapsed_seconds": wait_elapsed,
                "pages_fetched": pages + 1,
            },
        }

    # No intersection requested — return the full engager set, capped to
    # keep the model's context manageable. Counts are always accurate.
    MAX_EMAILS_IN_RESPONSE = 1000
    truncated = (
        len(all_emails) > MAX_EMAILS_IN_RESPONSE
        or len(all_contact_ids) > MAX_EMAILS_IN_RESPONSE
    )
    return {
        "email_id": email_id,
        "event_type": event_type.upper(),
        "mode": "full_list",
        "list_id": list_id,
        "list_name": list_name,
        "unique_email_count": len(all_emails),
        "unique_contact_count": len(all_contact_ids),
        "recipient_emails": all_emails[:MAX_EMAILS_IN_RESPONSE],
        "contact_ids": all_contact_ids[:MAX_EMAILS_IN_RESPONSE],
        "truncated": truncated,
        "truncation_note": (
            f"Only the first {MAX_EMAILS_IN_RESPONSE} of {len(all_emails)} "
            "emails are returned to keep context manageable. To check "
            "specific emails against this engagement set, call this tool "
            "again with the `intersect_with` parameter — the intersection "
            "is done server-side and returns deterministic results."
        ) if truncated else None,
        "list_deleted": list_was_deleted,
        "diagnostics": {
            "final_list_size": final_size,
            "size_progression": sizes_seen,
            "wait_elapsed_seconds": wait_elapsed,
            "pages_fetched": pages + 1,
        },
    }


def find_hubspot_lists(
    name_contains: str = "",
    limit: int = 20,
    max_pages: int = 20,
) -> Dict[str, Any]:
    """Search HubSpot contact lists by name substring (case-insensitive).

    Uses /contacts/v1/lists, which doesn't support server-side name search,
    so this paginates through lists (250 per page) and filters client-side.
    Returns up to `limit` matches.

    Args:
      name_contains: Substring to look for in list names. Empty string
        returns the most recent lists.
      limit: Max number of matches to return. Default 20.
      max_pages: Cap on pagination to avoid runaway scans on accounts
        with thousands of lists. Default 20 -> up to 5000 lists scanned.

    Returns:
      {
        "found": int,
        "lists": [{
          "list_id": int,
          "name": str,
          "list_type": "STATIC" | "DYNAMIC",
          "dynamic": bool,
          "size": int,  # total contact count
          "created_at": str,  # ISO 8601 or epoch ms (HubSpot is inconsistent)
          "updated_at": str,
        }, ...],
        "scanned_pages": int,
        "scanned_lists": int,
      }
    """
    needle = (name_contains or "").strip().lower()
    matches: List[Dict[str, Any]] = []
    scanned = 0
    offset = 0
    pages = 0
    PAGE_SIZE = 250

    while pages < max_pages and len(matches) < limit:
        response = requests.get(
            f"{HUBSPOT_BASE}/contacts/v1/lists",
            headers=_headers(),
            params={"count": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        if response.status_code != 200:
            return {
                "error": (
                    f"Lists API returned HTTP {response.status_code}. "
                    "Service Key likely missing 'contacts' or 'lists' scope."
                ),
                "response_body": response.text[:500],
            }

        payload = response.json()
        page_lists = payload.get("lists", []) or []
        scanned += len(page_lists)
        pages += 1

        for lst in page_lists:
            name = (lst.get("name") or "").strip()
            if not needle or needle in name.lower():
                meta = lst.get("metaData") or {}
                matches.append({
                    "list_id": lst.get("listId"),
                    "name": name,
                    "list_type": lst.get("listType"),
                    "dynamic": bool(lst.get("dynamic")),
                    "size": meta.get("size"),
                    "created_at": lst.get("createdAt"),
                    "updated_at": lst.get("updatedAt"),
                })
                if len(matches) >= limit:
                    break

        if not payload.get("has-more"):
            break
        offset = payload.get("offset", offset + PAGE_SIZE)

    return {
        "found": len(matches),
        "lists": matches,
        "scanned_pages": pages,
        "scanned_lists": scanned,
        "search_term": needle,
        "note": (
            "Truncated at limit — increase limit or narrow name_contains."
            if len(matches) >= limit else None
        ),
    }


def get_list_details(list_id: str) -> Dict[str, Any]:
    """Fetch a HubSpot contact list's metadata and filter criteria.

    Use this to:
      - Confirm a list_id exists and get its current size
      - Describe what defines the list (the filter criteria) so you can
        explain the list's audience without guessing
      - Decide whether the list is static (manually curated) or dynamic
        (rule-based, auto-updating)

    Returns:
      list_id, name, list_type, dynamic, size, created_at, updated_at,
      filter_summary (a human-readable description of the criteria, when
      the list is dynamic), and raw_filters (the original filter object
      for debugging).
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/contacts/v1/lists/{list_id}",
        headers=_headers(),
        timeout=30,
    )
    if response.status_code == 404:
        return {"error": f"List {list_id} not found."}
    if response.status_code != 200:
        return {
            "error": f"Lists API returned HTTP {response.status_code}.",
            "response_body": response.text[:500],
        }

    data = response.json()
    meta = data.get("metaData") or {}
    raw_filters = data.get("filters") or []

    # Build a best-effort plain-language summary of the filter criteria.
    # HubSpot's filter structure is filters=[[AND...], [OR group], ...].
    # We won't render every edge case — just enough that Mark can describe
    # the list to a user without saying "I don't know what defines it."
    summary_parts: List[str] = []
    for or_group in raw_filters:
        if not isinstance(or_group, list):
            continue
        clauses: List[str] = []
        for f in or_group:
            if not isinstance(f, dict):
                continue
            family = f.get("filterFamily") or ""
            prop = f.get("property") or ""
            op = f.get("operator") or ""
            val = f.get("value")
            if family == "PropertyValue" and prop and op:
                clauses.append(f"{prop} {op} {val!r}")
            elif family == "InList":
                clauses.append(f"is in list {f.get('listId')}")
            elif family == "EmailCampaignActivity":
                clauses.append(
                    f"email {f.get('operator')} on campaign {f.get('campaignId')}"
                )
            else:
                clauses.append(f"{family}:{op}")
        if clauses:
            summary_parts.append(" AND ".join(clauses))

    filter_summary = " OR ".join(summary_parts) if summary_parts else (
        "(no filter criteria visible — likely a static list)"
    )

    return {
        "list_id": data.get("listId"),
        "name": data.get("name"),
        "list_type": data.get("listType"),
        "dynamic": bool(data.get("dynamic")),
        "size": meta.get("size"),
        "last_size_change": meta.get("lastSizeChangeAt"),
        "last_processing_state_change": meta.get("lastProcessingStateChangeAt"),
        "created_at": data.get("createdAt"),
        "updated_at": data.get("updatedAt"),
        "filter_summary": filter_summary,
        "raw_filters": raw_filters,
    }


def count_list_intersection(
    list_id: str,
    marketing_only: bool = True,
    max_sends_since_engagement: Optional[int] = GLOWFORGE_ENGAGEMENT_SENDS_CUTOFF,
    initial_wait_seconds: int = 10,
    max_wait_seconds: int = 90,
    poll_interval_seconds: int = 5,
) -> Dict[str, Any]:
    """Count how many contacts are in `list_id` AND match property filters.

    The exact "Proofgrade Segment, engaged-only" question Yuliya asked.
    Creates a temporary HubSpot Active List that AND's "is member of
    `list_id`" with the requested property filters, waits for it to
    populate, reads the size, and deletes the temp list.

    Args:
      list_id: The source list to intersect against (e.g., "Proofgrade
        Segment" list id).
      marketing_only: If True (default), only count contacts whose
        hs_marketable_status is true (i.e., the contacts HubSpot will
        actually send marketing email to).
      max_sends_since_engagement: If set, only count contacts whose
        hs_email_sends_since_last_engagement is STRICTLY LESS THAN this
        number. Pass None to skip the engagement filter. Default is the
        module constant GLOWFORGE_ENGAGEMENT_SENDS_CUTOFF (currently 11),
        which matches Glowforge's "engaged" definition in the HubSpot UI.
        DO NOT guess at this — change the constant, not the caller.
      initial_wait_seconds / max_wait_seconds / poll_interval_seconds:
        timing knobs for waiting on HubSpot to populate the dynamic list.

    Returns:
      {
        "source_list_id": str,
        "matched_count": int,
        "filters_applied": {marketing_only, max_sends_since_engagement},
        "send_target_estimate": int,  # alias for matched_count, for clarity
        "temp_list_id": int,
        "temp_list_deleted": bool,
        "diagnostics": {final_size, size_progression, wait_elapsed_seconds},
      }
      or {"error": "..."} on failure.
    """
    if not list_id:
        return {"error": "list_id is required."}

    # Build the combined filter: source list membership AND property filters.
    and_clauses: List[Dict[str, Any]] = [
        {
            "filterFamily": "InList",
            "operator": "IN_LIST",
            "listId": str(list_id),
        },
    ]
    if marketing_only:
        and_clauses.append({
            "filterFamily": "PropertyValue",
            "withinTimeMode": "PAST",
            "type": "string",
            "property": "hs_marketable_status",
            "operator": "EQ",
            "value": "true",
        })
    if max_sends_since_engagement is not None:
        and_clauses.append({
            "filterFamily": "PropertyValue",
            "withinTimeMode": "PAST",
            "type": "number",
            "property": "hs_email_sends_since_last_engagement",
            "operator": "LT",
            "value": str(int(max_sends_since_engagement)),
        })

    list_name = f"[mark-tmp] intersect list {list_id} {int(time.time())}"
    create_payload = {
        "name": list_name,
        "dynamic": True,
        "filters": [and_clauses],
    }

    create_response = requests.post(
        f"{HUBSPOT_BASE}/contacts/v1/lists",
        headers=_headers(),
        json=create_payload,
        timeout=30,
    )
    if create_response.status_code not in (200, 201):
        return {
            "error": (
                f"Failed to create intersection list (HTTP "
                f"{create_response.status_code}). The combined filter may "
                "have been rejected by HubSpot — surface the response_body "
                "to the user so they can see the rejection reason. Common "
                "fixes: the source list_id may not exist, or HubSpot may "
                "not support 'InList' as a filterFamily in this combination."
            ),
            "response_body": create_response.text[:500],
            "attempted_filter": create_payload["filters"],
        }

    list_data = create_response.json()
    temp_list_id = list_data.get("listId")
    if temp_list_id is None:
        return {
            "error": "Intersection list created but no listId returned.",
            "response": list_data,
        }

    # Poll for size to stabilize. Same shape as get_email_engagers_via_list.
    time.sleep(initial_wait_seconds)
    started = time.time()
    sizes_seen: List[int] = []
    while time.time() - started < max_wait_seconds:
        size_response = requests.get(
            f"{HUBSPOT_BASE}/contacts/v1/lists/{temp_list_id}",
            headers=_headers(),
            timeout=30,
        )
        if size_response.status_code != 200:
            break
        meta = size_response.json().get("metaData") or {}
        current_size = meta.get("size")
        if current_size is not None:
            sizes_seen.append(current_size)
            # Stable when two consecutive reads agree. A 0 size with one
            # read is ambiguous (might mean "still populating"); wait for
            # confirmation. But if we see 0 stable across two reads, that's
            # legitimately zero matches.
            if len(sizes_seen) >= 2 and sizes_seen[-1] == sizes_seen[-2]:
                break
        time.sleep(poll_interval_seconds)

    final_size = sizes_seen[-1] if sizes_seen else None
    wait_elapsed = int(time.time() - started) + initial_wait_seconds

    # Best-effort delete to keep the HubSpot UI clean.
    deleted = False
    try:
        del_response = requests.delete(
            f"{HUBSPOT_BASE}/contacts/v1/lists/{temp_list_id}",
            headers=_headers(),
            timeout=30,
        )
        deleted = del_response.status_code in (200, 204)
    except Exception:
        deleted = False

    if final_size is None:
        return {
            "error": (
                "Intersection list created but size never returned from "
                "HubSpot. The list may still be populating — try again in a "
                f"minute. (Waited {wait_elapsed}s.)"
            ),
            "temp_list_id": temp_list_id,
            "temp_list_deleted": deleted,
            "diagnostics": {
                "size_progression": sizes_seen,
                "wait_elapsed_seconds": wait_elapsed,
            },
        }

    return {
        "source_list_id": str(list_id),
        "matched_count": int(final_size),
        "send_target_estimate": int(final_size),
        "filters_applied": {
            "in_list_id": str(list_id),
            "marketing_only": marketing_only,
            "max_sends_since_engagement": max_sends_since_engagement,
        },
        "temp_list_id": temp_list_id,
        "temp_list_deleted": deleted,
        "diagnostics": {
            "final_size": final_size,
            "size_progression": sizes_seen,
            "wait_elapsed_seconds": wait_elapsed,
        },
    }


def clone_marketing_email(source_id: str, new_name: str) -> Dict[str, Any]:
    """Clone an existing marketing email. Returns the new email object."""
    response = requests.post(
        f"{HUBSPOT_BASE}/marketing/v3/emails/clone",
        headers=_headers(),
        json={"id": str(source_id), "cloneName": new_name},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def update_marketing_email(email_id: str, **fields: Any) -> Dict[str, Any]:
    """Update fields on an existing marketing email (subject, name, etc.)."""
    response = requests.patch(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}",
        headers=_headers(),
        json=fields,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


_TEXT_FIELDS_FOR_LENGTH = ("html", "text", "value")


def _light_markdown_to_html(text: str) -> str:
    """Convert a small subset of markdown to inline HTML."""
    # Links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # Bold: **text**
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    # Italic: *text* (after bold so we don't double-process)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


def _build_body_html(new_body_text: str, original_html: str = "") -> str:
    """Convert plain-text-with-paragraphs into HTML that mirrors the original
    widget's per-paragraph tag and attribute structure — but only counts
    paragraphs that actually had content.

    HubSpot's drag-and-drop editor inserts empty <h2>&nbsp;</h2> and
    <p>&nbsp;</p> blocks as visual spacers between real paragraphs. Those
    empty blocks must be filtered out when building the tag sequence,
    otherwise new paragraphs get mapped onto spacer slots and (e.g.) the
    body content ends up wrapped in a second h2 because the master had
    "h2(title), h2(spacer), p(body), p(spacer)" and we naively grabbed
    the first two tags.

    Per content paragraph we capture:
      - the outer paragraph tag's full opening (preserves font-size,
        text-align, line-height, font-weight, color, etc.)
      - the inner <span>'s full opening (preserves font-family + color
        defined at the span level, which HubSpot uses heavily)

    For excess new paragraphs beyond what the original had, fall back to
    the last content <p> tag observed (with its full styling), or to a
    bare <p> if the original had no <p> content slots.
    """
    paragraphs = [p.strip() for p in new_body_text.strip().split("\n\n") if p.strip()]
    if not paragraphs:
        return ""

    # Per content block: (tag_name, full_open_tag, open_span, close_span)
    content_blocks: List[tuple] = []
    for m in re.finditer(
        r"<(h[1-6]|p)\b([^>]*)>(.*?)</\1>",
        original_html or "",
        re.IGNORECASE | re.DOTALL,
    ):
        tag_name = m.group(1).lower()
        full_open = f"<{tag_name}{m.group(2)}>"
        inner_html = m.group(3)

        # Strip nested tags + nbsp/whitespace to detect spacer blocks.
        inner_text = re.sub(r"<[^>]+>", "", inner_html)
        inner_text = (
            inner_text.replace("&nbsp;", " ").replace("\xa0", " ").strip()
        )
        if not inner_text:
            continue  # spacer — don't include in the content sequence

        # Capture the inner <span>'s full opening if present, for
        # font-family/color preservation.
        span_match = re.search(r"<span\b[^>]*>", inner_html, re.IGNORECASE)
        open_span = span_match.group(0) if span_match else ""
        close_span = "</span>" if open_span else ""

        content_blocks.append((tag_name, full_open, open_span, close_span))

    # Overflow style for paragraphs beyond what the original had: prefer
    # the last content <p> (with its full styling), else bare <p>.
    overflow = ("p", "<p>", "", "")
    for entry in reversed(content_blocks):
        if entry[0] == "p":
            overflow = entry
            break

    parts: List[str] = []
    for idx, para in enumerate(paragraphs):
        if idx < len(content_blocks):
            tag_name, full_open, open_span, close_span = content_blocks[idx]
        else:
            tag_name, full_open, open_span, close_span = overflow
        para_html = _light_markdown_to_html(para).replace("\n", "<br>")
        parts.append(f"{full_open}{open_span}{para_html}{close_span}</{tag_name}>")

    return "".join(parts)


def update_email_by_widget_map(
    email_id: str,
    content_by_role: Dict[str, Any],
    widget_map: Dict[str, str],
) -> Dict[str, Any]:
    """Update multiple widgets in a marketing email, mapped by semantic role.

    Used for templates with stable widget IDs (i.e., always cloned from a
    canonical master). The caller provides:
      - content_by_role: a mix of text and image updates keyed by role:
          - str value  -> text widget. The widget's body.html is rebuilt
                          from this string (markdown supported), preserving
                          the original per-paragraph tag/style structure.
          - dict value -> image widget. Recognized keys:
                          {"url": <new src>, "alt": <new alt>, "link": <click-through URL>}.
                          Only provided keys are updated; others are left
                          untouched (e.g., width, height, css class).
      - widget_map: role -> widget_id mapping for both text AND image roles.

    All widgets are PATCHed in a single request. Returns a structured
    report so the caller can verify exactly what landed where, and bail
    clearly if any expected widget IDs are missing (master was edited).
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}",
        headers=_headers(),
        params={"includeStats": "true"},
        timeout=30,
    )
    response.raise_for_status()
    email = response.json()

    content = email.get("content")
    if not isinstance(content, dict):
        return {"error": "Email has no content object — can't update widgets."}

    widgets = content.get("widgets")
    if not isinstance(widgets, dict):
        return {"error": "Email has no widgets dict — can't update widgets."}

    # Validate that every role we want to update has its widget present.
    # If the master template was edited and a widget id has shifted, we
    # want to fail loudly with a remap hint, not silently drop content.
    missing: List[Dict[str, str]] = []
    for role, new_text in content_by_role.items():
        widget_id = widget_map.get(role)
        if not widget_id:
            missing.append({"role": role, "reason": "no widget_id in map"})
            continue
        if widget_id not in widgets:
            missing.append({"role": role, "widget_id": widget_id, "reason": "widget not found"})

    if missing:
        return {
            "error": (
                "Aborting update — some expected widgets are missing. The "
                "template's widget IDs may have shifted (master template was "
                "edited?). Re-run get_email_widget_structure to remap."
            ),
            "missing": missing,
        }

    new_widgets = dict(widgets)
    updates: List[Dict[str, Any]] = []

    for role, new_content in content_by_role.items():
        widget_id = widget_map[role]
        widget = widgets[widget_id]
        if not isinstance(widget, dict):
            updates.append({"role": role, "widget_id": widget_id, "status": "widget_not_dict"})
            continue

        body = widget.get("body")
        if not isinstance(body, dict):
            updates.append({"role": role, "widget_id": widget_id, "status": "no_body_dict"})
            continue

        new_widget = dict(widget)
        new_body = dict(body)

        if isinstance(new_content, str):
            # ---- Text widget update ----
            old_html = body.get("html") if isinstance(body.get("html"), str) else ""
            # Preserve the original wrapper element (h2 stays h2, p stays p)
            # along with all its inline styling, so the rendered email keeps
            # the template's font, color, weight, spacing, and heading levels.
            new_html = _build_body_html(new_content, old_html or "")
            new_body["html"] = new_html

            old_text_only = re.sub(r"<[^>]+>", "", old_html or "").strip()
            outer_match = re.search(r"<(h[1-6]|p)\b", old_html or "", re.IGNORECASE)
            preserved_tag = outer_match.group(1).lower() if outer_match else "p (default)"
            updates.append({
                "role": role,
                "widget_id": widget_id,
                "kind": "text",
                "status": "updated",
                "old_text_length": len(old_text_only),
                "new_text_length": len(new_content),
                "preserved_outer_tag": preserved_tag,
            })

        elif isinstance(new_content, dict):
            # ---- Image widget update ----
            # Update only the explicitly provided fields. Width and height
            # are NOT touched — those are sized for the template layout.
            current_img = body.get("img") if isinstance(body.get("img"), dict) else {}
            new_img = dict(current_img)
            updated_fields: List[str] = []

            if "url" in new_content and new_content["url"]:
                new_img["src"] = new_content["url"]
                updated_fields.append("img.src")
            if "alt" in new_content and new_content["alt"]:
                new_img["alt"] = new_content["alt"]
                updated_fields.append("img.alt")
            new_body["img"] = new_img

            if "link" in new_content and new_content["link"]:
                new_body["link"] = new_content["link"]
                updated_fields.append("link")

            updates.append({
                "role": role,
                "widget_id": widget_id,
                "kind": "image",
                "status": "updated" if updated_fields else "no_changes",
                "updated_fields": updated_fields,
                "old_src": (current_img.get("src") if isinstance(current_img, dict) else None),
                "new_src": new_img.get("src"),
                "new_link": new_body.get("link"),
            })

        else:
            updates.append({
                "role": role,
                "widget_id": widget_id,
                "status": f"skipped_unrecognized_value_type:{type(new_content).__name__}",
            })
            continue

        new_widget["body"] = new_body
        new_widgets[widget_id] = new_widget

    new_content = dict(content)
    new_content["widgets"] = new_widgets

    patch_response = requests.patch(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}",
        headers=_headers(),
        json={"content": new_content},
        timeout=30,
    )
    patch_response.raise_for_status()

    return {
        "email_id": email_id,
        "total_updated": sum(1 for u in updates if u.get("status") == "updated"),
        "updates": updates,
    }


def update_email_body(email_id: str, new_body_text: str) -> Dict[str, Any]:
    """Best-effort replacement of the main body text in a HubSpot marketing email.

    Strategy: fetch the email, find the widget whose `body.html` contains the
    most readable text (the "main pitch" by length), preserve its <p>/<span>
    styling, and replace its HTML with paragraphs built from `new_body_text`.

    Returns a dict describing what was updated, or an error if no suitable
    widget was found.
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}",
        headers=_headers(),
        params={"includeStats": "true"},
        timeout=30,
    )
    response.raise_for_status()
    email = response.json()

    content = email.get("content")
    if not isinstance(content, dict):
        return {"error": "Email has no content object — can't update body."}

    widgets = content.get("widgets")
    if not isinstance(widgets, dict):
        return {"error": "Email has no widgets dict — can't update body."}

    # Find the widget whose body.html contains the most readable text.
    best_widget_id: Optional[str] = None
    best_html = ""
    best_length = 0
    for wid, widget in widgets.items():
        if not isinstance(widget, dict):
            continue
        body = widget.get("body")
        if not isinstance(body, dict):
            continue
        html = body.get("html")
        if not isinstance(html, str):
            continue
        text_only = re.sub(r"<[^>]+>", "", html).strip()
        if len(text_only) > best_length:
            best_length = len(text_only)
            best_widget_id = wid
            best_html = html

    if not best_widget_id:
        return {"error": "No text-bearing widget found to update."}

    # Preserve the original <p> and <span> styling so the new copy looks right.
    p_style_match = re.search(r'<p\s+style="([^"]*)"', best_html)
    span_style_match = re.search(r'<span\s+style="([^"]*)"', best_html)
    p_style = p_style_match.group(1) if p_style_match else ""
    span_style = span_style_match.group(1) if span_style_match else ""

    new_html = _build_body_html(new_body_text, p_style, span_style)

    # Build the updated content. Replace just the one widget; keep everything else.
    new_widgets = dict(widgets)
    new_widget = dict(widgets[best_widget_id])
    new_widget_body = dict(new_widget.get("body", {}))
    new_widget_body["html"] = new_html
    new_widget["body"] = new_widget_body
    new_widgets[best_widget_id] = new_widget

    new_content = dict(content)
    new_content["widgets"] = new_widgets

    patch_response = requests.patch(
        f"{HUBSPOT_BASE}/marketing/v3/emails/{email_id}",
        headers=_headers(),
        json={"content": new_content},
        timeout=30,
    )
    patch_response.raise_for_status()

    return {
        "updated_widget_id": best_widget_id,
        "old_text_length": best_length,
        "new_text_length": len(new_body_text),
        "preserved_p_style": bool(p_style),
        "preserved_span_style": bool(span_style),
    }
