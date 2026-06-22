"""HubSpot Social Media (Broadcast) API client.

Glowforge already manages organic FB + IG via HubSpot's social tool, so
this module is the path to drafting scheduled social posts INTO HubSpot
(where the team actually reviews them) instead of directly to Meta.

Underneath, HubSpot publishes to the connected Meta Pages itself, so
posts created here end up on the same Glowforge FB Page as the direct
Meta-API path — they just live in HubSpot's UI for review until they
fire.

Reference: HubSpot's social API has historically been called the
"Broadcast API" (legacy v1). HubSpot has since started migrating to
date-versioned APIs, so the legacy paths may be sunsetting. This module
tries the legacy paths first; if HubSpot's response indicates the
endpoint moved, the error surfaces clearly so we know to migrate.

Smoke-test from the repo root:

    python -m email_mark.hubspot_social list-channels
    python -m email_mark.hubspot_social create-post \\
        --channel-id <CID> \\
        --body "test from social-mark" \\
        --schedule-in-hours 24

Requires HUBSPOT_API_KEY (the Private App token) in env, with the
`social-media` scope enabled on the Private App.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

HUBSPOT_BASE = "https://api.hubapi.com"

# Legacy Broadcast API endpoints — these are what we'll probe first.
# If HubSpot has moved them to /marketing/2026-XX/social/... or similar,
# the response status will tell us and we'll migrate.
_LEGACY_CHANNELS_PATH = "/broadcast/v1/channels/setting/publish/current"
_LEGACY_BROADCAST_PATH = "/broadcast/v1/broadcasts"


class HubSpotSocialError(RuntimeError):
    """Raised on auth / scope / endpoint failures so callers can surface
    a clean message rather than the raw HTTP payload."""


def _headers() -> Dict[str, str]:
    token = os.environ.get("HUBSPOT_API_KEY")
    if not token:
        raise HubSpotSocialError(
            "HUBSPOT_API_KEY not set. Add the Private App token to .env."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _handle(resp: requests.Response, *, context: str) -> Any:
    if resp.status_code == 401:
        raise HubSpotSocialError(
            f"{context}: 401 unauthorized. The Private App token is missing "
            f"or doesn't have the `social-media` scope. Fix at HubSpot → "
            f"Settings → Integrations → Private Apps → your app → Scopes."
        )
    if resp.status_code == 404:
        raise HubSpotSocialError(
            f"{context}: 404 not found. The legacy Broadcast endpoint may "
            f"have been retired in HubSpot's 2026-03 date-versioned API "
            f"migration. Check developers.hubspot.com for the current path "
            f"and update _LEGACY_*_PATH constants."
        )
    if resp.status_code >= 400:
        raise HubSpotSocialError(
            f"{context}: HTTP {resp.status_code} — {resp.text[:400]}"
        )
    try:
        return resp.json()
    except ValueError:
        raise HubSpotSocialError(
            f"{context}: non-JSON response — {resp.text[:200]}"
        )


def list_channels() -> List[Dict[str, Any]]:
    """Return the social channels (FB Page, IG Business, LinkedIn, etc.)
    that are connected to the current HubSpot portal.

    Used to discover the `channelGuid` values you pass to create_post.
    Each entry includes the channel's GUID, the human-readable name, and
    which social network it's on.
    """
    resp = requests.get(
        f"{HUBSPOT_BASE}{_LEGACY_CHANNELS_PATH}",
        headers=_headers(),
        timeout=30,
    )
    data = _handle(resp, context="list_channels")
    # Legacy response is a list; the new API may wrap in {"results": [...]}.
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return data["results"]
    raise HubSpotSocialError(
        f"list_channels: unexpected response shape — {str(data)[:300]}"
    )


# Channel name → env-var override mapping. We let Render configure these
# explicitly so we don't pay the cost of list_channels() on every send.
_CHANNEL_ENV_VARS = {
    "facebook": "HUBSPOT_SOCIAL_FB_CHANNEL_GUID",
    "instagram": "HUBSPOT_SOCIAL_IG_CHANNEL_GUID",
}


def resolve_channel_guid(channel_name: str) -> str:
    """Return the channelGuid for 'facebook' or 'instagram'.

    Prefers the env-var override (HUBSPOT_SOCIAL_FB_CHANNEL_GUID /
    HUBSPOT_SOCIAL_IG_CHANNEL_GUID) so we don't have to hit list_channels
    on every send. Falls back to list_channels() if the env var isn't set.
    """
    name = (channel_name or "").strip().lower()
    if name not in _CHANNEL_ENV_VARS:
        raise HubSpotSocialError(
            f"Unknown channel name {channel_name!r}. "
            f"Supported: {list(_CHANNEL_ENV_VARS)}."
        )
    env_var = _CHANNEL_ENV_VARS[name]
    guid = os.environ.get(env_var, "").strip()
    if guid:
        return guid
    # No env var — discover via list_channels.
    matches = [
        ch for ch in list_channels()
        if (ch.get("channelType") or "").lower().replace("page", "") == name
    ]
    if not matches:
        raise HubSpotSocialError(
            f"Could not find a connected {name} channel in this HubSpot "
            f"portal. Connect it via Marketing → Social → Settings, or set "
            f"{env_var} explicitly."
        )
    return matches[0]["channelGuid"]


def create_posts_to_channels(
    *,
    channels: List[str],
    body: str,
    trigger_at_unix: int,
    photo_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Create one broadcast per requested channel.

    HubSpot's Broadcast API takes a single channelGuid per call, so cross-
    posting to FB + IG requires two calls. This helper resolves channel
    names → GUIDs and dispatches each, returning a list of broadcast
    responses (or per-channel error dicts if a single channel fails so
    the others can still go through).
    """
    results: List[Dict[str, Any]] = []
    for channel_name in channels:
        try:
            guid = resolve_channel_guid(channel_name)
            result = create_post(
                channel_guid=guid,
                body=body,
                trigger_at_unix=trigger_at_unix,
                photo_url=photo_url,
            )
            result["_channel_name"] = channel_name
            results.append(result)
        except HubSpotSocialError as exc:
            results.append({"_channel_name": channel_name, "error": str(exc)})
    return results


