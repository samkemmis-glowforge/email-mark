"""Reddit Ads API connector — reads + gated writes.

Mirrors meta_client.py's architecture and safety posture:

  READING (reporting):
    - list_reddit_campaigns / list_reddit_ad_groups / list_reddit_ads
    - get_reddit_ad_performance     spend/impressions/clicks via reports
    - reddit_read_api               raw GET fallback

  WRITING (campaign builds) — GATED behind ADS_MARK_ALLOW_WRITE, same
  gate as the Meta write path, with the same three guardrails enforced
  in code:
    1. GATE.   Refuses unless ADS_MARK_ALLOW_WRITE == "true".
    2. PAUSED. Campaigns/ad groups/ads are created PAUSED. Activation
               (update_reddit_object_status) is a separate explicit call.
    3. FENCE.  Created objects get ADS_MARK_NAME_PREFIX prepended and
               mutations refuse on any object whose name lacks the tag.
  Budget caps reuse ADS_MARK_MAX_DAILY_BUDGET_CENTS. Reddit budgets are
  in MICRO-currency (1 USD = 1,000,000); helpers convert from cents.

Auth: OAuth2 refresh-token flow. One-time bootstrap mints the refresh
token (scripts/reddit_oauth_bootstrap.py); after that this module
exchanges it for a ~24h access token on demand and caches it in-process.

Env: REDDIT_ADS_CLIENT_ID, REDDIT_ADS_CLIENT_SECRET,
     REDDIT_ADS_REFRESH_TOKEN, REDDIT_AD_ACCOUNT_ID.

NOTE: the v3 request/response schemas here (field names, {"data": ...}
envelope) follow Reddit's published docs but were written before the
first authenticated call — expect to adjust details when smoke-testing,
the same way the Meta client needed is_adset_budget_sharing_enabled and
instagram_user_id fixes only live traffic could reveal.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

ADS_API_BASE = "https://ads-api.reddit.com/api/v3"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
USER_AGENT = "email-mark/1.0 (Glowforge marketing bot)"


class RedditError(RuntimeError):
    """Raised when the Ads API returns an error or required config is missing."""


def _require(env_var: str, human: str) -> str:
    val = os.environ.get(env_var)
    if not val:
        raise RedditError(f"{env_var} not set — needed to {human}.")
    return val


# --- auth -------------------------------------------------------------------

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0.0}

# Access tokens live ~1h but our processes are short-lived, so an
# in-memory cache alone means one token-endpoint hit per process. Reddit's
# token endpoint is anti-abuse-sensitive (opaque 400s after bursts of
# refreshes — learned live), so the cache is also persisted to a
# gitignored file next to .env and shared across processes. The token
# endpoint is then touched at most ~once an hour.
_CACHE_FILENAME = ".reddit_token_cache.json"


def _cache_path() -> Optional[str]:
    env_path = find_dotenv()
    if env_path:
        return os.path.join(os.path.dirname(env_path), _CACHE_FILENAME)
    return None


def _load_disk_cache() -> None:
    path = _cache_path()
    if not path or not os.path.isfile(path):
        return
    try:
        import json as _json

        with open(path) as fh:
            data = _json.load(fh)
        if data.get("access_token") and float(data.get("expires_at", 0)) > time.time():
            _token_cache.update(
                access_token=data["access_token"], expires_at=float(data["expires_at"])
            )
    except (ValueError, OSError):
        pass


def save_token_cache(access_token: str, expires_in: int) -> None:
    """Persist an access token for cross-process reuse. Also used by the
    bootstrap script so the exchange's own access token isn't wasted."""
    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = time.time() + int(expires_in)
    path = _cache_path()
    if not path:
        return
    try:
        import json as _json

        with open(path, "w") as fh:
            _json.dump(
                {"access_token": access_token, "expires_at": _token_cache["expires_at"]},
                fh,
            )
    except OSError:
        pass


def _access_token() -> str:
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]
    _load_disk_cache()
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]
    client_id = _require("REDDIT_ADS_CLIENT_ID", "authenticate to the Reddit Ads API")
    client_secret = _require("REDDIT_ADS_CLIENT_SECRET", "authenticate to the Reddit Ads API")
    refresh_token = _require("REDDIT_ADS_REFRESH_TOKEN", "authenticate to the Reddit Ads API")
    resp = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    try:
        payload = resp.json()
    except ValueError:
        raise RedditError(f"Token refresh non-JSON (HTTP {resp.status_code}): {resp.text[:300]}")
    if resp.status_code >= 400 or "access_token" not in payload:
        raise RedditError(
            f"Token refresh failed (HTTP {resp.status_code}): {payload}. If this "
            f"persists, the refresh token was likely rotated and lost — re-run "
            f"scripts/reddit_oauth_bootstrap.py to mint a new one."
        )
    save_token_cache(payload["access_token"], int(payload.get("expires_in", 3600)))
    # Reddit ROTATES refresh tokens: a refresh response may carry a new
    # refresh_token, and the old one is eventually invalidated. Losing the
    # rotated value bricks auth (400 Bad Request on the next refresh), so
    # persist it: process env always; the .env file when one exists.
    rotated = payload.get("refresh_token")
    if rotated and rotated != refresh_token:
        os.environ["REDDIT_ADS_REFRESH_TOKEN"] = rotated
        env_path = find_dotenv()
        if env_path and os.path.isfile(env_path):
            try:
                with open(env_path) as fh:
                    lines = fh.readlines()
                key = "REDDIT_ADS_REFRESH_TOKEN="
                lines = [l for l in lines if not l.startswith(key)]
                lines.append(f"{key}{rotated}\n")
                with open(env_path, "w") as fh:
                    fh.writelines(lines)
            except OSError:
                pass
        print(
            "[reddit_client] refresh token ROTATED — new value persisted to "
            "env/.env; update any external secret store (Render, environment "
            "settings) with the new REDDIT_ADS_REFRESH_TOKEN.",
            flush=True,
        )
    return _token_cache["access_token"]


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


def _handle(resp: requests.Response) -> Dict[str, Any]:
    try:
        payload = resp.json()
    except ValueError:
        raise RedditError(f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:300]}")
    if resp.status_code >= 400:
        raise RedditError(f"Ads API error HTTP {resp.status_code}: {str(payload)[:500]}")
    return payload if isinstance(payload, dict) else {"data": payload}


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = requests.get(
        f"{ADS_API_BASE}/{path.lstrip('/')}", params=params, headers=_headers(), timeout=30
    )
    return _handle(resp)


def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    resp = requests.post(
        f"{ADS_API_BASE}/{path.lstrip('/')}", json={"data": body}, headers=_headers(), timeout=60
    )
    return _handle(resp)


def _patch(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    resp = requests.patch(
        f"{ADS_API_BASE}/{path.lstrip('/')}", json={"data": body}, headers=_headers(), timeout=60
    )
    return _handle(resp)


def _account() -> str:
    return _require("REDDIT_AD_ACCOUNT_ID", "reach the Reddit ad account")


# --- shared guardrails (same env knobs as the Meta write path) --------------


def ads_write_enabled() -> bool:
    return os.environ.get("ADS_MARK_ALLOW_WRITE", "").strip().lower() == "true"


def _guard_write() -> None:
    if not ads_write_enabled():
        raise RedditError(
            "Ads writes are disabled (ADS_MARK_ALLOW_WRITE != 'true'). "
            "Hand the draft to a human to build in Reddit Ads Manager."
        )


def _mark_prefix() -> str:
    return os.environ.get("ADS_MARK_NAME_PREFIX", "[mark]").strip() or "[mark]"


def _mark_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise RedditError("A non-empty name is required.")
    prefix = _mark_prefix()
    return name if name.startswith(prefix) else f"{prefix} {name}"


def _daily_cap_cents() -> int:
    return int(os.environ.get("ADS_MARK_MAX_DAILY_BUDGET_CENTS", "5000"))


def _cents_to_micros(cents: int) -> int:
    # Reddit budgets/bids are micro-currency: $1 = 1,000,000. 1 cent = 10,000.
    return int(cents) * 10_000


def _check_daily_budget(cents: Optional[int]) -> None:
    if cents is None:
        return
    if int(cents) <= 0:
        raise RedditError("daily_budget_cents must be a positive integer.")
    cap = _daily_cap_cents()
    if int(cents) > cap:
        raise RedditError(
            f"daily_budget_cents={cents} exceeds the ads-mark cap of {cap} "
            f"cents/day (ADS_MARK_MAX_DAILY_BUDGET_CENTS)."
        )


_TYPE_PATHS = {"campaign": "campaigns", "ad_group": "ad_groups", "ad": "ads"}


def _require_mark_object(object_type: str, object_id: str, action: str) -> Dict[str, Any]:
    if object_type not in _TYPE_PATHS:
        raise RedditError(f"object_type must be one of {sorted(_TYPE_PATHS)}.")
    obj = _get(f"{_TYPE_PATHS[object_type]}/{object_id}").get("data", {})
    name = obj.get("name") or ""
    prefix = _mark_prefix()
    if not name.startswith(prefix):
        raise RedditError(
            f"Refusing to {action}: {object_type} {object_id} ({name!r}) was "
            f"not created by ads-mark (name lacks the {prefix!r} tag)."
        )
    return obj


# --- READING ----------------------------------------------------------------


def list_reddit_campaigns(*, limit: int = 100) -> Dict[str, Any]:
    data = _get(f"ad_accounts/{_account()}/campaigns", {"page.size": min(limit, 100)})
    return {"campaigns": data.get("data", []), "pagination": data.get("pagination", {})}


def list_reddit_ad_groups(*, campaign_id: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    params: Dict[str, Any] = {"page.size": min(limit, 100)}
    data = _get(f"ad_accounts/{_account()}/ad_groups", params)
    groups = data.get("data", [])
    if campaign_id:
        groups = [g for g in groups if str(g.get("campaign_id")) == str(campaign_id)]
    return {"ad_groups": groups, "pagination": data.get("pagination", {})}


def list_reddit_ads(*, ad_group_id: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    params: Dict[str, Any] = {"page.size": min(limit, 100)}
    data = _get(f"ad_accounts/{_account()}/ads", params)
    ads = data.get("data", [])
    if ad_group_id:
        ads = [a for a in ads if str(a.get("ad_group_id")) == str(ad_group_id)]
    return {"ads": ads, "pagination": data.get("pagination", {})}


def get_reddit_ad_performance(
    *,
    starts_at: str,
    ends_at: str,
    level: str = "campaign",
) -> Dict[str, Any]:
    """Spend/impressions/clicks report. Dates are ISO YYYY-MM-DD (UTC).

    level: 'campaign' | 'ad_group' | 'ad' — the breakdown dimension.
    """
    breakdowns = {"campaign": "CAMPAIGN_ID", "ad_group": "AD_GROUP_ID", "ad": "AD_ID"}
    if level not in breakdowns:
        raise RedditError("level must be campaign | ad_group | ad.")
    body = {
        "breakdowns": [breakdowns[level]],
        "fields": ["SPEND", "IMPRESSIONS", "CLICKS", "CPC", "CTR"],
        "starts_at": f"{starts_at}T00:00:00Z",
        "ends_at": f"{ends_at}T00:00:00Z",
        "time_zone_id": "GMT",
    }
    data = _post(f"ad_accounts/{_account()}/reports", body)
    return {"level": level, "rows": data.get("data", {})}


def reddit_read_api(*, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Raw read-only GET against the Ads API — fallback for anything not
    covered above (profiles, posts, pixels, targeting metadata like
    community/interest search). Never performs writes.
    """
    if not path:
        raise RedditError("reddit_read_api: path is required.")
    return _get(path, params)


