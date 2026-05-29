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
    temporary HubSpot dynamic list (v3) and reading its members.

    Uses the v3 lists API (which speaks the modern crm.lists.* scopes
    Sam's Service Key has). The v1 version of this function failed with
    403 because v1 endpoints check legacy `contacts-lists-*` scope names
    that aren't grantable on the current Service Key.

    Flow:
      1. Create a v3 dynamic list with a filterBranch matching
         EMAIL_CAMPAIGN_ACTIVITY + operator (CLICKED, OPENED, etc.)
         + emailCampaignId.
      2. Wait initial_wait_seconds, then poll size every poll_interval_seconds
         until it stabilizes (two consecutive identical reads).
      3. Read members via /crm/v3/lists/{id}/memberships (returns contact
         IDs), then batch-read their emails via
         /crm/v3/objects/contacts/batch/read.
      4. Optionally delete the list to keep HubSpot tidy.

    event_type: CLICKED, OPENED, SENT, BOUNCED, OPTED_OUT, MARKED_AS_SPAM,
    etc. — same vocabulary as v1.

    Scopes needed: crm.lists.read, crm.lists.write, crm.objects.contacts.read.

    Return shape preserved from the v1 implementation so existing callers
    keep working: recipient_emails (lowercased, deduped), contact_ids,
    list_id, plus a diagnostics block.
    """
    if not email_id:
        return {"error": "email_id is required."}

    list_name = f"[mark-tmp] engagers of email {email_id} {event_type.upper()} {int(time.time())}"

    # v3 list create payload. The filterBranch tree expresses the same
    # "clicked email X" predicate that v1's EmailCampaignActivity filter
    # family did, but in the modern shape that the v3 lists API speaks.
    create_payload = {
        "name": list_name,
        "objectTypeId": "0-1",  # contacts
        "processingType": "DYNAMIC",
        "filterBranch": {
            "filterBranchType": "AND",
            "filterBranchOperator": "AND",
            "filters": [
                {
                    "filterType": "EMAIL_CAMPAIGN_ACTIVITY",
                    "operator": event_type.upper(),
                    "campaignId": str(email_id),
                }
            ],
            "filterBranches": [],
        },
    }

    # 1. Create the list.
    create_response = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/lists",
        headers=_headers(),
        json=create_payload,
        timeout=30,
    )
    if create_response.status_code not in (200, 201):
        return {
            "error": (
                f"Failed to create v3 dynamic list (HTTP "
                f"{create_response.status_code}). Likely causes: 400 means "
                "HubSpot rejected the EMAIL_CAMPAIGN_ACTIVITY filter shape "
                "(may want different field names — emailId vs campaignId, "
                "etc.); 403 means missing crm.lists.write scope. Surface "
                "the response_body verbatim to the user."
            ),
            "response_body": create_response.text[:500],
            "attempted_filter": create_payload["filterBranch"],
        }

    create_body = create_response.json()
    # v3 wraps the created object as {"list": {"listId": "..."}}; tolerate
    # both wrapped and flat shapes.
    created_list = create_body.get("list") if isinstance(create_body.get("list"), dict) else create_body
    list_id = created_list.get("listId")
    if list_id is None:
        return {
            "error": "v3 list was created but no listId in response.",
            "response": create_body,
        }
    list_id = str(list_id)

    # 2. Wait initial period, then poll until size stabilizes.
    time.sleep(initial_wait_seconds)
    started = time.time()
    sizes_seen: List[int] = []
    while time.time() - started < max_wait_seconds:
        size_response = requests.get(
            f"{HUBSPOT_BASE}/crm/v3/lists/{list_id}",
            headers=_headers(),
            timeout=30,
        )
        if size_response.status_code != 200:
            break
        size_body = size_response.json()
        size_list = size_body.get("list") if isinstance(size_body.get("list"), dict) else size_body
        addl = size_list.get("additionalProperties") or {}
        size_raw = addl.get("hs_list_size")
        try:
            current_size = int(size_raw) if size_raw is not None else None
        except (TypeError, ValueError):
            current_size = None
        if current_size is not None:
            sizes_seen.append(current_size)
            # Stable when two consecutive reads agree (even if 0 — that's
            # legitimately "no one engaged" once confirmed).
            if len(sizes_seen) >= 2 and sizes_seen[-1] == sizes_seen[-2]:
                break
        time.sleep(poll_interval_seconds)

    final_size = sizes_seen[-1] if sizes_seen else None
    wait_elapsed = int(time.time() - started) + initial_wait_seconds

    # 3a. Read memberships (just contact IDs, paginated).
    contact_id_set: set = set()
    after: Optional[str] = None
    pages = 0
    MAX_PAGES = 100
    while pages < MAX_PAGES:
        params: Dict[str, Any] = {"limit": 250}
        if after is not None:
            params["after"] = after
        members_response = requests.get(
            f"{HUBSPOT_BASE}/crm/v3/lists/{list_id}/memberships",
            headers=_headers(),
            params=params,
            timeout=30,
        )
        if members_response.status_code != 200:
            break
        data = members_response.json()
        for member in data.get("results", []) or []:
            record_id = member.get("recordId")
            if record_id is not None:
                contact_id_set.add(str(record_id))
        paging = (data.get("paging") or {}).get("next") or {}
        after = paging.get("after")
        if not after:
            break
        pages += 1

    # 3b. Batch-fetch emails for each contact ID. v3 batch/read takes
    # up to 100 ids per call.
    emails: set = set()
    all_ids = list(contact_id_set)
    BATCH = 100
    batch_errors = 0
    for start in range(0, len(all_ids), BATCH):
        chunk = all_ids[start:start + BATCH]
        batch_response = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/batch/read",
            headers=_headers(),
            json={"inputs": [{"id": cid} for cid in chunk], "properties": ["email"]},
            timeout=30,
        )
        if batch_response.status_code not in (200, 207):
            batch_errors += 1
            continue
        body = batch_response.json()
        for contact in body.get("results", []) or []:
            email_val = (contact.get("properties") or {}).get("email")
            if email_val:
                emails.add(str(email_val).strip().lower())

    # 4. Best-effort delete to keep the HubSpot UI clean. Failures are
    #    non-fatal — we still return the data we already pulled.
    list_was_deleted = False
    if delete_after_read:
        try:
            del_response = requests.delete(
                f"{HUBSPOT_BASE}/crm/v3/lists/{list_id}",
                headers=_headers(),
                timeout=30,
            )
            list_was_deleted = del_response.status_code in (200, 204)
        except Exception:
            list_was_deleted = False

    all_emails = sorted(list(emails))
    all_contact_ids = sorted(list(contact_id_set))

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
                "membership_pages_fetched": pages + 1,
                "batch_read_errors": batch_errors,
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
            "membership_pages_fetched": pages + 1,
            "batch_read_errors": batch_errors,
        },
    }


def find_hubspot_lists(
    name_contains: str = "",
    limit: int = 20,
) -> Dict[str, Any]:
    """Search HubSpot contact lists by name via the v3 lists search API.

    Uses POST /crm/v3/lists/search, which supports server-side name search
    (the legacy v1 endpoint required scanning all lists and filtering
    client-side). Needs the `crm.lists.read` scope.

    Args:
      name_contains: Substring to look for in list names. Case-insensitive
        match handled server-side.
      limit: Max number of matches to return. Default 20.

    Returns:
      {
        "found": int,
        "lists": [{
          "list_id": str,
          "name": str,
          "processing_type": "DYNAMIC" | "STATIC" | "MANUAL" | ...,
          "object_type_id": str,  # "0-1" for contacts
          "size": int | None,
          "created_at": str,  # ISO 8601 UTC
          "updated_at": str,
        }, ...],
        "total_matching": int,  # total matches HubSpot says exist
        "search_term": str,
      }
    """
    payload = {
        "query": (name_contains or "").strip(),
        "count": int(limit),
        "offset": 0,
        # Request the size field on each list so we don't need a second call.
        "additionalProperties": ["hs_list_size", "hs_list_reference_count"],
    }
    response = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/lists/search",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    if response.status_code != 200:
        return {
            "error": (
                f"v3 lists search returned HTTP {response.status_code}. "
                "Service Key likely missing 'crm.lists.read' scope, or the "
                "v3 lists endpoint isn't enabled on this account."
            ),
            "response_body": response.text[:500],
        }

    body = response.json()
    raw_lists = body.get("lists") or body.get("results") or []

    matches: List[Dict[str, Any]] = []
    for lst in raw_lists:
        addl = lst.get("additionalProperties") or {}
        size_val = addl.get("hs_list_size")
        try:
            size = int(size_val) if size_val is not None else None
        except (TypeError, ValueError):
            size = None
        matches.append({
            "list_id": str(lst.get("listId")) if lst.get("listId") is not None else None,
            "name": lst.get("name"),
            "processing_type": lst.get("processingType"),
            "object_type_id": lst.get("objectTypeId"),
            "size": size,
            "created_at": lst.get("createdAt"),
            "updated_at": lst.get("updatedAt"),
        })

    return {
        "found": len(matches),
        "lists": matches,
        "total_matching": body.get("total"),
        "search_term": (name_contains or "").strip(),
        "note": (
            "Search returned more matches than `limit` — narrow the search "
            "term or increase limit."
            if body.get("hasMore") else None
        ),
    }


def _summarize_v3_filter_branch(branch: Any) -> str:
    """Render a v3 list filterBranch tree as a short human-readable string.

    HubSpot's v3 filter tree has two node kinds:
      - Leaf: {filterType: PROPERTY | IN_LIST | ..., property, operator, value}
      - Branch: {filterBranchType: AND | OR, filters: [leaf...], filterBranches: [branch...]}
    """
    if not isinstance(branch, dict):
        return ""
    ftype = branch.get("filterType")
    if ftype == "IN_LIST":
        return f"is in list {branch.get('listId')}"
    if ftype == "PROPERTY":
        prop = branch.get("property") or ""
        op = branch.get("operator") or ""
        val = branch.get("value")
        if val is not None:
            return f"{prop} {op} {val!r}"
        values = branch.get("values")
        if values:
            return f"{prop} {op} {values!r}"
        return f"{prop} {op}"
    if ftype:
        # Other leaf filter types — render minimally.
        return f"{ftype}:{branch.get('operator', '')}"
    # Branch node — recurse.
    op = branch.get("filterBranchOperator", "AND")
    parts: List[str] = []
    for sub in (branch.get("filters") or []):
        s = _summarize_v3_filter_branch(sub)
        if s:
            parts.append(s)
    for sub in (branch.get("filterBranches") or []):
        s = _summarize_v3_filter_branch(sub)
        if s:
            parts.append(f"({s})")
    if not parts:
        return ""
    joiner = " AND " if op == "AND" else " OR "
    return joiner.join(parts)


def get_list_details(list_id: str) -> Dict[str, Any]:
    """Fetch a HubSpot list's metadata and filter criteria via the v3 API.

    Uses GET /crm/v3/lists/{listId} with includeFilters=true so we get the
    filter tree in the same call. Needs the `crm.lists.read` scope.

    Returns:
      list_id, name, processing_type, object_type_id, size, created_at,
      updated_at, filter_summary (plain-English render of the criteria when
      the list is dynamic), raw_filter (the full v3 filterBranch tree).
    """
    response = requests.get(
        f"{HUBSPOT_BASE}/crm/v3/lists/{list_id}",
        headers=_headers(),
        params={"includeFilters": "true"},
        timeout=30,
    )
    if response.status_code == 404:
        return {"error": f"List {list_id} not found."}
    if response.status_code != 200:
        return {
            "error": (
                f"v3 lists get returned HTTP {response.status_code}. "
                "Service Key likely missing 'crm.lists.read' scope."
            ),
            "response_body": response.text[:500],
        }

    body = response.json()
    # v3 wraps the list object as {"list": {...}}; older responses may
    # return the object at the top level. Tolerate both.
    lst = body.get("list") if isinstance(body.get("list"), dict) else body

    addl = lst.get("additionalProperties") or {}
    size_val = addl.get("hs_list_size")
    try:
        size = int(size_val) if size_val is not None else None
    except (TypeError, ValueError):
        size = None

    raw_filter = lst.get("filterBranch") or {}
    filter_summary = _summarize_v3_filter_branch(raw_filter) or (
        "(no filter criteria visible — likely a static or manual list)"
    )

    return {
        "list_id": str(lst.get("listId")) if lst.get("listId") is not None else None,
        "name": lst.get("name"),
        "processing_type": lst.get("processingType"),
        "object_type_id": lst.get("objectTypeId"),
        "size": size,
        "created_at": lst.get("createdAt"),
        "updated_at": lst.get("updatedAt"),
        "filter_summary": filter_summary,
        "raw_filter": raw_filter,
    }


def count_list_intersection(
    list_id: str,
    marketing_only: bool = True,
    max_sends_since_engagement: Optional[int] = GLOWFORGE_ENGAGEMENT_SENDS_CUTOFF,
) -> Dict[str, Any]:
    """Count contacts in `list_id` AND matching property filters via v3.

    Uses POST /crm/v3/objects/contacts/search with a combined filter:
      - ilsListMemberships.listId EQ <list_id>     (membership in source list)
      - hs_marketable_status EQ true                (if marketing_only)
      - hs_email_sends_since_last_engagement LT N   (if engagement cap set)
    Reads `total` from the search response. Single API call, no temp lists.

    Scopes needed (Sam already has both):
      - crm.objects.contacts.read
      - crm.lists.read

    Args:
      list_id: The source list to intersect against.
      marketing_only: If True (default), only count contacts whose
        hs_marketable_status is true (the ones HubSpot will actually send
        marketing email to).
      max_sends_since_engagement: If set (default 11, Glowforge's cutoff
        per GLOWFORGE_ENGAGEMENT_SENDS_CUTOFF), only count contacts whose
        hs_email_sends_since_last_engagement is STRICTLY LESS THAN this.
        Pass None to skip the engagement filter. DO NOT guess this — change
        the constant if the business rule changes.

    Returns:
      {
        "source_list_id": str,
        "matched_count": int,
        "send_target_estimate": int,  # alias for matched_count
        "filters_applied": {...},
        "attempted_filters": [...],   # the literal filters sent to HubSpot
        "method": "v3_contacts_search",
      }
      or {"error": "..."} on failure.
    """
    if not list_id:
        return {"error": "list_id is required."}

    filters: List[Dict[str, Any]] = [
        {
            "propertyName": "ilsListMemberships.listId",
            "operator": "EQ",
            "value": str(list_id),
        },
    ]
    if marketing_only:
        filters.append({
            "propertyName": "hs_marketable_status",
            "operator": "EQ",
            "value": "true",
        })
    if max_sends_since_engagement is not None:
        filters.append({
            "propertyName": "hs_email_sends_since_last_engagement",
            "operator": "LT",
            "value": str(int(max_sends_since_engagement)),
        })

    payload = {
        "filterGroups": [{"filters": filters}],
        "limit": 1,  # We only need `total` — don't pay to ship records.
        "properties": ["hs_object_id"],  # Cheapest possible projection.
    }

    response = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_headers(),
        json=payload,
        timeout=60,
    )
    if response.status_code != 200:
        return {
            "error": (
                f"v3 contacts search returned HTTP {response.status_code}. "
                "Likely causes: 400 means HubSpot rejected the filter shape "
                "(ilsListMemberships.listId may not be filterable here, or "
                "the listId doesn't exist); 403 means the Service Key is "
                "missing 'crm.objects.contacts.read'. The response body has "
                "the specific reason — share it with the user verbatim."
            ),
            "response_body": response.text[:500],
            "attempted_filters": filters,
        }

    body = response.json()
    total = body.get("total")
    if total is None:
        return {
            "error": (
                "v3 contacts search succeeded but returned no `total` field. "
                "HubSpot's response shape may have changed."
            ),
            "response_body": str(body)[:500],
            "attempted_filters": filters,
        }

    return {
        "source_list_id": str(list_id),
        "matched_count": int(total),
        "send_target_estimate": int(total),
        "filters_applied": {
            "in_list_id": str(list_id),
            "marketing_only": marketing_only,
            "max_sends_since_engagement": max_sends_since_engagement,
        },
        "attempted_filters": filters,
        "method": "v3_contacts_search",
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
