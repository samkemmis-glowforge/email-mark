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

- HubSpot's `POST /crm/v3/objects/contacts/batch/update` SILENTLY
  IGNORES writes to `hs_marketable_status`. The endpoint returns 200 OK,
  the response often doesn't echo the field, and the property simply
  doesn't change — no error, no warning, nothing in the contact's
  property history. Casey's marketable status being "true" after a sync
  run is NOT evidence the sync set it; she was probably already
  marketing. The only reliable way to flip non-marketing to marketing
  programmatically is a HubSpot Workflow with the dedicated "Set
  marketing contact status" action. Our sync_materials_to_proofgrade
  pattern is: script sets a custom property (e.g.
  `proofgrade_marketing_opt_in = true`), a Workflow triggers on that
  change and flips `hs_marketable_status` via the action. Don't try to
  set `hs_marketable_status` directly from the API on this account.
  (Learned 2026-06-11)

## Shopify — opt-in semantics

- Shopify has TWO distinct marketing opt-in signals on its data:
  order-level and customer-level. `orders.buyer_accepts_marketing` is
  a SNAPSHOT of what the customer ticked at checkout for that one
  order, and never changes after the order is placed. The customer's
  CURRENT subscription status lives at
  `customers.email_marketing_consent.state` (values: `'subscribed'`,
  `'not_subscribed'`, `'unsubscribed'`, `'pending'`). The older
  `customers.accepts_marketing` boolean is deprecated and returns
  NULL on Glowforge's current schema. For any "is this person opted
  in to marketing right now" question, use
  `customers.email_marketing_consent.state = 'subscribed'`. Using the
  order-level flag misses people who subscribed via channels other
  than checkout (newsletter signup, account preferences) AND can
  include people who have since unsubscribed. The
  sync_materials_to_proofgrade.py script's BQ query uses the
  customer-level state for this reason.
  (Learned 2026-06-11)

## HubSpot — marketing emails

- An email's type (`BATCH_EMAIL` vs `AUTOMATED_EMAIL`, aka "save for
  automation" / usable in workflows) is fixed at creation. PATCH-ing
  `type`/`subcategory` on an existing email returns 200 OK but silently
  changes nothing, and the v3 create endpoint silently ignores a
  `type` field too. The only API route to an automated email is to
  CLONE an existing `AUTOMATED_EMAIL` (clones inherit type) and then
  overwrite its name/subject/content. To finish "saving for
  automation," the email must also be published (state
  `AUTOMATED_DRAFT` → live for workflows) — that's a deliberate
  go-live action, not part of drafting.
  (Learned 2026-07-08)

## HubSpot — billing levers

- Marketing contact count is the dominant cost driver on Glowforge's
  HubSpot invoice (Feb 2026 bill: ~$95K with ~$50-60K of that being
  additional marketing contacts at ~1.58M over the 10K Enterprise
  base). When the team is looking to reduce HubSpot spend, the highest-
  leverage move is auditing dormant marketing contacts (no opens in
  12-24 months, no active subscription) and bulk-flipping them to
  non-marketing.
  (Learned 2026-05-11)
