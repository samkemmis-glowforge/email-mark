"""Reads the posting-cadence content calendar (a Google Sheet).

The calendar is the source of truth for WHAT to post and WHEN. Each row
is one scheduled item with columns roughly:

    Week # | Week Of (Mon) | Key Dates This Week | Date | Day | Type |
    Theme | Hook / Caption Angle | Audience Focus | Product Focus |
    Asset Link (Drive)

social-mark only acts on rows whose Type contains "Social" (e.g.
"Social Post") — the Email / ICYMI rows belong to email-mark.

Two read paths:
  1. Google Sheets API via a service-account key (GOOGLE_APPLICATION_CREDENTIALS).
  2. Fallback: the sheet's public CSV export (requires "anyone with the
     link can view"). Used when no creds are configured.

Public entrypoints:
    fetch_rows() -> List[CalendarRow]
    get_upcoming_social_posts(within_days=10, today=None) -> List[CalendarRow]
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

SHEET_ID = os.environ.get(
    "CONTENT_CALENDAR_SHEET_ID", "1eYfKNUu-TPwVd9EpT2rUfrv20S7fWD3N7gr_3g2aKsE"
)
SHEET_GID = os.environ.get("CONTENT_CALENDAR_GID", "0")

# Map of normalized header text -> the field name we expose. We match on a
# substring so minor header edits ("Hook / Caption Angle" vs "Caption Angle")
# don't break parsing.
_HEADER_ALIASES = {
    "week #": "week",
    "week of": "week_of",
    "key dates": "key_dates",
    "date": "date_raw",
    "day": "day",
    "type": "type",
    "theme": "theme",
    "hook": "caption_angle",
    "caption": "caption_angle",
    "audience": "audience",
    "product": "product",
    "asset": "asset_links_raw",
}

_DRIVE_URL_RE = re.compile(r"https?://\S+")


@dataclass
class CalendarRow:
    week: str = ""
    week_of: str = ""
    key_dates: str = ""
    date_raw: str = ""
    day: str = ""
    type: str = ""
    theme: str = ""
    caption_angle: str = ""
    audience: str = ""
    product: str = ""
    asset_links_raw: str = ""
    # Derived:
    post_date: Optional[date] = None  # parsed calendar date
    asset_links: Optional[List[str]] = None  # extracted Drive URLs

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["post_date"] = self.post_date.isoformat() if self.post_date else None
        return d

    @property
    def is_social(self) -> bool:
        return "social" in (self.type or "").lower()


# ---------------------------------------------------------------------------
# Date parsing — sheet dates look like "Apr 6", "May 1" (no year). We attach
# the most sensible year so they sort and compare correctly.
# ---------------------------------------------------------------------------


def _parse_post_date(date_raw: str, *, reference: date) -> Optional[date]:
    """Parse a 'Mon DD' style cell into a date near `reference`.

    The sheet omits the year. We assume the reference year, then correct
    for a December/January wrap so a 'Jan' row read in December lands next
    year (and vice versa).
    """
    text = (date_raw or "").strip()
    if not text:
        return None
    # Strip a leading weekday if present ("Mon, Apr 6" / "Apr 6").
    text = re.sub(r"^[A-Za-z]+,\s*", "", text)
    for fmt in ("%b %d", "%B %d", "%b %d %Y", "%B %d %Y", "%m/%d/%Y", "%m/%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        year = parsed.year if parsed.year != 1900 else reference.year
        candidate = date(year, parsed.month, parsed.day)
        if parsed.year == 1900:
            # Correct for year-boundary wrap (>6 months away = wrong year).
            if (candidate - reference).days < -180:
                candidate = date(year + 1, parsed.month, parsed.day)
            elif (candidate - reference).days > 180:
                candidate = date(year - 1, parsed.month, parsed.day)
        return candidate
    return None


# ---------------------------------------------------------------------------
# Raw sheet fetch
# ---------------------------------------------------------------------------


def _fetch_csv_rows() -> List[List[str]]:
    """Fetch the sheet via its public CSV export endpoint."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/export?format=csv&gid={SHEET_GID}"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    if "text/csv" not in resp.headers.get("content-type", ""):
        raise RuntimeError(
            "Calendar CSV export did not return CSV — the sheet is probably "
            "not shared 'anyone with the link can view'. Either share it or "
            "set GOOGLE_APPLICATION_CREDENTIALS for authenticated access."
        )
    return list(csv.reader(io.StringIO(resp.text)))


def _fetch_api_rows() -> List[List[str]]:
    """Fetch the sheet via the Google Sheets API with a service account."""
    from google.oauth2 import service_account  # lazy import
    from googleapiclient.discovery import build

    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    creds = service_account.Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    # Resolve the tab name for the configured gid.
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    tab_title = None
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if str(props.get("sheetId")) == str(SHEET_GID):
            tab_title = props.get("title")
            break
    rng = f"'{tab_title}'" if tab_title else "A:Z"
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=rng)
        .execute()
    )
    return result.get("values", [])


def _map_headers(header_row: List[str]) -> Dict[int, str]:
    """Map column index -> CalendarRow field name using header aliases."""
    mapping: Dict[int, str] = {}
    for idx, raw in enumerate(header_row):
        norm = (raw or "").strip().lower()
        for needle, field in _HEADER_ALIASES.items():
            if needle in norm:
                # Don't clobber an earlier, more specific match.
                if idx not in mapping:
                    mapping[idx] = field
                break
    return mapping


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_rows(*, reference: Optional[date] = None) -> List[CalendarRow]:
    """Fetch and parse all calendar rows.

    Uses the Sheets API when GOOGLE_APPLICATION_CREDENTIALS is set,
    otherwise the public CSV export.
    """
    reference = reference or date.today()
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        grid = _fetch_api_rows()
    else:
        grid = _fetch_csv_rows()

    if not grid:
        return []

    # Find the header row — the first row that mentions both "Date" and "Type".
    header_idx = 0
    for i, row in enumerate(grid[:10]):
        joined = " ".join(c.lower() for c in row)
        if "date" in joined and "type" in joined:
            header_idx = i
            break
    header_map = _map_headers(grid[header_idx])

    rows: List[CalendarRow] = []
    for raw in grid[header_idx + 1 :]:
        if not any((c or "").strip() for c in raw):
            continue
        row = CalendarRow()
        for idx, field in header_map.items():
            if idx < len(raw):
                setattr(row, field, (raw[idx] or "").strip())
        row.post_date = _parse_post_date(row.date_raw, reference=reference)
        row.asset_links = _DRIVE_URL_RE.findall(row.asset_links_raw or "")
        rows.append(row)
    return rows


def get_upcoming_social_posts(
    *, within_days: int = 10, today: Optional[date] = None
) -> List[CalendarRow]:
    """Return Social-Post rows scheduled from `today` through the window.

    Sorted by date. Excludes Email / ICYMI rows (those are email-mark's).
    """
    today = today or date.today()
    rows = fetch_rows(reference=today)
    upcoming = [
        r
        for r in rows
        if r.is_social
        and r.post_date is not None
        and 0 <= (r.post_date - today).days <= within_days
    ]
    upcoming.sort(key=lambda r: r.post_date or date.max)
    return upcoming
