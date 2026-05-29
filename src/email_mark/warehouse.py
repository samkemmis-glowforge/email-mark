"""BigQuery warehouse access for Glowforge marketing.

Patterns lifted from glowforge/hubspot-ticket-analysis.

Authentication:
- Locally: `gcloud auth application-default login` (uses your Glowforge identity).
  No env var needed; the BigQuery SDK picks up Application Default Credentials.
- On Render or other non-GCP hosts: paste the full JSON of a service-account key
  into the env var `GCP_SERVICE_ACCOUNT_JSON`. The account must have
  `roles/bigquery.dataViewer` (or equivalent) on the Glowforge data projects.

The account needs read access to:
- glowforge-data-production  (dbt mart + reporting — most of what we want)
- glowforge-production        (live app data, prints)
- glowforge-dev               (machine/user syncs)

Privacy: all functions in this module return AGGREGATE data only. No
individual user PII (names, emails, phone, addresses) is ever returned to
the agent. Counts, percentages, and distributions only.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import find_dotenv, load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account

load_dotenv(find_dotenv())

# Safety limits for ad-hoc SQL.
MAX_BYTES_BILLED = 10 * 1024 * 1024 * 1024  # 10 GB per query
MAX_RESULT_ROWS = 1000
QUERY_TIMEOUT_SECONDS = 90

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|merge|grant|revoke|call|exec)\b",
    re.IGNORECASE,
)
_STARTS_WITH_SELECT_OR_WITH = re.compile(r"^\s*(WITH|SELECT)\b", re.IGNORECASE)


_credentials_cache: Optional[service_account.Credentials] = None
_clients: Dict[str, bigquery.Client] = {}


_PROJECT_CONFIGS: Dict[str, tuple] = {
    "us": ("glowforge-production", "US"),
    "central": ("glowforge-production", "us-central1"),
    "dev": ("glowforge-dev", "US"),
    "data": ("glowforge-data-production", "US"),
}


def _get_credentials() -> Optional[service_account.Credentials]:
    """Load service-account credentials from env, or return None to use ADC."""
    global _credentials_cache
    if _credentials_cache is not None:
        return _credentials_cache
    raw = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    info = json.loads(raw)
    _credentials_cache = service_account.Credentials.from_service_account_info(info)
    return _credentials_cache


def _client(name: str) -> bigquery.Client:
    """Get or create a BigQuery client for one of our four endpoints."""
    if name in _clients:
        return _clients[name]
    project, location = _PROJECT_CONFIGS[name]
    creds = _get_credentials()
    _clients[name] = bigquery.Client(
        project=project, location=location, credentials=creds
    )
    return _clients[name]


# ---------------------------------------------------------------------------
# Aggregate query functions (no PII, no individual records).
# ---------------------------------------------------------------------------


def get_subscription_distribution() -> List[Dict[str, Any]]:
    """Current breakdown of subscriptions by plan and state.

    Returns one row per (plan, sub_state) combination, with user count and
    total MRR. Excludes Glowforge-internal accounts.
    """
    query = """
    WITH latest AS (
      SELECT MAX(date) AS d
      FROM `glowforge-data-production.reporting.subs_state_machine`
    )
    SELECT
      plan,
      sub_state,
      COUNT(*) AS user_count,
      ROUND(SUM(mrr), 2) AS total_mrr
    FROM `glowforge-data-production.reporting.subs_state_machine`
    WHERE date = (SELECT d FROM latest)
      AND glowforge_internal = FALSE
    GROUP BY plan, sub_state
    ORDER BY user_count DESC
    """
    return [dict(row) for row in _client("data").query(query).result()]


def count_inactive_users(inactive_days: int = 30) -> Dict[str, Any]:
    """Count of users who haven't printed in `inactive_days`.

    Returns aggregate counts and average inactivity, no individual users.
    Excludes Glowforge-internal accounts.
    """
    query = """
    WITH latest AS (
      SELECT MAX(date) AS d
      FROM `glowforge-data-production.reporting.active_users`
    )
    SELECT
      COUNT(*) AS user_count,
      ROUND(AVG(days_since_latest_active), 1) AS avg_days_inactive,
      MAX(days_since_latest_active) AS max_days_inactive
    FROM `glowforge-data-production.reporting.active_users`
    WHERE date = (SELECT d FROM latest)
      AND glowforge_internal = FALSE
      AND days_since_latest_active >= @inactive_days
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("inactive_days", "INT64", inactive_days),
        ]
    )
    rows = list(_client("data").query(query, job_config=cfg).result())
    return dict(rows[0]) if rows else {}


