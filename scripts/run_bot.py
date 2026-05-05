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

from email_mark.agent import chat, reset_conversation  # noqa: E402

load_dotenv(find_dotenv())
logging.basicConfig(level=logging.INFO)

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)


_MENTION_RE = re.compile(r"<@[^>]+>\s*")
_RESET_KEYWORDS = {"/reset", "reset conversation", "start over", "new conversation"}


def _is_reset(text: str) -> bool:
    t = text.strip().lower()
    return t in _RESET_KEYWORDS


@app.event("app_mention")
def handle_mention(event, say):
    text = _MENTION_RE.sub("", event.get("text", "")).strip()
    if not text:
        say("Hi! Tag me with a question or request and I'll do my best.")
        return

    # Conversation scoping rules:
    #   - If user is replying inside an existing thread → scope to that thread
    #     (so two separate threaded discussions don't cross-contaminate).
    #   - If user is making a fresh top-level mention → scope to the user
    #     within the channel (so their follow-up mentions remember context
    #     even when they don't reply in-thread).
    is_thread_reply = (
        event.get("thread_ts") is not None
        and event["thread_ts"] != event.get("ts")
    )
    if is_thread_reply:
        reply_thread_ts = event["thread_ts"]
        conversation_id = f"thread:{event['thread_ts']}"
    else:
        # Anchor the bot's reply as a thread off this fresh mention,
        # so the channel stays tidy.
        reply_thread_ts = event.get("ts")
        conversation_id = f"channel-user:{event['channel']}:{event['user']}"

    if _is_reset(text):
        reset_conversation(conversation_id)
        say(text="Okay, conversation cleared. Fresh start.", thread_ts=reply_thread_ts)
        return

    reply = chat(text, conversation_id=conversation_id)
    say(text=reply, thread_ts=reply_thread_ts)


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

    # Each DM channel is one ongoing conversation.
    conversation_id = f"dm:{event['channel']}"

    if _is_reset(text):
        reset_conversation(conversation_id)
        say("Okay, conversation cleared. Fresh start.")
        return

    say(chat(text, conversation_id=conversation_id))


def main() -> None:
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Bot starting... ctrl+C to stop.")
    handler.start()


if __name__ == "__main__":
    main()
