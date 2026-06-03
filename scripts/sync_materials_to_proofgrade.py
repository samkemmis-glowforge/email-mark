"""Sync Shopify materials buyers into the HubSpot Proofgrade dynamic list.

Sets TWO HubSpot contact properties in a single batch update:
  - proofgrade_marketing_opt_in = "true"  (custom property)
  - hs_marketable_status        = "true"  (billing-impacting standard property)

...on HubSpot contacts who, in the last `--lookback-days` days (default 30):
  - Placed a paid Shopify order
  - Bought at least one product whose product_type='Material'
  - Have buyer_accepts_marketing = TRUE on that order

The HubSpot Proofgrade dynamic list (id 10273) is expected to filter on
`proofgrade_marketing_opt_in = true AND hs_marketable_status = true`,
so setting both auto-enrolls matching contacts.

NOTE: hs_marketable_status is a billing-impacting property — flipping a
contact from non-marketing to marketing increments your HubSpot marketing
contact count. The buyer_accepts_marketing filter on the BQ side is the
guardrail: we only flip contacts who explicitly opted in at checkout.

Idempotent — running it daily is safe. Setting a property to "true" when
it's already "true" is a no-op on HubSpot's side.

Required HUBSPOT_API_KEY scopes (both must be on the Service Key):
  - crm.objects.contacts.read
  - crm.objects.contacts.write

Run from the project root:
    .venv/bin/python scripts/sync_materials_to_proofgrade.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from dotenv import find_dotenv, load_dotenv
from google.cloud import bigquery

# Reuse the existing module wiring for credentials + clients.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from email_mark.hubspot_marketing import HUBSPOT_BASE, _headers  # noqa: E402
from email_mark.warehouse import _client  # noqa: E402

load_dotenv(find_dotenv())

LOG = logging.getLogger("sync_materials_to_proofgrade")

# HubSpot batch endpoints accept up to 100 inputs per call.
BATCH_SIZE = 100

# The contact properties the script flips. The Proofgrade dynamic list's
# filter criteria should include BOTH — confirm in HubSpot before running.
PROOFGRADE_PROPERTY = "proofgrade_marketing_opt_in"
PROOFGRADE_PROPERTY_VALUE = "true"
# hs_marketable_status is a HubSpot standard property; setting it to "true"
# flips the contact from non-marketing to marketing (counts toward your
# HubSpot marketing-contacts billing tier). Only safe because the BQ query
# restricts to contacts who opted in at Shopify checkout.
MARKETABLE_STATUS_PROPERTY = "hs_marketable_status"
MARKETABLE_STATUS_VALUE = "true"

# Properties we set on every matched contact, packaged as the body
# fragment HubSpot's batch/update endpoint expects.
PROPERTIES_TO_SET = {
    PROOFGRADE_PROPERTY: PROOFGRADE_PROPERTY_VALUE,
    MARKETABLE_STATUS_PROPERTY: MARKETABLE_STATUS_VALUE,
}


# Canonical SQL. Parameterized on lookback_days. DISTINCT + lowercase + trim
# ensures we deduplicate emails on our side before hitting HubSpot. The
# `buyer_accepts_marketing = TRUE` filter is strict — NULL counts as
# "not opted in" and is excluded.
QUERY_SQL = """
SELECT DISTINCT LOWER(TRIM(o.email)) AS email
FROM `glowforge-dev.gf_shopify.orders` o,
     UNNEST(o.line_items) AS item
JOIN `glowforge-dev.gf_shopify.products` p
  ON item.value.product_id = p.id
WHERE p.product_type = 'Material'
  AND o.financial_status = 'paid'
  AND o.buyer_accepts_marketing = TRUE
  AND o.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback_days DAY)
  AND o.email IS NOT NULL
  AND TRIM(o.email) != ''
