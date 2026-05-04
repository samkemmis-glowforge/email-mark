# email-mark

A general email marketing engine for Glowforge. It connects three tools — BigQuery (warehouse audiences), the Claude API (content generation), and HubSpot (sending) — and lets you define and run programs that combine them.

The longer-term vision lives in [docs/concept.md](docs/concept.md). It's aspirational; the README describes what's actually built.

## Status

Scaffolding only. Folders and config in place; no code yet.

## Architecture

Three tool connectors form the foundation:

- **BigQuery client** — run audience queries, get back rows of users + signals.
- **Claude client** — fill a prompt template with row data, get back generated content.
- **HubSpot client** — push lists and content into HubSpot for sending.

Programs (whatever they end up being — activation, conversion, save, etc.) are thin glue on top of these three.

## Project structure

- `src/email_mark/` — Python package (the connectors live here)
- `queries/` — SQL files for warehouse queries
- `prompts/` — Claude prompt templates
- `scripts/` — entrypoint scripts (run from CLI or scheduler)
- `docs/` — design notes

## Setup

1. Copy `.env.example` to `.env` and fill in API keys. `.env` is gitignored — secrets stay local.
2. Create a Python virtual environment and install dependencies:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
