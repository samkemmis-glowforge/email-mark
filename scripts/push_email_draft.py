"""Create or update a HubSpot marketing email draft from a body-HTML file.

The Claude Code-surface counterpart to Mark's Slack drafting flow: email
source lives in emails/<slug>/ (body.html + meta.json), and this script
pushes it to HubSpot via create_email_draft_v2 / update_email_draft_v2.

Usage:
    # First push — creates the draft, writes email_id back into meta.json
    python scripts/push_email_draft.py emails/2026-07-premium-trial-day0

    # Subsequent pushes — sees email_id in meta.json and updates in place
    python scripts/push_email_draft.py emails/2026-07-premium-trial-day0

meta.json fields: name, subject, preheader (optional), email_id (written
back after the first push — commit it so later sessions update rather
than duplicate).
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from email_mark import hubspot_marketing as hm


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    email_dir = pathlib.Path(sys.argv[1])
    body_path = email_dir / "body.html"
    meta_path = email_dir / "meta.json"
    if not body_path.exists() or not meta_path.exists():
        print(f"Expected {body_path} and {meta_path} to exist.")
        return 2

    body_html = body_path.read_text()
    meta = json.loads(meta_path.read_text())

    email_id = meta.get("email_id")
    if email_id:
        result = hm.update_email_draft_v2(
            email_id=str(email_id),
            body_html=body_html,
            subject=meta.get("subject"),
            preheader=meta.get("preheader"),
            name=meta.get("name"),
        )
    else:
        result = hm.create_email_draft_v2(
            name=meta["name"],
            subject=meta["subject"],
            body_html=body_html,
            preheader=meta.get("preheader"),
        )
        if result.get("email_id"):
            meta["email_id"] = result["email_id"]
            meta_path.write_text(json.dumps(meta, indent=2) + "\n")
            print(f"Wrote email_id={result['email_id']} back to {meta_path}")

    print(json.dumps(result, indent=2))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
