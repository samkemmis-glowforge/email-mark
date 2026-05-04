# email-mark

Lifecycle Marketing Engine — a custom layer between Glowforge's BigQuery warehouse and HubSpot. It defines audiences from warehouse signals, generates personalized email content with the Claude API, and pushes audience + content into HubSpot for sending, A/B testing, and measurement.

See [docs/concept.md](docs/concept.md) for the full project concept, architecture, and roadmap.

## Status

Early scaffolding. Concept committed; project structure in place; code not yet written.

## Project structure

- `src/email_mark/` — Python package (BigQuery, Claude, and HubSpot clients live here)
- `queries/` — SQL files for warehouse audience queries
- `prompts/` — Claude prompt templates for content generation
- `scripts/` — Entrypoint scripts run by the scheduler
- `docs/` — Design notes and concept documents

## Setup

1. Copy `.env.example` to `.env` and fill in API keys. `.env` is gitignored — secrets stay local.
2. Create a Python virtual environment and install dependencies:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

## Initial programs

1. **Activation sequence** — triggered on first print, lifts activation rate.
2. **Premium conversion** — targets active non-subscribers, lifts paid conversion.
3. **Save campaign** — targets churn-signal subscribers, reduces churn.
