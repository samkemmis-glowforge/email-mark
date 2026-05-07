"""Smoke-test BigQuery access. Surfaces raw errors instead of bot summaries.

Run from the project root:
    .venv/bin/python scripts/test_warehouse.py
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from email_mark.warehouse import (  # noqa: E402
    count_inactive_users,
    get_print_recency_buckets,
    get_subscription_distribution,
)


def _try(name, fn, *args, **kwargs):
    print(f"--- {name} ---")
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, list):
            print(f"  Got {len(result)} rows. First row: {result[0] if result else '(empty)'}")
        else:
            print(f"  Result: {result}")
    except Exception:
        print("  FAILED:")
        traceback.print_exc()
    print()


def main() -> None:
    _try("get_subscription_distribution", get_subscription_distribution)
    _try("count_inactive_users(30)", count_inactive_users, 30)
    _try("get_print_recency_buckets", get_print_recency_buckets)


if __name__ == "__main__":
    main()
