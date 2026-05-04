# Lifecycle Marketing Engine — Concept

## What it is

A custom marketing automation layer that sits between Glowforge's data warehouse and HubSpot. It reads behavioral signals from the warehouse — printing activity, subscription state, error rates, forum engagement, NPS, web events — uses them to define audiences for lifecycle programs, generates personalized email content with AI, and pushes audience plus content into HubSpot for sending and A/B testing. Results flow back into the warehouse for measurement.

The goal is to operationalize the rich data already in the warehouse for marketing purposes — turning behavioral signals into triggered, personalized communications without rebuilding any infrastructure that already exists.

## Why a custom layer rather than HubSpot workflows alone

HubSpot workflows can handle simple triggers based on contact properties, but they can't easily express audience definitions that join warehouse data — e.g., "Glowforge owners who completed their first print 7 days ago AND don't have a Premium subscription AND have logged a support ticket in the last 14 days." That's a warehouse query, and the warehouse should remain the audience-definition layer rather than recreating logic inside HubSpot. Reverse-syncing audiences from the warehouse to HubSpot keeps the warehouse as the single source of truth and lets richer behavioral signals drive marketing decisions.

## Architecture (loose)

- **Warehouse (BigQuery)** — source of truth for user identity (`stg_mapping__users`), product behavior (`active_users`, machine telemetry), subscriptions (`subs_historic`), web/marketing events (GA4, Wistia), and CRM context.
- **Orchestration (Claude Code-driven scripts on a schedule)** — query the warehouse, identify users matching audience definitions, generate or fetch content, push to HubSpot.
- **Content generation (Claude API)** — produces subject lines, body copy, and personalization variants based on user behavior signals.
- **Activation (HubSpot API)** — receives audience lists and content, sends campaigns, runs A/B tests on subject lines and body content.
- **Measurement (BigQuery + Metabase)** — opens, clicks, conversions, and downstream subscription effects all observable in the warehouse and joinable to user identity.
- **Experimentation (LaunchDarkly, where applicable)** — for product-side experiments tied to lifecycle programs (e.g., onboarding flow variants for newly-activated users).

## Initial programs in priority order

1. **Activation sequence.** Triggered when a user completes their first print. Personalized based on what they made and how recently. Goal: lift activation rate, defined as some second-print or community-engagement milestone within a target window.
2. **Premium conversion campaign.** Targets active machine users who are not paying Premium subscribers. Uses behavioral signals (print frequency, file types, project complexity) to personalize the value proposition. Goal: lift conversion rate from active non-subscriber to paid.
3. **Save campaign.** Targets paying Premium subscribers showing churn signals — no prints in 30 days, low NPS, or elevated error rates. Goal: reduce churn through targeted re-engagement and, where appropriate, support outreach.

Each program runs on a schedule (daily or weekly), is A/B tested through HubSpot's native testing, and reports back into a Metabase dashboard for ongoing monitoring.

## Future extensions

- AI-generated landing pages personalized to acquisition source and machine-ownership status.
- SEO content engine producing use-case pages from product behavior and forum content at scale.
- Cross-channel orchestration extending the same audience definitions to retargeting ads and in-app messaging via LaunchDarkly.
- Holdout-based incrementality measurement for each lifecycle program.

## What's needed to build it

- Direct or scripted access to BigQuery for audience queries (currently bottlenecked behind tooling/access friction).
- HubSpot API access for list management, content publishing, and campaign sends.
- Claude API access for content generation.
- A scheduling system (cron, GitHub Actions, Cloud Scheduler, or similar).
- A repo for code with secrets management for API tokens.

## Open questions and current bottlenecks

- The internal text-to-SQL agent has a narrow per-query retrieval context that blocks cross-schema joins. Direct warehouse access (via service account, notebook, or Claude Code-driven scripts) is the more durable path and needs to be set up with the data team.
- HubSpot ingestion via Stitch is currently limited to contact-level data. Enabling the `campaigns` and `email_events` streams would meaningfully sharpen attribution and is a small ask of whoever administers Stitch.
- Segment Personas (`personas_personas.users`) appears to be in the stack but ownership and freshness are unclear — worth confirming whether it's actively maintained before building dependencies on it.
- LaunchDarkly is deployed and integrated with user identity, but it's unclear whether marketing has access or whether it's used purely for product engineering. Worth a short conversation with whoever owns it.
