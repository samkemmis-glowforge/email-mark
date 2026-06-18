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


def _post_multipart(
    path: str,
    data: Dict[str, Any],
    image_bytes: bytes,
    image_filename: str,
    image_mime: str,
) -> Dict[str, Any]:
    """POST with image bytes as multipart. Used by draft_facebook_post when
    the caller has actual file bytes (e.g., pulled from Drive) rather than a
    public image URL Meta can fetch itself.
    """
    data = dict(data)
    data.setdefault("access_token", _token())
    files = {"source": (image_filename, image_bytes, image_mime)}
    resp = requests.post(
        f"{GRAPH_BASE}/{path.lstrip('/')}", data=data, files=files, timeout=120
    )
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


# IG splits account-level metrics into two families that CAN'T be combined
# in one API call:
#   - Time series: one value per day, called with just `period=day`.
#       reach, follower_count.
#   - Total value: one rollup for the date range, requires
#       `metric_type=total_value`. views, profile_views, accounts_engaged,
#       total_interactions, likes, comments, shares, saves, replies,
#       website_clicks, profile_links_taps.
# get_instagram_insights() routes its metrics into both calls and merges
# the responses so the caller gets one unified result.
_IG_TOTAL_VALUE_METRICS = {
    "views",
    "profile_views",
    "accounts_engaged",
    "total_interactions",
    "likes",
    "comments",
    "shares",
    "saves",
    "replies",
    "website_clicks",
    "profile_links_taps",
}


def get_instagram_insights(
    *,
    metrics: Optional[List[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    period: str = "day",
) -> Dict[str, Any]:
    """Instagram Business account insights over a date range.

    Auto-splits requested metrics into time-series vs total-value calls so
    callers can mix the two families without worrying about Meta's split.
    """
    ig_id = _require("META_IG_USER_ID", "read Instagram insights")
    metrics = metrics or [
        "reach",
        "follower_count",
        "views",
        "profile_views",
        "accounts_engaged",
        "total_interactions",
    ]
    until = until or date.today().isoformat()
    since = since or (date.today() - timedelta(days=28)).isoformat()

    ts_metrics = [m for m in metrics if m not in _IG_TOTAL_VALUE_METRICS]
    tv_metrics = [m for m in metrics if m in _IG_TOTAL_VALUE_METRICS]

    combined: List[Any] = []
    if ts_metrics:
        ts = _get(
            f"{ig_id}/insights",
            {
                "metric": ",".join(ts_metrics),
                "since": since,
                "until": until,
                "period": period,
            },
        )
        combined.extend(ts.get("data", []))
    if tv_metrics:
        tv = _get(
            f"{ig_id}/insights",
            {
                "metric": ",".join(tv_metrics),
                "since": since,
                "until": until,
                "period": period,
                "metric_type": "total_value",
            },
        )
        combined.extend(tv.get("data", []))

    return {"since": since, "until": until, "metrics": combined}


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


def draft_facebook_post(
    *,
    message: str,
    image_url: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    image_filename: str = "asset.jpg",
    image_mime: str = "image/jpeg",
    scheduled_publish_time: Optional[int] = None,
) -> Dict[str, Any]:
    """Create a SCHEDULED post on the Facebook Page (the working "draft" UX).

    Pure unpublished posts (published=false alone) exist in the API but are
    INVISIBLE in the modern MBS Planner UI — Meta only shows API-scheduled
    posts in the human-facing surfaces. So this function defaults to
    scheduling the post 24h from creation, which gives the same UX as a
    HubSpot draft: it appears in MBS → Planner → Scheduled where a human
    can review, edit the caption, change the time, or delete it before it
    fires. If 24h passes without intervention, the post goes live — which
    is generally the desired outcome for a calendar-driven cadence.

    Caller can override by passing `scheduled_publish_time` (unix seconds,
    must be 10 min to 6 months in the future). For example, Mark batching
    from the content calendar should pass each row's target publish time.

    NOT gated by SOCIAL_MARK_ALLOW_PUBLISH because the human reviews in MBS
    before the scheduled time fires — MBS is the safety gate.

    Returns the Graph response (includes the new post `id`) plus the
    `scheduled_publish_time` actually used (added by this function so the
    caller can surface a human-readable "scheduled for X" message).
    """
    import time as _time

    import json as _json

    page_id = _require("META_PAGE_ID", "draft to the Facebook Page")
    if scheduled_publish_time is None:
        scheduled_publish_time = int(_time.time()) + 24 * 3600

    # Two-step pattern for image posts: Meta's /photos endpoint with
    # `published=false + scheduled_publish_time` quietly drops the schedule
    # and lands the upload in the "unpublished photos" bucket, which is
    # invisible in MBS → Planner → Scheduled. The fix is to upload the
    # photo unpublished WITHOUT a schedule (step 1), then create a
    # scheduled FEED post that attaches it via `attached_media` (step 2).
    # The feed post is what appears in MBS Scheduled.
    media_fbid: Optional[str] = None
    if image_bytes is not None:
        photo = _post_multipart(
            f"{page_id}/photos",
            {"published": "false"},
            image_bytes=image_bytes,
            image_filename=image_filename,
            image_mime=image_mime,
        )
        media_fbid = photo.get("id")
    elif image_url:
        photo = _post(
            f"{page_id}/photos",
            {"published": "false", "url": image_url},
        )
        media_fbid = photo.get("id")

    feed_params: Dict[str, Any] = {
        "message": message,
        "published": "false",
        "scheduled_publish_time": scheduled_publish_time,
    }
    if media_fbid:
        # Meta accepts attached_media as a JSON-encoded list of refs.
        feed_params["attached_media"] = _json.dumps(
            [{"media_fbid": media_fbid}]
        )

    result = _post(f"{page_id}/feed", feed_params)
    result["scheduled_publish_time"] = scheduled_publish_time
    if media_fbid:
        result["media_fbid"] = media_fbid
    return result


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
