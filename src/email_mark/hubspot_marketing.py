"""HubSpot Marketing Email API client.

Fills the gap left by the official HubSpot MCP, which doesn't expose
marketing emails or A/B test results. Uses the Private App token.
"""

from __future__ import annotations

import os
import re
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
            rows.append(
                {
                    "widget_id": wid,
                    "widget_type": widget.get("type") or widget.get("name") or "",
                    "label": widget.get("label") or "",
                    "html_text_length": len(text_only),
                    "text_preview": text_only[:200],
                    "has_html_field": bool(html),
                }
            )

    return {
        "email_id": email.get("id"),
        "email_name": email.get("name"),
        "subject": email.get("subject"),
        "widget_count": len(rows),
        "widgets": rows,
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
    """Pull unique HubSpot contact IDs who had a specific event with a
    marketing email — used for attribution analysis.

    `event_type` options: DELIVERED, SENT, OPEN, CLICK, BOUNCE,
    UNSUBSCRIBE, DROPPED, SPAMREPORT.

    Returns the count and a list of contact IDs (vids only — no emails or
    names). Caps pagination at max_unique IDs to prevent runaway calls
    on huge campaigns. The returned IDs can be passed to
    search_hubspot_contacts with a `hs_object_id IN [...]` filter to
    aggregate by subscription status, lifecycle stage, etc.
    """
    vids: set = set()
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

        for event in data.get("events", []) or []:
            vid = event.get("vid")
            if vid is not None:
                vids.add(vid)

        if not data.get("hasMore") or len(vids) >= max_unique:
            break
        offset = data.get("offset")
        pages += 1

    return {
        "email_id": email_id,
        "event_type": event_type,
        "unique_contact_count": len(vids),
        "contact_ids": sorted(list(vids))[:max_unique],
        "truncated": len(vids) >= max_unique or pages >= MAX_PAGES,
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


def _build_body_html(new_body_text: str, p_style: str, span_style: str) -> str:
    """Convert plain-text-with-paragraphs into the same HTML pattern as the
    template's existing body widget."""
    paragraphs = [p.strip() for p in new_body_text.strip().split("\n\n") if p.strip()]
    parts: List[str] = []
    for para in paragraphs:
        para = _light_markdown_to_html(para).replace("\n", "<br>")
        if p_style and span_style:
            parts.append(
                f'<p style="{p_style}"><span style="{span_style}">{para}</span></p>'
            )
        elif p_style:
            parts.append(f'<p style="{p_style}">{para}</p>')
        else:
            parts.append(f"<p>{para}</p>")
    return "".join(parts)


def update_email_by_widget_map(
    email_id: str,
    content_by_role: Dict[str, str],
    widget_map: Dict[str, str],
) -> Dict[str, Any]:
    """Update multiple widgets in a marketing email, mapped by semantic role.

    Used for templates with stable widget IDs (i.e., always cloned from a
    canonical master). The caller provides:
      - content_by_role: e.g., {"intro_body": "...", "project_1_title": "..."}
      - widget_map:      e.g., {"intro_body": "module_17734393985902", ...}

    For each role with content provided, finds the widget by ID, preserves
    its existing <p>/<span> styling, and replaces the body html with new
    paragraphs built from the role's text (markdown supported). All widgets
    are PATCHed in a single request.

    Returns a structured report so the caller can verify exactly what
    landed where, and bail clearly if the template's widget IDs have
    drifted (template was edited, etc.).
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

    for role, new_text in content_by_role.items():
        widget_id = widget_map[role]
        widget = widgets[widget_id]
        if not isinstance(widget, dict):
            updates.append({"role": role, "widget_id": widget_id, "status": "widget_not_dict"})
            continue

        body = widget.get("body")
        if not isinstance(body, dict):
            updates.append({"role": role, "widget_id": widget_id, "status": "no_body_dict"})
            continue

        old_html = body.get("html") if isinstance(body.get("html"), str) else ""
        # Preserve the original <p>/<span> styling so the rendered email
        # keeps the template's font, color, and spacing.
        p_style_match = re.search(r'<p\s+style="([^"]*)"', old_html or "")
        span_style_match = re.search(r'<span\s+style="([^"]*)"', old_html or "")
        p_style = p_style_match.group(1) if p_style_match else ""
        span_style = span_style_match.group(1) if span_style_match else ""

        new_html = _build_body_html(new_text, p_style, span_style)

        new_widget = dict(widget)
        new_body = dict(body)
        new_body["html"] = new_html
        new_widget["body"] = new_body
        new_widgets[widget_id] = new_widget

        old_text_only = re.sub(r"<[^>]+>", "", old_html or "").strip()
        updates.append({
            "role": role,
            "widget_id": widget_id,
            "status": "updated",
            "old_text_length": len(old_text_only),
            "new_text_length": len(new_text),
            "preserved_p_style": bool(p_style),
            "preserved_span_style": bool(span_style),
        })

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
