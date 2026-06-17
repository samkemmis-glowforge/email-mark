"""Scheduled job: draft upcoming social posts and drop them in Slack for review.

Run on a cadence (GitHub Actions cron). For each upcoming Social-Post row in
the content calendar, it asks Claude to draft a caption in Glowforge social
voice off the row's theme/angle/audience/product, then posts the draft to the
review channel (SLACK_REVIEW_CHANNEL). It does NOT publish to Meta — a human
reviews and posts.

Usage:
    python scripts/run_scheduled_drafts.py [--within-days N] [--dry-run]

--dry-run prints drafts to stdout instead of posting to Slack.
"""

import argparse
import sys
from pathlib import Path

# Make src/ importable without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import find_dotenv, load_dotenv  # noqa: E402

from email_mark import content, content_calendar  # noqa: E402
from email_mark.agent import _brand_voice_section, _social_playbook_section  # noqa: E402
from email_mark.slack_helpers import post_to_review_channel  # noqa: E402

load_dotenv(find_dotenv())


def _draft_caption(row: content_calendar.CalendarRow) -> str:
    """Ask Claude for a single caption for one calendar row.

    email_mark.content.generate has no system-prompt arg, so the brand voice
    and social playbook are folded into the prompt itself.
    """
    prompt = (
        "You are Mark, drafting an organic social caption for Glowforge in the "
        "brand's slightly-casual, fun social voice. Lead with the maker/"
        "creativity, then the project, then Glowforge. Keep it tight (1-3 short "
        "sentences). Use at most 1-2 of the approved hashtags when they fit "
        "(#laserthursday #whatmadethis #glowforge). Avoid 'fire/burns', 'users', "
        "and 'the Glowforge'. Output ONLY the caption text.\n"
        + _brand_voice_section()
        + _social_playbook_section()
        + "\n\nDraft one caption for this scheduled post.\n\n"
        f"Date: {row.date_raw} ({row.day})\n"
        f"Theme: {row.theme}\n"
        f"Caption angle: {row.caption_angle}\n"
        f"Audience focus: {row.audience}\n"
        f"Product focus: {row.product}\n"
        f"Key dates this week: {row.key_dates}\n"
    )
    return content.generate(prompt, max_tokens=400).strip()


def _format_for_slack(row: content_calendar.CalendarRow, caption: str) -> str:
    when = row.post_date.strftime("%a %b %-d") if row.post_date else row.date_raw
    asset = row.asset_links[0] if row.asset_links else "(no asset linked in calendar)"
    lines = [
        ":calendar: *Post draft for review*",
        " · ".join(p for p in [when, "Facebook + Instagram", row.theme] if p),
        "",
        caption,
        "",
        f"*Asset:* {asset}",
        "",
        "_Review, tweak, and post manually. React :white_check_mark: when posted._",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--within-days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = content_calendar.get_upcoming_social_posts(within_days=args.within_days)
    if not rows:
        print(f"No social posts due in the next {args.within_days} days.")
        return 0

    print(f"Drafting {len(rows)} post(s) due in the next {args.within_days} days...")
    failures = 0
    for row in rows:
        try:
            caption = _draft_caption(row)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Failed to draft '{row.theme}' ({row.date_raw}): {exc}")
            failures += 1
            continue

        message = _format_for_slack(row, caption)
        if args.dry_run:
            print("\n" + "=" * 60)
            print(message)
        else:
            result = post_to_review_channel(message)
            status = "posted" if result.get("ok") else f"FAILED ({result.get('error')})"
            print(f"  - {row.date_raw} {row.theme}: {status}")
            if not result.get("ok"):
                failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