def run_warehouse_query(sql: str) -> Dict[str, Any]:
    """Execute an ad-hoc SELECT against BigQuery, with safety rails.

    Rules:
      - Must start with SELECT or WITH (CTEs ok).
      - Forbidden: INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/etc.
      - Bytes scanned capped at MAX_BYTES_BILLED (query is rejected before run
        if dry-run estimates more).
      - Up to MAX_RESULT_ROWS rows returned; rest are dropped (truncated=True).
      - Wall-clock timeout enforced.

    Returns a dict with rows, row_count, bytes_processed, and truncated flag,
    or {"error": "..."} if rejected or failed.
    """
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return {"error": "Empty SQL."}
    if not _STARTS_WITH_SELECT_OR_WITH.match(sql):
        return {"error": "Only SELECT queries (or CTEs starting with WITH) are allowed."}
    if _FORBIDDEN_SQL.search(sql):
        return {
            "error": (
                "Query rejected — contains forbidden keywords. "
                "Only read-only SELECT statements are allowed."
            )
        }

    client = _client("data")  # billing project; cross-project SELECT works as long as SA has read.

    # Dry-run to estimate bytes and reject expensive queries before execution.
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    try:
        dry_job = client.query(sql, job_config=dry_cfg)
        estimated_bytes = dry_job.total_bytes_processed or 0
    except Exception as exc:
        return {"error": f"Query validation failed: {exc}"}

    if estimated_bytes > MAX_BYTES_BILLED:
        gb = estimated_bytes / (1024 ** 3)
        return {
            "error": (
                f"Query rejected — would scan {gb:.1f} GB, exceeding the "
                f"{MAX_BYTES_BILLED / (1024**3):.0f} GB cap. Add date filters, "
                "narrow column selection, or break the query into smaller pieces."
            )
        }

    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED)
    try:
        job = client.query(sql, job_config=cfg, timeout=QUERY_TIMEOUT_SECONDS)
        result = job.result(timeout=QUERY_TIMEOUT_SECONDS, max_results=MAX_RESULT_ROWS)
        rows = [dict(row) for row in result]
    except Exception as exc:
        return {"error": f"Query failed: {exc}"}

    return {
        "row_count": len(rows),
        "rows": rows,
        "bytes_processed": job.total_bytes_processed,
        "truncated": len(rows) >= MAX_RESULT_ROWS,
    }


def describe_table(fully_qualified_table_id: str) -> Dict[str, Any]:
    """Return schema info for a BigQuery table.

    `fully_qualified_table_id` should be in the form
    'project.dataset.table' (e.g.,
    'glowforge-data-production.reporting.subs_state_machine').
    """
    client = _client("data")
    try:
        table = client.get_table(fully_qualified_table_id)
    except Exception as exc:
        return {"error": f"Failed to describe table: {exc}"}

    return {
        "table_id": fully_qualified_table_id,
        "row_count": table.num_rows,
        "size_bytes": table.num_bytes,
        "last_modified": str(table.modified),
        "description": table.description or "",
        "schema": [
            {
                "name": f.name,
                "type": f.field_type,
                "mode": f.mode,
                "description": f.description or "",
            }
            for f in table.schema
        ],
    }


