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

from email_mark.agent import (  # noqa: E402
    chat,
    has_conversation,
    reset_conversation,
    seed_conversation,
)
from email_mark.slack_helpers import (  # noqa: E402
    get_user_display as slack_get_user_display,
)

load_dotenv(find_dotenv())
logging.basicConfig(level=logging.INFO)

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
)


_MENTION_RE = re.compile(r"<@[^>]+>\s*")
_RESET_KEYWORDS = {"/reset", "reset conversation", "start over", "new conversation"}

# Markdown -> Slack mrkdwn conversion. Mark's training makes him default to
# Markdown formatting even after prompt instructions to use Slack syntax,
# so we do a defensive pass on his output before sending.
#
# We deliberately do NOT convert single-asterisk *italic* because in Slack
# the same syntax means BOLD — guessing wrong would corrupt valid output.
# Bold (**), links, and headers are safe to convert.
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _markdown_to_slack(text):
    """Best-effort conversion of common Markdown to Slack mrkdwn syntax."""
    if not text:
        return text
    text = _MD_BOLD_RE.sub(r"*\1*", text)        # **bold** -> *bold*
    text = _MD_LINK_RE.sub(r"<\2|\1>", text)     # [text](url) -> <url|text>
    text = _MD_HEADER_RE.sub(r"*\2*", text)      # # Header   -> *Header*
    # Slack requires whitespace/punctuation around *bold* for it to render.
    # If Mark writes *Header*Text with no space, the post-converted result
    # won't render as bold. Insert a space when bold runs straight into a
    # word character, and a newline when it runs into a sentence-starter.
    text = re.sub(r"(\*[^*\s][^*\n]*?[^*\s]\*)(?=\w)", r"\1 ", text)
    return text

# Bot's own Slack user ID. Fetched lazily on first rehydration so we can
# tell our own messages apart from user messages in thread history.
_BOT_USER_ID = None


def _get_bot_user_id():
    global _BOT_USER_ID
    if _BOT_USER_ID is not None:
        return _BOT_USER_ID
    try:
        auth = app.client.auth_test()
        _BOT_USER_ID = auth.get("user_id")
    except Exception as exc:
        logging.warning("Couldn't fetch bot user_id: %s", exc)
    return _BOT_USER_ID


def _slack_msg_to_turn(msg, bot_id):
    """Convert one Slack message into a {role, content} turn, or None to skip.

    Bot messages become assistant turns. Other users' messages become user
    turns prefixed with the speaker's display name so Mark can distinguish
    between multiple participants in a conversation ("Yuliya asked X, Sam
    asked me to dig into it" wouldn't work without attribution).
    """
    text = (msg.get("text") or "").strip()
    if not text:
        return None
    is_bot = msg.get("bot_id") is not None or (
        bot_id and msg.get("user") == bot_id
    )
    if is_bot:
        return {"role": "assistant", "content": text}
    text = _MENTION_RE.sub("", text).strip()
    if not text:
        return None
    speaker_id = msg.get("user")
    speaker = slack_get_user_display(speaker_id) if speaker_id else None
    if speaker:
        text = f"[{speaker}]: {text}"
    return {"role": "user", "content": text}


def _rehydrate_thread_history(channel, thread_ts):
    """Fetch a Slack thread's messages and rebuild user/assistant turns.

    Called when in-memory conversation state is empty for a thread Mark
    was already part of — typically after a worker restart wiped the
    dict. Returns a list of {role, content} turns in chronological order.
    """
    bot_id = _get_bot_user_id()
    try:
        result = app.client.conversations_replies(
            channel=channel, ts=thread_ts, limit=200
        )
    except Exception as exc:
        logging.warning("Failed to fetch thread %s: %s", thread_ts, exc)
        return []
    turns = []
    for msg in result.get("messages", []) or []:
        turn = _slack_msg_to_turn(msg, bot_id)
        if turn is not None:
            turns.append(turn)
    return turns