"""


def fetch_eligible_emails(lookback_days: int) -> List[str]:
    """Run the canonical materials-buyer query and return unique emails."""
    client = _client("dev")  # gf_shopify.orders lives in glowforge-dev
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        ]
    )
    job = client.query(QUERY_SQL, job_config=job_config)
    return [row["email"] for row in job.result() if row.get("email")]


def _lookup_vids_by_email(
    emails: List[str],
) -> Tuple[Dict[str, str], List[str]]:
    """Resolve HubSpot contact vids from email addresses via batch/read.

    HubSpot's batch/update endpoint with `idProperty=email` doesn't reliably
    find contacts (silently returns OBJECT_NOT_FOUND even when the contact
    exists with that exact email). batch/read with `idProperty=email`
    works, so we do a two-step pattern: read vids first, then update by
    vid.

    Returns (email -> vid map, list of emails not found).
    """
    email_to_vid: Dict[str, str] = {}
    missing: List[str] = []

    for start in range(0, len(emails), BATCH_SIZE):
        chunk = emails[start:start + BATCH_SIZE]
        response = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/batch/read",
            headers=_headers(),
            params={"idProperty": "email"},
            json={
                "inputs": [{"id": e} for e in chunk],
                "properties": ["email"],
            },
            timeout=60,
        )
        if response.status_code not in (200, 207):
            LOG.error(
                "batch/read HTTP %d: %s",
                response.status_code,
                response.text[:500],
            )
            missing.extend(chunk)
            continue

        body = response.json()
        # HubSpot returns matched contacts in `results`, each with a vid
        # in `id` and the resolved properties (including the canonical
        # email) in `properties`.
        for result in body.get("results", []) or []:
            vid = result.get("id")
            props = result.get("properties") or {}
            canonical_email = (props.get("email") or "").strip().lower()
            if vid and canonical_email:
                email_to_vid[canonical_email] = str(vid)

        # Anything in the errors block (typically a single OBJECT_NOT_FOUND
        # bundling all missing ids in context.ids) goes to missing.
        for err in body.get("errors", []) or []:
            context_ids = (err.get("context") or {}).get("ids") or []
            for missing_id in context_ids:
                missing.append(missing_id)

    # Catch any inputs that didn't appear in either results or errors.
    chunk_email_set = {e.strip().lower() for e in emails}
    found_set = set(email_to_vid.keys())
    error_set = {m.strip().lower() for m in missing}
    silently_missing = chunk_email_set - found_set - error_set
    missing.extend(sorted(silently_missing))

    return email_to_vid, missing


def set_proofgrade_property(
    emails: List[str], dry_run: bool = False
) -> Dict[str, int]:
    """Set proofgrade_marketing_opt_in + hs_marketable_status on each contact.

    Two-step pattern:
      1. batch/read with idProperty=email to resolve vids from emails.
         (idProperty=email on batch/update doesn't work reliably — silently
         returns NOT_FOUND for contacts that exist with that exact email.
         batch/read works.)
      2. batch/update by vid to set both properties in one call per batch.

    Both properties go in one batch/update payload per HubSpot batch (up
    to 100 contacts per call). Contacts that don't exist are reported via
    the not_found counter and skipped, not errored.

    Returns counts: attempted, updated, not_found, errored.
    """
    counts = {"attempted": len(emails), "updated": 0, "not_found": 0, "errored": 0}

    if dry_run:
        LOG.info(
            "DRY RUN: would resolve %d emails -> vids, then batch/update both "
            "properties on the matched contacts.",
            len(emails),
        )
        return counts

    # Step 1: resolve emails to vids.
    LOG.info("Looking up HubSpot vids for %d emails via batch/read...", len(emails))
    email_to_vid, missing = _lookup_vids_by_email(emails)
    counts["not_found"] = len(missing)
    LOG.info(
        "vid lookup: %d found, %d not in HubSpot",
        len(email_to_vid),
        len(missing),
    )

    if not email_to_vid:
        return counts

    # Step 2: batch/update by vid.
    vids = list(email_to_vid.values())
    for start in range(0, len(vids), BATCH_SIZE):
        chunk = vids[start:start + BATCH_SIZE]
        payload = {
            "inputs": [
                {"id": vid, "properties": dict(PROPERTIES_TO_SET)}
                for vid in chunk
            ],
        }
        response = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/batch/update",
            headers=_headers(),
            json=payload,
            timeout=60,
        )
        if response.status_code in (200, 207):
            body = response.json()
            counts["updated"] += len(body.get("results", []))
            for err in body.get("errors", []) or []:
                ids = (err.get("context") or {}).get("ids") or []
                affected = max(1, len(ids))
                counts["errored"] += affected
                LOG.warning(
                    "batch/update error (%d affected): %s",
                    affected,
                    err,
                )
        else:
            LOG.error(
                "batch/update HTTP %d: %s",
                response.status_code,
                response.text[:500],
            )
            counts["errored"] += len(chunk)

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="How far back to look for materials orders. Default 30.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query BQ and print intended updates without hitting HubSpot.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    LOG.info(
        "Querying eligible materials buyers (lookback %d days, "
        "buyer_accepts_marketing=TRUE)...",
        args.lookback_days,
    )
    try:
        emails = fetch_eligible_emails(args.lookback_days)
    except Exception as exc:
        LOG.error("BigQuery query failed: %s", exc)
        return 2

    LOG.info("Found %d unique eligible emails", len(emails))

    if not emails:
        LOG.info("Nothing to do. Exiting.")
        return 0

    props_summary = ", ".join(f"{k}={v!r}" for k, v in PROPERTIES_TO_SET.items())
    if args.dry_run:
        LOG.info(
            "DRY RUN: would set %s on up to %d contacts.",
            props_summary,
            len(emails),
        )
    else:
        LOG.info(
            "Setting %s on matching HubSpot contacts...",
            props_summary,
        )

    counts = set_proofgrade_property(emails, dry_run=args.dry_run)

    LOG.info(
        "Done. Eligible=%d Attempted=%d Updated=%d NotFound=%d Errored=%d",
        len(emails),
        counts["attempted"],
        counts["updated"],
        counts["not_found"],
        counts["errored"],
    )

    # Non-zero exit if anything errored hard (excluding NotFound, which is
    # expected for emails that aren't in HubSpot yet).
    return 1 if counts["errored"] else 0


if __name__ == "__main__":
    sys.exit(main())