def compute_email_revenue(
    email_id: str,
    window_days: int = 7,
) -> Dict[str, Any]:
    """Canonical revenue calculation for a HubSpot marketing email.

    This is the ONE supported way to compute email revenue. Same inputs ->
    identical output, every time. The whole point is to remove the
    free-form-SQL variance that produced contradictory answers in Slack.

    Methodology (fixed — do not pretend you have other options):
      - Send time: pulled from HubSpot's `publishDate` on the email via
        get_email_statistics. NEVER guessed from order data.
      - Attribution: UTM-style. HubSpot adds `_hsmi=<email_id>` to every
        link in a marketing email; the landing_site URL on the resulting
        Shopify order carries that tag. We count orders whose
        landing_site LIKE '%_hsmi=<email_id>%' AND created_at in
        [send_time, send_time + window_days). Deterministic from order
        data alone — no HubSpot list dependency.
      - Revenue: SUM(total_price_usd) on glowforge-dev.gf_shopify.orders,
        restricted to paid, non-cancelled, non-test orders. Refunds are
        NOT subtracted — we measure gross attributed revenue.
      - Self-consistency: same query runs twice with the BQ cache disabled.
        Mismatched results are a hard error rather than silent reconcile.

    PRIVACY: returns aggregate counts only. NEVER returns individual
    customer emails, locations, organizations, order ids, or per-order
    amounts.

    Migration note: previously used clicker-list attribution (joining
    HubSpot's CLICKED engagers to Shopify orders by email). That path
    depended on legacy HubSpot list scopes that aren't grantable on this
    Service Key, which caused the tool to silently fail and Mark to fall
    back to free-form SQL — producing variance. UTM attribution removes
    that dependency entirely.

    Args:
      email_id: HubSpot marketing email ID. Pass as string.
      window_days: Attribution window in days from send time. Default 7.

    Returns:
      Dict including total_revenue_usd, order_count, customer_count,
      the exact SQL run, and params_summary. Or {"error": "..."} on any
      failure mode — DO NOT improvise around errors with free-form SQL.
    """
    # Local import keeps warehouse.py importable without HubSpot setup
    # and avoids a circular import via hubspot_marketing -> agent helpers.
    from email_mark.hubspot_marketing import get_email_statistics

    if not email_id:
        return {"error": "email_id is required."}
    if window_days < 1 or window_days > 90:
        return {
            "error": (
                f"window_days must be between 1 and 90 (got {window_days}). "
                "Wider windows just measure background order rate."
            )
        }

    # 1. Resolve send time from HubSpot. No guessing allowed.
    try:
        email_obj = get_email_statistics(str(email_id))
    except Exception as exc:
        return {"error": f"Failed to fetch email {email_id} from HubSpot: {exc}"}

    send_raw = email_obj.get("publishDate")
    if not send_raw:
        return {
            "error": (
                f"Email {email_id} has no publishDate. Either it hasn't been "
                "sent yet, or HubSpot didn't return the field. Refusing to "
                "estimate a send time. Confirm the email is in PUBLISHED state."
            )
        }
    send_iso = str(send_raw)

    # 2. Extract the actual _hsmi tracking id from a link in the email.
    # HubSpot stamps `_hsmi=<tracking_id>` into every link; that id is NOT
    # the same as the email's API id (212960105020 vs 420880582 — totally
    # different numbers for the same email). The tracking id lives in the
    # rendered widget HTML, so we walk widgets looking for it.
    hsmi_id = _extract_hsmi_from_email_object(email_obj)
    if not hsmi_id:
        return {
            "error": (
                f"Could not find an _hsmi tracking id in email {email_id}'s "
                "rendered widget HTML. Most likely HubSpot stamps the tracking "
                "id at send-time rather than at template-edit-time, so the "
                "saved widgets don't carry it. The fix is to use a different "
                "attribution method — most likely migrating clicker-list "
                "attribution to the v3 lists API. DO NOT estimate revenue "
                "with free-form SQL; surface this error and stop."
            ),
            "email_id": email_id,
            "send_time_iso": send_iso,
        }

    # 3. Build & run the canonical UTM-attribution query, twice, cache off.
    sql = (
        "SELECT\n"
        "  COUNT(DISTINCT o.id) AS order_count,\n"
        "  COUNT(DISTINCT LOWER(TRIM(COALESCE(o.email, o.contact_email)))) AS customer_count,\n"
        "  ROUND(COALESCE(SUM(o.total_price_usd), 0), 2) AS total_revenue_usd\n"
        "FROM `glowforge-dev.gf_shopify.orders` o\n"
        "WHERE o.landing_site LIKE @hsmi_pattern\n"
        "  AND o.created_at >= TIMESTAMP(@send_time)\n"
        "  AND o.created_at < TIMESTAMP_ADD(TIMESTAMP(@send_time), INTERVAL @window_days DAY)\n"
        "  AND o.financial_status = 'paid'\n"
        "  AND o.cancelled_at IS NULL\n"
        "  AND (o.test IS NULL OR o.test = FALSE)"
    )

    hsmi_pattern = f"%_hsmi={hsmi_id}%"

    params = [
        bigquery.ScalarQueryParameter("hsmi_pattern", "STRING", hsmi_pattern),
        bigquery.ScalarQueryParameter("send_time", "TIMESTAMP", send_iso),
        bigquery.ScalarQueryParameter("window_days", "INT64", window_days),
    ]

    client = _client("dev")  # orders live in glowforge-dev, not the data project

    def _run_once() -> Dict[str, Any]:
        cfg = bigquery.QueryJobConfig(
            query_parameters=params,
            use_query_cache=False,
            maximum_bytes_billed=MAX_BYTES_BILLED,
        )
        rows = list(
            client.query(sql, job_config=cfg).result(timeout=QUERY_TIMEOUT_SECONDS)
        )
        return dict(rows[0]) if rows else {
            "order_count": 0,
            "customer_count": 0,
            "total_revenue_usd": 0,
        }

    try:
        run_a = _run_once()
        run_b = _run_once()
    except Exception as exc:
        return {"error": f"BigQuery query failed: {exc}"}

    def _shape(r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "order_count": int(r.get("order_count") or 0),
            "customer_count": int(r.get("customer_count") or 0),
            "total_revenue_usd": float(r.get("total_revenue_usd") or 0),
        }

    a, b = _shape(run_a), _shape(run_b)
    if a != b:
        return {
            "error": (
                "Self-consistency check failed: the canonical revenue query "
                f"returned different results on two consecutive runs. "
                f"run1={a} run2={b}. DO NOT report a revenue number — surface "
                "this error to the user and stop."
            ),
            "first_run": a,
            "second_run": b,
        }

    return {
        "email_id": str(email_id),
        "hsmi_tracking_id": hsmi_id,
        "send_time_iso": send_iso,
        "window_days": window_days,
        "attribution": "utm_hsmi",
        "order_count": a["order_count"],
        "customer_count": a["customer_count"],
        "total_revenue_usd": a["total_revenue_usd"],
        "sql": sql,
        "params_summary": {
            "table": "glowforge-dev.gf_shopify.orders",
            "hsmi_pattern": hsmi_pattern,
            "hsmi_tracking_id": hsmi_id,
            "send_time_utc": send_iso,
            "window_days": window_days,
            "filters": (
                "o.landing_site LIKE '%_hsmi=<hsmi_tracking_id>%', "
                "o.created_at in [send_time, send_time + window_days), "
                "o.financial_status = 'paid', "
                "o.cancelled_at IS NULL, o.test != TRUE; "
                "refunds NOT subtracted (gross attributed revenue)"
            ),
        },
    }


