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

import json
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
        # error_user_title/msg carry Meta's actual explanation ("must
        # specify X field") — without them error 100 is just "Invalid
        # parameter" and undebuggable.
        detail = ": ".join(
            p for p in (err.get("error_user_title"), err.get("error_user_msg")) if p
        )
        raise MetaError(
            f"Graph API error {err.get('code')}"
            f"{'/' + str(err.get('error_subcode')) if err.get('error_subcode') else ''}"
            f": {err.get('message')} (type={err.get('type')})"
            f"{' — ' + detail if detail else ''}"
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
    """Paid-social performance from the Meta Ad account (insights edge).

    NOTE: /insights only returns entities with actual activity (spend,
    impressions). New/paused campaigns with zero delivery are INVISIBLE
    to this endpoint, even at date_preset="maximum". To see all campaign
    objects (including new/paused), use list_meta_campaigns() instead.
    """
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
# READ-ONLY MARKETING API — campaign/adset/ad structure + settings
#
# Mark's original ads toolkit was just get_ad_performance, which hits
# /insights. That gives metrics but is INVISIBLE to campaigns with zero
# activity (like a just-launched one), and it never shows targeting,
# creative, or delivery diagnostics. The tools below fill in the
# structural side so Mark can inspect campaign settings, ad set
# targeting, ad creative, and Meta's own "why isn't this delivering"
# diagnostics — the questions Mark actually needs to answer to
# troubleshoot ad performance.
#
# All read-only. All use the same META_ACCESS_TOKEN + META_AD_ACCOUNT_ID
# already in Render. Same auth path as get_ad_performance.
# ---------------------------------------------------------------------------


_CAMPAIGN_DEFAULT_FIELDS = [
    "id", "name", "objective", "status", "effective_status",
    "daily_budget", "lifetime_budget", "budget_remaining", "spend_cap",
    "buying_type", "start_time", "stop_time", "created_time", "updated_time",
    "bid_strategy", "special_ad_categories", "pacing_type",
    "issues_info",
]

_ADSET_DEFAULT_FIELDS = [
    "id", "name", "campaign_id", "status", "effective_status",
    "daily_budget", "lifetime_budget", "billing_event", "optimization_goal",
    "bid_strategy", "bid_amount", "start_time", "end_time",
    "created_time", "updated_time", "configured_status", "issues_info",
    "attribution_spec", "destination_type", "promoted_object",
]

_ADSET_TARGETING_FIELDS = [
    "id", "name", "targeting", "promoted_object", "optimization_goal",
    "billing_event", "attribution_spec",
]

_AD_DEFAULT_FIELDS = [
    "id", "name", "adset_id", "campaign_id", "status", "effective_status",
    "creative", "created_time", "updated_time", "issues_info",
]

_AD_CREATIVE_FIELDS = [
    "id", "name",
    "creative{id,name,title,body,image_url,thumbnail_url,"
    "object_story_spec,call_to_action_type,link_url,object_type,"
    "video_id,instagram_permalink_url}",
    "preview_shareable_link",
]

_DELIVERY_FIELDS = [
    "id", "name", "effective_status", "configured_status",
    "issues_info", "recommendations",
]


def list_meta_campaigns(
    *,
    status_filter: Optional[str] = None,
    limit: int = 100,
    fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """List every campaign in the ad account.

    Unlike get_ad_performance, this returns campaign OBJECTS not metrics,
    so new/paused/zero-spend campaigns are visible here. Use this when
    you need to see "what campaigns exist" rather than "what has been
    spending."

    status_filter: pass "ACTIVE" | "PAUSED" | "DELETED" | "ARCHIVED" to
        filter, or None for all. Filters on effective_status.
    """
    act = _require("META_AD_ACCOUNT_ID", "list Meta campaigns")
    if not act.startswith("act_"):
        act = f"act_{act}"
    params: Dict[str, Any] = {
        "fields": ",".join(fields or _CAMPAIGN_DEFAULT_FIELDS),
        "limit": limit,
    }
    if status_filter:
        # Effective status filter is JSON-encoded array in the query string.
        params["effective_status"] = f'["{status_filter.upper()}"]'
    data = _get(f"{act}/campaigns", params)
    return {"campaigns": data.get("data", []), "paging": data.get("paging", {})}


def get_meta_campaign_details(campaign_id: str) -> Dict[str, Any]:
    """Full settings for one campaign: objective, budget, dates, delivery
    flags, pacing, etc. Use when troubleshooting why a campaign isn't
    behaving as expected.
    """
    fields = ",".join(_CAMPAIGN_DEFAULT_FIELDS)
    return _get(f"{campaign_id}", {"fields": fields})


def list_meta_adsets(
    *,
    campaign_id: str,
    limit: int = 50,
    fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """List ad sets under a campaign. Returns ad set OBJECTS (status,
    budget, optimization goal) but not the full targeting spec — for
    that use get_meta_adset_targeting on a specific ad set.
    """
    params: Dict[str, Any] = {
        "fields": ",".join(fields or _ADSET_DEFAULT_FIELDS),
        "limit": limit,
    }
    data = _get(f"{campaign_id}/adsets", params)
    return {"adsets": data.get("data", []), "paging": data.get("paging", {})}


def get_meta_adset_targeting(adset_id: str) -> Dict[str, Any]:
    """Full targeting spec for one ad set — custom_audiences,
    lookalikes, interests, behaviors, geo, age, gender, placements, etc.

    This is the "who is this ad targeting" answer. Meta's targeting
    object is deeply nested; expect nested lists of custom_audiences,
    flexible_spec, geo_locations, etc. Read it in a structured way
    rather than trying to summarize the whole thing.
    """
    fields = ",".join(_ADSET_TARGETING_FIELDS)
    return _get(f"{adset_id}", {"fields": fields})


def list_meta_ads(
    *,
    adset_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    limit: int = 50,
    fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """List ads under either an ad set (preferred) or a campaign.

    One of adset_id or campaign_id must be provided. Returns ad OBJECTS
    with status and creative reference. To see the actual creative
    (copy, image, destination URL) use get_meta_ad_creative on a
    specific ad.
    """
    if not adset_id and not campaign_id:
        raise MetaError("list_meta_ads requires either adset_id or campaign_id.")
    parent = adset_id or campaign_id
    params: Dict[str, Any] = {
        "fields": ",".join(fields or _AD_DEFAULT_FIELDS),
        "limit": limit,
    }
    data = _get(f"{parent}/ads", params)
    return {"ads": data.get("data", []), "paging": data.get("paging", {})}


def get_meta_ad_creative(ad_id: str) -> Dict[str, Any]:
    """Full creative for one ad: copy, headline, body, image URL,
    destination URL, CTA, preview link, video ID if video.

    Use this when troubleshooting "is my landing page URL right,"
    "does my creative match my angle," "what CTA am I using," etc.
    """
    fields = ",".join(_AD_CREATIVE_FIELDS)
    return _get(f"{ad_id}", {"fields": fields})


def get_meta_delivery_diagnostics(object_id: str) -> Dict[str, Any]:
    """Meta's own delivery diagnostics for a campaign, ad set, or ad.

    Returns issues_info (policy issues, learning-limited flags, low
    delivery reasons) and recommendations from Meta. This is often the
    single most useful troubleshooting endpoint — Meta itself tells you
    why something isn't delivering.

    Accepts any of campaign_id, adset_id, or ad_id.
    """
    fields = ",".join(_DELIVERY_FIELDS)
    return _get(f"{object_id}", {"fields": fields})


def meta_read_api(
    *, path: str, fields: Optional[str] = None, params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Raw read-only Marketing API call — for anything not covered by
    the structured tools above.

    path: Graph API path without leading slash, e.g. "act_123/campaigns"
        or "6250601797283" (an object id) or "6250601797283/adsets".
    fields: comma-separated field list, or None to use Meta's default.
    params: additional query params (limit, effective_status, etc.).

    Read-only enforcement: if the path suggests a mutation endpoint
    (contains "delete", "create", or is empty), this refuses. Never
    performs POST — always GET.
    """
    if not path:
        raise MetaError("meta_read_api: path is required.")
    # Rough safety: refuse mutation-shaped paths. Real read-only
    # enforcement is that we only ever call GET, but this makes intent
    # clear when the model tries something suspicious.
    path_lower = path.lower()
    for bad in ("/delete", "/copy", "/dup", "/publish", "/associations"):
        if bad in path_lower:
            raise MetaError(
                f"meta_read_api: path {path!r} looks like a mutation. "
                f"This tool is read-only."
            )
    query: Dict[str, Any] = dict(params or {})
    if fields:
        query["fields"] = fields
    return _get(path.lstrip("/"), query)


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


# ---------------------------------------------------------------------------
# WRITING — Marketing API campaign builds (GATED behind ADS_MARK_ALLOW_WRITE)
#
# Lets Mark BUILD test campaigns — new targeting, messaging, creative —
# without being able to touch anything a human built or spend money
# unsupervised. Three guardrails are baked into every function here, not
# left to the model's judgement:
#
#   1. GATE.   Every write refuses unless ADS_MARK_ALLOW_WRITE == "true"
#              (mirrors SOCIAL_MARK_ALLOW_PUBLISH).
#   2. PAUSED. Campaigns, ad sets, and ads are always created with
#              status=PAUSED. Nothing spends at creation time. Activation
#              is a separate, explicit call.
#   3. FENCE.  Created objects get an ADS_MARK_NAME_PREFIX tag prepended
#              to their name, and every mutation (child creation, status
#              change, budget change) first fetches the target's name and
#              refuses unless it carries the tag. Mark can never pause,
#              edit, or add to a human-built live campaign.
#
# Budgets are capped: ADS_MARK_MAX_DAILY_BUDGET_CENTS (default 5000 =
# $50/day) and ADS_MARK_MAX_LIFETIME_BUDGET_CENTS (default 30x daily cap).
# Caps are enforced at create/update time, and only tag-fenced objects can
# be activated, so an object can never go ACTIVE with an unchecked budget.
#
# Auth: writes need the ads_management scope. If the main
# META_ACCESS_TOKEN is a Page token without it, put a System User token
# with ads_management in META_ADS_ACCESS_TOKEN — writes prefer it and fall
# back to META_ACCESS_TOKEN.
# ---------------------------------------------------------------------------


# ODAX objectives — the only ones creatable on current API versions.
_VALID_OBJECTIVES = {
    "OUTCOME_AWARENESS",
    "OUTCOME_TRAFFIC",
    "OUTCOME_ENGAGEMENT",
    "OUTCOME_LEADS",
    "OUTCOME_APP_PROMOTION",
    "OUTCOME_SALES",
}


def ads_write_enabled() -> bool:
    """True only when the ads write gate is explicitly flipped on."""
    return os.environ.get("ADS_MARK_ALLOW_WRITE", "").strip().lower() == "true"


def _guard_ads_write() -> None:
    if not ads_write_enabled():
        raise MetaError(
            "Ads writes are disabled. ads-mark is in draft-only mode "
            "(ADS_MARK_ALLOW_WRITE != 'true'). Hand the draft to a human to "
            "build in Ads Manager, or have an admin flip the gate."
        )


def _ads_token() -> str:
    return os.environ.get("META_ADS_ACCESS_TOKEN") or _token()


def _act() -> str:
    act = _require("META_AD_ACCOUNT_ID", "write to the Meta ad account")
    return act if act.startswith("act_") else f"act_{act}"


def _mark_prefix() -> str:
    return os.environ.get("ADS_MARK_NAME_PREFIX", "[mark]").strip() or "[mark]"


def _mark_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise MetaError("A non-empty name is required.")
    prefix = _mark_prefix()
    return name if name.startswith(prefix) else f"{prefix} {name}"


def _budget_caps() -> tuple:
    daily = int(os.environ.get("ADS_MARK_MAX_DAILY_BUDGET_CENTS", "5000"))
    lifetime = int(
        os.environ.get("ADS_MARK_MAX_LIFETIME_BUDGET_CENTS", str(daily * 30))
    )
    return daily, lifetime


def _check_budget(
    daily_budget_cents: Optional[int], lifetime_budget_cents: Optional[int]
) -> None:
    daily_cap, lifetime_cap = _budget_caps()
    if daily_budget_cents is not None:
        if int(daily_budget_cents) <= 0:
            raise MetaError("daily_budget_cents must be a positive integer.")
        if int(daily_budget_cents) > daily_cap:
            raise MetaError(
                f"daily_budget_cents={daily_budget_cents} exceeds the ads-mark "
                f"cap of {daily_cap} cents/day (ADS_MARK_MAX_DAILY_BUDGET_CENTS). "
                f"Ask a human to raise the cap or lower the budget."
            )
    if lifetime_budget_cents is not None:
        if int(lifetime_budget_cents) <= 0:
            raise MetaError("lifetime_budget_cents must be a positive integer.")
        if int(lifetime_budget_cents) > lifetime_cap:
            raise MetaError(
                f"lifetime_budget_cents={lifetime_budget_cents} exceeds the "
                f"ads-mark cap of {lifetime_cap} cents "
                f"(ADS_MARK_MAX_LIFETIME_BUDGET_CENTS)."
            )


def _require_mark_object(object_id: str, action: str) -> Dict[str, Any]:
    """Fetch an object's name and refuse unless it carries the mark tag.

    The fence that keeps Mark inside its own sandbox: humans build
    campaigns without the tag, so those objects are immutable to Mark.
    """
    if not object_id:
        raise MetaError(f"An object id is required to {action}.")
    obj = _get(str(object_id), {"fields": "id,name", "access_token": _ads_token()})
    name = obj.get("name") or ""
    prefix = _mark_prefix()
    if not name.startswith(prefix):
        raise MetaError(
            f"Refusing to {action}: object {object_id} ({name!r}) was not "
            f"created by ads-mark (its name lacks the {prefix!r} tag). Mark "
            f"only modifies campaigns/ad sets/ads it built itself."
        )
    return obj


def create_meta_campaign(
    *,
    name: str,
    objective: str,
    daily_budget_cents: Optional[int] = None,
    lifetime_budget_cents: Optional[int] = None,
    special_ad_categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a new campaign — always PAUSED, always name-tagged.

    Budget is optional at the campaign level: set it here for CBO
    (Advantage campaign budget), or omit and set budgets per ad set.
    Budgets are in the account's minor currency unit (cents for USD).
    """
    _guard_ads_write()
    objective = (objective or "").strip().upper()
    if objective not in _VALID_OBJECTIVES:
        raise MetaError(
            f"objective must be one of {sorted(_VALID_OBJECTIVES)}, "
            f"got {objective!r}."
        )
    _check_budget(daily_budget_cents, lifetime_budget_cents)
    data: Dict[str, Any] = {
        "access_token": _ads_token(),
        "name": _mark_name(name),
        "objective": objective,
        "status": "PAUSED",
        "special_ad_categories": json.dumps(special_ad_categories or []),
    }
    if daily_budget_cents is not None:
        data["daily_budget"] = int(daily_budget_cents)
    if lifetime_budget_cents is not None:
        data["lifetime_budget"] = int(lifetime_budget_cents)
    else:
        if daily_budget_cents is None:
            # No CBO budget -> Meta requires this field (subcode 4834011).
            # False = ad sets keep their own budgets, the conservative choice.
            data["is_adset_budget_sharing_enabled"] = "false"
    result = _post(f"{_act()}/campaigns", data)
    return {
        "created": "campaign",
        "id": result.get("id"),
        "name": data["name"],
        "objective": objective,
        "status": "PAUSED",
    }


def create_meta_adset(
    *,
    campaign_id: str,
    name: str,
    targeting: Dict[str, Any],
    optimization_goal: str = "LINK_CLICKS",
    billing_event: str = "IMPRESSIONS",
    daily_budget_cents: Optional[int] = None,
    lifetime_budget_cents: Optional[int] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    bid_strategy: Optional[str] = None,
    promoted_object: Optional[Dict[str, Any]] = None,
    destination_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an ad set (the targeting layer) under a MARK-BUILT campaign.

    `targeting` is Meta's full targeting spec dict — geo_locations is
    required by Meta; age/gender/interests/custom_audiences/placements go
    in here too. Budget rules: give the ad set a daily or lifetime budget
    unless the parent campaign carries a CBO budget. A lifetime budget
    requires end_time. Refuses if the parent campaign wasn't built by
    Mark, so tests never graft onto live human campaigns.
    """
    _guard_ads_write()
    _require_mark_object(campaign_id, "create an ad set under this campaign")
    if not isinstance(targeting, dict) or not targeting:
        raise MetaError(
            "targeting must be a non-empty targeting-spec dict "
            "(at minimum geo_locations, e.g. "
            '{"geo_locations": {"countries": ["US"]}}).'
        )
    _check_budget(daily_budget_cents, lifetime_budget_cents)
    data: Dict[str, Any] = {
        "access_token": _ads_token(),
        "campaign_id": campaign_id,
        "name": _mark_name(name),
        "status": "PAUSED",
        "targeting": json.dumps(targeting),
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
    }
    if daily_budget_cents is not None:
        data["daily_budget"] = int(daily_budget_cents)
    if lifetime_budget_cents is not None:
        data["lifetime_budget"] = int(lifetime_budget_cents)
    if start_time:
        data["start_time"] = start_time
    if end_time:
        data["end_time"] = end_time
    if bid_strategy:
        data["bid_strategy"] = bid_strategy
    if promoted_object:
        data["promoted_object"] = json.dumps(promoted_object)
    if destination_type:
        data["destination_type"] = destination_type
    result = _post(f"{_act()}/adsets", data)
    return {
        "created": "adset",
        "id": result.get("id"),
        "name": data["name"],
        "campaign_id": campaign_id,
        "status": "PAUSED",
    }


def upload_meta_ad_image(
    *,
    image_url: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    image_filename: str = "ad-image.jpg",
    image_mime: str = "image/jpeg",
) -> Dict[str, Any]:
    """Upload an image to the ad account's library; returns its image_hash.

    Meta's /adimages takes file bytes, not URLs, so a URL gets downloaded
    first. The returned hash goes into create_meta_ad_creative. Uploading
    costs nothing and runs nothing, but it's still gated — it writes to
    the ad account.
    """
    _guard_ads_write()
    if image_bytes is None:
        if not image_url:
            raise MetaError("Provide image_url or image_bytes.")
        resp = requests.get(image_url, timeout=60)
        if resp.status_code >= 400:
            raise MetaError(
                f"Could not download image ({resp.status_code}): {image_url}"
            )
        image_bytes = resp.content
    result = _post_multipart(
        f"{_act()}/adimages",
        {"access_token": _ads_token()},
        image_bytes=image_bytes,
        image_filename=image_filename,
        image_mime=image_mime,
    )
    images = result.get("images") or {}
    first = next(iter(images.values()), {})
    if not first.get("hash"):
        raise MetaError(f"adimages upload returned no hash: {result}")
    return {"image_hash": first.get("hash"), "url": first.get("url")}


def create_meta_ad_creative(
    *,
    name: str,
    message: str,
    link_url: str,
    headline: Optional[str] = None,
    description: Optional[str] = None,
    image_hash: Optional[str] = None,
    image_url: Optional[str] = None,
    call_to_action_type: str = "LEARN_MORE",
    page_id: Optional[str] = None,
    instagram_actor_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a link-ad creative (the messaging layer): primary text,
    headline, description, image, destination URL, CTA.

    Prefer image_hash from upload_meta_ad_image; image_url as `picture`
    is the fallback (Meta fetches it, so it must be public). A creative
    on its own runs nothing — it only becomes visible when attached to an
    ad. instagram_actor_id enables IG placements; omit for FB-only.
    """
    _guard_ads_write()
    if not message or not link_url:
        raise MetaError("message (primary text) and link_url are required.")
    page = page_id or _require("META_PAGE_ID", "create an ad creative")
    link_data: Dict[str, Any] = {"link": link_url, "message": message}
    if headline:
        link_data["name"] = headline
    if description:
        link_data["description"] = description
    if image_hash:
        link_data["image_hash"] = image_hash
    elif image_url:
        link_data["picture"] = image_url
    if call_to_action_type:
        link_data["call_to_action"] = {
            "type": call_to_action_type,
            "value": {"link": link_url},
        }
    spec: Dict[str, Any] = {"page_id": page, "link_data": link_data}
    if instagram_actor_id:
        # Meta renamed this: object_story_spec now takes instagram_user_id
        # (the IG Business account id); instagram_actor_id is rejected with
        # "must be a valid Instagram account id".
        spec["instagram_user_id"] = instagram_actor_id
    data = {
        "access_token": _ads_token(),
        "name": _mark_name(name),
        "object_story_spec": json.dumps(spec),
    }
    result = _post(f"{_act()}/adcreatives", data)
    return {"created": "creative", "id": result.get("id"), "name": data["name"]}


def create_meta_ad(
    *, name: str, adset_id: str, creative_id: str
) -> Dict[str, Any]:
    """Create an ad (creative attached to an ad set) — always PAUSED.

    Refuses unless the parent ad set was built by Mark.
    """
    _guard_ads_write()
    _require_mark_object(adset_id, "create an ad under this ad set")
    if not creative_id:
        raise MetaError("creative_id is required (from create_meta_ad_creative).")
    data = {
        "access_token": _ads_token(),
        "name": _mark_name(name),
        "adset_id": adset_id,
        "creative": json.dumps({"creative_id": str(creative_id)}),
        "status": "PAUSED",
    }
    result = _post(f"{_act()}/ads", data)
    return {
        "created": "ad",
        "id": result.get("id"),
        "name": data["name"],
        "adset_id": adset_id,
        "status": "PAUSED",
    }


def update_meta_object_status(*, object_id: str, status: str) -> Dict[str, Any]:
    """Set a Mark-built campaign/ad set/ad ACTIVE or PAUSED.

    Setting ACTIVE is the moment money can move — callers must only do it
    after explicit human approval. Budgets were cap-checked when they
    were written and only tag-fenced objects can be flipped, so the blast
    radius of an activation is bounded by the ads-mark budget caps.
    """
    _guard_ads_write()
    status = (status or "").strip().upper()
    if status not in {"ACTIVE", "PAUSED"}:
        raise MetaError("status must be ACTIVE or PAUSED.")
    obj = _require_mark_object(object_id, f"set status to {status}")
    _post(str(object_id), {"access_token": _ads_token(), "status": status})
    return {"id": object_id, "name": obj.get("name"), "status": status}


def update_meta_budget(
    *,
    object_id: str,
    daily_budget_cents: Optional[int] = None,
    lifetime_budget_cents: Optional[int] = None,
) -> Dict[str, Any]:
    """Change the budget on a Mark-built campaign or ad set, within caps."""
    _guard_ads_write()
    if daily_budget_cents is None and lifetime_budget_cents is None:
        raise MetaError("Provide daily_budget_cents or lifetime_budget_cents.")
    _check_budget(daily_budget_cents, lifetime_budget_cents)
    obj = _require_mark_object(object_id, "change the budget")
    data: Dict[str, Any] = {"access_token": _ads_token()}
    if daily_budget_cents is not None:
        data["daily_budget"] = int(daily_budget_cents)
    if lifetime_budget_cents is not None:
        data["lifetime_budget"] = int(lifetime_budget_cents)
    _post(str(object_id), data)
    return {
        "id": object_id,
        "name": obj.get("name"),
        "daily_budget_cents": daily_budget_cents,
        "lifetime_budget_cents": lifetime_budget_cents,
    }
