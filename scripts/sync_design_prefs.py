"""Expand gf_design_preferences into flat, token-friendly contact properties.

Why this exists: HubSpot marketing emails substitute recipient contact
properties AFTER HubL evaluation — in raw-HTML widgets AND in coded
(programmable) modules on this portal. Template logic can print
contact.x but can never parse or branch on it (it sees the literal
token string). So personalization that depends on the VALUE must be
precomputed onto the contact, and the email/workflow use only plain
tokens and property-based branches. (See prompts/lessons_learned.md
2026-07-10.)

For every contact with gf_design_preferences set, this script parses the
JSON array of shop slugs and writes:
  - gf_design_pref_count           (number of valid picks, 0-3; extras ignored)
  - gf_design_pref_{1,2,3}_name    (display name)
  - gf_design_pref_{1,2,3}_url     (https://shop.glowforge.com/products/<slug>)
  - gf_design_pref_{1,2,3}_img     (product image URL, or "" if unknown)
Unused slots are written as "" so re-syncs clear stale values.

Slug -> name/image comes from emails/2026-07-premium-trial-your-designs/
design_catalog.json. Unknown slugs get their og:title/og:image fetched
live from the shop page and are appended to the catalog file (best
effort); if the fetch fails, the name falls back to the title-cased slug
and the image is left blank.

Usage:
    python scripts/sync_design_prefs.py            # sync all
    python scripts/sync_design_prefs.py --dry-run  # print, write nothing
"""

import html
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests

from email_mark.hubspot_marketing import HUBSPOT_BASE, _headers

CATALOG_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "emails/2026-07-premium-trial-your-designs/design_catalog.json"
)
MAX_PICKS = 3


def fetch_og(slug: str):
    url = f"https://shop.glowforge.com/products/{slug}"
    try:
        page = subprocess.run(
            ["curl", "-sL", "--max-time", "15", url],
            capture_output=True, text=True, timeout=25,
        ).stdout
        title = re.search(r'property="og:title"[^>]*content="([^"]*)"', page)
        image = re.search(r'property="og:image"[^>]*content="([^"]*)"', page)
        if not title:
            return None
        img = image.group(1) if image else ""
        if img.startswith("//"):
            img = "https:" + img
        img = img.replace("http://", "https://")
        return {"url": url, "title": title.group(1), "image": img}
    except Exception:
        return None


def resolve(slug: str, catalog: dict, catalog_dirty: list):
    info = catalog.get(slug)
    if not info:
        info = fetch_og(slug)
        if info:
            catalog[slug] = info
            catalog_dirty.append(slug)
    name = html.unescape(info["title"]) if info else slug.replace("-", " ").title()
    img = (info.get("image") or "") if info else ""
    return {
        "name": name,
        "url": f"https://shop.glowforge.com/products/{slug}",
        "img": img,
    }


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    catalog = json.loads(CATALOG_PATH.read_text())
    catalog_dirty: list = []

    # pull every contact with the source property
    contacts, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [{
                "propertyName": "gf_design_preferences", "operator": "HAS_PROPERTY"}]}],
            "properties": ["gf_design_preferences", "gf_design_pref_count"],
            "limit": 200,
        }
        if after:
            body["after"] = after
        r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
                          headers=_headers(), json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        contacts.extend(data.get("results", []))
        after = (data.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break

    updates = []
    for c in contacts:
        raw = (c.get("properties") or {}).get("gf_design_preferences") or ""
        try:
            slugs = [str(s).strip() for s in json.loads(raw) if str(s).strip()]
        except Exception:
            slugs = []
        slugs = slugs[:MAX_PICKS]
        props = {"gf_design_pref_count": str(len(slugs))}
        for i in range(MAX_PICKS):
            if i < len(slugs):
                d = resolve(slugs[i], catalog, catalog_dirty)
            else:
                d = {"name": "", "url": "", "img": ""}
            for k, v in d.items():
                props[f"gf_design_pref_{i + 1}_{k}"] = v
        updates.append({"id": c["id"], "properties": props})

    print(f"{len(contacts)} contacts to sync"
          + (f", {len(catalog_dirty)} new slugs fetched" if catalog_dirty else ""))
    if dry_run:
        print(json.dumps(updates[:3], indent=2))
        return 0

    for i in range(0, len(updates), 100):
        batch = updates[i:i + 100]
        r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/batch/update",
                          headers=_headers(), json={"inputs": batch}, timeout=60)
        if r.status_code != 200:
            print(f"batch {i // 100} FAILED: {r.status_code} {r.text[:300]}")
            return 1
        print(f"batch {i // 100}: updated {len(batch)}")

    if catalog_dirty:
        CATALOG_PATH.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
        print(f"catalog extended with: {catalog_dirty} — commit design_catalog.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