# --- WRITING (GATED) ---------------------------------------------------------

# Verified against the live API (error message enumerates the valid set).
_VALID_OBJECTIVES = {
    "APP_INSTALLS",
    "CATALOG_SALES",
    "CLICKS",
    "CONVERSIONS",
    "IMPRESSIONS",
    "LEAD_GENERATION",
    "VIDEO_VIEWABLE_IMPRESSIONS",
}


def create_reddit_campaign(*, name: str, objective: str = "CLICKS") -> Dict[str, Any]:
    """Create a campaign — PAUSED, name-tagged. Budget lives on ad groups."""
    _guard_write()
    objective = (objective or "").strip().upper()
    if objective not in _VALID_OBJECTIVES:
        raise RedditError(f"objective must be one of {sorted(_VALID_OBJECTIVES)}.")
    body = {
        "name": _mark_name(name),
        "objective": objective,
        "configured_status": "PAUSED",
    }
    result = _post(f"ad_accounts/{_account()}/campaigns", body).get("data", {})
    return {
        "created": "campaign",
        "id": result.get("id"),
        "name": body["name"],
        "objective": objective,
        "status": "PAUSED",
    }


def create_reddit_ad_group(
    *,
    campaign_id: str,
    name: str,
    daily_budget_cents: int,
    communities: Optional[List[str]] = None,
    interests: Optional[List[str]] = None,
    geolocations: Optional[List[str]] = None,
    bid_value_cents: Optional[int] = None,
    bid_type: str = "CPC",
    bid_strategy: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    conversion_pixel_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an ad group (targeting + budget layer) under a MARK campaign.

    communities: subreddit names without the r/ (e.g. ["lasercutting"]).
    interests: Reddit interest-taxonomy ids (discover via reddit_read_api
        on targeting metadata endpoints).
    geolocations: ISO country/region codes, default ["US"].
    Budget/bids in cents, converted to Reddit micros; capped.
    """
    _guard_write()
    _require_mark_object("campaign", campaign_id, "create an ad group under this campaign")
    _check_daily_budget(daily_budget_cents)
    if not communities and not interests:
        raise RedditError("Provide communities and/or interests targeting.")
    targeting: Dict[str, Any] = {"geolocations": geolocations or ["US"]}
    if communities:
        targeting["communities"] = communities
    if interests:
        targeting["interests"] = interests
    # bid_strategy + bid_type are required by the live API (400 without
    # them). Default: auto-bid CPC; manual only when a bid value is given.
    if bid_strategy is None:
        bid_strategy = "MANUAL_BIDDING" if bid_value_cents is not None else "MAXIMIZE_VOLUME"
    body: Dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": _mark_name(name),
        "configured_status": "PAUSED",
        "goal_type": "DAILY_SPEND",
        "goal_value": _cents_to_micros(daily_budget_cents),
        "bid_type": bid_type,
        "bid_strategy": bid_strategy,
        "targeting": targeting,
    }
    # start_time is required by the live API ("Input should be a valid
    # datetime"). Default to now (UTC) — PAUSED status still controls
    # when anything actually serves.
    if not start_time:
        from datetime import datetime, timezone

        start_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body["start_time"] = start_time
    if end_time:
        body["end_time"] = end_time
    if bid_value_cents is not None:
        if bid_value_cents <= 0:
            raise RedditError("bid_value_cents must be positive.")
        body["bid_value"] = _cents_to_micros(bid_value_cents)
    if conversion_pixel_id:
        body["conversion_pixel_id"] = conversion_pixel_id
    result = _post(f"ad_accounts/{_account()}/ad_groups", body).get("data", {})
    return {
        "created": "ad_group",
        "id": result.get("id"),
        "name": body["name"],
        "campaign_id": campaign_id,
        "status": "PAUSED",
    }


def create_reddit_ad_post(
    *,
    profile_id: str,
    headline: str,
    image_url: str,
    destination_url: str,
) -> Dict[str, Any]:
    """Create an IMAGE ad post (the creative) under the advertiser profile.

    Schema verified against the live API:
      - Reddit ads must reference a post ('invalid post ID' otherwise);
        valid post types are CAROUSEL/IMAGE/TEXT/VIDEO.
      - Click-through ads need an IMAGE post: TEXT posts are 'free form
        ads' that cannot carry a click url.
      - The destination rides INSIDE the content item:
        content=[{media_url, destination_url}]. Reddit downloads the
        image from media_url and re-hosts it on i.redd.it.
      - call_to_action belongs to the AD (create_reddit_ad), not the post.

    Ad posts only reach feeds when a paying ad serves them, so the
    PAUSED/fence guardrails on the ad + ad group control exposure. Gated
    because it publishes advertiser content under the profile. The
    headline is public-facing ad copy, so it is NOT name-tagged.

    Discover profile_id via reddit_read_api on
    'ad_accounts/{account}/profiles'.
    """
    _guard_write()
    if not headline or not image_url or not destination_url:
        raise RedditError("headline, image_url and destination_url are required.")
    payload: Dict[str, Any] = {
        "type": "IMAGE",
        "headline": headline,
        "content": [{"media_url": image_url, "destination_url": destination_url}],
    }
    result = _post(f"profiles/{profile_id}/posts", payload).get("data", {})
    content = (result.get("content") or [{}])[0]
    return {
        "created": "post",
        "id": result.get("id"),
        "headline": headline,
        "media_url": content.get("media_url"),
        "destination_url": content.get("destination_url"),
    }


def create_reddit_ad(
    *,
    ad_group_id: str,
    name: str,
    post_id: Optional[str] = None,
    headline: Optional[str] = None,
    destination_url: Optional[str] = None,
    call_to_action: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    profile_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an ad — PAUSED — under a MARK ad group.

    Two modes:
      - post_id given: promote an existing Reddit post (created in Ads
        Manager or via the posts endpoint).
      - no post_id: attempt an inline creative (headline + destination_url
        [+ thumbnail]); the API creates the underlying promoted post. If
        the account requires pre-created posts this errors with Reddit's
        message — create the post first and pass post_id.
    """
    _guard_write()
    _require_mark_object("ad_group", ad_group_id, "create an ad under this ad group")
    body: Dict[str, Any] = {
        "ad_group_id": ad_group_id,
        "name": _mark_name(name),
        "configured_status": "PAUSED",
    }
    if post_id:
        body["post_id"] = post_id
    else:
        if not headline or not destination_url:
            raise RedditError("Without post_id, headline and destination_url are required.")
        creative: Dict[str, Any] = {"headline": headline, "destination_url": destination_url}
        if thumbnail_url:
            creative["thumbnail_url"] = thumbnail_url
        if profile_id:
            creative["profile_id"] = profile_id
        body["creative"] = creative
    if destination_url and post_id:
        body["click_url"] = destination_url
    if call_to_action:
        body["call_to_action"] = call_to_action
    result = _post(f"ad_accounts/{_account()}/ads", body).get("data", {})
    return {
        "created": "ad",
        "id": result.get("id"),
        "name": body["name"],
        "ad_group_id": ad_group_id,
        "status": "PAUSED",
    }


def update_reddit_object_status(
    *, object_type: str, object_id: str, status: str
) -> Dict[str, Any]:
    """Set a MARK campaign/ad_group/ad ACTIVE or PAUSED. ACTIVE starts
    spend — only call after explicit human approval in conversation.
    """
    _guard_write()
    status = (status or "").strip().upper()
    if status not in {"ACTIVE", "PAUSED"}:
        raise RedditError("status must be ACTIVE or PAUSED.")
    obj = _require_mark_object(object_type, object_id, f"set status to {status}")
    _patch(f"{_TYPE_PATHS[object_type]}/{object_id}", {"configured_status": status})
    return {"id": object_id, "name": obj.get("name"), "status": status}


def update_reddit_ad_group_budget(
    *, ad_group_id: str, daily_budget_cents: int
) -> Dict[str, Any]:
    """Change a MARK ad group's daily budget, within the shared cap."""
    _guard_write()
    _check_daily_budget(daily_budget_cents)
    obj = _require_mark_object("ad_group", ad_group_id, "change the budget")
    # PATCH accepts only the changed value — resending goal_type errors
    # with 'Unknown Attribute' (verified live).
    _patch(
        f"ad_groups/{ad_group_id}",
        {"goal_value": _cents_to_micros(daily_budget_cents)},
    )
    return {
        "id": ad_group_id,
        "name": obj.get("name"),
        "daily_budget_cents": daily_budget_cents,
    }
