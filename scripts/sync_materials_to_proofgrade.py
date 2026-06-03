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
import logging
import sys
from pathlib import Path
from typing import Dict, List

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


def set_proofgrade_property(
    emails: List[str], dry_run: bool = False
) -> Dict[str, int]:
    """Set proofgrade_marketing_opt_in + hs_marketable_status on each contact.

    Both properties go in one batch/update payload per HubSpot batch (up to
    100 contacts per call). Uses idProperty=email so we don't have to look
    up vids first. Contacts that don't exist come back as errors with
    category='OBJECT_NOT_FOUND' — we count those separately rather than
    failing the batch.

    Returns counts: attempted, updated, not_found, errored.
    """
    counts = {"attempted": 0, "updated": 0, "not_found": 0, "errored": 0}

    for start in range(0, len(emails), BATCH_SIZE):
        batch = emails[start:start + BATCH_SIZE]
        counts["attempted"] += len(batch)

        payload = {
            "inputs": [
                {
                    "id": email,
                    "properties": dict(PROPERTIES_TO_SET),
                }
                for email in batch
            ],
        }

        if dry_run:
            LOG.info(
                "DRY RUN: would update %d contacts (batch %d/%d)",
                len(batch),
                start // BATCH_SIZE + 1,
                (len(emails) + BATCH_SIZE - 1) // BATCH_SIZE,
            )
            continue

        response = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/batch/update",
            headers=_headers(),
            params={"idProperty": "email"},
            json=payload,
            timeout=60,
        )

        # 200 = all updated. 207 = partial success (some updated, some errored).
        # 4xx/5xx = full failure (e.g., missing scope, bad payload).
        if response.status_code in (200, 207):
            body = response.json()
            batch_updated = len(body.get("results", []))
            batch_not_found = 0
            batch_errored = 0
            counts["updated"] += batch_updated
            for err in body.get("errors", []) or []:
                category = (err.get("category") or "").upper()
                message = err.get("message") or str(err)
                # HubSpot uses categories like OBJECT_NOT_FOUND for
                # "no contact with that email exists".
                if (
                    "OBJECT_NOT_FOUND" in category
                    or "NOT_FOUND" in category
                    or "not found" in message.lower()
                    or "no contact" in message.lower()
                ):
                    batch_not_found += 1
                    counts["not_found"] += 1
                else:
                    batch_errored += 1
                    counts["errored"] += 1
                    LOG.warning("Batch error: %s", err)

            # Diagnostic: if HubSpot's response doesn't account for every
            # input we sent, log the raw body so we can see what shape it
            # actually used. (HubSpot batch endpoints sometimes return
            # success counts in different fields than we expect.)
            accounted_for = batch_updated + batch_not_found + batch_errored
            if accounted_for < len(batch):
                import json as _json
                LOG.warning(
                    "HubSpot response only accounted for %d of %d inputs. "
                    "HTTP %d. Raw body: %s",
                    accounted_for,
                    len(batch),
                    response.status_code,
                    _json.dumps(body)[:2000],
                )
        else:
            LOG.error(
                "Batch update HTTP %d: %s",
                response.status_code,
                response.text[:500],
            )
            counts["errored"] += len(batch)

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
