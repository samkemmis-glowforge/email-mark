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
      - Attribution: clicker-list. A contact counts if their email both
        (a) clicked the marketing email per HubSpot (resolved via the v3
        lists API — see get_email_engagers_via_list), AND (b) placed a
        Shopify order between send_time and send_time + window_days.
      - Revenue: SUM(total_price_usd) on glowforge-dev.gf_shopify.orders,
        restricted to paid, non-cancelled, non-test orders. Refunds are
        NOT subtracted — we measure gross attributed revenue.
      - Self-consistency: same query runs twice with the BQ cache disabled.
        Mismatched results are a hard error rather than silent reconcile.

    PRIVACY: returns aggregate counts only. NEVER returns individual
    customer emails, locations, organizations, order ids, or per-order
    amounts.

    Implementation history (read before changing): briefly used UTM
    attribution (`landing_site LIKE '%_hsmi=...%'`) to avoid HubSpot list
    scope dependencies. That broke because HubSpot stamps `_hsmi` at
    send-time, not at template-edit-time, so the value isn't fetchable
    from the email API object. Migrated back to clicker-list attribution,
    now on the v3 lists API (which uses scopes Sam's Service Key has).

    Args:
      email_id: HubSpot marketing email ID. Pass as string.
      window_days: Attribution window in days from send time. Default 7.

    Returns:
      Dict including total_revenue_usd, order_count, customer_count,
      clicker_count, the exact SQL run, and params_summary. Or
      {"error": "..."} on any failure — DO NOT improvise around errors
      with free-form SQL.
    """
    # Local imports keep warehouse.py importable without HubSpot setup
    # and avoid a circular import via hubspot_marketing -> agent helpers.
    from email_mark.hubspot_marketing import (
        get_email_engagers_via_list,
        get_email_statistics,
    )

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

    # 2. Fetch the clicker email set via v3 lists API.
    try:
        clickers_result = get_email_engagers_via_list(
            email_id=str(email_id),
            event_type="CLICKED",
        )
    except Exception as exc:
        return {"error": f"Failed to fetch clickers for email {email_id}: {exc}"}

    if isinstance(clickers_result, dict) and "error" in clickers_result:
        return {
            "error": (
                f"Clicker lookup failed: {clickers_result['error']} "
                "DO NOT estimate revenue with free-form SQL; surface this "
                "error and stop."
            ),
            "clicker_lookup_response": clickers_result,
        }

    recipient_emails = clickers_result.get("recipient_emails") or []
    clicker_count = len(recipient_emails)

    if not recipient_emails:
        return {
            "email_id": str(email_id),
            "send_time_iso": send_iso,
            "window_days": window_days,
            "attribution": "clickers",
            "clicker_count": 0,
            "order_count": 0,
            "customer_count": 0,
            "total_revenue_usd": 0.0,
            "sql": None,
            "params_summary": None,
            "note": (
                "Zero clickers returned by the v3 lists API. Either no one "
                "clicked this email, or the temp list didn't populate within "
                "the polling window. Revenue is reported as $0; surface this "
                "caveat in the Slack reply."
            ),
        }

    # 3. Build & run the canonical query, twice, cache off.
    sql = (
        "SELECT\n"
        "  COUNT(DISTINCT o.id) AS order_count,\n"
        "  COUNT(DISTINCT LOWER(TRIM(COALESCE(o.email, o.contact_email)))) AS customer_count,\n"
        "  ROUND(COALESCE(SUM(o.total_price_usd), 0), 2) AS total_revenue_usd\n"
        "FROM `glowforge-dev.gf_shopify.orders` o\n"
        "WHERE LOWER(TRIM(COALESCE(o.email, o.contact_email))) IN UNNEST(@clicker_emails)\n"
        "  AND o.created_at >= TIMESTAMP(@send_time)\n"
        "  AND o.created_at < TIMESTAMP_ADD(TIMESTAMP(@send_time), INTERVAL @window_days DAY)\n"
        "  AND o.financial_status = 'paid'\n"
        "  AND o.cancelled_at IS NULL\n"
        "  AND (o.test IS NULL OR o.test = FALSE)"
    )

    normalized_emails = sorted({
        e.strip().lower() for e in recipient_emails if e and e.strip()
    })

    params = [
        bigquery.ArrayQueryParameter("clicker_emails", "STRING", normalized_emails),
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
        "send_time_iso": send_iso,
        "window_days": window_days,
        "attribution": "clickers",
        "clicker_count": clicker_count,
        "order_count": a["order_count"],
        "customer_count": a["customer_count"],
        "total_revenue_usd": a["total_revenue_usd"],
        "sql": sql,
        "params_summary": {
            "table": "glowforge-dev.gf_shopify.orders",
            "send_time_utc": send_iso,
            "window_days": window_days,
            "clicker_count_into_query": len(normalized_emails),
            "filters": (
                "o.email (or contact_email) matches a clicker email, "
                "o.created_at in [send_time, send_time + window_days), "
                "o.financial_status = 'paid', "
                "o.cancelled_at IS NULL, o.test != TRUE; "
                "refunds NOT subtracted (gross attributed revenue)"
            ),
        },
    }


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
