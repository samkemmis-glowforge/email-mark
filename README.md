# email-mark

A Slack-resident AI coworker for the Glowforge marketing team. People @-mention or DM the bot to ask questions, draft emails, explore data, and (eventually) push lists and content to HubSpot.

The longer-term vision lives in [docs/concept.md](docs/concept.md). The README describes what's actually built.

## Status

Bot scaffolding in place. Claude content connector working. Slack app created; install pending admin approval. No tools wired in yet — once installed, the bot can hold conversations but can't read or write to HubSpot/warehouse.

## How it works

1. Someone messages the bot in Slack (mention or DM).
2. The bot's running process receives the event via Slack's Socket Mode.
3. The message is sent to Claude with a marketing-team system prompt.
4. Claude's reply is posted back to Slack.

Tools (HubSpot, BigQuery, etc.) get added over time so the bot can do real work, not just write content.

## Setup

1. Copy `.env.example` to `.env` and fill in the keys you have. `.env` is gitignored.
2. Create a Python virtual environment and install dependencies:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Run the bot:
   ```
   .venv/bin/python scripts/run_bot.py
   ```
   It'll connect to Slack via Socket Mode and stay running. Stop with Ctrl+C.

## Project structure

- `src/email_mark/` — Python package (Claude client and tool connectors)
- `scripts/` — entrypoints (the bot itself, plus connector smoke tests)
- `prompts/` — playbooks injected into Mark's system prompt (brand voice, social, **ads**, lessons learned)
- `docs/` — design notes (see [docs/ads.md](docs/ads.md) for the paid-ads project)
