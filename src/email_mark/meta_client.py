"""Meta Graph API connector — Facebook Page + Instagram.

Two jobs, mirroring how email-mark talks to HubSpot's REST API directly:

  READING (reporting):
    - get_page_insights        Facebook Page metrics over a date range
    - get_instagram_insights   Instagram account metrics over a date range
    - get_recent_posts         recent FB/IG posts with per-post engagement
    - get_ad_performance       paid-social spend/results from the Ad account

  WRITING (publishing) — GATED:
    - publish_facebook_post    text + optional image to the Page feed
    - publish_instagram_post   image + caption to Instagram (container -> publish)

  Publishing is OFF unless SOCIAL_MARK_ALLOW_PUBLISH == "true". v1 is
  draft-only: drafts go to Slack for human review and a person posts.
  The publish functions exist so wiring approval-to-publish later is a
  config change, not a rewrite.

All functions return plain dicts and raise MetaError on hard failures so
callers (agent tools) can surface a clean message.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


class MetaError(RuntimeError):
    """Raised when the Graph API returns an error or required config is missing."""


def _token() -> str:
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        raise MetaError(
            "META_ACCESS_TOKEN not set. Add a long-lived Page token to .env "
            "(see .env.example for required scopes)."
        )
    return token


def _require(env_var: str, human: str) -> str:
    val = os.environ.get(env_var)
    if not val:
        raise MetaError(f"{env_var} not set — needed to {human}.")
    return val


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = dict(params or {})
    params.setdefault("access_token", _token())
    resp = requests.get(f"{GRAPH_BASE}/{path.lstrip('/')}", params=params, timeout=30)
    return _handle(resp)


def _post(path: str, data: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(data)
    data.setdefault("access_token", _token())
    resp = requests.post(f"{GRAPH_BASE}/{path.lstrip('/')}", data=data, timeout=60)
    return _handle(resp)


def _handle(resp: requests.Response) -> Dict[str, Any]:
    try:
        payload = resp.json()
    except ValueError:
        raise MetaError(f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:300]}")
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        raise MetaError(
            f"Graph API error {err.get('code')}: {err.get('message')} "
            f"(type={err.get('type')})"
        )
    if resp.status_code >= 400:
        raise MetaError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return payload


def publishing_enabled() -> bool:
    """True only when the explicit safety gate is flipped on."""
    return os.environ.get("SOCIAL_MARK_ALLOW_PUBLISH", "").strip().lower() == "true"


# ---------------------------------------------------------------------------
# READING — reporting
# ---------------------------------------------------------------------------


def get_page_insights(
    *,
    metrics: Optional[List[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    period: str = "day",
) -> Dict[str, Any]:
    """Facebook Page insights over a date range.

    `metrics` default to a useful organic-reach set. Dates are ISO
    (YYYY-MM-DD); default is the trailing 28 days.
    """
    page_id = _require("META_PAGE_ID", "read Facebook Page insights")
    # Late-2025 Meta purge killed `page_impressions*`, `page_fans`,
    # `page_views_total`, `page_fan_adds`, `page_consumptions`. These four
    # are the empirically-confirmed survivors as of v21.0. Also note:
    # Page Insights REQUIRE a Page Access Token (not a System User token).
    # Generate via GET /me/accounts with your system user token; the
    # `access_token` field in the response for your Page is what goes
    # in META_ACCESS_TOKEN.
    metrics = metrics or [
        "page_post_engagements",
        "page_video_views",
        "page_actions_post_reactions_total",
        "page_total_actions",
    ]
    until = until or date.today().isoformat()
    since = since or (date.today() - timedelta(days=28)).isoformat()
    data = _get(
        f"{page_id}/insights",
        {"metric": ",".join(metrics), "since": since, "until": until, "period": period},
    )
    return {"since": since, "until": until, "metrics": data.get("data", [])}


def get_instagram_insights(
    *,
    metrics: Optional[List[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    period: str = "day",
) -> Dict[str, Any]:
    """Instagram Business account insights over a date range."""
    ig_id = _require("META_IG_USER_ID", "read Instagram insights")
    # `impressions` was deprecated for IG account-level insights; `views` is
    # the current account-level equivalent. The rest are stable. Other valid
    # metrics include accounts_engaged / total_interactions, but those require
    # an extra `metric_type=total_value` param — keep them out of defaults.
    metrics = metrics or ["reach", "views", "profile_views", "follower_count"]
    until = until or date.today().isoformat()
    since = since or (date.today() - timedelta(days=28)).isoformat()
    data = _get(
        f"{ig_id}/insights",
        {"metric": ",".join(metrics), "since": since, "until": until, "period": period},
    )
    return {"since": since, "until": until, "metrics": data.get("data", [])}


def get_recent_posts(*, platform: str = "facebook", limit: int = 10) -> Dict[str, Any]:
    """Recent posts with per-post engagement.

    platform: "facebook" -> Page feed, "instagram" -> IG media.
    """
    if platform == "instagram":
        ig_id = _require("META_IG_USER_ID", "read Instagram posts")
        fields = (
            "id,caption,media_type,permalink,timestamp,"
            "like_count,comments_count"
        )
        data = _get(f"{ig_id}/media", {"fields": fields, "limit": limit})
        return {"platform": "instagram", "posts": data.get("data", [])}

    page_id = _require("META_PAGE_ID", "read Facebook posts")
    fields = (
        "id,message,created_time,permalink_url,"
        "insights.metric(post_impressions,post_engaged_users){name,values}"
    )
    data = _get(f"{page_id}/posts", {"fields": fields, "limit": limit})
    return {"platform": "facebook", "posts": data.get("data", [])}


def get_ad_performance(
    *,
    fields: Optional[List[str]] = None,
    date_preset: str = "last_28d",
    level: str = "campaign",
) -> Dict[str, Any]:
    """Paid-social performance from the Meta Ad account (insights edge)."""
    act = _require("META_AD_ACCOUNT_ID", "read paid-social performance")
    if not act.startswith("act_"):
        act = f"act_{act}"
    fields = fields or [
        "campaign_name",
        "impressions",
        "reach",
        "clicks",
        "spend",
        "cpc",
        "ctr",
        "actions",
    ]
    data = _get(
        f"{act}/insights",
        {"fields": ",".join(fields), "date_preset": date_preset, "level": level},
    )
    return {"date_preset": date_preset, "level": level, "rows": data.get("data", [])}


# ---------------------------------------------------------------------------
# WRITING — publishing (GATED behind SOCIAL_MARK_ALLOW_PUBLISH)
# ---------------------------------------------------------------------------


def _guard_publish() -> None:
    if not publishing_enabled():
        raise MetaError(
            "Publishing is disabled. social-mark is in draft-only mode "
            "(SOCIAL_MARK_ALLOW_PUBLISH != 'true'). Drafts go to Slack for "
            "human review; a person publishes. Flip the gate only after the "
            "approval flow is signed off."
        )


def publish_facebook_post(
    *, message: str, image_url: Optional[str] = None
) -> Dict[str, Any]:
    """Publish text (and an optional image) to the Facebook Page feed."""
    _guard_publish()
    page_id = _require("META_PAGE_ID", "publish to the Facebook Page")
    if image_url:
        return _post(f"{page_id}/photos", {"caption": message, "url": image_url})
    return _post(f"{page_id}/feed", {"message": message})


def publish_instagram_post(*, image_url: str, caption: str) -> Dict[str, Any]:
    """Publish an image + caption to Instagram (create container, then publish)."""
    _guard_publish()
    ig_id = _require("META_IG_USER_ID", "publish to Instagram")
    container = _post(
        f"{ig_id}/media", {"image_url": image_url, "caption": caption}
    )
    creation_id = container.get("id")
    if not creation_id:
        raise MetaError(f"IG container creation returned no id: {container}")
    return _post(f"{ig_id}/media_publish", {"creation_id": creation_id})
