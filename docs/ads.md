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
- **Execute (draft-only)** — produce launch-ready ad copy, audience
  definitions, budget/schedule recommendations, and creative briefs. Mark
  hands these off in Slack for a human to build. **ads-mark never launches,
  edits, pauses, or spends on a live ad account** — same safety posture as
  social-mark's draft-only gate.
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

- Draft-only. No campaign launches, edits, pauses, or budget changes from the
  bot. Drafts go to a human to build and launch.
- Paid copy still follows `brand_voice.md`; claims must be substantiated before
  they ship.

## Future extensions

- Native read connectors for Google/TikTok/etc. in-repo (beyond Supermetrics).
- A gated write path for campaign creation, mirroring the
  `SOCIAL_MARK_ALLOW_PUBLISH` pattern, only after an approval flow is signed
  off.
- Audience builds that reverse-sync warehouse segments into Meta Custom
  Audiences via HubSpot.
- Weekly scheduled spend report into the Slack review channel.
