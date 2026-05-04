"""Pull marketing email stats matching a name pattern.

Run from the project root:
    .venv/bin/python scripts/test_marketing_emails.py "universal premium"
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from email_mark.hubspot_marketing import (  # noqa: E402
    get_email_statistics,
    list_marketing_emails,
)


def _pct(part, whole):
    if not whole:
        return "—"
    return f"{(part / whole) * 100:.1f}%"


def _extract_counters(email_obj: dict) -> dict:
    """Stats can live in a few places depending on the email type. Try them."""
    candidates = [
        email_obj.get("stats", {}).get("counters"),
        email_obj.get("statistics", {}).get("counters"),
        email_obj.get("aggregateStatistics", {}).get("counters"),
        email_obj.get("aggregate", {}).get("counters"),
        email_obj.get("counters"),
    ]
    for c in candidates:
        if c:
            return c
    return {}


def main() -> None:
    needle = sys.argv[1] if len(sys.argv) > 1 else "universal premium"
    print(f"Searching marketing emails for name containing: {needle!r}\n")

    emails = list_marketing_emails(name_contains=needle, limit=100)
    if not emails:
        print("No matching marketing emails found.")
        return

    print(f"Found {len(emails)} email(s).\n")

    # Diagnostic: dump the raw structure of the first email so we can see
    # where stats actually live in the response.
    first_id = emails[0].get("id")
    print(f"=== RAW STRUCTURE OF EMAIL {first_id} (for debugging) ===")
    try:
        raw = get_email_statistics(first_id)
        print(json.dumps(raw, indent=2, default=str)[:4000])
        print("=== (truncated to 4000 chars) ===\n")
    except Exception as ex:
        print(f"Failed to fetch raw email: {ex}\n")

    print("=== SUMMARY ===\n")
    for e in emails:
        eid = e.get("id")
        name = e.get("name")
        state = e.get("state")
        subject = e.get("subject")
        print(f"--- {name} ---")
        print(f"  id:      {eid}")
        print(f"  state:   {state}")
        print(f"  subject: {subject}")

        try:
            stats = get_email_statistics(eid)
        except Exception as ex:
            print(f"  stats:   (failed — {ex})")
            print()
            continue

        counters = _extract_counters(stats)
        sent = counters.get("sent", 0)
        opens = counters.get("open", 0)
        clicks = counters.get("click", 0)
        unsubs = counters.get("unsubscribed", 0)

        print(f"  sent:    {sent}")
        print(f"  opens:   {opens} ({_pct(opens, sent)})")
        print(f"  clicks:  {clicks} ({_pct(clicks, sent)})")
        print(f"  unsubs:  {unsubs} ({_pct(unsubs, sent)})")
        print()


if __name__ == "__main__":
    main()
