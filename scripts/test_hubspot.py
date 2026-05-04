"""Sanity-check the HubSpot connection.

Run from the project root:
    .venv/bin/python scripts/test_hubspot.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from email_mark.hubspot_client import list_contacts  # noqa: E402


def main() -> None:
    print("Fetching first 5 contacts from HubSpot...\n")
    contacts = list_contacts(limit=5)
    if not contacts:
        print(
            "No contacts returned. Token works but the contacts list is "
            "empty (or the contacts.read scope is missing)."
        )
        return
    for c in contacts:
        props = c.get("properties", {})
        first = props.get("firstname") or "(no first)"
        last = props.get("lastname") or "(no last)"
        email = props.get("email") or "(no email)"
        print(f"- {first} {last} <{email}>")


if __name__ == "__main__":
    main()
