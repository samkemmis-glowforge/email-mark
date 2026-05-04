"""Slack bot — AI assistant for the Glowforge marketing team.

Runs in Socket Mode, so no public web server is required. Set these in .env:

    ANTHROPIC_API_KEY        (you already have this)
    SLACK_BOT_TOKEN          (starts xoxb-, available after Slack install)
    SLACK_SIGNING_SECRET     (Slack app -> Basic Information page)
    SLACK_APP_TOKEN          (starts xapp-, from Socket Mode setup)

Then run from the project root:

    .venv/bin/python scripts/run_bot.py
"""

import logging
import os
import re
import sys
from pathlib import Path

# Make src/ importable without having to install the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import find_dotenv, load_dotenv  # noqa: E402
from slack_bolt import App  # noqa: E402
from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402

from email_mark.content import generate  # noqa: E402

load_dotenv(find_dotenv())
logging.basicConfig(level=logging.INFO)

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)


SYSTEM_PROMPT = (
    "You are an AI assistant for the Glowforge marketing team, available "
    "in Slack. You help with lifecycle marketing tasks: drafting emails, "
    "proposing audiences, exploring data, and answering questions about "
    "marketing strategy. Keep responses friendly and concise; use Slack-style "
    "formatting (no Markdown headers, light use of *bold*). You are early in "
    "development — you do not yet have direct access to HubSpot or the "
    "data warehouse, but you can help draft content and reason about plans."
)


_MENTION_RE = re.compile(r"<@[^>]+>\s*")


def _ask_claude(user_message: str) -> str:
    prompt = f"{SYSTEM_PROMPT}\n\nUser: {user_message}\n\nAssistant:"
    return generate(prompt)


@app.event("app_mention")
def handle_mention(event, say):
    text = _MENTION_RE.sub("", event.get("text", "")).strip()
    if not text:
        say("Hi! Tag me with a question or request and I'll do my best.")
        return
    say(_ask_claude(text))


@app.event("message")
def handle_dm(event, say):
    # Only respond in DMs, not in channels, and not to other bots.
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id"):
        return
    text = (event.get("text") or "").strip()
    if not text:
        return
    say(_ask_claude(text))


def main() -> None:
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Bot starting... ctrl+C to stop.")
    handler.start()


if __name__ == "__main__":
    main()
