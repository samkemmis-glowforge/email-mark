"""Glowforge community forum fetcher.

community.glowforge.com runs Discourse, which exposes a JSON API: append
`.json` to any topic URL and you get structured topic data — title, posts,
author, "cooked" HTML body — without scraping the rendered page.

Used by the ICYMI workflow: Mark gets 3 forum URLs from the user, calls
fetch_forum_post on each, and uses the returned title / author / body /
image URLs to draft the weekly project highlight email.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import requests

COMMUNITY_DOMAIN = "community.glowforge.com"
USER_AGENT = "email-mark/1.0 (Glowforge marketing bot)"


def _is_community_url(url: str) -> bool:
    """Return True only if url is on community.glowforge.com."""
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return False
    return netloc == COMMUNITY_DOMAIN or netloc.endswith("." + COMMUNITY_DOMAIN)


def _strip_tags(html: str) -> str:
    """Crude HTML → text. Convert <p>/<br> to newlines, drop other tags."""
    text = re.sub(r"</p\s*>", "\n\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    # Collapse runs of blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_image_urls(html: str, base_url: str) -> List[str]:
    """Pull image src URLs out of cooked Discourse HTML.

    Discourse renders user images as full-size links wrapped around a
    thumbnail. We grab every <img src=...> that isn't an emoji or avatar,
    then resolve any relative URLs against the topic URL so the agent can
    paste them directly into HubSpot.
    """
    urls: List[str] = []
    for match in re.finditer(r'<img[^>]+src="([^"]+)"', html, flags=re.IGNORECASE):
        src = unescape(match.group(1))
        # Discourse uses /images/emoji/ for emoji and /user_avatar/ for
        # author headshots — neither is useful for the email.
        if "/images/emoji/" in src or "/user_avatar/" in src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = urljoin(base_url, src)
        urls.append(src)

    # De-dup while preserving order.
    seen: set = set()
    ordered: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def _topic_json_url(url: str) -> str:
    """Convert a Discourse topic URL into its JSON endpoint.

    Topic URLs look like /t/<slug>/<topic_id> or
    /t/<slug>/<topic_id>/<post_number>. We trim to the topic level and
    append `.json`.
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "t":
        parts = parts[:3]  # ['t', '<slug>', '<topic_id>']
    path = "/" + "/".join(parts)
    return f"{parsed.scheme or 'https'}://{parsed.netloc}{path}.json"


def fetch_forum_post(url: str) -> Dict[str, Any]:
    """Fetch a community.glowforge.com topic and return the fields useful for ICYMI.

    Returns:
        {
            "title": "<topic title>",
            "author": "<original poster's username>",
            "url": "<canonical topic URL>",
            "body_text": "<first post body, HTML-stripped>",
            "image_urls": ["<full-size image URLs from the first post>", ...],
        }

    On error returns {"error": "..."} so the agent can recover gracefully.
    """
    url = (url or "").strip()
    if not url:
        return {"error": "Empty URL."}
    if not _is_community_url(url):
        return {
            "error": (
                f"URL is not on {COMMUNITY_DOMAIN}: {url}. "
                "ICYMI only pulls from the Glowforge community forum."
            )
        }

    json_url = _topic_json_url(url)

    try:
        response = requests.get(
            json_url,
            timeout=20,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return {"error": f"Failed to fetch {json_url}: {exc}"}

    title = data.get("title") or data.get("fancy_title") or ""
    posts = (data.get("post_stream") or {}).get("posts") or []
    if not posts:
        return {
            "title": title,
            "author": "",
            "url": url,
            "body_text": "",
            "image_urls": [],
            "warning": "Topic returned no posts.",
        }

    first = posts[0]
    author = first.get("username") or first.get("name") or ""
    cooked_html = first.get("cooked") or ""
    body_text = _strip_tags(cooked_html)
    image_urls = _extract_image_urls(cooked_html, url)

    return {
        "title": title,
        "author": author,
        "url": url,
        "body_text": body_text,
        "image_urls": image_urls,
    }
