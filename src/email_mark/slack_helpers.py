"""Helpers for the agent to look up Slack users and (optionally) post messages.

Uses the same SLACK_BOT_TOKEN we already have in env. Requires the `users:read`
scope, which the bot already has.
"""

from __future__ import annotations

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


def send_dm(user_id: str, text: str) -> Dict[str, Any]:
    """Send a direct message to a Slack user. Opens a DM channel if needed."""
    client = _get_client()
    open_response = client.conversations_open(users=user_id)
    channel = open_response["channel"]["id"]
    msg = client.chat_postMessage(channel=channel, text=text)
    return {"ok": msg.get("ok"), "channel": channel, "ts": msg.get("ts")}
