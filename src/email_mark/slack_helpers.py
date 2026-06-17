"""Helpers for the agent to look up Slack users and (optionally) post messages.

Uses the same SLACK_BOT_TOKEN we already have in env. Requires the `users:read`
scope, which the bot already has.

For CSV file uploads (the `share_table` tool side-channel), additionally
requires the `files:write` scope.
"""

from __future__ import annotations

import csv
import io
import os
import time
from typing import Any, Dict, List, Optional

from dotenv import find_dotenv, load_dotenv
from slack_sdk import WebClient

load_dotenv(find_dotenv())

_client: Optional[WebClient] = None
_users_cache: Optional[List[Dict[str, Any]]] = None
_users_cache_time: float = 0.0
USERS_CACHE_TTL_SECONDS = 300  # Refresh user list every 5 minutes.


def _get_client() -> WebClient:
    global _client
    if _client is None:
        _client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    return _client


def _get_users() -> List[Dict[str, Any]]:
    """Return cached active users from the workspace, refreshing as needed."""
    global _users_cache, _users_cache_time
    now = time.time()
    if _users_cache is not None and (now - _users_cache_time) < USERS_CACHE_TTL_SECONDS:
        return _users_cache

    response = _get_client().users_list()
    members = response.get("members", []) if response.get("ok") else []
    _users_cache = members
    _users_cache_time = now
    return _users_cache


def lookup_user(query: str) -> List[Dict[str, Any]]:
    """Find Slack users whose name (or email, if available) contains `query`.

    Returns a list of {id, username, real_name, display_name} dicts.
    Excludes deactivated accounts and bot users (including ourselves).
    """
    needle = (query or "").strip().lower()
    if not needle:
        return []

    matches: List[Dict[str, Any]] = []
    for user in _get_users():
        if user.get("deleted") or user.get("is_bot"):
            continue
        profile = user.get("profile", {}) or {}
        candidates = [
            user.get("name", ""),
            profile.get("display_name", ""),
            profile.get("real_name", ""),
            profile.get("email", ""),
        ]
        if any(needle in (c or "").lower() for c in candidates):
            matches.append({
                "id": user.get("id"),
                "username": user.get("name"),
                "real_name": profile.get("real_name") or "",
                "display_name": profile.get("display_name") or "",
            })
    return matches


def get_user_display(user_id: str) -> Optional[str]:
    """Return a user's display name from the cached workspace user list.

    Tries display_name → real_name → username, in that order. Returns
    None if the user isn't found (e.g., outside the workspace, deactivated).
    """
    if not user_id:
        return None
    for user in _get_users():
        if user.get("id") == user_id:
            profile = user.get("profile", {}) or {}
            return (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("name")
                or None
            )
    return None


def send_dm(user_id: str, text: str) -> Dict[str, Any]:
    """Send a direct message to a Slack user. Opens a DM channel if needed."""
    client = _get_client()
    open_response = client.conversations_open(users=user_id)
    channel = open_response["channel"]["id"]
    msg = client.chat_postMessage(channel=channel, text=text)
    return {"ok": msg.get("ok"), "channel": channel, "ts": msg.get("ts")}


def post_message(channel: str, text: str) -> Dict[str, Any]:
    """Post a message to a channel. Returns {ok, channel, ts}."""
    msg = _get_client().chat_postMessage(channel=channel, text=text)
    return {"ok": msg.get("ok"), "channel": msg.get("channel"), "ts": msg.get("ts")}


def post_to_review_channel(text: str) -> Dict[str, Any]:
    """Post a social post-draft into the configured review channel.

    Channel comes from SLACK_REVIEW_CHANNEL. Returns {ok, error?} so callers
    (the scheduled-drafts job, the post_draft_to_review_channel tool) can
    report cleanly. Never raises.
    """
    channel = os.environ.get("SLACK_REVIEW_CHANNEL", "").strip()
    if not channel:
        return {"ok": False, "error": "SLACK_REVIEW_CHANNEL not set"}
    try:
        return post_message(channel, text)
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the caller
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def upload_csv_to_thread(
    *,
    channel: str,
    thread_ts: Optional[str],
    headers: List[str],
    rows: List[List[Any]],
    filename: str,
    initial_comment: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload a CSV file as a thread attachment in the given channel.

    The CSV is built in-memory from headers + rows and uploaded via
    Slack's files_upload_v2. Used by the `share_table` agent tool as a
    side-channel for grid-shaped data — the file lands as a Slack
    attachment (Slack renders its own scrollable preview + download
    button), and the model's prose summary still posts through the
    normal text path.

    Requires the `files:write` scope on SLACK_BOT_TOKEN.

    Returns {"ok": True, "file_id": ..., "permalink": ...} on success,
    or {"ok": False, "error": "..."} on failure. Designed to NEVER
    raise — the calling tool surfaces the error to the model so it can
    report cleanly in chat.
    """
    if not filename.endswith(".csv"):
        filename = f"{filename}.csv"

    # Build the CSV in memory. csv.writer handles quoting/escaping
    # correctly so cells with commas or newlines don't break the file.
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(["" if cell is None else cell for cell in row])
    csv_content = buf.getvalue()

    try:
        client = _get_client()
        # files_upload_v2 accepts either `file` (bytes/path) or `content`
        # (string). We use content since the CSV is already in memory.
        upload_kwargs: Dict[str, Any] = {
            "channel": channel,
            "content": csv_content,
            "filename": filename,
            "title": filename,
        }
        if thread_ts:
            upload_kwargs["thread_ts"] = thread_ts
        if initial_comment:
            upload_kwargs["initial_comment"] = initial_comment

        response = client.files_upload_v2(**upload_kwargs)
        if not response.get("ok"):
            return {
                "ok": False,
                "error": (
                    response.get("error")
                    or "files_upload_v2 returned ok=false with no error field"
                ),
            }
        # Response shape: response['files'] is a list with one file dict.
        files = response.get("files") or response.get("file") or []
        if isinstance(files, dict):
            files = [files]
        first = files[0] if files else {}
        return {
            "ok": True,
            "file_id": first.get("id"),
            "permalink": first.get("permalink"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