def _rehydrate_channel_context(channel, before_ts, limit=10, max_minutes_back=30):
    """Pull recent channel messages preceding a top-level @-mention.

    Used when Mark is mentioned at the channel level (not inside an
    existing thread) — the user is likely asking him to weigh in on what
    was just being discussed in the channel. We grab the last few
    messages within a recency window so Mark has the surrounding context
    instead of cold-starting.

    Limited to messages within `max_minutes_back` minutes to avoid
    pulling in stale conversation from earlier in the day.
    """
    bot_id = _get_bot_user_id()
    try:
        result = app.client.conversations_history(
            channel=channel,
            latest=before_ts,
            inclusive=False,
            limit=limit,
        )
    except Exception as exc:
        logging.warning("Failed to fetch channel history for %s: %s", channel, exc)
        return []
    try:
        cutoff = float(before_ts) - (max_minutes_back * 60)
    except (TypeError, ValueError):
        cutoff = 0.0
    turns = []
    # conversations.history returns newest-first; reverse to chronological.
    for msg in reversed(result.get("messages", []) or []):
        msg_ts = msg.get("ts")
        try:
            if float(msg_ts) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        turn = _slack_msg_to_turn(msg, bot_id)
        if turn is not None:
            turns.append(turn)
    return turns


def _rehydrate_dm_history(channel):
    """Fetch a DM channel's recent messages and rebuild user/assistant turns."""
    bot_id = _get_bot_user_id()
    try:
        result = app.client.conversations_history(channel=channel, limit=50)
    except Exception as exc:
        logging.warning("Failed to fetch DM history for %s: %s", channel, exc)
        return []
    # conversations.history returns newest-first; reverse to chronological.
    turns = []
    for msg in reversed(result.get("messages", []) or []):
        turn = _slack_msg_to_turn(msg, bot_id)
        if turn is not None:
            turns.append(turn)
    return turns


def _maybe_rehydrate(conversation_id, history_fn):
    """If we have no in-memory history, fetch it from Slack and seed.

    The LAST turn is dropped because it's the user message that just
    triggered this handler — chat() will append it from the live event.
    """
    if has_conversation(conversation_id):
        return
    history = history_fn()
    if history and history[-1].get("role") == "user":
        history = history[:-1]
    if history:
        seed_conversation(conversation_id, history)
        logging.info(
            "Rehydrated %d Slack turns into conversation %s",
            len(history),
            conversation_id,
        )


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

    # Rehydrate from Slack if memory is empty. Two cases:
    #   - Reply inside an existing thread → fetch the thread.
    #   - Top-level @-mention → fetch recent channel context so Mark can
    #     see what the user is asking him to weigh in on.
    is_thread_reply = (
        event.get("thread_ts") is not None
        and event["thread_ts"] != event.get("ts")
    )
    if is_thread_reply:
        rehydrate_fn = lambda: _rehydrate_thread_history(  # noqa: E731
            event["channel"], thread_root_ts
        )
    else:
        rehydrate_fn = lambda: _rehydrate_channel_context(  # noqa: E731
            event["channel"], event["ts"]
        )
    _maybe_rehydrate(conversation_id, rehydrate_fn)

    reply = chat(
        text,
        conversation_id=conversation_id,
        channel=event.get("channel"),
        user=event.get("user"),
        slack_message_ts=event.get("ts"),
        thread_ts=reply_thread_ts,
    )
    say(text=_markdown_to_slack(reply), thread_ts=reply_thread_ts)


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

    # Rehydrate from Slack if we lost in-memory state (e.g., after a deploy).
    _maybe_rehydrate(
        conversation_id,
        lambda: _rehydrate_dm_history(event["channel"]),
    )

    say(_markdown_to_slack(chat(
        text,
        conversation_id=conversation_id,
        channel=event.get("channel"),
        user=event.get("user"),
        slack_message_ts=event.get("ts"),
        # DMs aren't threaded — pass None so share_table posts the CSV
        # to the DM channel without a thread_ts.
        thread_ts=None,
    )))


def _start_reddit_keepalive() -> None:
    """Keep the Reddit Ads refresh token alive from this long-running process.

    Reddit refresh tokens die after ~1h of disuse, so ads tooling in
    short-lived scripts kept losing auth. The bot process refreshes every
    25 minutes; chain state persists to the token cache file
    (REDDIT_TOKEN_CACHE_PATH — point it at a persistent disk in prod so
    restarts resume the chain). No-op when Reddit creds aren't configured.
    """
    if not os.environ.get("REDDIT_ADS_REFRESH_TOKEN"):
        return
    import threading
    import time

    from email_mark import reddit_client

    def _loop() -> None:
        while True:
            ok = reddit_client.refresh_keepalive()
            logging.info("Reddit token keepalive: %s", "ok" if ok else "FAILED")
            time.sleep(25 * 60)

    threading.Thread(target=_loop, daemon=True, name="reddit-keepalive").start()


def main() -> None:
    _start_reddit_keepalive()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Bot starting... ctrl+C to stop.")
    handler.start()


if __name__ == "__main__":
    main()
