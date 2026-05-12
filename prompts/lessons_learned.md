# Lessons learned

Durable domain knowledge captured from real conversations. New entries get
loaded into Mark's system prompt at startup, so a deploy is required after
adding one. Format each lesson as a bullet under a topical heading, in 2-4
sentences of plain language, ending with `(Learned YYYY-MM-DD)`.

Keep entries SHORT and SPECIFIC. The goal is to give Mark just enough
context to avoid the trap, not to teach the full topic.

---

## BigQuery / Data warehouse

- `glowforge-data-production.hubspot.email_events` only contains records
  from Dec 27, 2024 onward. Using it to compute "never sent an email"
  or other historical email engagement counts produces false negatives
  for any contact mailed before that date. For email engagement going
  back further than late-2024, query HubSpot's API directly
  (get_contact_email_events, get_email_engagement_contacts) — treat
  HubSpot as the source of truth for email engagement, and BigQuery as
  best-effort/incomplete for pre-2025 events.
  (Learned 2026-05-11)

## HubSpot — contacts and marketing status

- "Marketing contact status" on a contact (`hs_marketable_status`) gates
  whether HubSpot will send any marketing email to them. Non-marketing
  contacts are silently skipped by workflows. Trial signups at Glowforge
  currently land as Non-marketing contacts by default — meaning a
  HubSpot-based activation drip would NOT reach them unless the
  integration is changed or a workflow flips their status. When sizing
  marketing audiences, always confirm whether the segment is marketing
  contacts or not.
  (Learned 2026-05-11)

## HubSpot — billing levers

- Marketing contact count is the dominant cost driver on Glowforge's
  HubSpot invoice (Feb 2026 bill: ~$95K with ~$50-60K of that being
  additional marketing contacts at ~1.58M over the 10K Enterprise
  base). When the team is looking to reduce HubSpot spend, the highest-
  leverage move is auditing dormant marketing contacts (no opens in
  12-24 months, no active subscription) and bulk-flipping them to
  non-marketing.
  (Learned 2026-05-11)
