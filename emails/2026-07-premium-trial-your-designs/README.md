# Your designs (personalized Day 3 replacement)

Source for the three static variants in `../2026-07-premium-trial-your-designs-{1,2,3}/`:

- `template.html` — Jinja source; render with `python scripts/gen_your_designs_variants.py`
- `design_catalog.json` — slug -> title/image/url map; extended automatically by
  `scripts/sync_design_prefs.py` when new slugs appear

Why three variants + precomputed properties instead of one dynamic email:
recipient contact properties substitute AFTER template evaluation in HubSpot
emails (raw-HTML widgets AND coded modules) — logic can print them but never
parse or branch on them. `sync_design_prefs.py` flattens gf_design_preferences
into gf_design_pref_count/_1.._3_{name,url,img}; the workflow branches on the
count. See prompts/lessons_learned.md (2026-07-10).