def _extract_hsmi_from_email_object(email_obj: Dict[str, Any]) -> Optional[str]:
    """Find the first `_hsmi=<digits>` tracking id in an email's widget HTML.

    HubSpot's marketing email object has a `content.widgets` map where each
    widget can carry rendered HTML in body.html / body.value / body.rich_text
    / body.text. Tracking-wrapped links in that HTML look like:
        https://hs.example.com/...?_hsmi=420880582&_hsenc=...
    We walk all widgets and return the first numeric `_hsmi` we find. If
    none is present (e.g., HubSpot didn't stamp tracking ids at template
    edit time), returns None.

    Tolerates HTML-entity-encoded ampersands (`&amp;_hsmi=`) and both `?`
    and `&` query-string introducers.
    """
    content = email_obj.get("content") if isinstance(email_obj.get("content"), dict) else {}
    widgets = (content or {}).get("widgets") if isinstance(content, dict) else None
    if not isinstance(widgets, dict):
        return None

    # `_hsmi=` may be preceded by `?`, `&`, or `&amp;` in HTML attributes.
    # We don't strictly anchor on the introducer to be forgiving.
    hsmi_re = re.compile(r"_hsmi=(\d+)")

    for widget in widgets.values():
        if not isinstance(widget, dict):
            continue
        body = widget.get("body") if isinstance(widget.get("body"), dict) else {}
        for field in ("html", "value", "rich_text", "text"):
            raw = body.get(field)
            if isinstance(raw, str):
                match = hsmi_re.search(raw)
                if match:
                    return match.group(1)
    return None


def get_print_recency_buckets() -> List[Dict[str, Any]]:
    """Distribution of users by how recently they last printed.

    Returns one row per recency bucket with user count. Useful for
    understanding the activation/churn funnel at a glance.
    """
    query = """
    WITH latest AS (
      SELECT MAX(date) AS d
      FROM `glowforge-data-production.reporting.active_users`
    )
    SELECT
      CASE
        WHEN days_since_latest_active IS NULL THEN '99_never_printed'
        WHEN days_since_latest_active = 0   THEN '00_today'
        WHEN days_since_latest_active <= 7   THEN '01_within_7d'
        WHEN days_since_latest_active <= 30  THEN '02_8_to_30d'
        WHEN days_since_latest_active <= 90  THEN '03_31_to_90d'
        WHEN days_since_latest_active <= 365 THEN '04_91_to_365d'
        ELSE '05_over_365d'
      END AS recency_bucket,
      COUNT(*) AS user_count
    FROM `glowforge-data-production.reporting.active_users`
    WHERE date = (SELECT d FROM latest)
      AND glowforge_internal = FALSE
    GROUP BY recency_bucket
    ORDER BY recency_bucket
    """
    return [dict(row) for row in _client("data").query(query).result()]
