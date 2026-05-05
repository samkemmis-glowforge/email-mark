"""Inspect the body structure of a HubSpot marketing email.

Used to figure out how to replace body content for a given template family.

Run from the project root:
    .venv/bin/python scripts/inspect_email.py <email_id>
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from email_mark.hubspot_marketing import get_email_statistics  # noqa: E402


TEXT_FIELDS = ["body", "html", "value", "text", "rich_text", "html_text"]


def _preview_text(s: str, n: int = 200) -> str:
    s = " ".join(s.split())  # collapse whitespace
    return s[:n] + ("..." if len(s) > n else "")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: .venv/bin/python scripts/inspect_email.py <email_id>")
        sys.exit(1)

    email_id = sys.argv[1]
    email = get_email_statistics(email_id)

    print(f"Email name:    {email.get('name')}")
    print(f"Email id:      {email.get('id')}")
    print(f"State:         {email.get('state')}")
    print(f"Subject:       {email.get('subject')}")
    print(f"Email type:    {email.get('emailType') or email.get('type')}")
    print()

    # Top-level body fields some templates use
    print("=== TOP-LEVEL BODY-ISH FIELDS ===")
    for k in TEXT_FIELDS + ["htmlBody", "previewText"]:
        v = email.get(k)
        if v:
            print(f"  email.{k}: {_preview_text(str(v))}")
    print()

    content = email.get("content", {}) if isinstance(email.get("content"), dict) else {}
    print(f"=== email.content keys: {list(content.keys())}")
    print()

    # Inspect widgets dict
    widgets = content.get("widgets")
    if isinstance(widgets, dict):
        print(f"=== WIDGETS DICT ({len(widgets)} total) ===")
        for wid, w in widgets.items():
            if not isinstance(w, dict):
                continue
            wtype = w.get("type") or w.get("module_type") or w.get("name") or "?"
            body_field = None
            body_preview = None
            for field in TEXT_FIELDS:
                if w.get(field):
                    body_field = field
                    body_preview = _preview_text(str(w[field]))
                    break
            # Body might also be nested under 'body' as dict
            body = w.get("body")
            if isinstance(body, dict):
                for field in TEXT_FIELDS:
                    if body.get(field):
                        body_field = f"body.{field}"
                        body_preview = _preview_text(str(body[field]))
                        break
            print(f"  [{wid}] type={wtype}, keys={list(w.keys())}")
            if body_field:
                print(f"    -> {body_field}: {body_preview!r}")
        print()

    # Dump flexAreas widget references for traversal mapping
    fa = content.get("flexAreas")
    if isinstance(fa, dict):
        print("=== FLEX AREA → WIDGET MAPPING ===")
        for area_name, area in fa.items():
            if not isinstance(area, dict):
                continue
            for section in area.get("sections", []) or []:
                sid = section.get("id")
                for col in section.get("columns", []) or []:
                    for widget_id in col.get("widgets", []) or []:
                        print(f"  {area_name} / section {sid} → widget {widget_id}")
        print()


if __name__ == "__main__":
    main()
