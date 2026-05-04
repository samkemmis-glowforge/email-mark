# email-mark

Lifecycle Marketing Engine — a custom layer between Glowforge's BigQuery warehouse and HubSpot. It defines audiences from warehouse signals, generates personalized email content with the Claude API, and pushes audience + content into HubSpot for sending, A/B testing, and measurement.

See [docs/concept.md](docs/concept.md) for the full project concept, architecture, and roadmap.

## Status

Early scaffolding. Concept doc committed; code not yet written.

## Initial programs

1. **Activation sequence** — triggered on first print, lifts activation rate.
2. **Premium conversion** — targets active non-subscribers, lifts paid conversion.
3. **Save campaign** — targets churn-signal subscribers, reduces churn.
