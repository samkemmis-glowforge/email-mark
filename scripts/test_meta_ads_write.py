"""Sanity-check the gated Meta ads write path (ads-mark test builds).

Safe by default: with the gate off it just verifies the guardrails refuse.
Run from the project root:
    .venv/bin/python scripts/test_meta_ads_write.py

With ADS_MARK_ALLOW_WRITE=true in .env, add --create-test-campaign to
create one PAUSED, name-tagged, zero-budget campaign end-to-end and read
it back. It spends nothing (PAUSED, no budget), but you'll want to delete
it in Ads Manager afterwards.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from email_mark import meta_client  # noqa: E402


def main() -> None:
    daily_cap, lifetime_cap = meta_client._budget_caps()
    print(f"gate ADS_MARK_ALLOW_WRITE : {meta_client.ads_write_enabled()}")
    print(f"name prefix               : {meta_client._mark_prefix()!r}")
    print(f"daily budget cap          : {daily_cap} cents")
    print(f"lifetime budget cap       : {lifetime_cap} cents")
    print(f"ads token override set    : {bool(os.environ.get('META_ADS_ACCESS_TOKEN'))}")
    print()

    if not meta_client.ads_write_enabled():
        try:
            meta_client.create_meta_campaign(
                name="gate check", objective="OUTCOME_TRAFFIC"
            )
        except meta_client.MetaError as exc:
            print(f"OK — gate is off and create refused:\n  {exc}")
            return
        print("PROBLEM: gate is off but create_meta_campaign did not refuse!")
        sys.exit(1)

    # Gate is on: check the budget cap refuses an over-cap create.
    try:
        meta_client.create_meta_campaign(
            name="cap check",
            objective="OUTCOME_TRAFFIC",
            daily_budget_cents=daily_cap + 1,
        )
        print("PROBLEM: over-cap budget was not refused!")
        sys.exit(1)
    except meta_client.MetaError as exc:
        print(f"OK — over-cap budget refused:\n  {exc}\n")

    if "--create-test-campaign" not in sys.argv:
        print(
            "Gate is on. Pass --create-test-campaign to create a PAUSED "
            "smoke-test campaign for real."
        )
        return

    created = meta_client.create_meta_campaign(
        name="smoke test (safe to delete)", objective="OUTCOME_TRAFFIC"
    )
    print(f"Created: {created}")
    details = meta_client.get_meta_campaign_details(created["id"])
    print(f"Read back: status={details.get('status')} name={details.get('name')!r}")
    if details.get("status") != "PAUSED":
        print("PROBLEM: campaign was not created PAUSED!")
        sys.exit(1)
    print("OK — created PAUSED and name-tagged. Delete it in Ads Manager when done.")


if __name__ == "__main__":
    main()