def create_post(
    *,
    channel_guid: str,
    body: str,
    trigger_at_unix: int,
    photo_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a scheduled social post via HubSpot Broadcast API.

    Args:
        channel_guid: the channel GUID from list_channels() (one channel
            per call — to cross-post to FB + IG, call twice).
        body: the post text.
        trigger_at_unix: scheduled publish time as unix seconds. HubSpot
            actually wants milliseconds, so this gets multiplied by 1000.
        photo_url: optional public image URL HubSpot will attach.
    """
    payload: Dict[str, Any] = {
        "channelGuid": channel_guid,
        "triggerAt": int(trigger_at_unix) * 1000,
        "content": {"body": body},
    }
    if photo_url:
        # Legacy API used `photoUrl` at the top level; the newer envelope
        # nests media under content. Send both forms — HubSpot picks the
        # one the endpoint understands.
        payload["photoUrl"] = photo_url
        payload["content"]["photoUrl"] = photo_url

    resp = requests.post(
        f"{HUBSPOT_BASE}{_LEGACY_BROADCAST_PATH}",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    return _handle(resp, context="create_post")


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def _cmd_list_channels(_: argparse.Namespace) -> int:
    try:
        channels = list_channels()
    except HubSpotSocialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not channels:
        print("No connected social channels found on this portal.")
        return 0
    print(f"Found {len(channels)} channel(s):\n")
    for ch in channels:
        # Print every key — schema varies by network type.
        for k, v in ch.items():
            print(f"  {k}: {v}")
        print()
    return 0


def _cmd_create_post(args: argparse.Namespace) -> int:
    trigger_at = int(time.time()) + args.schedule_in_hours * 3600
    try:
        result = create_post(
            channel_guid=args.channel_id,
            body=args.body,
            trigger_at_unix=trigger_at,
            photo_url=args.photo_url,
        )
    except HubSpotSocialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Post created:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-channels", help="Print connected social channels.")

    p_post = sub.add_parser("create-post", help="Create a scheduled post.")
    p_post.add_argument("--channel-id", required=True, help="channelGuid from list-channels")
    p_post.add_argument("--body", required=True, help="Post text")
    p_post.add_argument("--schedule-in-hours", type=int, default=24,
                        help="Schedule N hours from now. Default 24.")
    p_post.add_argument("--photo-url", help="Optional public image URL")

    args = parser.parse_args(argv)
    if args.cmd == "list-channels":
        return _cmd_list_channels(args)
    if args.cmd == "create-post":
        return _cmd_create_post(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
