# Ads project (ads-mark) — Concept

## What it is

A paid-advertising hat for Mark, built inside the email-mark framework. It
gives the marketing team an AI coworker that helps **ideate**, **execute
(draft-only)**, and **report** on paid campaigns — reusing the same
permissions, env, and tool wiring as the rest of the bot (Meta and HubSpot in
particular).

It mirrors how `social-mark` works: a playbook in `prompts/ads_playbook.md`
gets injected into Mark's system prompt at startup (via
`_ads_playbook_section()` in `src/email_mark/agent.py`). No new bot process,
no new auth — it's a persona layered onto the existing one.

## The three jobs

- **Ideate** — turn a goal into a short, actionable brief: objective,
  audience, creative concept(s), offer/CTA, budget/flight, and the one metric
  that defines success. Grounded in recent performance where possible.
- **Execute** — two modes. Default is **draft-only**: launch-ready ad copy,
  audience definitions, budget/schedule recommendations, and creative briefs,
  handed off in Slack for a human to build. With the `ADS_MARK_ALLOW_WRITE`
  gate on, Mark can additionally **build test campaigns on Meta directly**
  (campaign → ad set/targeting → creative → ad), with hard guardrails baked
  into the tools — see Safety below.
- **Report** — turn spend into a tight read: what we spent, what it returned
  against the primary metric, what's working, and the next change worth making.

## Platforms and data wiring

| Platform | Reporting path | Status |
|---|---|---|
| Meta (FB/IG) | `meta_client.get_ad_performance` (`META_AD_ACCOUNT_ID`) | Native, live |
| Google Ads | Supermetrics connector | Via Supermetrics |
| TikTok | Supermetrics connector | Via Supermetrics |
| LinkedIn | Supermetrics connector | Via Supermetrics |
| Pinterest | Supermetrics connector | Via Supermetrics |
| Reddit | Supermetrics connector | Via Supermetrics |

Meta is wired directly into the repo through the existing
`get_ad_performance` tool against `META_AD_ACCOUNT_ID`. The other platforms are
covered through the **Supermetrics** connector for cross-platform reporting.
Downstream conversion (clicks → contacts → deals → revenue) comes from the
existing HubSpot and warehouse tooling, subject to attribution limits.

## Safety

- Draft-only by default. No campaign launches, edits, pauses, or budget
  changes from the bot unless `ADS_MARK_ALLOW_WRITE=true`.
- The gated write path (Meta only, `create_meta_*` tools in
  `src/email_mark/meta_client.py`) enforces its guardrails in code, not
  prompt text:
  - Everything is created **PAUSED**; activation
    (`update_meta_object_status`) is a separate call the playbook reserves
    for explicit human approval.
  - **Name-tag fence**: Mark's objects get an `ADS_MARK_NAME_PREFIX` tag
    (default `[mark]`) prepended, and every mutation — creating children,
    status changes, budget changes — first checks the target's name and
    refuses without the tag. Human-built campaigns are read-only to Mark.
  - **Budget caps**: `ADS_MARK_MAX_DAILY_BUDGET_CENTS` (default $50/day) and
    `ADS_MARK_MAX_LIFETIME_BUDGET_CENTS` (default 30× daily) are enforced on
    every create and budget update, so nothing fenced can go ACTIVE with an
    unchecked budget.
- Writes need a token with the `ads_management` scope — `META_ADS_ACCESS_TOKEN`
  (falls back to `META_ACCESS_TOKEN`).
- Paid copy still follows `brand_voice.md`; claims must be substantiated before
  they ship.

## Future extensions

- Native read connectors for Google/TikTok/etc. in-repo (beyond Supermetrics).
- Write paths for platforms beyond Meta.
- Audience builds that reverse-sync warehouse segments into Meta Custom
  Audiences via HubSpot.
- Weekly scheduled spend report into the Slack review channel.
