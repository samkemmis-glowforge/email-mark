"""Render the 'your designs' email template into its 1/2/3-pick variants.

Recipient contact properties substitute AFTER template evaluation in
HubSpot emails, so one email can't vary its card count by logic — the
workflow branches on gf_design_pref_count to one of three static
variants instead. This script renders template.html (Jinja) into
emails/2026-07-premium-trial-your-designs-{1,2,3}/body.html and seeds
meta.json for new variants (existing meta.json, and its email_id, is
preserved).

After running: python scripts/push_email_draft.py <each variant dir>
"""

import json
import pathlib
import sys

import jinja2

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "emails/2026-07-premium-trial-your-designs"
CLONE_FROM = "216751522376"  # Day 0 automated email (correct sender)


def main() -> int:
    template = jinja2.Environment().from_string((SRC_DIR / "template.html").read_text())
    for n in (1, 2, 3):
        out_dir = ROOT / f"emails/2026-07-premium-trial-your-designs-{n}"
        out_dir.mkdir(exist_ok=True)
        body = template.render(n=n)
        assert "{%" not in body, "unrendered Jinja left in body"
        assert body.count("gf_design_pref_") >= n * 3
        (out_dir / "body.html").write_text(body)

        meta_path = out_dir / "meta.json"
        if not meta_path.exists():
            plural = n != 1
            meta = {
                "name": f"Premium Trial — Your picked designs ({n} design{'s' if plural else ''}, automated)",
                "subject": ("The designs you picked are free with your trial" if plural
                            else "The design you picked is free with your trial"),
                "preheader": ("Remember the designs that caught your eye? They're unlocked."
                              if plural else "Remember the design that caught your eye? It's unlocked."),
                "clone_from_email_id": CLONE_FROM,
                "note": (f"Variant for gf_design_pref_count = {n}. Generated from "
                         "../2026-07-premium-trial-your-designs/template.html by "
                         "scripts/gen_your_designs_variants.py — edit the template, "
                         "not this body.html. Requires scripts/sync_design_prefs.py "
                         "to have populated the derived properties."),
            }
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        print(f"variant {n}: {out_dir.name}/body.html ({len(body)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
