# emails/

Version-controlled source for HubSpot marketing email drafts built from
Claude Code (the Code-surface counterpart to Mark's Slack drafting flow).

Each email lives in its own directory:

- `body.html` — the body HTML that goes into the blank-canvas template's
  custom-HTML widget. The template supplies the header (Glowforge logo)
  and footer (unsubscribe block); this file is only the body. Follow
  `prompts/email_design_references.md`.
- `meta.json` — `name`, `subject`, `preheader`, and (after the first
  push) `email_id`. Commit `email_id` so later sessions update the same
  HubSpot draft instead of creating duplicates.

Push a draft to HubSpot with:

```
python scripts/push_email_draft.py emails/<slug>
```

Requires `HUBSPOT_API_KEY` with the `content` scope (marketing email
read/write) — the same Private App token the Slack bot uses.
