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


def _parse_csv(value: str) -> set:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


# Optional access control. Empty = allow all (open mode).
ALLOWED_USERS = _parse_csv(os.environ.get("SLACK_ALLOWED_USERS", ""))
ALLOWED_CHANNELS = _parse_csv(os.environ.get("SLACK_ALLOWED_CHANNELS", ""))

DENIAL_MESSAGE = (
    "I'm gated to a specific list of users right now and you're not on it yet. "
    "Reach out to Sam (sam.kemmis@glowforge.com) if you should have access."
)


def _user_allowed(user_id: str) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def _channel_allowed(channel_id: str) -> bool:
    return not ALLOWED_CHANNELS or channel_id in ALLOWED_CHANNELS


@app.event("app_mention")
def handle_mention(event, say):
    text = _MENTION_RE.sub("", event.get("text", "")).strip()
    if not text:
        say("Hi! Tag me with a question or request and I'll do my best.")
        return

    # Access control — channel scope first, then user scope.
    if not _channel_allowed(event.get("channel", "")):
        # Silent — don't acknowledge in unallowed channels.
        logging.info("Ignored mention in unallowed channel %s", event.get("channel"))
        return
    if not _user_allowed(event.get("user", "")):
        say(text=DENIAL_MESSAGE, thread_ts=event.get("thread_ts") or event.get("ts"))
        return

    # Conversation scoping: always keyed by the thread root timestamp.
    #
    # For a fresh top-level mention, the user's message ts BECOMES the
    # thread root when Mark anchors his reply with thread_ts=event['ts'].
    # For a reply already inside a thread, event['thread_ts'] is the root.
    # In both cases, every message in that thread (now and later) will
    # resolve to the same conversation_id, so Mark sees the full history
    # of the conversation he's already part of.
    #
    # The previous design used channel-user:{channel}:{user} for fresh
    # mentions, which created a different key from thread:{ts} once Mark
    # replied — so context was lost the moment the user replied inside
    # the thread Mark just created.
    thread_root_ts = event.get("thread_ts") or event.get("ts")
    reply_thread_ts = thread_root_ts
    conversation_id = f"thread:{thread_root_ts}"

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

    # Access control — channel allowlist doesn't apply to DMs (DMs are 1:1),
    # but user allowlist still does.
    if not _user_allowed(event.get("user", "")):
        say(DENIAL_MESSAGE)
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
