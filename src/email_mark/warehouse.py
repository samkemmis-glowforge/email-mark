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
