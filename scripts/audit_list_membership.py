"""Cross-check a set of emails against a HubSpot dynamic list's membership.

Given a CSV file of emails (typically exported from somewhere external,
e.g. a Shopify opt-in list) and a HubSpot list id, report:
  - How many of those emails ARE currently in the list
  - How many ARE in HubSpot but NOT in the list
  - How many AREN'T in HubSpot at all

This is the diagnostic for "is the sync script doing its job" — when you
expect everyone in source set X to be in HubSpot list Y, this tells you
exactly who's missing and where the gap is.

Usage:
    .venv/bin/python scripts/audit_list_membership.py 10273 path/to/emails.csv

Email file format: plain text with one email per line, OR a CSV where
the email is in the first column (header row is auto-stripped).

Required env vars:
    HUBSPOT_API_KEY  - same Service Key the rest of email-mark uses

Required HubSpot scopes:
    crm.objects.contacts.read
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Set

# Make email_mark importable when running from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import requests
from dotenv import find_dotenv, load_dotenv

from email_mark.hubspot_marketing import HUBSPOT_BASE, _headers  # noqa: E402

load_dotenv(find_dotenv())

BATCH_SIZE = 100  # HubSpot's `IN` filter accepts up to 100 values per filter


def parse_emails_file(path: Path) -> List[str]:
    """Read emails from a file. Tolerates CSV header rows + multi-column.

    Scans every comma-separated field in every line and keeps the ones
    that look like email addresses (have an @ sign with text on both
    sides). This means the email can be in any column — col 1, col 2,
    col 5 — and a header row gets auto-stripped because "Customer email"
    has no @.
    """
    raw_lines = path.read_text().splitlines()
    emails: Set[str] = set()
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        for cell in line.split(","):
            cell = cell.strip().strip('"').lower()
            if "@" in cell and "." in cell.split("@", 1)[-1]:
                emails.add(cell)
                break  # one email per row is the expected shape
    return sorted(emails)


def search_contacts(
    emails: List[str],
    list_id: str = None,
) -> Set[str]:
    """Search HubSpot for contacts whose email is IN `emails`.

    If list_id is provided, restrict to contacts who are members of
    that list (via the ilsListMemberships.listId filter — the legacy
    URL id, since that's what works on this account per the work
    we did building count_list_intersection).

    Returns the set of canonical emails found, normalized to lowercase.
    """
    filters = [
        {
            "propertyName": "email",
            "operator": "IN",
            "values": emails,
        }
    ]
    if list_id:
        filters.append({
            "propertyName": "ilsListMemberships.listId",
            "operator": "EQ",
            "value": str(list_id),
        })

    found: Set[str] = set()
    after: str = None
    while True:
        payload = {
            "filterGroups": [{"filters": filters}],
            "properties": ["email"],
            "limit": 100,
        }
        if after:
            payload["after"] = after

        response = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
            headers=_headers(),
            json=payload,
            timeout=60,
        )
        if response.status_code != 200:
            print(
                f"ERROR: HubSpot search returned HTTP {response.status_code}: "
                f"{response.text[:500]}",
                file=sys.stderr,
            )
            sys.exit(2)

        body = response.json()
        for record in body.get("results", []) or []:
            email = (record.get("properties") or {}).get("email")
            if email:
                found.add(email.strip().lower())

        next_page = (body.get("paging") or {}).get("next") or {}
        after = next_page.get("after")
        if not after:
            break

    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "list_id",
        help="HubSpot list id to audit against (the one from the URL: "
        "/objectLists/<this_id>/).",
    )
    parser.add_argument(
        "emails_file",
        help="Path to a file containing emails (one per line, or CSV with "
        "emails in the first column).",
    )
    parser.add_argument(
        "--show-found",
        action="store_true",
        help="Also list the emails that WERE found in the list "
        "(default: only show gaps).",
    )
    args = parser.parse_args()

    src_path = Path(args.emails_file)
    if not src_path.exists():
        print(f"ERROR: file not found: {src_path}", file=sys.stderr)
        return 2

    source_emails = parse_emails_file(src_path)
    if not source_emails:
        print(
            f"ERROR: no emails parsed from {src_path} "
            "(expected one per line or first CSV column).",
            file=sys.stderr,
        )
        return 2

    print(
        f"Checking {len(source_emails)} unique source emails against "
        f"HubSpot list {args.list_id}...\n"
    )

    in_list: Set[str] = set()
    in_hubspot: Set[str] = set()

    for start in range(0, len(source_emails), BATCH_SIZE):
        chunk = source_emails[start:start + BATCH_SIZE]
        # Two searches per chunk: one constrained to the list, one not.
        # Letting HubSpot do the intersection server-side is cheaper than
        # pulling every list member back and intersecting on our side.
        in_list.update(search_contacts(chunk, list_id=args.list_id))
        in_hubspot.update(search_contacts(chunk))

    source_set = set(source_emails)
    not_in_list = sorted(in_hubspot - in_list)
    not_in_hubspot = sorted(source_set - in_hubspot)
    in_list_only = sorted(in_list)

    print(f"=== Results ===")
    print(f"In source set:                  {len(source_emails)}")
    print(f"In HubSpot AND in list {args.list_id}: {len(in_list)}")
    print(f"In HubSpot but NOT in list:     {len(not_in_list)}")
    print(f"NOT in HubSpot at all:          {len(not_in_hubspot)}")

    if args.show_found and in_list_only:
        print(f"\n--- {len(in_list_only)} emails in list {args.list_id} ---")
        for e in in_list_only:
            print(f"  ✓ {e}")

    if not_in_list:
        print(
            f"\n--- {len(not_in_list)} emails in HubSpot but NOT in "
            f"list {args.list_id} ---"
        )
        print(
            "(These exist as HubSpot contacts but don't currently match "
            "the list's filter criteria. Most likely cause: "
            "proofgrade_marketing_opt_in or hs_marketable_status isn't set "
            "on them yet — check whether they're in the rolling 30-day "
            "window the sync script queries.)"
        )
        for e in not_in_list:
            print(f"  ✗ {e}")

    if not_in_hubspot:
        print(
            f"\n--- {len(not_in_hubspot)} emails NOT in HubSpot at all ---"
        )
        print(
            "(These don't exist as HubSpot contacts. Either the "
            "Shopify→HubSpot customer sync hasn't created them yet, OR "
            "they exist under a different primary email address.)"
        )
        for e in not_in_hubspot:
            print(f"  ✗ {e}")

    # Exit 0 if everyone's in the list; 1 if there are gaps to investigate.
    return 0 if (not not_in_list and not not_in_hubspot) else 1


if __name__ == "__main__":
    sys.exit(main())
