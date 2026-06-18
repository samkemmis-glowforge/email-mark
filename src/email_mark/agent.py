"""Tool-using agent loop.

Runs a multi-turn conversation with Claude where Claude can call tools
to look things up in HubSpot (and eventually BigQuery, etc.) and to
take actions like creating draft emails.

Public entrypoint:
    chat(user_message: str) -> str
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from anthropic import Anthropic
from dotenv import find_dotenv, load_dotenv

from email_mark.forum import fetch_forum_post
from email_mark import content_calendar, meta_client
from email_mark.hubspot_crm import (
    list_contact_properties,
    search_contacts,
)
from email_mark.hubspot_marketing import (
    GLOWFORGE_ENGAGEMENT_SENDS_CUTOFF,
    clone_marketing_email,
    count_list_intersection,
    create_email_draft_v2,
    find_hubspot_lists,
    get_contact_email_events,
    get_email_body_text,
    get_email_engagement_contacts,
    get_email_engagers_via_list,
    get_email_statistics,
    get_email_widget_html,
    get_email_widget_structure,
    get_list_details,
    get_workflow_details,
    get_workflow_enrollments,
    list_marketing_emails,
    list_workflows,
    update_email_draft_v2,
    update_email_body,
    update_email_by_widget_map,
    update_marketing_email,
)
from email_mark.slack_helpers import (
    lookup_user as slack_lookup_user,
    post_to_review_channel,
    send_dm as slack_send_dm,
)
from contextvars import ContextVar

from email_mark.answer_cards import (
    format_card_for_slack,
    get_recent_cards,
    record_card as _record_answer_card,
    search_cards,
)
from email_mark.slack_helpers import upload_csv_to_thread
from email_mark.warehouse import (
    compute_email_revenue,
    count_inactive_users,
    describe_table,
    get_print_recency_buckets,
    get_subscription_distribution,
    run_warehouse_query,
)

load_dotenv(find_dotenv())

MODEL = "claude-sonnet-4-5"
MAX_AGENT_TURNS = 25  # Hard cap so a runaway loop can't burn through tokens.

# Per-turn Slack context for tools that need to know which channel/thread
# they're operating in. Set at the start of chat() based on the Slack event
# that triggered the turn; tools (currently just `share_table`) read these
# to target file uploads at the right thread.
_current_channel: ContextVar[Optional[str]] = ContextVar("_current_channel", default=None)
_current_thread_ts: ContextVar[Optional[str]] = ContextVar("_current_thread_ts", default=None)
HUBSPOT_PORTAL_ID = "8614495"  # Glowforge HubSpot portal — used for UI URLs.

# Canonical ICYMI master template. Mark always clones this for the weekly
# ICYMI workflow. Widget IDs below were captured from this email's content
# tree on 2026-05-08 — HubSpot preserves widget IDs across clones, so the
# clone Mark creates will have the same IDs and the patcher can target
# each module by role. If you ever rebuild this template, re-run
# get_email_widget_structure on the new master and refresh ICYMI_WIDGET_MAP.
ICYMI_MASTER_TEMPLATE_ID = "212542521240"
ICYMI_WIDGET_MAP: Dict[str, str] = {
    # Intro paragraph that sets up the week's theme.
    "intro_body":       "module_17734393985902",
    # Project 1 — visually first project module (title + body + image).
    "project_1_title":  "module_17606404695542",
    "project_1_body":   "module_17606404695544",
    "project_1_image":  "module_17606404695531",
    # Project 2 — visually second project module.
    "project_2_title":  "module-6-1-0",
    "project_2_body":   "module-6-1-2",
    "project_2_image":  "module-6-0-0",
    # Project 3 — visually third project module.
    "project_3_title":  "module_17606408446199",
    "project_3_body":   "module_176064084461911",
    "project_3_image":  "module_17606408446198",
    # Laser Focus of the Week — title and body live in one widget.
    "laser_focus_body": "module_17609870518031",
    # "Happy Making! The Glowforge Team" sign-off.
    "signoff_body":     "module_17636900856152",
    # Note: module-8-0-0 is the bottom "Shop materials" banner image; we
    # don't update it — it stays the same week to week.
}

# Conversation memory. Keyed by an external conversation_id (e.g., Slack
# channel for DMs, thread_ts for channel mentions). In-memory only — wipes
# on bot restart. Move to a persistent store (sqlite/redis) when needed.
_conversations: Dict[str, List[Dict[str, Any]]] = {}
MAX_CONVERSATION_MESSAGES = 60  # Cap to keep token usage bounded.

_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


# ---------------------------------------------------------------------------
# Brand-voice loading: an optional file at prompts/brand_voice.md gets
# injected into the system prompt at startup.
# ---------------------------------------------------------------------------


def _load_brand_voice() -> str:
    repo_root = Path(__file__).resolve().parent.parent.parent
    voice_file = repo_root / "prompts" / "brand_voice.md"
    if not voice_file.exists():
        return ""
    text = voice_file.read_text().strip()
    if not text:
        return ""
    return text


_BRAND_VOICE = _load_brand_voice()


def _brand_voice_section() -> str:
    if not _BRAND_VOICE:
        return ""
    return (
        "\n\nBrand voice and tone — apply these to ALL drafted email content:\n\n"
        + _BRAND_VOICE
    )


# ---------------------------------------------------------------------------
# Social playbook loading: an optional file at prompts/social_playbook.md gets
# injected into the system prompt at startup. Platform formats, caption
# structure, posting cadence, and the review workflow for organic social.
# ---------------------------------------------------------------------------


def _load_social_playbook() -> str:
    repo_root = Path(__file__).resolve().parent.parent.parent
    playbook_file = repo_root / "prompts" / "social_playbook.md"
    if not playbook_file.exists():
        return ""
    return playbook_file.read_text().strip()


_SOCIAL_PLAYBOOK = _load_social_playbook()


def _social_playbook_section() -> str:
    if not _SOCIAL_PLAYBOOK:
        return ""
    return (
        "\n\nSOCIAL PLAYBOOK — platform formats, caption structure, cadence "
        "rules, and the review workflow. Consult before drafting any social "
        "post.\n\n"
        + _SOCIAL_PLAYBOOK
    )


def _load_email_design_references() -> str:
    """Load the email design references doc (universal best practices +
    Glowforge brand DNA + HubSpot constraints + experimentation
    guardrails). Mark consults this when designing emails from scratch
    via create_email_draft_v2 / update_email_draft_v2."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    refs_file = repo_root / "prompts" / "email_design_references.md"
    if not refs_file.exists():
        return ""
    text = refs_file.read_text().strip()
    if not text:
        return ""
    return text


_EMAIL_DESIGN_REFERENCES = _load_email_design_references()


def _email_design_references_section() -> str:
    if not _EMAIL_DESIGN_REFERENCES:
        return ""
    return (
        "\n\nEMAIL DESIGN REFERENCES — consult these when designing emails "
        "from scratch (via create_email_draft_v2 / update_email_draft_v2). "
        "Universal email design best practices, Glowforge brand DNA across "
        "all sub-brands, HubSpot-specific constraints, and experimentation "
        "guardrails. Scan the cheat sheet (§5) before every from-scratch "
        "design; consult the brand application section (§2) when picking "
        "colors and typography.\n\n"
        + _EMAIL_DESIGN_REFERENCES
    )


# ---------------------------------------------------------------------------
# Lessons-learned loading: domain knowledge captured from past conversations.
# An editable file at prompts/lessons_learned.md gets injected into the system
# prompt at startup. Add entries as we discover gotchas about data sources,
# tool behavior, business rules, etc. — see the file's header for the format.
# ---------------------------------------------------------------------------


def _lessons_file_path() -> Path:
    """Return the path Mark reads/writes lessons to.

    If LESSONS_FILE_PATH is set (Render persistent disk path, e.g.
    /var/data/lessons_learned.md), use it — lessons there survive every
    deploy. On first run when the persistent file doesn't exist yet, we
    seed it from the repo's prompts/lessons_learned.md so existing
    lessons aren't lost.

    Without the env var, fall back to the repo path. That works for
    local dev; on Render without a disk, lessons are ephemeral.
    """
    override = os.environ.get("LESSONS_FILE_PATH")
    if override:
        persistent = Path(override)
        if not persistent.exists():
            persistent.parent.mkdir(parents=True, exist_ok=True)
            repo_seed = (
                Path(__file__).resolve().parent.parent.parent
                / "prompts"
                / "lessons_learned.md"
            )
            if repo_seed.exists():
                persistent.write_text(repo_seed.read_text())
        return persistent

    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / "prompts" / "lessons_learned.md"


def _load_lessons() -> str:
    lessons_file = _lessons_file_path()
    if not lessons_file.exists():
        return ""
    text = lessons_file.read_text().strip()
    if not text:
        return ""
    return text


def _lessons_section() -> str:
    # Re-read on every call so new lessons saved at runtime (via the
    # remember_lesson tool) take effect immediately on the next inference.
    text = _load_lessons()
    if not text:
        return ""
    return (
        "\n\nLESSONS LEARNED — domain knowledge captured from past "
        "conversations with the team. These are real gotchas about the data, "
        "tools, business rules, or workflows that have tripped you up before. "
        "Treat them as authoritative and apply them BEFORE reasoning from "
        "first principles. When a lesson applies, surface it briefly so the "
        "user knows you're using it.\n\n"
        + text
    )


def _commit_lessons_to_github(new_content: str, message: str) -> Dict[str, Any]:
    """Push the updated lessons file to GitHub via the Contents API.

    Required env vars:
      GITHUB_TOKEN   - fine-grained PAT with Contents:Write on the repo
      GITHUB_REPO    - "owner/name" form, e.g. "Glowforge/email-mark"
    Optional:
      GITHUB_BRANCH  - branch to commit to (default: "main")

    Includes `[skip render]` in the commit message so Render's auto-deploy
    doesn't trigger on every lesson save (which would cause a continuous
    restart loop). Mark's own `_lessons_section()` re-reads the file on
    every inference, so new lessons take effect without needing a deploy.

    Returns {committed: bool, ...details}. Failures are non-fatal — the
    caller still has the lesson saved to local disk.
    """
    import base64 as _b64

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    if not token or not repo:
        return {
            "committed": False,
            "reason": "GITHUB_TOKEN or GITHUB_REPO env var not set",
        }

    import requests as _requests
    api = "https://api.github.com"
    path = "prompts/lessons_learned.md"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1. Get current SHA so we can update (vs create).
    current_sha: Optional[str] = None
    try:
        get_response = _requests.get(
            f"{api}/repos/{repo}/contents/{path}",
            headers=headers,
            params={"ref": branch},
            timeout=15,
        )
        if get_response.status_code == 200:
            current_sha = get_response.json().get("sha")
        elif get_response.status_code != 404:
            return {
                "committed": False,
                "reason": (
                    f"GitHub GET returned HTTP {get_response.status_code}: "
                    f"{get_response.text[:200]}"
                ),
            }
    except Exception as exc:
        return {"committed": False, "reason": f"GitHub GET error: {exc}"}

    # 2. PUT updated content.
    payload: Dict[str, Any] = {
        "message": f"{message} [skip render]",
        "content": _b64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if current_sha:
        payload["sha"] = current_sha

    try:
        put_response = _requests.put(
            f"{api}/repos/{repo}/contents/{path}",
            headers=headers,
            json=payload,
            timeout=15,
        )
    except Exception as exc:
        return {"committed": False, "reason": f"GitHub PUT error: {exc}"}

    if put_response.status_code in (200, 201):
        body = put_response.json()
        commit = body.get("commit", {})
        return {
            "committed": True,
            "commit_sha": commit.get("sha"),
            "commit_url": commit.get("html_url"),
            "branch": branch,
        }
    return {
        "committed": False,
        "reason": (
            f"GitHub PUT returned HTTP {put_response.status_code}: "
            f"{put_response.text[:300]}"
        ),
    }


def remember_lesson(heading: str, lesson: str) -> Dict[str, Any]:
    """Append a lesson to prompts/lessons_learned.md and commit to git.

    Flow:
      1. Update the local file on the Render worker (so the running
         process sees the new lesson immediately via _lessons_section()
         which re-reads on each inference).
      2. Push the updated file to GitHub via the Contents API. Commit
         includes [skip render] so we don't trigger a deploy loop.
      3. Return a structured result describing both the local write and
         the GitHub commit.
    """
    import re as _re
    from datetime import date as _date

    heading_clean = (heading or "").strip()
    lesson_clean = (lesson or "").strip()
    if not heading_clean or not lesson_clean:
        return {"error": "Both heading and lesson are required."}

    lessons_file = _lessons_file_path()
    if not lessons_file.exists():
        return {"error": f"Lessons file not found at {lessons_file}"}

    today = _date.today().isoformat()
    new_bullet = f"\n- {lesson_clean}\n  (Learned {today})\n"

    content = lessons_file.read_text()
    heading_marker = f"## {heading_clean}"
    heading_pattern = _re.escape(heading_marker)
    section_match = _re.search(
        rf"({heading_pattern}\n.*?)(?=\n## |\Z)",
        content,
        _re.DOTALL,
    )
    if section_match:
        existing = section_match.group(1).rstrip()
        replacement = existing + new_bullet
        new_content = (
            content[: section_match.start()]
            + replacement
            + content[section_match.end():]
        )
        section_action = "appended_to_existing"
    else:
        new_content = (
            content.rstrip()
            + f"\n\n{heading_marker}\n{new_bullet}"
        )
        section_action = "created_new_section"

    # 1. Local write (always — the running process needs to see this).
    lessons_file.write_text(new_content)

    result: Dict[str, Any] = {
        "saved": True,
        "heading": heading_clean,
        "lesson": lesson_clean,
        "date": today,
        "section_action": section_action,
        "storage_path": str(lessons_file),
    }

    # 2. Optional GitHub push for version history — only attempt if env
    #    vars are configured. If not, the persistent disk IS the durable
    #    store and we don't want to spam "github commit failed" messages.
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"):
        result["github"] = _commit_lessons_to_github(
            new_content,
            message=f"Add lesson under '{heading_clean}'",
        )

    return result


# ---------------------------------------------------------------------------
# Lesson parsing — list, update, delete
# ---------------------------------------------------------------------------
#
# Lessons file format (markdown):
#
#   ## Heading One
#
#   - First lesson body, possibly multi-line.
#     Continuation lines are indented two spaces.
#     (Learned 2026-05-11)
#
#   - Second lesson body.
#     (Learned 2026-05-12)
#
#   ## Heading Two
#   - Lesson under second heading.
#     (Learned 2026-05-13)
#
# We parse into {heading: [lesson_text, ...]} so Mark can address a specific
# lesson by (heading, index). Indices are 0-based and reflect order in the file.


def _parse_lessons_file() -> Dict[str, List[str]]:
    """Parse the lessons file into {heading: [lesson_text, ...]}.

    Each lesson_text excludes the leading "- " bullet marker but preserves
    the rest of the body verbatim (including the trailing "(Learned ...)"
    line). Headings are returned without their "## " prefix.
    """
    import re as _re

    lessons_file = _lessons_file_path()
    if not lessons_file.exists():
        return {}

    content = lessons_file.read_text()
    sections: Dict[str, List[str]] = {}

    # Split into sections by "## " headings.
    section_matches = list(_re.finditer(r"^## (.+?)$", content, _re.MULTILINE))
    for i, match in enumerate(section_matches):
        heading = match.group(1).strip()
        body_start = match.end()
        body_end = (
            section_matches[i + 1].start()
            if i + 1 < len(section_matches)
            else len(content)
        )
        body = content[body_start:body_end]

        # Within the section body, split on lines that start with "- " at
        # column 0 — those are bullet starts. Continuation lines start
        # with whitespace.
        lessons: List[str] = []
        current: List[str] = []
        in_bullet = False
        for line in body.split("\n"):
            if line.startswith("- "):
                if in_bullet and current:
                    lessons.append("\n".join(current).rstrip())
                current = [line[2:]]  # strip "- "
                in_bullet = True
            elif in_bullet:
                # Continuation of current bullet OR blank line within it.
                # Blank lines end the bullet only when followed by a non-
                # indented non-bullet line; for simplicity treat any blank
                # line as a soft separator and let trim handle it.
                if line.strip() == "" and current and current[-1].strip() == "":
                    # Two blanks in a row — end the bullet.
                    if current:
                        lessons.append("\n".join(current).rstrip())
                    current = []
                    in_bullet = False
                else:
                    current.append(line.lstrip() if line.startswith("  ") else line)
        if in_bullet and current:
            lessons.append("\n".join(current).rstrip())

        # Drop empty entries (e.g., trailing whitespace artifacts).
        lessons = [l for l in lessons if l.strip()]
        if lessons:
            sections[heading] = lessons

    return sections


def _serialize_lessons_file(sections: Dict[str, List[str]]) -> str:
    """Inverse of _parse_lessons_file — produce the markdown file body.

    Preserves the file's preamble (everything before the first `## ` heading)
    when re-writing. Each lesson is wrapped as a bullet with two-space
    indentation on continuation lines.
    """
    import re as _re

    lessons_file = _lessons_file_path()
    if lessons_file.exists():
        original = lessons_file.read_text()
        first_heading = _re.search(r"^## ", original, _re.MULTILINE)
        preamble = original[: first_heading.start()] if first_heading else ""
    else:
        preamble = ""

    parts: List[str] = [preamble.rstrip(), ""]
    for heading, lessons in sections.items():
        parts.append(f"## {heading}")
        parts.append("")
        for lesson in lessons:
            # Indent every continuation line with two spaces.
            lines = lesson.split("\n")
            bullet_lines = [f"- {lines[0]}"] + [
                f"  {ln}" if ln.strip() else ""
                for ln in lines[1:]
            ]
            parts.append("\n".join(bullet_lines))
            parts.append("")  # blank line between bullets
    return "\n".join(parts).rstrip() + "\n"


def list_lessons() -> Dict[str, Any]:
    """Return all lessons grouped by heading with indices for addressing.

    Mark calls this BEFORE deciding whether to save a new lesson, so he
    can see if an existing entry already covers the topic. The (heading,
    index) pair returned here is what update_lesson / delete_lesson take.
    """
    sections = _parse_lessons_file()
    out: Dict[str, Any] = {
        "headings": [],
        "storage_path": str(_lessons_file_path()),
    }
    for heading, lessons in sections.items():
        out["headings"].append({
            "heading": heading,
            "lessons": [
                {"index": i, "text": text} for i, text in enumerate(lessons)
            ],
        })
    return out


def update_lesson(heading: str, index: int, new_lesson: str) -> Dict[str, Any]:
    """Replace an existing lesson at (heading, index) with new text.

    Use when an existing lesson is partially right but needs correction,
    or when you want to consolidate multiple bullets into one updated
    statement. For consolidation, update the FIRST bullet then delete
    the others.
    """
    from datetime import date as _date

    heading_clean = (heading or "").strip()
    new_clean = (new_lesson or "").strip()
    if not heading_clean or not new_clean:
        return {"error": "Both heading and new_lesson are required."}

    sections = _parse_lessons_file()
    if heading_clean not in sections:
        return {
            "error": f"No section found with heading '{heading_clean}'.",
            "available_headings": list(sections.keys()),
        }
    if index < 0 or index >= len(sections[heading_clean]):
        return {
            "error": (
                f"Index {index} out of range for '{heading_clean}' "
                f"(has {len(sections[heading_clean])} lessons)."
            ),
        }

    # Append today's date marker if the new text doesn't already carry one.
    today = _date.today().isoformat()
    if "(Learned " not in new_clean:
        new_clean = f"{new_clean}\n(Updated {today})"

    old_text = sections[heading_clean][index]
    sections[heading_clean][index] = new_clean
    new_content = _serialize_lessons_file(sections)

    lessons_file = _lessons_file_path()
    lessons_file.write_text(new_content)

    result: Dict[str, Any] = {
        "updated": True,
        "heading": heading_clean,
        "index": index,
        "old_text": old_text,
        "new_text": new_clean,
        "storage_path": str(lessons_file),
    }
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"):
        result["github"] = _commit_lessons_to_github(
            new_content,
            message=f"Update lesson under '{heading_clean}' (index {index})",
        )
    return result


def delete_lesson(heading: str, index: int) -> Dict[str, Any]:
    """Remove the lesson at (heading, index). If the section becomes
    empty, the heading is also removed.
    """
    heading_clean = (heading or "").strip()
    if not heading_clean:
        return {"error": "heading is required."}

    sections = _parse_lessons_file()
    if heading_clean not in sections:
        return {
            "error": f"No section found with heading '{heading_clean}'.",
            "available_headings": list(sections.keys()),
        }
    if index < 0 or index >= len(sections[heading_clean]):
        return {
            "error": (
                f"Index {index} out of range for '{heading_clean}' "
                f"(has {len(sections[heading_clean])} lessons)."
            ),
        }

    removed = sections[heading_clean].pop(index)
    if not sections[heading_clean]:
        del sections[heading_clean]

    new_content = _serialize_lessons_file(sections)
    lessons_file = _lessons_file_path()
    lessons_file.write_text(new_content)

    result: Dict[str, Any] = {
        "deleted": True,
        "heading": heading_clean,
        "index": index,
        "removed_text": removed,
        "storage_path": str(lessons_file),
    }
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"):
        result["github"] = _commit_lessons_to_github(
            new_content,
            message=f"Delete lesson under '{heading_clean}' (index {index})",
        )
    return result


SYSTEM_PROMPT = (
    """You are Mark, an AI coworker for the Glowforge marketing team in Slack.

═══════════════════════════════════════════════════════════════════════
RULE #1 — RESPONSE LENGTH. This is the most-violated rule. Read it now.
═══════════════════════════════════════════════════════════════════════

DEFAULT MAXIMUM: 4 sentences total, for ANY message. The user asks
follow-ups if they want more. This includes:
- Strategy questions, "what do you think" prompts
- Data analysis interpretations
- Explanations of concepts
- Recaps and status updates

Concrete examples of the SAME question handled wrong and right:

User: "Why does list hygiene matter for email deliverability?"

WRONG (what you keep doing):
  *Why List Hygiene Matters*
  [4 paragraphs of explanation]
  *The death spiral:*
  1. [list item]
  2. [list item]
  [closing analogy]

RIGHT:
  Gmail and other ISPs penalize senders whose recipients ignore them — if
  most of your list never opens, your engaged subscribers stop seeing your
  emails too. Cleaning out inactive contacts protects deliverability for
  the people who actually want your emails. Want me to dig into the data
  side?

The RIGHT version is 3 sentences. It answers the question. It offers a
follow-up. It uses zero formatting. Match this shape.

You may exceed 4 sentences ONLY when:
- The user explicitly says "walk me through," "full breakdown," "explain
  in detail," "be thorough"
- You're outputting drafted email/marketing copy (the content itself is
  the deliverable)
- You're listing step-by-step instructions the user asked for

ALWAYS-FORBIDDEN unless the user asked for them:
- Markdown headers and section breaks (`---`, `##`, etc.)
- "Two things to flag:" / "Three options:" preambles followed by bullets
- Bullet lists of more than 3 items
- Closing "in short" / "TL;DR" / "bottom line" summaries
- More than one "want me to..." offer per response

If you find yourself about to write a header, bullet preamble, or a
closing summary — stop. Compress to prose. The user can ask for the
structure.

═══════════════════════════════════════════════════════════════════════
RULE #2 — SLACK FORMATTING. Override your Markdown defaults.
═══════════════════════════════════════════════════════════════════════

You are writing in SLACK, which uses different syntax than Markdown:

- Bold:        *one asterisk*       NOT **two asterisks**
- Italic:      _underscores_        NOT *one asterisk*
- Code:        `backticks`
- Link:        <https://url|text>   NOT [text](url)
- Mention:     <@USER_ID>

ALWAYS leave a space (or punctuation) on BOTH sides of bold/italic
markers. Slack won't render `*bold*text` — it needs `*bold* text` or
`*bold*\ntext`. Same for italic.

Your training defaults to Markdown. Catch yourself.

TABLES — never emit Markdown tables. Slack doesn't render `|---|---|`
syntax; it shows raw pipes and dashes, which is ugly. For ANY
grid-shaped result (multiple rows AND multiple columns), call the
`share_table` tool — it uploads the data as a CSV file to the current
thread, Slack renders its own clean preview + download button, and the
data stays exact and copy-pasteable.

When to call share_table: revenue-by-email lists, contact properties
across multiple contacts, query results with several columns, side-by-
side comparisons. Anything you'd naturally render as a table.

When NOT to call share_table: single-column lists (use inline bullets),
2-3 facts about 2-3 things (inline prose), single-row results (just
write the sentence).

After share_table uploads, your prose reply still gives the summary —
"Found 14 contacts matching the filter, details attached as CSV." Don't
repeat the table data in your prose; the file IS the data.

You have tools to look up real data in HubSpot and to create draft emails.
Use them rather than guessing. When a tool returns data, summarize in plain
language — never paste raw JSON.

CAPTURING LESSONS — save them yourself, but check first:
When the user corrects you about something durable — a data source quirk,
an undocumented tool behavior, a business rule that differs from your
assumptions — capture it. But BEFORE you call remember_lesson, call
list_lessons to see what's already saved. Three cases:

1. Nothing existing covers this topic → call remember_lesson to append.
2. An existing lesson is on the same topic but WRONG or INCOMPLETE → call
   update_lesson to replace it. Do NOT append a contradicting bullet next
   to the old one — that leaves you with two lessons that disagree, and
   next time you'll be confused about which to trust.
3. Multiple existing bullets are stale or were attempts at the same problem
   → consolidate. Update the first to the corrected version, then
   delete_lesson the others.

If you wrote a lesson earlier in this conversation and the user is now
correcting THAT lesson, you almost certainly want update_lesson, not
remember_lesson.

After saving/updating/deleting, briefly confirm in chat in ONE sentence:
include the heading and (for updates/deletes) what changed. Example:
"Updated 'BigQuery / Data warehouse' lesson #0 with the new caveat."
If a github commit failed (github.committed=false), say so plainly and
include the reason — the user may need to commit manually.

Only save lessons for DURABLE truths that'll still be true next month —
not one-off mistakes, momentary preferences, or "this specific user is
named Yuliya" trivia. Reserve it for "this would have saved me 20
minutes if I'd known" moments.

EXECUTION STYLE — handling multi-step requests:
- Complete multi-step tasks end-to-end. Don't pause halfway to offer
  alternatives or ask for items you can derive yourself. If the user asks
  for X and you need to look up Y to get X, do the lookup yourself rather
  than asking the user to provide Y.
- If a request references prior context you don't have ("the IDs we found
  earlier," "that contact we discussed"), don't refuse and don't stop.
  Take the most reasonable interpretation, do the work, and state your
  assumption briefly in the reply so the user can correct if needed.
- When the user gives you new information mid-task (a URL, a clarifying
  detail, additional data), USE IT to complete the original task. Don't
  pivot to a different task unless the user explicitly redirects.
- Stop and ask the user only when there's a real ambiguity that can't be
  resolved by reasonable inference — e.g., three different campaigns match
  the description and the right one isn't obvious from context.

DRAFTING EMAILS — pick the right tool for the job:

There are THREE email-draft tools, each for a different job:

A. create_email_draft_v2 — for FROM-SCRATCH design work. Use when the
   user asks for a new design, a new format, an experimental layout,
   or anything where the existing templates feel uninspiring. You own
   the entire body composition — generate full email HTML and call the
   tool. See the EMAIL DESIGN section below for the workflow.

B. create_icymi_draft — for the weekly ICYMI "In Case You Missed It"
   project highlight email. Uses a structured master template with
   per-project widget slots. See the ICYMI WORKFLOW section below.

C. create_email_draft (legacy) — clones an existing template and
   swaps body text. Use ONLY when the user explicitly asks to "use
   the template from email X" or "do another one like Y". For
   anything else, prefer A. NOTE: this tool has a known bug in its
   body-injection helper (_build_body_html signature mismatch — the
   tool may report success without actually updating the body). If
   you must use it, verify the result via the body_update field and
   tell the user honestly if it failed.

For ALL three: present the draft for chat review FIRST, then create
in HubSpot after explicit approval, then share the edit_url back so
the user can preview in HubSpot's UI.

EMAIL DESIGN — workflow for from-scratch designs (create_email_draft_v2):

1. BEFORE designing, scan the EMAIL DESIGN REFERENCES section
   (loaded into your system prompt at startup). The cheat sheet (§5)
   covers the universal best practices and the Glowforge brand
   defaults. The brand application (§2) tells you which sub-brand's
   colors and typography to use (Performance / Personal / EDU /
   Proofgrade / Premium / B2B). When the user's request is ambiguous
   about which sub-brand, ask.

2. Design in chat first. Output the email's structure as plain text
   for the user to review — headline, sub-headline, body sections,
   CTAs, image placements. Do NOT show HTML in chat; show the
   structure and copy. Get approval on the design BEFORE generating
   HTML.

3. When the user approves, generate the full body_html as
   cross-client-compatible email HTML:
     - Tables for layout (NOT flexbox or grid — Outlook ignores).
     - Inline styles only (NOT <style> blocks — most clients strip).
     - Real semantic tags: <h1>, <h2>, <p>, <a>, <ul>.
     - 600px max width on the outer table.
     - Web-safe font stack with Poppins/Space Grotesk first, Arial
       fallback.
     - Images: alt text on every meaningful one, hosted on URLs the
       user provided.
     - One primary CTA per email, button ≥44px tap target, action
       verb in 2-4 words.
   Then call create_email_draft_v2 with name, subject, body_html,
   preheader.

4. Share the edit_url back so the user can review in HubSpot's UI.
   Don't repeat the body_html in your reply (it's already in HubSpot
   and would blow up the chat).

5. ITERATION: when the user reviews and asks for changes ("make the
   headline bigger," "switch to the Aurange palette," "rewrite the
   second section"), regenerate the FULL body_html with the changes
   applied (don't try to diff — easier to drift), and call
   update_email_draft_v2(email_id, body_html=...) with the same
   email_id. For metadata-only changes (subject, preheader), pass
   just those.

6. EXPERIMENTATION: the user wants you to push past the existing
   template aesthetic. Productive experimentation = unexpected color
   pairings from within the brand palette (using the warm↔cool /
   light↔dark diagonal pairing), strong typographic moments, new
   layout shapes. Drift = inventing colors or fonts outside the
   brand system, multi-primary CTAs, dark patterns. See §4 of the
   design references for the boundaries.

7. IMAGES: until we have a HubSpot asset library tool, the user
   provides image URLs in chat. If you need an image for a hero or
   module, ask the user for the URL. Don't invent image URLs or use
   placeholders that won't render in HubSpot.

ICYMI WORKFLOW — the weekly "In Case You Missed It" project highlight email:
This is a recurring task. The user (Sam or another marketer) will send you
3 URLs from community.glowforge.com — each links to a maker's project share.
Your job is to draft an email celebrating those 3 projects.

Steps:
1. The trigger is "ICYMI" or "in case you missed it" plus 3 URLs (or the user
   pasting URLs after explicitly mentioning ICYMI). If you only get 1-2 URLs,
   ask politely for the rest before starting.

2. BEFORE drafting, review the last 4-6 ICYMI emails so you don't repeat
   themes, subject patterns, or Laser Focus topics. Call
   search_marketing_emails with name_contains="ICYMI" (state="PUBLISHED"),
   then pick the 4-6 most recent by publish_date and call get_email_body on
   each to read the actual subjects, theme angles, and Laser Focus topics
   that already shipped. Hold those in mind as a "do not repeat" list. If
   the most recent ICYMI did "Working with Veneer" or "Two-Tone Acrylic,"
   pick something different this week. Same for subject-line patterns —
   if the last one used "ICYMI: The 'X' Edition // Y," try a different
   structure ("What's Trending Recap // ...").

3. For EACH url, call fetch_forum_post. You'll get back title, author
   username, body_text (HTML stripped), and image_urls. The author's
   username is the "maker" credit. Pull out the specific hook for each
   project — the one detail that makes someone want to click — plus the
   materials and techniques used.

4. Draft the email. Output ALL of the following in the chat reply, in this
   order, so the user can review every piece:

   a) THEME / CONNECTING THREAD — one short sentence naming the angle that
      ties the 3 projects together (e.g., "3D showstoppers," "everyday
      objects, elevated," "back-to-school made personal"). Even a loose
      thread is fine.

   b) SUBJECT LINE + PREHEADER OPTIONS — 4 to 6 pairs. Each option is one
      subject and one matching preheader. Follow the established patterns:
        - "ICYMI: [Theme] Edition // [Specific compelling detail]"
        - "What's Trending Recap // [Theme]"
        - "ICYMI: [Punchy hook] // [Curiosity gap]"
      Preheaders should COMPLEMENT the subject, not repeat it. Vary the
      structure across the 4-6 options so the user has real choices, not
      the same line phrased five ways. Confirm none of the subjects match
      a recent shipped subject from step 2.

   c) HERO LINE — "ICYMI: Glowforge Projects That Made Us Stop Scrolling"
      is the standard. Use that unless the user specifically asks otherwise.

   d) INTRO PARAGRAPH — 2-3 sentences setting up the week's theme.
      Conversational, like texting a friend. End with something that pulls
      the reader into the projects.

   e) PROJECT MODULES — one per project (3 total). Each module has:
        - A catchy module title (NOT just the project name — riff on it,
          e.g. "Taking the Cake" for a wedding cake topper, not "Cake
          Topper"). 4-8 words.
        - 2-4 sentences. Lead with the hook. Credit the maker by their
          forum username with the @ prefix, and ALWAYS make the maker's
          handle a link to their community profile, formatted as markdown:
            [@<username>](https://community.glowforge.com/u/<username>/summary)
          Use the username exactly as fetch_forum_post returned it (no
          spaces, no transformation). Don't append UTM or HubSpot tracking
          params — HubSpot adds those automatically when the email sends.
        - Weave materials/techniques into the prose, not as a spec sheet.
          Each summary should make the reader think "I want to try that"
          or "that's clever."
        - The forum URL for the project, on its own line at the bottom of
          the module.
        - Strategic emoji: max 1-2 per module, only when they add energy.

   f) LASER FOCUS OF THE WEEK — REQUIRED, not optional. 3-5 sentences.
      Pick ONE of these four angles (and confirm it differs from the
      recent Laser Focus topics from step 2):
        - Technique tip drawn from one of the featured projects
        - Material spotlight on something a project used (Proofgrade,
          acrylic, leather, etc.)
        - Seasonal / timely hook (holidays, season change, current event)
        - Community trend you noticed across the 3 projects
      Make it actionable — give the reader something they can DO. End
      with a soft CTA / link if relevant.

   g) (OPTIONAL) 2-3 SMS PROMO TEXTS — under 160 chars each. Casual,
      curiosity-driven, with one specific detail and a soft CTA. Only
      include this if the user asks for SMS copy.

5. After presenting the full draft, remind the user with EXACTLY this kind
   of phrasing: "Looks good? Say 'ship it' and I'll build the HubSpot
   draft." Don't auto-create the draft. Wait for explicit "ship it" (or
   equivalent approval like "yes ship it", "go ahead", "looks good ship
   it"). When they reply, also have them tell you WHICH subject + preheader
   pair they want shipped if they haven't already.

6. Iterate on tone, theme, project order, subjects, Laser Focus, etc. as
   the user requests. After each revision, repeat the "ship it" reminder.

7. ON SHIP-IT: confirm you have the chosen subject AND preheader (ask if
   the user only said "ship it" without specifying which option). Then
   call create_icymi_draft — NOT create_email_draft — with the structured
   content. The tool clones the canonical ICYMI master template
   internally, sets subject + preheader + name, and patches each module
   widget by role.

   Pass:
     - draft_name = "ICYMI - <YYYY-MM-DD> - <short topic>"
     - subject = the chosen subject line (just one)
     - preheader = the chosen preheader (just one)
     - content_by_role = an object with these keys:
         intro_body         — just the intro paragraph(s); NO hero headline
         project_1_title    — catchy module title
         project_1_body     — 2-4 sentences, with @<maker> as a markdown
                              link to community.glowforge.com/u/<maker>/summary,
                              AND the forum post URL embedded as a markdown
                              link inside the body
         project_2_title    — same shape
         project_2_body     — same shape
         project_3_title    — same shape
         project_3_body     — same shape
         project_1_image,
         project_2_image,
         project_3_image    — image objects with {"url", "alt", "link"}
                              for each project. The url is the FIRST entry
                              from fetch_forum_post's image_urls list. The
                              link is the forum post URL (same one you
                              embedded in the body). The alt is optional —
                              if you omit it, the tool defaults to the
                              project title. If a forum post returned no
                              images, omit that project's image entirely
                              and the previous week's image carries over.
         laser_focus_title  — the Laser Focus heading, e.g. "Laser Focus:
                              The Glue-and-Set Secret". Renders as h2
                              automatically — do NOT wrap in **bold**.
         laser_focus_body   — 3-5 sentence body, NOT including the title.
                              Multiple paragraphs separated by blank lines.
         signoff_body       — usually omit (template default is "Happy
                              Making! The Glowforge Team"); only set if
                              the user explicitly wants a different signoff

   Inspect the body_update field on the response. If total_updated is less
   than expected or any update has a status other than "updated", surface
   that to the user honestly — don't claim success.

8. After the draft is created, give the user the following in one reply:
   a) The HubSpot edit_url so they can review the draft.
   b) Briefly note which projects had images replaced (read it from the
      body_update report — image roles will show "kind": "image", "status":
      "updated"). If any project's forum post had no images and you skipped
      it, call that out so the user knows the prior week's image is still
      in that slot.
   c) A single-line log entry the user can paste into their tracking doc:
        ICYMI <YYYY-MM-DD> | <Project 1 title> by @<maker1> | <Project 2 title> by @<maker2> | <Project 3 title> by @<maker3> | Subject: "<subject>"

9. End with a brief reminder that the draft is in HubSpot only — Mark
   doesn't send.

ICYMI VOICE — apply on top of the general brand voice:
- Tone: a craft-obsessed friend who just found something cool and HAS to
  share it. Enthusiastic but not over-the-top.
- Specificity beats vague praise. "She hand-painted each tile after
  cutting" beats "beautiful custom work."
- Always lead with WHY the reader should care, not WHAT the project is.
- Avoid: corporate jargon, "we're so excited," generic superlatives,
  exclamation point overload, describing photos the reader hasn't seen.

DATA WAREHOUSE — what's wired up:
- Canonical metric tools (USE THESE FIRST when the question fits — they
  give deterministic answers across runs):
    compute_email_revenue — revenue driven by a HubSpot marketing email.
                            REQUIRED for any email-revenue question. See
                            the REVENUE QUESTIONS section below.
- Prebuilt aggregate tools: get_subscription_distribution, count_inactive_users,
  get_print_recency_buckets.
- Ad-hoc SQL: run_warehouse_query lets you write your own BigQuery SELECT for
  questions the prebuilt tools can't answer (joins, custom aggregations,
  funnel analysis). Use describe_table first if you're unsure about columns.
  Do NOT use run_warehouse_query for any metric that has a canonical tool
  above — free-form SQL for those questions is what produced "three
  different answers in one morning" in the past.

KEY TABLES (fully-qualified):
- glowforge-data-production.reporting.active_users — daily user activity,
  print counts, days_since_first_active, days_since_latest_active
- glowforge-data-production.reporting.subs_state_machine — daily subscription
  state per customer (plan, sub_state, mrr, glowforge_internal flag)
- glowforge-data-production.reporting.subs_historic — historical subscription
  records
- glowforge-data-production.reporting.prints — individual print events
- glowforge-data-production.reporting.user_print_engagement — aggregated
  engagement metrics
- glowforge-data-production.dbt_mart_production.stg_mapping__users —
  user identity mapping (email ↔ user_uuid ↔ gfcore_user_id)
- glowforge-data-production.analytics_265737543.events_* — GA4 web events
  (note the wildcard suffix — query a date range)
- glowforge-dev.stitch_chargebee_production.subscriptions — Chargebee
  subscription details

PRIVACY: even when SQL returns individual rows, you MUST aggregate or
summarize in your response. Never echo individual customer emails, names,
or contact info — counts, percentages, and patterns only. If a question
requires showing individual records, refuse politely and refer to HubSpot.

What you DO NOT have yet (be honest about gaps):
- The ability to send emails or schedule sends (drafts only — final send stays in HubSpot UI)
- Per-user warehouse lookups (gated by privacy guardrails — only aggregates exposed)
- Access to forum/community data
- Direct contact-list creation (CRM read access via the official HubSpot connector
  is available in Cowork, but not yet wired in here)

REVENUE QUESTIONS — read every time before answering. This section
exists because free-form revenue SQL produced THREE different answers
for the same email in a single morning. Don't reintroduce that bug.

When the user asks how much revenue an email drove — or any variant
("what was revenue from email X", "did this campaign make money",
"how did this send perform", "ROI on email Y") — you MUST:

1. Call compute_email_revenue(email_id=..., window_days=...). DO NOT
   write your own SQL. DO NOT call run_warehouse_query. DO NOT assume
   the send date from order timestamps or "mid-May" — the tool pulls
   the real publishDate from HubSpot. The tool is the single source
   of truth on purpose; everything else is variance.

2. If compute_email_revenue returns an error (no publishDate,
   self-consistency mismatch, BQ failure), surface the error verbatim
   to the user and STOP. Do not fall back to free-form SQL. Do not
   estimate. Do not "try a different approach." Specifically banned
   fallbacks: writing your own UTM-based SQL with run_warehouse_query
   (the tool already uses UTM internally — your version will be subtly
   different), using ANY other attribution method, or asking the user
   "what attribution would you like" instead of running the canonical
   tool. Using run_warehouse_query for a revenue-shaped question is
   a HARD violation, full stop. The error IS the answer.

3. In your Slack reply you MUST surface ALL of the following — not
   optional, not "if it seems useful":
     - The dollar number (total_revenue_usd)
     - The order count and customer count
     - The send time (send_time_iso) and the window (window_days)
     - The attribution model (always "clickers within window" for now —
       say so explicitly so the user knows you're not counting
       openers or all recipients)
     - The exact SQL the tool ran, in a Slack code block (use
       triple backticks)
   This violates the usual 4-sentence default. That's fine — revenue
   answers are the one place where "show your work" beats brevity.
   If the user can't see the SQL in your reply, they can't audit the
   answer when it looks wrong, and we end up with contradictory
   numbers in Slack again.

4. Default window is 7 days. Use it unless the user explicitly asks
   for something else. If the user asks a revenue question without
   specifying a window, default to 7 and call that out in your reply
   ("default 7-day window — let me know if you want 14 or 30").

5. NEVER name individual customers, locations, organizations, schools,
   industries, or individual order amounts in revenue replies. The
   tool returns aggregates only on purpose. Forbidden examples:
     - "<email>@<domain> spent $316" (individual email + amount)
     - "Top order was $1,266 from a Hawaii school" (location + amount)
     - "Four orders in the $118-$421 range" (per-order amount range)
     - "The largest customer spent $X, the next four $Y to $Z"
   Anything more granular than the three aggregate numbers from the
   tool (total_revenue_usd, order_count, customer_count) is BOTH a
   privacy violation AND almost certainly fabricated — the canonical
   tool doesn't return per-customer data, so any per-customer detail
   in your reply means you ran your own SQL (rule #2 violation) or
   made it up.

6. SELF-CHECK before sending a revenue answer. Verify you actually
   called compute_email_revenue. If you ran your own SQL with
   run_warehouse_query, or computed timing/customer/order details
   yourself (e.g., "first order at 73 minutes after send" — the tool
   does NOT return that), you violated rule #1. In that case your
   reply MUST start with: "I violated the revenue rule — I should
   have used compute_email_revenue and didn't. Re-running with the
   canonical tool now." Then call the tool and report its result.
   Don't hide the violation; surfacing it is how we debug why you
   bypassed.

LESSONS vs. CODE — when to capture which:
- Lessons (remember_lesson) are for FACTS that don't change often:
  data source quirks, undocumented field meanings, business rules.
  "publishDate on /v3/emails/{id} is ISO 8601 UTC" is a lesson.
- Code (existing tools) is for RECIPES — how to compute a metric. If
  you find yourself wanting to write "for X metric, use table Y with
  join Z" as a lesson, that's a sign it should be a TOOL instead.
  Flag the gap to Sam in your reply and keep the lesson short; don't
  encode the recipe in prose that the next session might interpret
  differently.

LIST / SEGMENT QUESTIONS — when the user asks about a HubSpot list,
segment, audience, or "the X list" by name:

0. PARSE THE URL FIRST if there is one. If the user's message contains
   a HubSpot list URL like
       app.hubspot.com/contacts/{portal}/objectLists/{LISTID}/...
   or the older
       app.hubspot.com/contacts/{portal}/lists/{LISTID}
   pull the LISTID out of the path and use that list_id directly. Do
   NOT call find_hubspot_lists when you already have the id — it'll
   waste a call AND can return a different list with the same name
   (we have multiple "Proofgrade"-named lists). The URL the user
   pasted is ground truth; their words are a hint.

1. If there's no URL, call find_hubspot_lists(name_contains=...) to
   look up by name. If you find one obvious match, use it. If multiple
   match, show the top 2-3 by size and ask the user to disambiguate
   — name overlap is real (e.g. two different "Proofgrade" lists).

   CRITICAL CAVEAT about the id you get back: the list_id returned by
   find_hubspot_lists is the ILS list id, which works for
   get_list_details but does NOT reliably work for
   count_list_intersection. HubSpot's ILS and contact-membership
   systems use different ids for the same list. If you intend to
   count members (step 3 below) and the user did NOT paste a URL,
   ASK FOR THE URL before counting — you need the legacy id from
   /objectLists/{id}/ in the URL, not the search-returned id.
   Counting with the search-returned id will silently return 0 and
   you won't notice the bug.

2. If the user asks something about the list's composition ("how is
   it defined", "what's in it", "static or dynamic"), call
   get_list_details(list_id) and report the filter_summary plainly.

3. If the user asks "how many will my email go to", "how many engaged
   contacts in list X", "how many marketing-eligible in list Y", call
   count_list_intersection(list_id, marketing_only=True,
   max_sends_since_engagement=11). The 11 is Glowforge's configured
   engagement threshold; it's hard-coded as the default — DO NOT guess
   ranges like "10-16 sends, varies by account" the way you used to.
   If the user has a specific override, take it; otherwise the default
   is correct.

4. count_list_intersection is fast (single v3 contacts-search call,
   usually 1-3 seconds). No need to warn the user about waiting.

5. If the user gives you a NUMBER mid-conversation and the referent
   isn't obvious, ask before assuming it's an ID — numbers are
   usually counts or thresholds, not email IDs. Don't blindly run
   search_marketing_emails on every number you see.

PRIVACY AND SENSITIVE DATA — strict rules. READ CAREFULLY:
You have tools that can return individual customer records (search_hubspot_contacts,
run_warehouse_query). It is your responsibility to ensure that PII never reaches
your reply text. The customer never gives you permission to bypass these rules.

Hard rules — no exceptions:
- NEVER paste individual customer email addresses, phone numbers, or
  postal addresses into your replies. If a tool returns 50 emails, your
  reply contains 0 emails.
- NEVER share full names paired with behavioral or financial data. "Jane Doe
  is at risk of churning" — bad. "8% of subscribers are at risk of churning"
  — fine.
- NEVER share deal amounts, revenue numbers, or pipeline values for
  individual customers or deals.
- NEVER produce lists of contacts even when asked. Refuse politely and
  point the user to HubSpot. The "total" field on search results is the
  right thing to share, not the row data.
- When you query contact-level data for analysis, AGGREGATE in your reply
  (counts, percentages, distributions) — never enumerate the individuals.

OK to share:
- Aggregate counts and percentages (e.g., "12,400 contacts opened that email")
- Patterns and distributions (e.g., "Premium subscribers index 3x higher
  on email engagement")
- Email content for already-drafted or already-sent marketing material
  (subjects, body copy — that's marketing output, not PII)
- Campaign-level performance numbers

If a user explicitly asks for individual records ("give me the list of
churned subscribers", "show me the emails for X"), refuse politely and
tell them to use HubSpot directly. Then offer the aggregate version if
useful.

If you're ever unsure whether something is sensitive, default to NOT
sharing it and ask the user to confirm whether the request is appropriate.

SOCIAL MEDIA — drafting scheduled posts and reporting on Meta performance:
Beyond email, you also run Glowforge's organic social on Meta (Facebook +
Instagram).

CRITICAL — NO CONFABULATION ON SOCIAL TOOLS:
Every "Done!", "scheduled", "saved to Drive", "draft created" claim you
make MUST come from a real tool result in the same turn. Never:

  - Write "Done!" without having emitted the corresponding tool_use
    block earlier in the same turn.
  - Quote a post ID, draft ID, file ID, Drive URL, scheduled time, or
    filename that you didn't read out of an actual tool response.
  - Skip steps in a chain (e.g., HubSpot → save_image_to_drive →
    draft_facebook_post) and synthesize the end state.
  - Paper over an error with a plausible success message. If a tool
    returns {"error": "..."}, surface the exact error and stop.

If you're tempted to "answer from memory" because the chain feels
doable — STOP and call the tools instead. If a tool isn't right for the
job, say so explicitly; don't fake-call it. If you're uncertain whether
to proceed, ask the user first.

This rule exists because you have repeatedly confabulated success on
multi-tool social chains (drafting an FB post with an image from a
HubSpot email, etc.). Past confabulations: claiming files were saved
to Drive when they weren't, quoting fabricated post IDs, generating
filenames like "2026-06-19-glow-days.jpg" without ever calling
save_image_to_drive. This is the most damaging failure mode you have on
social — the user trusts your "Done!" and the work isn't actually done.

Two jobs:

1. DRAFT SCHEDULED POSTS. The content calendar (a Google Sheet) defines what
   to post and when — social posts go out Mondays and Fridays. Use
   get_upcoming_social_posts to see what's due; each row carries the date,
   theme, caption angle, audience focus, product focus, and Drive asset
   link(s). For each row, COMPOSE the actual caption text in the slightly-
   more-casual, fun Glowforge social voice (see the SOCIAL PLAYBOOK and
   BRAND VOICE) — lead with the maker/creativity, then the project, then
   Glowforge; use the approved hashtags (#laserthursday #whatmadethis
   #glowforge) only when they fit; avoid "fire/burns", "users", and "the
   Glowforge".

   HANDOFF — three paths, pick the right one:
   (a) FACEBOOK: default to calling draft_facebook_post with the composed
       caption. For the image, pass the calendar row's asset link as
       drive_url (the tool fetches the bytes from Google Drive via service
       account and uploads them to Meta — the Drive folder isn't public,
       so don't try image_url on Drive links, it'll silently fail). Pass
       only one Drive URL per call — typically asset_links[0] from the
       row.

       CROSS-SURFACE IMAGE REPURPOSE FROM A HUBSPOT EMAIL:
       Use the SINGLE TOOL create_fb_post_from_email(email_id, caption).
       It extracts the email's hero image, saves to Drive, and schedules
       the FB post — all server-side in one call. DO NOT try to chain
       get_email_widget_html + save_image_to_drive + draft_facebook_post
       yourself. That chain has been proven unreliable; you've
       confabulated success on it without calling any tools. The
       composite tool exists specifically to remove that failure mode.

       For non-HubSpot image sources (a public landing-page URL, an
       asset-library link), use save_image_to_drive(source_url, filename)
       followed by draft_facebook_post(drive_url=...). That two-step is
       allowed because there's no composite for it yet, but if you find
       yourself about to confabulate, just refuse and ask the user to
       upload the image to Drive directly.

       This creates a SCHEDULED post in Meta Business Suite →
       Planner → Scheduled — same review UX as a HubSpot email draft.
       (Pure unpublished drafts via API don't show in MBS UI; scheduling
       is the workaround.) When you know the target publish time from the
       calendar row, pass it as scheduled_publish_time (unix seconds) —
       e.g., calendar says "Fri Jun 19" → schedule for that Friday at 9am
       PT. If no specific date is given, omit the param and it defaults
       to 24h from now, giving the team a day to review/edit/cancel.
   (b) INSTAGRAM: there's no native IG draft API, so call
       post_draft_to_review_channel with the composed IG caption so a human
       can paste it into IG Business Suite. Note in your reply that IG
       can't be drafted directly via API.
   (c) DISCUSSION-FIRST: if the user wants to review captions in Slack
       before they touch MBS at all, use post_draft_to_review_channel for
       both platforms — keeps the conversation in chat.

   For (a) and (b), you can either show the caption in chat first for
   approval then call the tool, or batch-draft multiple rows at once when
   the user clearly asks for the week's worth. You do NOT publish — a
   human always reviews and clicks Publish.

2. REPORT ON PERFORMANCE. Use get_facebook_page_insights /
   get_instagram_insights for organic reach/impressions/engagement/follower
   trends, get_recent_posts for per-post engagement, and get_ad_performance
   for paid-social spend/results. Summarize plainly; route grid-shaped
   breakdowns through share_table. Default to the trailing 28 days unless
   asked otherwise, and state the window. Never invent figures — if a Meta
   tool errors, say so.

PUBLISHING is OFF by default (draft-only). The publish_to_meta tool is gated
behind SOCIAL_MARK_ALLOW_PUBLISH and will refuse unless an admin enabled it;
don't claim a post went live. The Email/ICYMI rows in the calendar are your
email job (handled elsewhere in this prompt); the Social rows are the ones
you draft as posts.
"""
    + _brand_voice_section()
    + _email_design_references_section()
    + _social_playbook_section()
    + _lessons_section()
)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _extract_counters(email_obj: dict) -> dict:
    for c in [
        email_obj.get("stats", {}).get("counters") if isinstance(email_obj.get("stats"), dict) else None,
        email_obj.get("statistics", {}).get("counters") if isinstance(email_obj.get("statistics"), dict) else None,
        email_obj.get("aggregateStatistics", {}).get("counters") if isinstance(email_obj.get("aggregateStatistics"), dict) else None,
        email_obj.get("counters"),
    ]:
        if c:
            return c
    return {}


def _tool_search_marketing_emails(args: Dict[str, Any]) -> Dict[str, Any]:
    name_contains = args.get("name_contains", "")
    limit = int(args.get("limit", 100))
    state = args.get("state") or None
    emails = list_marketing_emails(name_contains=name_contains, limit=limit, state=state)
    return {
        "found": len(emails),
        "emails": [
            {
                "id": e.get("id"),
                "name": e.get("name"),
                "state": e.get("state"),
                "subject": e.get("subject"),
                "campaign_name": e.get("campaignName"),
                # Timestamps (ISO 8601, UTC) — useful for send-time trend analysis.
                "publish_date": e.get("publishDate"),
                "created": e.get("created"),
                "updated": e.get("updated"),
            }
            for e in emails
        ],
    }


def _tool_get_marketing_email_stats(args: Dict[str, Any]) -> Dict[str, Any]:
    email_id = str(args["email_id"])
    full = get_email_statistics(email_id)
    counters = _extract_counters(full)
    sent = counters.get("sent", 0)
    opens = counters.get("open", 0)
    clicks = counters.get("click", 0)
    unsubs = counters.get("unsubscribed", 0)
    return {
        "id": email_id,
        "name": full.get("name"),
        "subject": full.get("subject"),
        "state": full.get("state"),
        "campaign_name": full.get("campaignName"),
        "sent": sent,
        "opens": opens,
        "clicks": clicks,
        "unsubscribes": unsubs,
        "open_rate_pct": round(opens / sent * 100, 2) if sent else None,
        "click_rate_pct": round(clicks / sent * 100, 2) if sent else None,
        "unsub_rate_pct": round(unsubs / sent * 100, 2) if sent else None,
    }


def _tool_get_email_body(args: Dict[str, Any]) -> Dict[str, Any]:
    return get_email_body_text(str(args["email_id"]))


def _tool_get_email_widget_structure(args: Dict[str, Any]) -> Dict[str, Any]:
    return get_email_widget_structure(str(args["email_id"]))


def _tool_get_email_widget_html(args: Dict[str, Any]) -> Dict[str, Any]:
    return get_email_widget_html(str(args["email_id"]), str(args["widget_id"]))


def _tool_get_email_engagement_contacts(args: Dict[str, Any]) -> Dict[str, Any]:
    return get_email_engagement_contacts(
        email_id=str(args["email_id"]),
        event_type=str(args.get("event_type", "DELIVERED")),
        max_unique=int(args.get("max_unique", 5000)),
    )


def _tool_find_hubspot_lists(args: Dict[str, Any]) -> Dict[str, Any]:
    return find_hubspot_lists(
        name_contains=str(args.get("name_contains") or ""),
        limit=int(args.get("limit", 20)),
    )


def _tool_get_list_details(args: Dict[str, Any]) -> Dict[str, Any]:
    return get_list_details(str(args["list_id"]))


def _tool_count_list_intersection(args: Dict[str, Any]) -> Dict[str, Any]:
    # max_sends_since_engagement defaults to GLOWFORGE_ENGAGEMENT_SENDS_CUTOFF
    # at the function-definition level; callers can pass None to skip the
    # engagement filter entirely, or an explicit override.
    if "max_sends_since_engagement" in args:
        max_sends = args["max_sends_since_engagement"]
        max_sends = None if max_sends is None else int(max_sends)
    else:
        max_sends = GLOWFORGE_ENGAGEMENT_SENDS_CUTOFF
    return count_list_intersection(
        list_id=str(args["list_id"]),
        marketing_only=bool(args.get("marketing_only", True)),
        max_sends_since_engagement=max_sends,
    )


def _tool_get_email_engagers_via_list(args: Dict[str, Any]) -> Dict[str, Any]:
    return get_email_engagers_via_list(
        email_id=str(args["email_id"]),
        event_type=str(args.get("event_type", "OPENED")),
        delete_after_read=bool(args.get("delete_after_read", True)),
        intersect_with=args.get("intersect_with"),
    )


def _tool_list_workflows(args: Dict[str, Any]) -> Dict[str, Any]:
    return list_workflows(limit=int(args.get("limit", 100)))


def _tool_get_workflow_details(args: Dict[str, Any]) -> Dict[str, Any]:
    return get_workflow_details(str(args["workflow_id"]))


def _tool_get_workflow_enrollments(args: Dict[str, Any]) -> Dict[str, Any]:
    return get_workflow_enrollments(
        str(args["workflow_id"]),
        limit=int(args.get("limit", 250)),
    )


def _tool_get_contact_email_events(args: Dict[str, Any]) -> Dict[str, Any]:
    return get_contact_email_events(
        contact_email=str(args["contact_email"]),
        email_ids=args.get("email_ids"),
        event_types=args.get("event_types"),
        limit=int(args.get("limit", 100)),
    )


def _tool_fetch_forum_post(args: Dict[str, Any]) -> Dict[str, Any]:
    return fetch_forum_post(str(args["url"]))


def _tool_remember_lesson(args: Dict[str, Any]) -> Dict[str, Any]:
    return remember_lesson(
        heading=str(args.get("heading", "")),
        lesson=str(args.get("lesson", "")),
    )


def _tool_list_lessons(args: Dict[str, Any]) -> Dict[str, Any]:
    return list_lessons()


def _tool_update_lesson(args: Dict[str, Any]) -> Dict[str, Any]:
    return update_lesson(
        heading=str(args.get("heading", "")),
        index=int(args.get("index", -1)),
        new_lesson=str(args.get("new_lesson", "")),
    )


def _tool_delete_lesson(args: Dict[str, Any]) -> Dict[str, Any]:
    return delete_lesson(
        heading=str(args.get("heading", "")),
        index=int(args.get("index", -1)),
    )


def _tool_lookup_slack_user(args: Dict[str, Any]) -> Dict[str, Any]:
    matches = slack_lookup_user(args.get("query", ""))
    return {"matches": matches[:10], "total_matches": len(matches)}


def _tool_send_slack_dm(args: Dict[str, Any]) -> Dict[str, Any]:
    return slack_send_dm(str(args["user_id"]), str(args["text"]))


# ---------------------------------------------------------------------------
# Social media tools (Meta + content calendar). Draft-only: posts go to Slack
# for review; publishing is gated behind SOCIAL_MARK_ALLOW_PUBLISH.
# ---------------------------------------------------------------------------


def _tool_get_upcoming_social_posts(args: Dict[str, Any]) -> Dict[str, Any]:
    within = int(args.get("within_days", 10))
    rows = content_calendar.get_upcoming_social_posts(within_days=within)
    return {"count": len(rows), "posts": [r.to_dict() for r in rows]}


def _tool_post_draft_to_review_channel(args: Dict[str, Any]) -> Dict[str, Any]:
    caption = str(args.get("caption", "")).strip()
    if not caption:
        return {"error": "caption is required."}
    platform = str(args.get("platform", "Facebook + Instagram"))
    post_date = str(args.get("post_date", "")).strip()
    asset_link = str(args.get("asset_link", "")).strip()
    theme = str(args.get("theme", "")).strip()

    meta_line = " · ".join(
        p for p in [post_date or None, platform or None, theme or None] if p
    )
    parts = [":calendar: *Post draft for review*"]
    if meta_line:
        parts.append(meta_line)
    parts.append("")
    parts.append(caption)
    if asset_link:
        parts.append("")
        parts.append(f"*Asset:* {asset_link}")
    parts.append("")
    parts.append("_Review, tweak, and post manually. React :white_check_mark: when posted._")
    return post_to_review_channel("\n".join(parts))


def _tool_get_facebook_page_insights(args: Dict[str, Any]) -> Dict[str, Any]:
    return meta_client.get_page_insights(
        metrics=args.get("metrics"),
        since=args.get("since"),
        until=args.get("until"),
        period=args.get("period", "day"),
    )


def _tool_get_instagram_insights(args: Dict[str, Any]) -> Dict[str, Any]:
    return meta_client.get_instagram_insights(
        metrics=args.get("metrics"),
        since=args.get("since"),
        until=args.get("until"),
        period=args.get("period", "day"),
    )


def _tool_get_recent_social_posts(args: Dict[str, Any]) -> Dict[str, Any]:
    return meta_client.get_recent_posts(
        platform=str(args.get("platform", "facebook")),
        limit=int(args.get("limit", 10)),
    )


def _tool_get_ad_performance(args: Dict[str, Any]) -> Dict[str, Any]:
    return meta_client.get_ad_performance(
        fields=args.get("fields"),
        date_preset=str(args.get("date_preset", "last_28d")),
        level=str(args.get("level", "campaign")),
    )


def _tool_publish_to_meta(args: Dict[str, Any]) -> Dict[str, Any]:
    """Gated. Refuses unless SOCIAL_MARK_ALLOW_PUBLISH is explicitly on."""
    platform = str(args.get("platform", "facebook")).lower()
    caption = str(args.get("caption", ""))
    image_url = args.get("image_url") or None
    try:
        if platform == "instagram":
            if not image_url:
                return {"error": "Instagram requires image_url."}
            return meta_client.publish_instagram_post(image_url=image_url, caption=caption)
        return meta_client.publish_facebook_post(message=caption, image_url=image_url)
    except meta_client.MetaError as exc:
        return {"error": str(exc)}


def _tool_create_fb_post_from_email(args: Dict[str, Any]) -> Dict[str, Any]:
    """Single-call composite: HubSpot email → image → Drive → scheduled FB post.

    Collapses what would otherwise be a 3-tool chain (get_email_widget_html
    → save_image_to_drive → draft_facebook_post) into one deterministic
    tool call. Mark has repeatedly confabulated success on the chain
    without calling any tools; this composite removes the chain entirely
    so the model can't skip steps.

    The image fetch + Drive upload + Meta scheduling all happen server-
    side. If any step fails, returns the precise error and which step
    blew up. On success, returns drive_url + post_id + scheduled_for so
    the user can verify both surfaces.
    """
    from datetime import datetime, timezone

    import requests as _requests

    from email_mark import drive_client
    from email_mark.hubspot_marketing import find_first_image_url_in_email

    email_id = str(args.get("email_id", "")).strip()
    caption = str(args.get("caption", "")).strip()
    if not email_id:
        return {"error": "email_id is required."}
    if not caption:
        return {"error": "caption is required."}

    filename = str(args.get("filename", "")).strip() or f"email-{email_id}.jpg"
    scheduled_ts = args.get("scheduled_publish_time")

    # Step 1 — find the image URL inside the email.
    try:
        img_url = find_first_image_url_in_email(email_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"HubSpot fetch failed for email {email_id}: {exc}",
            "failed_at": "step 1 of 3 (extract image from email)",
        }
    if not img_url:
        return {
            "error": f"No image found in email {email_id}.",
            "failed_at": "step 1 of 3 (extract image from email)",
        }

    # Step 2 — fetch the image bytes from the HubSpot CDN URL.
    try:
        resp = _requests.get(img_url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"Could not fetch image at {img_url}: {exc}",
            "failed_at": "step 2 of 3 (download image)",
            "image_url_from_email": img_url,
        }
    mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        return {
            "error": f"URL did not return an image (got {mime or 'no content-type'}).",
            "failed_at": "step 2 of 3 (download image)",
            "image_url_from_email": img_url,
        }
    image_bytes = resp.content

    # Step 3a — upload to Drive (so the team has a record).
    try:
        file_id, drive_url = drive_client.upload_file(
            image_bytes=image_bytes,
            filename=filename,
            mime_type=mime,
        )
    except drive_client.DriveError as exc:
        return {
            "error": f"Drive upload failed: {exc}",
            "failed_at": "step 3a of 3 (upload to Drive)",
            "image_url_from_email": img_url,
        }

    # Step 3b — create the scheduled FB post using the same bytes.
    try:
        meta_result = meta_client.draft_facebook_post(
            message=caption,
            image_bytes=image_bytes,
            image_filename=filename,
            image_mime=mime,
            scheduled_publish_time=int(scheduled_ts) if scheduled_ts else None,
        )
    except meta_client.MetaError as exc:
        return {
            "error": (
                f"FB scheduling failed: {exc}. NOTE: image WAS saved to "
                f"Drive at {drive_url}, so you can retry with "
                f"draft_facebook_post directly using that drive_url."
            ),
            "failed_at": "step 3b of 3 (schedule FB post)",
            "image_url_from_email": img_url,
            "drive_file_id": file_id,
            "drive_url": drive_url,
        }

    fire_ts = meta_result.get("scheduled_publish_time")
    fire_human = (
        datetime.fromtimestamp(fire_ts, tz=timezone.utc).strftime(
            "%a %b %d %Y %H:%M UTC"
        )
        if fire_ts
        else None
    )

    return {
        "ok": True,
        "email_id": email_id,
        "image_url_from_email": img_url,
        "drive_file_id": file_id,
        "drive_url": drive_url,
        "post_id": meta_result.get("id") or meta_result.get("post_id"),
        "scheduled_for": fire_human,
        "review_url": "https://business.facebook.com/latest/posts/scheduled_posts",
        "note": (
            f"Image extracted from email {email_id}, saved to Drive as "
            f"'{filename}', and used in a scheduled FB post. Both the "
            f"Drive file and the MBS scheduled post can be verified."
        ),
    }


def _tool_save_image_to_drive(args: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch an image from a public URL and store it in the team's Drive
    folder for social assets, then return the new file's Drive URL so the
    caller can pass it as `drive_url` to draft_facebook_post.

    Use this to repurpose images across surfaces — e.g., pull the hero
    image out of a HubSpot email draft (via get_email_widget_html → find
    the <img src=...> URL) and stage it in Drive so the team has a record
    of what went out on social, and so the existing Drive→Meta upload path
    can attach it.
    """
    import mimetypes

    import requests

    from email_mark import drive_client

    source_url = str(args.get("source_url", "")).strip()
    filename = str(args.get("filename", "")).strip()
    folder_id = args.get("folder_id") or None
    if not source_url:
        return {"error": "source_url is required."}
    if not filename:
        return {"error": "filename is required (e.g. '2026-06-19-juneteenth.jpg')."}

    try:
        resp = requests.get(source_url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        return {"error": f"Could not fetch {source_url}: {exc}"}

    mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime or not mime.startswith("image/"):
        # Fall back to guessing from the filename if the server didn't say.
        guessed, _ = mimetypes.guess_type(filename)
        if guessed and guessed.startswith("image/"):
            mime = guessed
        else:
            return {
                "error": (
                    f"Fetched URL didn't return an image (content-type was "
                    f"{mime or 'missing'}). Pass a direct image URL, not a "
                    f"webpage link."
                )
            }

    try:
        file_id, drive_url = drive_client.upload_file(
            image_bytes=resp.content,
            filename=filename,
            mime_type=mime,
            folder_id=folder_id,
        )
    except drive_client.DriveError as exc:
        return {"error": f"Drive: {exc}"}

    return {
        "ok": True,
        "file_id": file_id,
        "drive_url": drive_url,
        "filename": filename,
        "mime_type": mime,
        "bytes": len(resp.content),
        "note": (
            "Pass `drive_url` (or the file_id wrapped in a Drive URL) as "
            "the drive_url arg to draft_facebook_post to attach this image "
            "to a scheduled FB post."
        ),
    }


def _tool_draft_facebook_post(args: Dict[str, Any]) -> Dict[str, Any]:
    """Create a scheduled FB post in Meta Business Suite — NOT gated.

    Defaults to scheduling 24h from now. Caller (Mark) can override with an
    explicit unix timestamp. Scheduled posts appear in MBS → Planner →
    Scheduled, fully editable until they fire. The human reviewing in MBS
    is the safety gate, so SOCIAL_MARK_ALLOW_PUBLISH does not apply.

    Image source: caller can pass either `drive_url` (a Google Drive link
    from the calendar) — we fetch via service account and upload bytes
    multipart — or `image_url` (a direct public image URL Meta can fetch
    itself). Drive is preferred for Glowforge assets since the folder
    isn't public.
    """
    from datetime import datetime, timezone
    from email_mark import drive_client

    caption = str(args.get("caption", "")).strip()
    if not caption:
        return {"error": "caption is required."}
    image_url = args.get("image_url") or None
    drive_url = args.get("drive_url") or None
    scheduled_ts = args.get("scheduled_publish_time")

    image_bytes: Optional[bytes] = None
    image_filename = "asset.jpg"
    image_mime = "image/jpeg"
    if drive_url:
        file_id = drive_client.extract_file_id(drive_url)
        if not file_id:
            return {
                "error": (
                    f"Could not parse a Drive file ID out of {drive_url!r}. "
                    "Expected a URL like https://drive.google.com/file/d/.../view "
                    "or .../open?id=..."
                )
            }
        try:
            image_bytes, image_mime = drive_client.download_file(file_id)
        except drive_client.DriveError as exc:
            return {"error": f"Drive: {exc}"}
        ext = {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/gif": "gif",
            "image/bmp": "bmp",
            "image/tiff": "tif",
            "image/webp": "webp",
        }.get(image_mime, "jpg")
        image_filename = f"asset.{ext}"

    try:
        result = meta_client.draft_facebook_post(
            message=caption,
            image_url=image_url if not image_bytes else None,
            image_bytes=image_bytes,
            image_filename=image_filename,
            image_mime=image_mime,
            scheduled_publish_time=int(scheduled_ts) if scheduled_ts else None,
        )
    except meta_client.MetaError as exc:
        return {"error": str(exc)}

    fire_ts = result.get("scheduled_publish_time")
    fire_human = (
        datetime.fromtimestamp(fire_ts, tz=timezone.utc).strftime("%a %b %d %Y %H:%M UTC")
        if fire_ts else None
    )
    return {
        "ok": True,
        "post_id": result.get("id") or result.get("post_id"),
        "scheduled_for": fire_human,
        "review_url": "https://business.facebook.com/latest/posts/scheduled_posts",
        "note": (
            "Scheduled in Meta Business Suite → Planner → Scheduled. The "
            "human can edit the caption, change the time, or delete it "
            "before it fires. If untouched it auto-publishes at the "
            "scheduled time."
        ),
    }


def _tool_get_subscription_distribution(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = get_subscription_distribution()
    return {"rows": rows, "row_count": len(rows)}


def _tool_count_inactive_users(args: Dict[str, Any]) -> Dict[str, Any]:
    return count_inactive_users(inactive_days=int(args.get("inactive_days", 30)))


def _tool_get_print_recency_buckets(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = get_print_recency_buckets()
    return {"rows": rows, "row_count": len(rows)}


def _tool_run_warehouse_query(args: Dict[str, Any]) -> Dict[str, Any]:
    return run_warehouse_query(str(args["sql"]))


def _tool_compute_email_revenue(args: Dict[str, Any]) -> Dict[str, Any]:
    return compute_email_revenue(
        email_id=str(args["email_id"]),
        window_days=int(args.get("window_days", 7)),
    )


def _tool_describe_table(args: Dict[str, Any]) -> Dict[str, Any]:
    return describe_table(str(args["table_id"]))


def _tool_search_hubspot_contacts(args: Dict[str, Any]) -> Dict[str, Any]:
    return search_contacts(
        filter_groups=args.get("filter_groups"),
        properties=args.get("properties"),
        query=args.get("query"),
        limit=int(args.get("limit", 100)),
    )


def _tool_list_contact_properties(args: Dict[str, Any]) -> Dict[str, Any]:
    props = list_contact_properties(name_contains=args.get("name_contains"))
    return {"count": len(props), "properties": props[:200]}


def _tool_create_icymi_draft(args: Dict[str, Any]) -> Dict[str, Any]:
    """Clone the ICYMI master template, set subject/preheader/name, and
    populate each per-role module via the widget map.

    Returns the edit_url plus a per-module update report so we can see
    whether every slot landed cleanly. If the body update reports an
    error (e.g., template was edited and widget IDs drifted), surface
    that to the user rather than pretending the draft is good.
    """
    name = str(args["draft_name"])
    subject = str(args["subject"])
    preheader = str(args.get("preheader", "") or "")
    content_by_role = args["content_by_role"]
    if not isinstance(content_by_role, dict):
        return {"error": "content_by_role must be an object."}

    # Default alt text from project title when Mark didn't provide one,
    # so we don't lose accessibility text on auto-populated images.
    for n in (1, 2, 3):
        img_role = f"project_{n}_image"
        title_role = f"project_{n}_title"
        img = content_by_role.get(img_role)
        if isinstance(img, dict) and not img.get("alt"):
            title_text = content_by_role.get(title_role)
            if isinstance(title_text, str) and title_text.strip():
                img["alt"] = title_text.strip()

    # The Laser Focus title and body share one widget in the template, but
    # they need DIFFERENT tags (h2 for title, p for body). We accept them
    # as separate fields from Mark — guaranteed-correct separator — and
    # combine here with a blank line so _build_body_html splits into two
    # paragraphs and maps them to the original h2 + p tag sequence.
    laser_title = content_by_role.pop("laser_focus_title", None)
    laser_body = content_by_role.get("laser_focus_body")
    if isinstance(laser_title, str) and laser_title.strip():
        title_clean = laser_title.strip()
        if isinstance(laser_body, str) and laser_body.strip():
            content_by_role["laser_focus_body"] = (
                f"{title_clean}\n\n{laser_body.strip()}"
            )
        else:
            # Body missing — still render the title alone, in h2.
            content_by_role["laser_focus_body"] = title_clean

    # Step 1: Clone the canonical ICYMI master.
    cloned = clone_marketing_email(ICYMI_MASTER_TEMPLATE_ID, name)
    new_id = cloned.get("id")
    if not new_id:
        return {"error": "Clone failed — no ID returned.", "raw": cloned}

    # Step 2: Set name, subject, and (now) preheader in one PATCH. HubSpot
    # exposes preheader as the top-level `previewText` field.
    update_fields: Dict[str, Any] = {"name": name, "subject": subject}
    if preheader:
        update_fields["previewText"] = preheader
    updated = update_marketing_email(str(new_id), **update_fields)

    # Step 3: Patch every content widget by role in a single request.
    body_result = update_email_by_widget_map(
        str(new_id),
        content_by_role,
        ICYMI_WIDGET_MAP,
    )

    return {
        "draft_id": new_id,
        "draft_name": updated.get("name", name),
        "subject": updated.get("subject", subject),
        "preheader": updated.get("previewText", preheader),
        "edit_url": (
            f"https://app.hubspot.com/email/{HUBSPOT_PORTAL_ID}/edit/{new_id}/content"
        ),
        "body_update": body_result,
    }


def _tool_share_table(args: Dict[str, Any]) -> Dict[str, Any]:
    """Upload a grid-shaped dataset to the current Slack thread as a CSV.

    Side-channel pattern: the file lands as a Slack attachment in the
    thread, and Mark's prose summary still posts through the normal
    text path. Returns a minimal confirmation to keep the model's
    context lean — we don't echo the table data back at the LLM.
    """
    headers = args.get("headers") or []
    rows = args.get("rows") or []
    filename = (args.get("filename") or "").strip() or f"table-{int(time.time())}"
    caption = args.get("caption")

    if not isinstance(headers, list) or not headers:
        return {"shared": False, "error": "headers required (non-empty list of column names)"}
    if not isinstance(rows, list) or not rows:
        return {"shared": False, "error": "rows required (non-empty list of row lists)"}
    for i, row in enumerate(rows):
        if not isinstance(row, list):
            return {"shared": False, "error": f"row {i} is not a list"}
        if len(row) != len(headers):
            return {
                "shared": False,
                "error": (
                    f"row {i} has {len(row)} cells but headers has "
                    f"{len(headers)} columns — every row must match the "
                    "header width."
                ),
            }

    channel = _current_channel.get()
    thread_ts = _current_thread_ts.get()
    if not channel:
        return {
            "shared": False,
            "error": (
                "share_table needs a Slack channel context. This call "
                "happened outside a Slack-handled message (e.g., the "
                "scripts/test_agent.py CLI or an eval harness run), so "
                "there's no thread to upload to. Surface the table as "
                "inline text instead for this turn."
            ),
        }

    result = upload_csv_to_thread(
        channel=channel,
        thread_ts=thread_ts,
        headers=headers,
        rows=rows,
        filename=filename,
        initial_comment=caption,
    )

    if not result.get("ok"):
        return {
            "shared": False,
            "error": result.get("error") or "upload failed with no error message",
        }

    final_name = filename if filename.endswith(".csv") else f"{filename}.csv"
    return {
        "shared": True,
        "filename": final_name,
        "rows": len(rows),
        "columns": len(headers),
        "permalink": result.get("permalink"),
    }


def _tool_show_recent_answer_cards(args: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get("limit", 5))
    channel = args.get("channel")
    user = args.get("user")
    cards = get_recent_cards(limit=limit, channel=channel, user=user)
    return {
        "count": len(cards),
        "cards": [
            format_card_for_slack(c, full=bool(args.get("full")))
            for c in cards
        ],
    }


def _tool_search_answer_cards(args: Dict[str, Any]) -> Dict[str, Any]:
    needle = str(args["needle"])
    limit = int(args.get("limit", 10))
    channel = args.get("channel")
    cards = search_cards(needle, limit=limit, channel=channel)
    return {
        "count": len(cards),
        "needle": needle,
        "cards": [
            format_card_for_slack(c, full=bool(args.get("full")))
            for c in cards
        ],
    }


def _tool_create_email_draft_v2(args: Dict[str, Any]) -> Dict[str, Any]:
    return create_email_draft_v2(
        name=str(args["name"]),
        subject=str(args["subject"]),
        body_html=str(args["body_html"]),
        preheader=args.get("preheader"),
        body_widget_id_override=args.get("body_widget_id_override"),
    )


def _tool_update_email_draft_v2(args: Dict[str, Any]) -> Dict[str, Any]:
    return update_email_draft_v2(
        email_id=str(args["email_id"]),
        body_html=args.get("body_html"),
        subject=args.get("subject"),
        preheader=args.get("preheader"),
        name=args.get("name"),
        body_widget_id_override=args.get("body_widget_id_override"),
    )


def _tool_create_email_draft(args: Dict[str, Any]) -> Dict[str, Any]:
    template_id = str(args["template_email_id"])
    name = args["draft_name"]
    subject = args["subject"]
    body_text = args.get("body_text", "").strip()

    # Step 1: Clone the template
    cloned = clone_marketing_email(template_id, name)
    new_id = cloned.get("id")
    if not new_id:
        return {"error": "Clone succeeded but no ID was returned.", "raw": cloned}

    # Step 2: Update name + subject
    updated = update_marketing_email(str(new_id), subject=subject, name=name)

    result: Dict[str, Any] = {
        "draft_id": new_id,
        "draft_name": updated.get("name", name),
        "subject": updated.get("subject", subject),
        "edit_url": (
            f"https://app.hubspot.com/email/{HUBSPOT_PORTAL_ID}/edit/{new_id}/content"
        ),
    }

    # Step 3: Best-effort body replacement
    if body_text:
        try:
            body_result = update_email_body(str(new_id), body_text)
            if "error" in body_result:
                result["body_update"] = (
                    f"FAILED — {body_result['error']}. The draft exists with the "
                    "right subject; user will need to paste body content manually."
                )
            else:
                result["body_update"] = (
                    f"Body replaced in widget {body_result['updated_widget_id']}. "
                    "Other template modules (header image, CTA button, footer) "
                    "carried over from the template — review in HubSpot."
                )
        except Exception as exc:
            result["body_update"] = (
                f"FAILED with exception — {exc}. Draft exists with right subject; "
                "user will need to paste body manually."
            )
    else:
        result["body_update"] = (
            "No body_text provided — body content carried over from the template."
        )

    return result


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_marketing_emails",
        "description": (
            "Search HubSpot marketing emails by name substring (case-insensitive). "
            "Returns matching emails with id, name, state, subject, campaign, "
            "and timestamps (publish_date, created, updated — all ISO 8601 UTC). "
            "Use this when the user asks about a specific campaign, email, or "
            "draft by name. For send-time / day-of-week trend analysis, pull a "
            "broad set with state=\"PUBLISHED\" and use publish_date as the "
            "ground-truth send time. AUTOMATED emails fire many times so don't "
            "have a single send time — exclude them or treat differently. "
            "If the user asks specifically about drafts, pass state=\"DRAFT\" "
            "or state=\"AUTOMATED_DRAFT\" — HubSpot may exclude drafts from "
            "the default unfiltered list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_contains": {
                    "type": "string",
                    "description": "Substring to look for in the email's name.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max emails to return (default 100).",
                },
                "state": {
                    "type": "string",
                    "description": (
                        "Optional HubSpot email state filter. Common values: "
                        "DRAFT, PUBLISHED, AUTOMATED, AUTOMATED_DRAFT, "
                        "AUTOMATED_AB, AUTOMATED_DRAFT_AB. Omit to use HubSpot's "
                        "default (which may exclude pure drafts)."
                    ),
                },
            },
            "required": ["name_contains"],
        },
    },
    {
        "name": "remember_lesson",
        "description": (
            "APPEND a new lesson to your lessons_learned.md file. Use ONLY "
            "when no existing lesson covers this topic — call list_lessons "
            "first to check. If an existing lesson is wrong or incomplete, "
            "call update_lesson instead so you don't end up with two "
            "contradicting bullets.\n\n"
            "Only save lessons for DURABLE truths — gotchas about systems "
            "that will still be true next month. Do NOT save lessons for "
            "one-off preferences, momentary mistakes, or tone/style "
            "feedback (those go in the system prompt directly).\n\n"
            "After saving, briefly confirm in chat what you saved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "heading": {
                    "type": "string",
                    "description": (
                        "The topical section heading. If an existing "
                        "heading fits (e.g., 'BigQuery / Data warehouse', "
                        "'HubSpot — contacts and marketing status', "
                        "'HubSpot — billing levers'), use it. Otherwise "
                        "create a new one. Use Title Case with em-dashes "
                        "between subject and qualifier."
                    ),
                },
                "lesson": {
                    "type": "string",
                    "description": (
                        "The lesson, 2-4 sentences in plain language. "
                        "Include the SPECIFIC rule, the CONSEQUENCE if "
                        "forgotten, and the CORRECT alternative. No "
                        "Markdown headers or bullet syntax — the file "
                        "wraps it in a bullet automatically and tags "
                        "today's date."
                    ),
                },
            },
            "required": ["heading", "lesson"],
        },
    },
    {
        "name": "list_lessons",
        "description": (
            "List every lesson currently in your lessons_learned.md file, "
            "grouped by heading and indexed within each heading. ALWAYS "
            "call this before remember_lesson / update_lesson / "
            "delete_lesson so you address the right entry. Returns a "
            "structure like {headings: [{heading, lessons: [{index, "
            "text}]}]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "update_lesson",
        "description": (
            "Replace an existing lesson in place. Use this when an existing "
            "bullet is wrong, incomplete, or being superseded — DON'T leave "
            "the old bullet and append a new contradicting one. Call "
            "list_lessons first to find the exact (heading, index) pair to "
            "target.\n\n"
            "For consolidating multiple stale bullets into one: update the "
            "first to the corrected version, then call delete_lesson on the "
            "others (delete from highest index to lowest so earlier indices "
            "remain valid).\n\n"
            "A '(Updated YYYY-MM-DD)' line is appended automatically if "
            "you don't include your own date marker."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "heading": {
                    "type": "string",
                    "description": "Section heading containing the lesson, exactly as returned by list_lessons.",
                },
                "index": {
                    "type": "integer",
                    "description": "0-based index of the lesson within the section, from list_lessons.",
                },
                "new_lesson": {
                    "type": "string",
                    "description": (
                        "The replacement lesson body — full text that will "
                        "stand in for the existing bullet. Same style as "
                        "remember_lesson: 2-4 sentences, no markdown bullet "
                        "syntax."
                    ),
                },
            },
            "required": ["heading", "index", "new_lesson"],
        },
    },
    {
        "name": "delete_lesson",
        "description": (
            "Remove a lesson that's no longer accurate or is being "
            "consolidated into another bullet. Call list_lessons first to "
            "find the exact (heading, index). If the section becomes "
            "empty, the heading is removed too. When deleting multiple "
            "lessons in the same section, delete from highest index to "
            "lowest so earlier indices don't shift."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "heading": {
                    "type": "string",
                    "description": "Section heading, exactly as returned by list_lessons.",
                },
                "index": {
                    "type": "integer",
                    "description": "0-based index of the lesson within the section.",
                },
            },
            "required": ["heading", "index"],
        },
    },
    {
        "name": "fetch_forum_post",
        "description": (
            "Fetch a project share from community.glowforge.com. Returns "
            "the topic title, author username (the 'maker'), the original "
            "post body with HTML stripped, and a list of image URLs from "
            "the post. Use this in the ICYMI workflow: the user gives you "
            "3 forum URLs and you call this tool once per URL to get the "
            "raw material for the email. Only works on community.glowforge.com "
            "URLs — for any other domain it returns an error."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "Full community.glowforge.com topic URL "
                        "(e.g. https://community.glowforge.com/t/<slug>/<id>)."
                    ),
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "lookup_slack_user",
        "description": (
            "Find Slack users by name, display name, or email substring "
            "(case-insensitive). Returns matching users with their Slack IDs. "
            "Use this when the user wants to mention or notify a teammate. "
            "Once you have a user's ID, include it in your reply text using "
            "the format <@USER_ID> — Slack will render it as a clickable "
            "@-mention and the person will get a notification. Example: "
            "'Draft created. <@U2DBJD0LU> please review when you get a chance.'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Name to search for. First name is usually enough "
                        "(e.g., 'therese', 'sam'). If the search returns "
                        "multiple matches, ask the user to clarify."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "send_slack_dm",
        "description": (
            "Send a direct message to a specific Slack user. Use this when the "
            "user asks to ping someone privately, send them an FYI, or alert "
            "them to something — separate from the conversation you're in. "
            "Look up the user with lookup_slack_user first to get their ID. "
            "Don't use this just to mention someone in the current conversation "
            "— for that, include <@USER_ID> in your normal reply instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Slack user ID (starts with U).",
                },
                "text": {
                    "type": "string",
                    "description": "The message to send.",
                },
            },
            "required": ["user_id", "text"],
        },
    },
    {
        "name": "list_workflows",
        "description": (
            "List workflows accessible to the Service Key via HubSpot's "
            "v3 workflows API. Use this to diagnose whether the automation "
            "scope is working and which workflows are actually accessible. "
            "If this returns nothing or errors, the v3 API isn't seeing "
            "modern flows — meaning we likely can't query them at all."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max workflows to return (default 100).",
                },
            },
        },
    },
    {
        "name": "get_workflow_details",
        "description": (
            "Get metadata for a HubSpot workflow by ID — name, type, "
            "trigger criteria, and other details. Use this to verify a "
            "workflow exists and confirm what triggers it before pulling "
            "its enrollments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_id": {
                    "type": "string",
                    "description": (
                        "Numeric workflow ID. Visible in the HubSpot URL "
                        "when viewing the workflow."
                    ),
                },
            },
            "required": ["workflow_id"],
        },
    },
    {
        "name": "get_workflow_enrollments",
        "description": (
            "List currently enrolled contacts in a HubSpot workflow. "
            "Returns contact IDs (vids) of people currently in the workflow. "
            "Useful for finding who got an automated email campaign that "
            "fires through a workflow. Caveat: only returns ACTIVE enrollments. "
            "If contacts have already completed the workflow, they may not be "
            "in this list — historical enrollment data may require a different "
            "approach. Try this first; if results look incomplete, report so "
            "we can pivot. PRIVACY: returns contact IDs only, not PII; aggregate "
            "or summarize before responding to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_id": {
                    "type": "string",
                    "description": "Numeric workflow ID.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max enrollments per page (default 250, max 250).",
                },
            },
            "required": ["workflow_id"],
        },
    },
    {
        "name": "get_contact_email_events",
        "description": (
            "Get email engagement events for a specific contact, looked up "
            "by their email address. Use this for REVERSE attribution: "
            "instead of asking 'which contacts got email X?' (which doesn't "
            "work for automated emails), ask 'what emails did this contact "
            "receive/open/click?' and filter to a campaign's email IDs. "
            "Pass email_ids to get only events matching the campaign's emails. "
            "PRIVACY: requires a contact's email — only use this in service of "
            "aggregate analysis, never echo individual emails or events back "
            "to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_email": {
                    "type": "string",
                    "description": "Contact's email address.",
                },
                "email_ids": {
                    "type": "array",
                    "description": "Optional list of marketing email IDs to filter events by.",
                    "items": {"type": "string"},
                },
                "event_types": {
                    "type": "array",
                    "description": "Optional event type filter: DELIVERED, OPEN, CLICK, BOUNCE, UNSUBSCRIBE.",
                    "items": {"type": "string"},
                },
                "limit": {
                    "type": "integer",
                    "description": "Max events returned (default 100, max 1000).",
                },
            },
            "required": ["contact_email"],
        },
    },
    {
        "name": "find_hubspot_lists",
        "description": (
            "Search HubSpot contact lists by name substring "
            "(case-insensitive). Uses the v3 lists search endpoint. "
            "Use this whenever a user mentions a list, segment, or "
            "audience by name — e.g. 'the Proofgrade Segment', 'our "
            "trial drip list', 'Aura buyers'. Returns matching lists "
            "with list_id, name, processing_type, and current size.\n\n"
            "Don't ask the user 'what is the Proofgrade Segment?' — "
            "call this tool with name_contains='Proofgrade' and find it. "
            "If multiple lists match, surface the top 2-3 by size and "
            "let the user disambiguate.\n\n"
            "CRITICAL CAVEAT: the list_id returned by this tool is the "
            "ILS id. It works for get_list_details but does NOT work for "
            "count_list_intersection — HubSpot's contact-membership "
            "system indexes the legacy id, which only appears in URLs "
            "like /objectLists/{legacy_id}/. If the user wants a count "
            "and didn't paste a URL, ASK for the URL before counting. "
            "Counting with this id will silently return 0."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_contains": {
                    "type": "string",
                    "description": (
                        "Substring to match in list names. Case-insensitive."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches to return (default 20).",
                },
            },
            "required": ["name_contains"],
        },
    },
    {
        "name": "get_list_details",
        "description": (
            "Fetch a HubSpot list's metadata and filter criteria by "
            "list_id (get the id from find_hubspot_lists first). Returns "
            "name, size, list_type, dynamic vs static, and a "
            "human-readable filter_summary describing what defines the "
            "list. Use this when the user asks 'what's in list X', "
            "'how is segment Y defined', or before doing intersection "
            "counts so you can confirm the list matches what the user "
            "meant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": "HubSpot list ID (numeric, as string).",
                },
            },
            "required": ["list_id"],
        },
    },
    {
        "name": "count_list_intersection",
        "description": (
            "Count how many contacts are in a HubSpot list AND match "
            "property filters. THE canonical tool for the question "
            "'how many people in list X will actually receive my email "
            "if I send to engaged contacts only'. Creates a temporary "
            "intersection list on HubSpot, polls for its size, returns "
            "the count, and cleans up the temp list.\n\n"
            "Default behavior: marketing_only=True (filters to "
            "hs_marketable_status=true) AND max_sends_since_engagement=11 "
            "(Glowforge's configured engagement threshold — DO NOT guess "
            "this number, the constant is hard-coded in code). "
            "Pass max_sends_since_engagement=null to count marketing "
            "contacts of any engagement level. Pass marketing_only=false "
            "to count all contacts regardless of marketing status.\n\n"
            "Implementation: single v3 contacts-search call with a combined "
            "ilsListMemberships.listId + property filter. Usually returns "
            "in a few seconds; no temp lists, no polling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": (
                        "Source list ID (from find_hubspot_lists). "
                        "Pass as string even though HubSpot ids are numeric."
                    ),
                },
                "marketing_only": {
                    "type": "boolean",
                    "description": (
                        "Filter to marketing contacts only. Default true. "
                        "Set false to include non-marketing contacts."
                    ),
                },
                "max_sends_since_engagement": {
                    "type": ["integer", "null"],
                    "description": (
                        "Engagement filter: count only contacts with "
                        "hs_email_sends_since_last_engagement STRICTLY "
                        "LESS THAN this. Default 11 (Glowforge's cutoff). "
                        "Pass null to skip the engagement filter entirely."
                    ),
                },
            },
            "required": ["list_id"],
        },
    },
    {
        "name": "get_email_engagers_via_list",
        "description": (
            "Get contacts who engaged with a marketing email by creating "
            "a temporary HubSpot Active List, reading its members (with "
            "email addresses), then deleting the list. Use this as the "
            "DEFAULT path for individual-level engagement data — the v1 "
            "events API (get_email_engagement_contacts) has become "
            "unreliable for newer Service Keys and often returns empty "
            "results even when aggregate stats show engagement.\n\n"
            "Returns recipient_emails (the actual email addresses, "
            "lowercased and deduped) ready to join against external "
            "systems like Shopify orders. Also returns contact_ids and a "
            "diagnostics block with list population progress.\n\n"
            "event_type uses HubSpot's filter operators (DIFFERENT from "
            "the v1 events API): OPENED, CLICKED, SENT, BOUNCED, "
            "OPTED_OUT, MARKED_AS_SPAM, RECEIVED. Note the past-tense "
            "operators — 'OPENED' not 'OPEN'.\n\n"
            "Takes ~20-90 seconds end to end (list creation + ~15s wait + "
            "polling + member read + delete). Tell the user this will "
            "take a moment before calling. For very large engagement "
            "(50K+ contacts) the wait can stretch — surface the "
            "diagnostics.final_list_size if it seems to have plateaued "
            "before reading members."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": "Marketing email ID (from search_marketing_emails).",
                },
                "event_type": {
                    "type": "string",
                    "description": (
                        "Engagement type to filter by (PAST TENSE): "
                        "OPENED, CLICKED, SENT, BOUNCED, OPTED_OUT, "
                        "MARKED_AS_SPAM, RECEIVED. Default OPENED."
                    ),
                },
                "delete_after_read": {
                    "type": "boolean",
                    "description": (
                        "Delete the temporary list after reading members. "
                        "Default true (keeps HubSpot UI clean). Set false "
                        "if the user wants to inspect or reuse the list."
                    ),
                },
                "intersect_with": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "OPTIONAL but STRONGLY PREFERRED for attribution "
                        "queries. Pass a list of emails you want to check "
                        "(e.g., the 40 Shopify customer emails). The tool "
                        "will compute the intersection server-side and "
                        "return ONLY the matched + unmatched subsets, plus "
                        "the match_rate_pct. Use this whenever the user "
                        "asks 'which of these people did X' — DO NOT try "
                        "to do the set intersection yourself in text, "
                        "LLMs hallucinate when matching 40+ items against "
                        "1000+ items and you will get partial/wrong answers."
                    ),
                },
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "get_email_engagement_contacts",
        "description": (
            "Get unique HubSpot contacts (BOTH contact IDs AND recipient "
            "email addresses) who had a specific engagement event with a "
            "marketing email — DELIVERED, OPEN, CLICK, BOUNCE, UNSUBSCRIBE, "
            "etc. Returns:\n"
            "  - contact_ids: list of HubSpot vids\n"
            "  - recipient_emails: list of actual email addresses, lowercased\n"
            "  - unique_contact_count / unique_email_count\n"
            "  - diagnostics block with pages_fetched, total_events_seen, "
            "    last_response_has_more, sample_event_keys\n\n"
            "Use recipient_emails when you need to join against external "
            "systems (Shopify orders, Stripe customers, your own user data) "
            "where the join key is email. Use contact_ids when aggregating "
            "against HubSpot CRM properties via search_hubspot_contacts.\n\n"
            "Event type is CASE-SENSITIVE — use 'OPEN' not 'OPENED', "
            "'CLICK' not 'CLICKED'. If you get unexpected zero results, "
            "read the diagnostics: total_events_seen=0 means the API "
            "returned no events (wrong event_type spelling? wrong email_id "
            "format? automation emails sometimes return empty here). "
            "total_events_seen>0 but no contact_ids means events came back "
            "without vid fields — surface this to the user.\n\n"
            "Caveats: caps at 5000 by default. For large campaigns this "
            "may truncate — say so honestly. search_hubspot_contacts limits "
            "IN-filter values per call (~100), so chunking is needed for "
            "the contact-IDs path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": "Marketing email ID (from search_marketing_emails).",
                },
                "event_type": {
                    "type": "string",
                    "description": (
                        "DELIVERED (default — recipients), SENT, OPEN, CLICK, "
                        "BOUNCE, UNSUBSCRIBE, DROPPED, SPAMREPORT."
                    ),
                },
                "max_unique": {
                    "type": "integer",
                    "description": "Cap on unique contact IDs returned (default 5000).",
                },
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "get_email_body",
        "description": (
            "Get the full readable body text of a HubSpot marketing email by ID, "
            "with HTML stripped. Returns subject, preview text, state, and the "
            "concatenated body content from all text modules. Use this when the "
            "user asks you to review, give feedback on, or quote actual copy "
            "from a specific email — not just its metadata."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": "The marketing email's HubSpot ID.",
                },
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "get_email_widget_html",
        "description": (
            "Return the RAW HTML of a single widget in a marketing email — "
            "for debugging when a rendered widget doesn't look right and we "
            "need to inspect its actual tag structure (which the text-only "
            "preview from get_email_widget_structure hides). Provide the "
            "email_id and the specific widget_id you want to inspect."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": "The marketing email's HubSpot ID.",
                },
                "widget_id": {
                    "type": "string",
                    "description": (
                        "The widget ID (e.g. 'module_17609870518031'). "
                        "Get these from get_email_widget_structure."
                    ),
                },
            },
            "required": ["email_id", "widget_id"],
        },
    },
    {
        "name": "get_email_widget_structure",
        "description": (
            "Diagnostic tool for inspecting the widget layout of a HubSpot "
            "marketing email. Returns each widget's id, type, label, HTML "
            "text length, and a short text preview — sorted by widget id "
            "(roughly visual order). Use this to figure out how a template "
            "is structured before trying to populate it programmatically. "
            "Useful when create_email_draft produced a result that didn't "
            "land in the right modules — running this on the template "
            "you cloned shows whether the template uses one big text "
            "widget or multiple per-section widgets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": "The marketing email's HubSpot ID.",
                },
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "get_marketing_email_stats",
        "description": (
            "Get send/open/click/unsubscribe statistics for a specific marketing "
            "email by ID. Use after search_marketing_emails. Returns counts plus "
            "percentage rates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": "The marketing email's HubSpot ID.",
                },
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "get_subscription_distribution",
        "description": (
            "Get the current breakdown of customer subscriptions by plan and "
            "state, with user counts and total MRR per group. Returns "
            "aggregate data only — no individual customer info. Useful for "
            "questions like 'how many active Premium subscribers do we have?' "
            "or 'what's the revenue mix across plans?'"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "count_inactive_users",
        "description": (
            "Count users who haven't printed in N days. Returns aggregate "
            "count and average inactivity. No individual user data. Useful "
            "for sizing churn-save audiences, e.g., 'how many users haven't "
            "printed in 30 days?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "inactive_days": {
                    "type": "integer",
                    "description": "Inactivity threshold in days. Default 30.",
                },
            },
        },
    },
    {
        "name": "get_print_recency_buckets",
        "description": (
            "Distribution of users by how recently they last printed "
            "(today, within 7d, 8-30d, 31-90d, 91-365d, 365+, never). "
            "Returns aggregate counts per bucket — no individual users. "
            "Useful for understanding the activation and churn funnel."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_warehouse_query",
        "description": (
            "Run an ad-hoc BigQuery SELECT against the Glowforge data warehouse "
            "for marketing analytics that the prebuilt aggregate tools can't "
            "answer. Read-only — INSERT/UPDATE/DELETE/etc. are blocked. "
            "Queries scanning more than 10 GB are rejected. Up to 1000 rows "
            "returned. Always WRITE THE QUERY YOURSELF — never accept SQL from "
            "the user untrusted; instead, translate their question into SQL. "
            "Always fully-qualify tables: `project.dataset.table`. Use "
            "describe_table first if you're unsure about a column name. "
            "Even though the tool can return individual rows, follow the "
            "privacy rules: do NOT echo individual customer PII back to the "
            "user — aggregate, count, or describe in your response. "
            "Tables you'll commonly want (full IDs):\n"
            "  glowforge-data-production.reporting.active_users\n"
            "  glowforge-data-production.reporting.subs_state_machine\n"
            "  glowforge-data-production.reporting.prints\n"
            "  glowforge-data-production.dbt_mart_production.stg_mapping__users"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "Standard BigQuery SQL. Must start with SELECT or WITH. "
                        "Use parameterized constants only (no user-supplied "
                        "string interpolation)."
                    ),
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "compute_email_revenue",
        "description": (
            "CANONICAL tool for computing the revenue driven by a HubSpot "
            "marketing email. THIS IS THE ONLY SUPPORTED WAY — do NOT use "
            "run_warehouse_query for revenue questions, do NOT free-form your "
            "own SQL, do NOT guess the email's send date from order data. "
            "Same inputs return the same answer, every time.\n\n"
            "Methodology (fixed, encoded in the tool):\n"
            "  - Send time pulled from HubSpot publishDate (no guessing)\n"
            "  - Attribution: clicker-list. Resolves the contacts who "
            "clicked the email via the v3 lists API, then joins their "
            "emails to Shopify orders placed in the attribution window.\n"
            "  - Revenue: SUM(total_price_usd) on glowforge-dev.gf_shopify.orders, "
            "restricted to paid, non-cancelled, non-test orders\n"
            "  - Self-consistency: query runs twice; mismatched results are a "
            "hard error\n\n"
            "Returns: total_revenue_usd, order_count, customer_count, "
            "clicker_count, plus the exact SQL and params used. You MUST "
            "echo the send time, window, attribution method, AND the SQL "
            "into your Slack reply — see the REVENUE QUESTIONS section of "
            "the system prompt for the required reply shape."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": "HubSpot marketing email ID (numeric, as string).",
                },
                "window_days": {
                    "type": "integer",
                    "description": (
                        "Attribution window in days from send time. Default 7. "
                        "Only override if the user explicitly asks for a "
                        "different window (and confirm that's what they meant). "
                        "Must be 1-90."
                    ),
                    "minimum": 1,
                    "maximum": 90,
                },
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "list_contact_properties",
        "description": (
            "List the contact properties HubSpot knows about, optionally "
            "filtered by name substring. Use this to discover field names "
            "before searching contacts — e.g., search for 'subscription' to "
            "find subscription-related properties, 'source' to find "
            "attribution properties, 'campaign' for campaign tracking. "
            "Returns name, label, type, and description for each property."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_contains": {
                    "type": "string",
                    "description": (
                        "Optional substring filter on property name or label "
                        "(case-insensitive). Omit to return all properties."
                    ),
                },
            },
        },
    },
    {
        "name": "search_hubspot_contacts",
        "description": (
            "Search HubSpot CRM contacts using filter groups. Returns the "
            "total count plus up to 100 matching contact records with the "
            "properties you request. Use this for attribution analysis "
            "(e.g., 'how many contacts have an active Premium subscription "
            "AND original source X'). \n\n"
            "PRIVACY: this tool returns individual contact records including "
            "PII (email, name, phone) if you request those properties. "
            "You MUST aggregate or count before responding to the user — "
            "NEVER paste individual emails, names, or phone numbers into "
            "your reply. If the user explicitly asks for individual records, "
            "refuse politely and tell them to use HubSpot directly. The "
            "'total' field is your friend: usually the right answer is the "
            "count, not the list.\n\n"
            "WORKFLOW: call list_contact_properties first if you don't know "
            "the exact property name to filter on. Filter operators include "
            "EQ, NEQ, LT, LTE, GT, GTE, BETWEEN, IN, NOT_IN, HAS_PROPERTY, "
            "NOT_HAS_PROPERTY, CONTAINS_TOKEN."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_groups": {
                    "type": "array",
                    "description": (
                        "List of filter groups. Filters within a group are "
                        "ANDed; groups are ORed. Each filter is "
                        "{propertyName, operator, value}."
                    ),
                    "items": {"type": "object"},
                },
                "properties": {
                    "type": "array",
                    "description": (
                        "List of contact properties to return. Request only "
                        "what you need; avoid PII (email, firstname, "
                        "lastname, phone) unless absolutely required."
                    ),
                    "items": {"type": "string"},
                },
                "query": {
                    "type": "string",
                    "description": "Optional free-text search.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max contacts per page (default 100, capped at 100).",
                },
            },
        },
    },
    {
        "name": "describe_table",
        "description": (
            "Get the schema (column names, types, modes, descriptions) and "
            "stats (row count, size, last modified) for a BigQuery table. "
            "Use before writing run_warehouse_query SQL when you're unsure "
            "about a table's columns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_id": {
                    "type": "string",
                    "description": (
                        "Fully-qualified table ID, like "
                        "'glowforge-data-production.reporting.subs_state_machine'."
                    ),
                },
            },
            "required": ["table_id"],
        },
    },
    {
        "name": "create_icymi_draft",
        "description": (
            "Create a new ICYMI weekly recap draft in HubSpot. Clones the "
            "canonical ICYMI master template (handled internally — Mark does "
            "not pass a template ID), sets subject/preheader/name, and "
            "populates each per-role module: intro, three project modules "
            "(title + body each), Laser Focus, and signoff.\n\n"
            "Use this tool — NOT create_email_draft — for the ICYMI workflow. "
            "Only call after the user has approved the draft with explicit "
            "'ship it' or equivalent. The tool returns an edit_url plus a "
            "body_update report; if any module reports anything other than "
            "'updated' status, surface that to the user honestly so they "
            "know to fix it in HubSpot.\n\n"
            "Body formatting: each role's text supports light markdown — "
            "**bold**, *italic*, [link text](url). Use double newlines "
            "between paragraphs. The maker's @username MUST be a markdown "
            "link to their community profile. The forum post URL should "
            "be embedded as a markdown link inside the project body (e.g., "
            "'... [View on the forum](https://...)' at the end)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_name": {
                    "type": "string",
                    "description": (
                        "Internal name for the new draft (visible in HubSpot, "
                        "not to recipients). Use the format "
                        "'ICYMI - <YYYY-MM-DD> - <short topic>'."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": (
                        "The chosen subject line — one final pick, NOT all "
                        "the options the user reviewed."
                    ),
                },
                "preheader": {
                    "type": "string",
                    "description": (
                        "The chosen preheader (preview text). Will be set as "
                        "the email's previewText field automatically."
                    ),
                },
                "content_by_role": {
                    "type": "object",
                    "description": (
                        "Structured body content. Text values are plain "
                        "text/markdown (the tool builds the HTML). Image "
                        "values are objects with url/alt/link — see the "
                        "project_N_image fields below."
                    ),
                    "properties": {
                        "intro_body": {
                            "type": "string",
                            "description": (
                                "Just the intro paragraph(s) that set up the "
                                "week's theme. Do NOT include the hero "
                                "headline ('ICYMI: Glowforge Projects That "
                                "Made Us Stop Scrolling') — that lives in a "
                                "separate header image module."
                            ),
                        },
                        "project_1_title": {
                            "type": "string",
                            "description": (
                                "Catchy 4-8 word title for project 1. Riff "
                                "on the project, don't just name it. Optional "
                                "leading emoji."
                            ),
                        },
                        "project_1_body": {
                            "type": "string",
                            "description": (
                                "2-4 sentence body for project 1. Must "
                                "include the maker handle as a markdown link "
                                "to their community profile, AND the forum "
                                "post URL as an inline markdown link "
                                "somewhere in the body."
                            ),
                        },
                        "project_1_image": {
                            "type": "object",
                            "description": (
                                "The project image to display alongside the "
                                "text. Pulled from the first usable image "
                                "URL in fetch_forum_post's image_urls list. "
                                "If the forum post had no images, omit this "
                                "field entirely and the previous week's "
                                "image will carry over."
                            ),
                            "properties": {
                                "url": {
                                    "type": "string",
                                    "description": (
                                        "Direct URL to the new project image "
                                        "(use the FIRST entry of image_urls "
                                        "from fetch_forum_post)."
                                    ),
                                },
                                "alt": {
                                    "type": "string",
                                    "description": (
                                        "Short alt text describing the image. "
                                        "If omitted, derives from the project "
                                        "title."
                                    ),
                                },
                                "link": {
                                    "type": "string",
                                    "description": (
                                        "URL the image links to when clicked. "
                                        "Use the forum post URL — same one "
                                        "you embedded in project_1_body."
                                    ),
                                },
                            },
                            "required": ["url", "link"],
                        },
                        "project_2_title": {"type": "string"},
                        "project_2_body": {"type": "string"},
                        "project_2_image": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "alt": {"type": "string"},
                                "link": {"type": "string"},
                            },
                            "required": ["url", "link"],
                        },
                        "project_3_title": {"type": "string"},
                        "project_3_body": {"type": "string"},
                        "project_3_image": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "alt": {"type": "string"},
                                "link": {"type": "string"},
                            },
                            "required": ["url", "link"],
                        },
                        "laser_focus_title": {
                            "type": "string",
                            "description": (
                                "The Laser Focus heading, e.g. 'Laser Focus: "
                                "The Glue-and-Set Secret'. Will render as h2 "
                                "automatically — do NOT bold it manually. The "
                                "title shares a widget with the body but is "
                                "passed as a separate field so the heading "
                                "and body get the right tags."
                            ),
                        },
                        "laser_focus_body": {
                            "type": "string",
                            "description": (
                                "The 3-5 sentence body of the Laser Focus "
                                "section, NOT including the title. Separate "
                                "multiple paragraphs with blank lines. "
                                "Markdown links and bold work as usual."
                            ),
                        },
                        "signoff_body": {
                            "type": "string",
                            "description": (
                                "Optional. If omitted, the template's "
                                "default 'Happy Making! The Glowforge Team' "
                                "carries over. Only override if the user "
                                "wants something different."
                            ),
                        },
                    },
                    "required": [
                        "intro_body",
                        "project_1_title", "project_1_body",
                        "project_2_title", "project_2_body",
                        "project_3_title", "project_3_body",
                        "laser_focus_title", "laser_focus_body",
                    ],
                },
            },
            "required": ["draft_name", "subject", "preheader", "content_by_role"],
        },
    },
    {
        "name": "share_table",
        "description": (
            "Upload a grid-shaped dataset to the current Slack thread "
            "as a CSV file. Use this for ANY tabular result with "
            "multiple rows AND multiple columns — never emit Markdown "
            "tables in chat. Slack renders the CSV with its own "
            "scrollable preview + download button, the data stays "
            "exact and copy-pasteable, and the file doesn't bloat your "
            "context (only a small confirmation comes back).\n\n"
            "WHEN TO USE: anything that would naturally be displayed "
            "as a table — list of emails with metrics, contacts with "
            "properties, query results across multiple columns, "
            "comparison data across categories.\n\n"
            "WHEN NOT TO USE: a single column of values (use a bulleted "
            "list inline). A handful of rows where you'd be sharing 2-3 "
            "facts about 2-3 things (inline prose is clearer). Anything "
            "that's really just a single sentence reshaped as a table.\n\n"
            "PAIRING: this tool delivers the FILE. Your chat reply "
            "still gives the prose summary — '14 contacts found, "
            "details attached as CSV.' Don't repeat the table data in "
            "your prose; the file IS the data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "headers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Column names, in left-to-right order. Required."
                    ),
                },
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {},  # cells can be any scalar — handled by csv.writer
                    },
                    "description": (
                        "Row data. Each row is a list of cells in the "
                        "same order as headers. Every row must have "
                        "the same length as headers."
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Short slug for the CSV filename (e.g. "
                        "'proofgrade-revenue-by-email', "
                        "'engaged-contacts-2026-06-11'). Lowercase, "
                        "hyphens, no spaces. The .csv extension is "
                        "added automatically. Optional — defaults to "
                        "a timestamped name."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": (
                        "Optional short text posted alongside the file "
                        "as an initial comment (e.g. 'Revenue by "
                        "Proofgrade email, last 30 days'). Keep it "
                        "brief — the file's filename + your prose "
                        "reply usually carry enough context."
                    ),
                },
            },
            "required": ["headers", "rows"],
        },
    },
    {
        "name": "show_recent_answer_cards",
        "description": (
            "Show the N most recent answer cards (your past responses) "
            "for this channel. Use when the user asks 'what did you say "
            "earlier', 'show me my last few answers', 'what was my last "
            "response about revenue'. Returns the question, the tool "
            "sequence you used, and your response for each.\n\n"
            "Set full=true to include the FULL tool call args and outputs "
            "(longer but lets the user audit exactly what you ran). "
            "Default false shows just tool names + response previews."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many recent cards to return. Default 5.",
                },
                "full": {
                    "type": "boolean",
                    "description": (
                        "If true, include full tool args and outputs in each "
                        "card. Use sparingly — long responses."
                    ),
                },
            },
        },
    },
    {
        "name": "search_answer_cards",
        "description": (
            "Search past answer cards for ones whose question or response "
            "contains a substring. Use when the user asks 'find my past "
            "answers about X', 'show history for the Proofgrade revenue "
            "question', 'when did I last answer about list 10273'. "
            "Returns matching cards with question + tools used + response."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "needle": {
                    "type": "string",
                    "description": (
                        "Substring to search for. Matches against both the "
                        "user question and the response text."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches to return. Default 10.",
                },
                "full": {
                    "type": "boolean",
                    "description": "Include full tool details. Default false.",
                },
            },
            "required": ["needle"],
        },
    },
    {
        "name": "create_email_draft_v2",
        "description": (
            "PREFERRED tool for creating a marketing email Mark designed "
            "from scratch. Clones a blank-canvas template (Glowforge logo "
            "header + empty custom-HTML body + standard HubSpot footer) "
            "and writes the entire body as Mark-generated HTML.\n\n"
            "Use this when: the user asks for a from-scratch design, a "
            "new email format, an experimental layout, or something not "
            "covered by an existing template. The body_html is Mark's "
            "design surface — he owns the entire body composition.\n\n"
            "DO NOT use this for the weekly ICYMI workflow (use "
            "create_icymi_draft — it has structured per-project widgets "
            "the template needs). DO NOT use this for copy-only tweaks on "
            "an existing template (use create_email_draft — clones and "
            "swaps body text on an existing email's structure).\n\n"
            "body_html requirements: structured, semantic, cross-client-"
            "compatible email HTML. Table-based layout (NOT flexbox or "
            "grid — Outlook ignores those). Inline CSS (NOT linked "
            "stylesheets — most email clients strip <style> blocks). No "
            "SVG (use raster images). Use real <h1>/<h2>/<p>/<a> "
            "semantic tags. Read prompts/email_design_references.md "
            "section 5 (cheat sheet) BEFORE designing.\n\n"
            "Returns the email_id, edit_url, and status. The body_html "
            "is NOT echoed back (would blow up your context); keep your "
            "own copy if you need to iterate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Internal name for the draft (visible in HubSpot, "
                        "not the subject the recipient sees). Follow team "
                        "convention: '<Program> - <YYYY-MM-DD> - <topic>'."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": (
                        "Email subject line. The recipient sees this. "
                        "Mobile clients show ~30-40 chars before truncation."
                    ),
                },
                "body_html": {
                    "type": "string",
                    "description": (
                        "Full email body as cross-client-compatible HTML. "
                        "Tables-for-layout, inline styles, real semantic "
                        "tags. Mark's design surface."
                    ),
                },
                "preheader": {
                    "type": "string",
                    "description": (
                        "Preview text shown in inbox after the subject. "
                        "Strong complement to the subject line — don't just "
                        "repeat it. Recommended length 40-130 chars."
                    ),
                },
                "body_widget_id_override": {
                    "type": "string",
                    "description": (
                        "Optional. Skip widget auto-detection and patch "
                        "this specific widget id. Use only if a previous "
                        "call returned the 'could not identify HTML "
                        "widget' error with widget_info — pick the right "
                        "id from there and pass it here for subsequent "
                        "calls."
                    ),
                },
            },
            "required": ["name", "subject", "body_html"],
        },
    },
    {
        "name": "update_email_draft_v2",
        "description": (
            "Iterate on a from-scratch email draft previously created by "
            "create_email_draft_v2. Updates the body_html and/or "
            "metadata (subject/preheader/name) on an existing draft. "
            "Header and footer are never touched.\n\n"
            "Iteration pattern: when the user reviews the HubSpot preview "
            "and asks for a change ('make the headline bigger,' 'switch "
            "to Aurange,' 'rewrite the second section to be punchier'), "
            "regenerate the FULL body_html (no diffing — simpler and "
            "avoids drift), call this tool with the new body, and tell "
            "the user to refresh their HubSpot preview.\n\n"
            "Pass only the fields you want to change — omitted fields "
            "are left untouched. Pass at least one field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": (
                        "HubSpot email ID of the draft to update (the one "
                        "create_email_draft_v2 returned)."
                    ),
                },
                "body_html": {
                    "type": "string",
                    "description": (
                        "New full email body HTML. Omit to leave the body "
                        "unchanged. If passed, must be non-empty."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": "New subject line. Omit to keep current.",
                },
                "preheader": {
                    "type": "string",
                    "description": "New preheader text. Omit to keep current.",
                },
                "name": {
                    "type": "string",
                    "description": "New internal name. Omit to keep current.",
                },
                "body_widget_id_override": {
                    "type": "string",
                    "description": (
                        "Optional. Skip widget auto-detection and patch "
                        "this specific widget id (same as on create)."
                    ),
                },
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "create_email_draft",
        "description": (
            "Create a NEW draft marketing email in HubSpot by cloning an existing "
            "email and updating its name, subject, and main body content. "
            "ONLY call this after the user has explicitly approved the drafted "
            "content. The tool will replace the largest text block in the "
            "template with your body_text; other modules (header image, CTA "
            "button, footer) carry over from the template. Tell the user to "
            "review the draft in HubSpot before sending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_email_id": {
                    "type": "string",
                    "description": (
                        "The HubSpot ID of an existing marketing email to clone. "
                        "Use search_marketing_emails to find candidates if "
                        "the user hasn't specified one."
                    ),
                },
                "draft_name": {
                    "type": "string",
                    "description": (
                        "Internal name for the new draft (visible in HubSpot, "
                        "not to recipients). Be descriptive."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": "The subject line for the new email.",
                },
                "body_text": {
                    "type": "string",
                    "description": (
                        "The body copy for the email. Plain text with double "
                        "newlines between paragraphs. Light markdown supported: "
                        "**bold**, *italic*, [link text](https://url). Don't "
                        "include greeting/signature unless they're part of the "
                        "main pitch — those usually live in separate template "
                        "modules that carry over."
                    ),
                },
            },
            "required": ["template_email_id", "draft_name", "subject", "body_text"],
        },
    },
    {
        "name": "get_upcoming_social_posts",
        "description": (
            "Read the content calendar and return upcoming SOCIAL posts "
            "(Email/ICYMI rows are excluded — those are your email job). Each "
            "post has the scheduled date, day, theme, caption angle, audience "
            "focus, product focus, and Google Drive asset link(s). Use this to "
            "see what's due before drafting social posts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "within_days": {
                    "type": "integer",
                    "description": "Look-ahead window from today, in days. Default 10.",
                }
            },
        },
    },
    {
        "name": "post_draft_to_review_channel",
        "description": (
            "Send a FINALIZED social post draft to the team's Slack review "
            "channel (SLACK_REVIEW_CHANNEL), where the person who publishes "
            "picks it up. Call ONLY after the user has reviewed and approved "
            "the caption in chat. You do not publish to Meta — this is the "
            "handoff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "caption": {"type": "string", "description": "The approved caption copy."},
                "platform": {
                    "type": "string",
                    "description": "Target platform(s), e.g. 'Instagram' or 'Facebook + Instagram'.",
                },
                "post_date": {"type": "string", "description": "Scheduled date, e.g. 'Mon Jun 22'."},
                "asset_link": {"type": "string", "description": "Drive link to the image/video asset."},
                "theme": {"type": "string", "description": "Short theme label from the calendar row."},
            },
            "required": ["caption"],
        },
    },
    {
        "name": "get_facebook_page_insights",
        "description": (
            "Facebook Page organic insights (impressions, reach, post "
            "engagements, fans, views) over a date range. Dates are "
            "YYYY-MM-DD; default is the trailing 28 days. Pass custom "
            "`metrics` only if you know the Graph API metric names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metrics": {"type": "array", "items": {"type": "string"}},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "period": {"type": "string", "description": "day | week | days_28. Default day."},
            },
        },
    },
    {
        "name": "get_instagram_insights",
        "description": (
            "Instagram Business account insights (reach, impressions, profile "
            "views, follower count) over a date range. Dates YYYY-MM-DD; "
            "default trailing 28 days."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metrics": {"type": "array", "items": {"type": "string"}},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "period": {"type": "string"},
            },
        },
    },
    {
        "name": "get_recent_social_posts",
        "description": (
            "Recent organic posts with per-post engagement, to see which "
            "content resonated. platform: 'facebook' (Page feed) or "
            "'instagram' (IG media)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "facebook | instagram. Default facebook."},
                "limit": {"type": "integer", "description": "How many posts. Default 10."},
            },
        },
    },
    {
        "name": "get_ad_performance",
        "description": (
            "Paid-social performance from the Meta Ad account (spend, "
            "impressions, reach, clicks, CPC, CTR, actions). date_preset like "
            "'last_7d', 'last_28d', 'last_30d'; level 'campaign' | 'adset' | 'ad'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {"type": "array", "items": {"type": "string"}},
                "date_preset": {"type": "string"},
                "level": {"type": "string"},
            },
        },
    },
    {
        "name": "publish_to_meta",
        "description": (
            "GATED — publishing is disabled by default (draft-only). Refuses "
            "unless an admin has set SOCIAL_MARK_ALLOW_PUBLISH=true. Don't "
            "call it expecting a post to go live; use "
            "post_draft_to_review_channel for the human-review handoff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "facebook | instagram."},
                "caption": {"type": "string"},
                "image_url": {"type": "string", "description": "Required for Instagram."},
            },
            "required": ["platform", "caption"],
        },
    },
    {
        "name": "create_fb_post_from_email",
        "description": (
            "SINGLE-CALL composite tool — the PREFERRED way to create a "
            "Facebook post from a HubSpot email's hero image. Does the "
            "entire chain server-side: extracts the first image from the "
            "email's widgets, saves it to the team's Drive folder, and "
            "creates a scheduled FB post with that image. Use this "
            "INSTEAD OF manually calling get_email_widget_html + "
            "save_image_to_drive + draft_facebook_post — the chained "
            "approach is unreliable. If the user says 'make a Facebook "
            "post based on the [campaign name] email', this is the tool. "
            "Returns drive_url + post_id + scheduled_for so the human "
            "can verify both surfaces."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {
                    "type": "string",
                    "description": (
                        "HubSpot marketing email ID — the long number "
                        "from the email's edit URL."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": (
                        "Facebook caption in Glowforge social voice — "
                        "1-3 short sentences, lead with maker/creativity, "
                        "approved hashtags only if they fit."
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Drive filename. Convention: "
                        "'YYYY-MM-DD-campaign-slug.jpg'. Defaults to "
                        "'email-{email_id}.jpg'."
                    ),
                },
                "scheduled_publish_time": {
                    "type": "integer",
                    "description": (
                        "Optional unix seconds for scheduled publish. "
                        "Default: 24h from now."
                    ),
                },
            },
            "required": ["email_id", "caption"],
        },
    },
    {
        "name": "save_image_to_drive",
        "description": (
            "Fetch an image from any public URL (HubSpot email CDN, an "
            "asset library, etc.) and store it in the team's social-assets "
            "Drive folder. Returns a Drive URL you can immediately pass as "
            "`drive_url` to draft_facebook_post. Use this to repurpose "
            "images across surfaces — e.g., grab the hero image from a "
            "HubSpot email (via get_email_widget_html → find img src) and "
            "stage it in Drive before drafting the matching social post. "
            "The Drive folder gives the team an auditable record of "
            "everything social-mark uploads, separate from the API."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_url": {
                    "type": "string",
                    "description": (
                        "Public URL of the image to fetch. Must serve "
                        "image bytes directly (content-type: image/*), not "
                        "an HTML page that embeds an image."
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Filename to save the image under in Drive, "
                        "including extension. Convention: "
                        "'YYYY-MM-DD-theme-slug.jpg' so files sort by date "
                        "(e.g., '2026-06-19-juneteenth.jpg')."
                    ),
                },
                "folder_id": {
                    "type": "string",
                    "description": (
                        "Optional Drive folder ID override. Defaults to "
                        "SOCIAL_ASSETS_DRIVE_FOLDER_ID env var."
                    ),
                },
            },
            "required": ["source_url", "filename"],
        },
    },
    {
        "name": "draft_facebook_post",
        "description": (
            "Create a SCHEDULED Facebook post in Meta Business Suite — the "
            "Meta-compatible equivalent of a HubSpot email draft. Lands in "
            "MBS → Planner → Scheduled, fully editable. Defaults to firing "
            "24 hours from creation if no time is specified; that gives the "
            "team a full day to review/edit/cancel in MBS. NOT gated by "
            "SOCIAL_MARK_ALLOW_PUBLISH — the human reviewing in MBS is the "
            "safety gate. This is the PREFERRED way to hand off a "
            "finalized FB caption. Note: pure 'draft only' posts via API "
            "don't appear in MBS Planner UI (Meta limitation), which is "
            "why this tool schedules instead of drafts. For Instagram (no "
            "draft API) or 'discuss in Slack first' workflows, use "
            "post_draft_to_review_channel instead. When batching from the "
            "calendar, set scheduled_publish_time to each row's intended "
            "publish time so the post fires on the right day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "caption": {
                    "type": "string",
                    "description": (
                        "Composed Facebook caption text in Glowforge social "
                        "voice — ready to publish. 1-3 short sentences, "
                        "approved hashtags only."
                    ),
                },
                "image_url": {
                    "type": "string",
                    "description": (
                        "Optional public URL of an image Meta can fetch "
                        "directly. Only works for truly public URLs (NOT "
                        "Drive links). Use drive_url instead for assets "
                        "from the calendar."
                    ),
                },
                "drive_url": {
                    "type": "string",
                    "description": (
                        "Google Drive URL of an image asset, taken from "
                        "the calendar row's asset_links. The tool fetches "
                        "the file bytes via service account and uploads "
                        "them to Meta. PREFERRED over image_url for "
                        "Glowforge content since the Drive folder isn't "
                        "public. Forms accepted: "
                        "https://drive.google.com/file/d/FILE_ID/view, "
                        "https://drive.google.com/open?id=FILE_ID, etc. "
                        "File must be JPG/PNG/GIF/BMP/TIFF/WebP."
                    ),
                },
                "scheduled_publish_time": {
                    "type": "integer",
                    "description": (
                        "Optional unix timestamp (seconds) to schedule the "
                        "post for auto-publish. Must be 10 minutes to 6 "
                        "months in the future. Omit for pure draft (no "
                        "auto-publish until a human clicks Publish)."
                    ),
                },
            },
            "required": ["caption"],
        },
    },
]

TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "search_marketing_emails": _tool_search_marketing_emails,
    "fetch_forum_post": _tool_fetch_forum_post,
    "remember_lesson": _tool_remember_lesson,
    "list_lessons": _tool_list_lessons,
    "update_lesson": _tool_update_lesson,
    "delete_lesson": _tool_delete_lesson,
    "get_email_body": _tool_get_email_body,
    "get_email_widget_structure": _tool_get_email_widget_structure,
    "get_email_widget_html": _tool_get_email_widget_html,
    "get_email_engagement_contacts": _tool_get_email_engagement_contacts,
    "get_email_engagers_via_list": _tool_get_email_engagers_via_list,
    "get_contact_email_events": _tool_get_contact_email_events,
    "list_workflows": _tool_list_workflows,
    "get_workflow_details": _tool_get_workflow_details,
    "get_workflow_enrollments": _tool_get_workflow_enrollments,
    "get_marketing_email_stats": _tool_get_marketing_email_stats,
    "create_icymi_draft": _tool_create_icymi_draft,
    "create_email_draft": _tool_create_email_draft,
    "create_email_draft_v2": _tool_create_email_draft_v2,
    "show_recent_answer_cards": _tool_show_recent_answer_cards,
    "search_answer_cards": _tool_search_answer_cards,
    "share_table": _tool_share_table,
    "update_email_draft_v2": _tool_update_email_draft_v2,
    "get_subscription_distribution": _tool_get_subscription_distribution,
    "count_inactive_users": _tool_count_inactive_users,
    "get_print_recency_buckets": _tool_get_print_recency_buckets,
    "run_warehouse_query": _tool_run_warehouse_query,
    "compute_email_revenue": _tool_compute_email_revenue,
    "find_hubspot_lists": _tool_find_hubspot_lists,
    "get_list_details": _tool_get_list_details,
    "count_list_intersection": _tool_count_list_intersection,
    "describe_table": _tool_describe_table,
    "search_hubspot_contacts": _tool_search_hubspot_contacts,
    "list_contact_properties": _tool_list_contact_properties,
    "lookup_slack_user": _tool_lookup_slack_user,
    "send_slack_dm": _tool_send_slack_dm,
    "get_upcoming_social_posts": _tool_get_upcoming_social_posts,
    "post_draft_to_review_channel": _tool_post_draft_to_review_channel,
    "get_facebook_page_insights": _tool_get_facebook_page_insights,
    "get_instagram_insights": _tool_get_instagram_insights,
    "get_recent_social_posts": _tool_get_recent_social_posts,
    "get_ad_performance": _tool_get_ad_performance,
    "publish_to_meta": _tool_publish_to_meta,
    "draft_facebook_post": _tool_draft_facebook_post,
    "save_image_to_drive": _tool_save_image_to_drive,
    "create_fb_post_from_email": _tool_create_fb_post_from_email,
}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def _execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a tool, log how long it took, and return the result.

    Output goes to stdout so Render captures it. Grep `[timing]` to see
    just the perf lines. Format: tool=<name> status=<ok|error> elapsed=<s>
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}
    start = time.perf_counter()
    status = "ok"
    try:
        result = handler(args)
        if isinstance(result, dict) and "error" in result:
            status = "tool_error"
    except Exception as exc:
        result = {"error": f"Tool {name} failed: {exc}"}
        status = "exception"
    elapsed = time.perf_counter() - start
    print(
        f"[timing] tool={name} status={status} elapsed={elapsed:.2f}s",
        flush=True,
    )
    return result


def reset_conversation(conversation_id: str) -> None:
    """Wipe the stored history for a single conversation."""
    _conversations.pop(conversation_id, None)


def has_conversation(conversation_id: str) -> bool:
    """Return True if any stored history exists for this conversation."""
    return bool(_conversations.get(conversation_id))


def seed_conversation(
    conversation_id: str, messages: List[Dict[str, Any]]
) -> None:
    """Replace any stored history for this conversation with these messages.

    Used by the Slack runner to rehydrate context from a thread after a
    worker restart wiped the in-memory dict. The runner reconstructs the
    user/assistant turns from Slack's record of the thread and seeds them
    here so chat() picks them up on the next call.
    """
    if not messages:
        return
    _conversations[conversation_id] = list(messages)


def _sanitize_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Trim leading orphaned tool_results and trailing unanswered tool_use blocks.

    Anthropic's API rejects a request if:
      - The first message contains a tool_result without a preceding tool_use, OR
      - An assistant message ends with a tool_use that has no following tool_result.

    Either can happen when the agent loop hits the turn cap mid-tool-call and
    we save partial state. This makes loaded history safe to send.
    """
    msgs = list(messages)

    # Drop leading user messages whose content includes any tool_result.
    while msgs:
        first = msgs[0]
        content = first.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            msgs = msgs[1:]
            continue
        break

    # Drop trailing assistant messages whose content includes any tool_use
    # (without a follow-up tool_result, which we'd already have appended).
    while msgs:
        last = msgs[-1]
        if last.get("role") != "assistant":
            break
        content = last.get("content")
        has_tool_use = False
        if isinstance(content, list):
            for b in content:
                btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                if btype == "tool_use":
                    has_tool_use = True
                    break
        if has_tool_use:
            msgs = msgs[:-1]
            continue
        break

    return msgs


# Confabulation markers: phrases in Mark's final response that strongly
# imply a specific social tool was JUST executed in this turn. If the
# phrase appears but the tool wasn't called, that's a hallucination —
# Mark is generating a plausible "Done!" without actually doing the work.
# Conservative list — phrases here should ONLY be state-change claims,
# never informational mentions. ("got 100 engagements yesterday" is fine;
# "I just scheduled the post" is not.)
# NB: each marker's set lists every tool whose execution legitimately
# justifies the claim. The composite `create_fb_post_from_email` runs the
# Drive upload AND the FB scheduling internally, so it satisfies both
# kinds of markers in a single call.
_CONFABULATION_MARKERS = [
    ("saved to drive", {"save_image_to_drive", "create_fb_post_from_email"}),
    ("uploaded to drive", {"save_image_to_drive", "create_fb_post_from_email"}),
    ("saved to google drive", {"save_image_to_drive", "create_fb_post_from_email"}),
    ("image saved", {"save_image_to_drive", "create_fb_post_from_email"}),
    ("drive url:", {"save_image_to_drive", "create_fb_post_from_email"}),
    ("post is scheduled", {"draft_facebook_post", "create_fb_post_from_email"}),
    ("post is now scheduled", {"draft_facebook_post", "create_fb_post_from_email"}),
    ("scheduled the post", {"draft_facebook_post", "create_fb_post_from_email"}),
    ("scheduled fb post", {"draft_facebook_post", "create_fb_post_from_email"}),
    ("scheduled facebook post", {"draft_facebook_post", "create_fb_post_from_email"}),
    ("draft created", {"draft_facebook_post", "create_fb_post_from_email", "post_draft_to_review_channel"}),
    ("posted to the review channel", {"post_draft_to_review_channel"}),
    ("posted to social-review", {"post_draft_to_review_channel"}),
]


def _detect_confabulation(
    final_text: Optional[str], tools_called: List[str]
) -> Optional[str]:
    """If the response claims a social action succeeded but the
    corresponding tool wasn't called this turn, return a human-readable
    explanation; otherwise None.
    """
    text = (final_text or "").lower()
    called = set(tools_called or [])
    triggered = []
    for phrase, expected in _CONFABULATION_MARKERS:
        if phrase in text and not (expected & called):
            triggered.append((phrase, expected))
    if not triggered:
        return None
    lines = []
    for phrase, expected in triggered:
        lines.append(
            f"  - claimed '{phrase}' but never called "
            + " or ".join(sorted(expected))
        )
    return "\n".join(lines)


def chat(
    user_message: str,
    *,
    conversation_id: Optional[str] = None,
    system_prompt: str = SYSTEM_PROMPT,
    channel: Optional[str] = None,
    user: Optional[str] = None,
    slack_message_ts: Optional[str] = None,
    thread_ts: Optional[str] = None,
) -> str:
    """Run an agent loop until Claude produces a final text response.

    If conversation_id is provided, prior messages from that conversation
    are loaded as context, and the updated history is saved back at the end.
    Without conversation_id, every call is a fresh conversation.

    channel / user / slack_message_ts are metadata used to build the
    answer card written at the end of the response.

    channel / thread_ts are also set on per-turn contextvars so tools
    like `share_table` can target the right Slack thread for file
    uploads without needing channel/thread args plumbed through every
    tool signature. thread_ts should be the thread root (event.thread_ts
    if reply, event.ts if a new top-level mention).
    """
    # Bind per-turn Slack context for tools that need it (share_table).
    channel_token = _current_channel.set(channel)
    thread_ts_token = _current_thread_ts.set(thread_ts)
    try:
        return _chat_inner(
            user_message,
            conversation_id=conversation_id,
            system_prompt=system_prompt,
            channel=channel,
            user=user,
            slack_message_ts=slack_message_ts,
        )
    finally:
        _current_channel.reset(channel_token)
        _current_thread_ts.reset(thread_ts_token)


def _chat_inner(
    user_message: str,
    *,
    conversation_id: Optional[str] = None,
    system_prompt: str = SYSTEM_PROMPT,
    channel: Optional[str] = None,
    user: Optional[str] = None,
    slack_message_ts: Optional[str] = None,
) -> str:
    """Body of chat() — wrapped so the public chat() can set/reset
    contextvars in a finally block. All real work happens here.
    """
    client = _get_client()

    if conversation_id is not None:
        messages: List[Dict[str, Any]] = _sanitize_history(
            _conversations.get(conversation_id, [])
        )
    else:
        messages = []

    messages.append({"role": "user", "content": user_message})

    chat_start = time.perf_counter()
    chat_start_unix = time.time()  # for ISO timestamps in answer cards
    turn_count = 0
    tool_call_count = 0
    # Audit trail of every tool call this turn — populated as we dispatch
    # tools, then written to the answer card at the end.
    tool_call_log: List[Dict[str, Any]] = []

    # ----- Prompt caching setup -----
    # The system prompt and tool definitions don't change between turns,
    # so we mark them as cacheable. Anthropic caches the prefix up to (and
    # including) each cache_control marker. With markers on the last tool
    # AND the system prompt, both blocks get reused on subsequent turns
    # within the 5-minute cache window. For Mark's usage pattern — multi-
    # turn ICYMI iteration sessions — every turn after the first should
    # hit the cache for the ~14k of static prompt prefix.
    system_blocks = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    tools_for_call: List[Dict[str, Any]] = [dict(t) for t in TOOLS]
    if tools_for_call:
        tools_for_call[-1] = {
            **tools_for_call[-1],
            "cache_control": {"type": "ephemeral"},
        }

    # Per-request timeout for Anthropic. Without this the SDK can sit on a
    # degraded connection for many minutes silently retrying. Two minutes
    # is plenty for any normal turn (largest turn is ~60s for a long draft
    # generation); anything beyond that is a real problem and we want it
    # to surface as an exception, not a hang.
    INFERENCE_TIMEOUT_SECONDS = 120

    final_text = ""
    # When the confabulation guard fires and we still have a retry budget,
    # we set this to {"type": "any"} for the NEXT API call so the model is
    # physically required by the API to emit a tool_use block. Reset to
    # None after each use so subsequent turns go back to auto.
    next_tool_choice: Optional[Dict[str, Any]] = None
    confab_retries_used = 0
    MAX_CONFAB_RETRIES = 1

    for turn_idx in range(MAX_AGENT_TURNS):
        turn_count = turn_idx + 1

        # Log BEFORE the call so we can see in-flight inferences in Render
        # logs even when the API hangs.
        in_flight_msg_count = len(messages)
        print(
            f"[timing] inference turn={turn_count} "
            f"starting (messages_in_history={in_flight_msg_count}) "
            f"tool_choice={next_tool_choice or 'auto'}",
            flush=True,
        )

        inference_start = time.perf_counter()
        try:
            create_kwargs: Dict[str, Any] = dict(
                model=MODEL,
                max_tokens=4096,
                system=system_blocks,
                tools=tools_for_call,
                messages=messages,
                timeout=INFERENCE_TIMEOUT_SECONDS,
            )
            if next_tool_choice is not None:
                create_kwargs["tool_choice"] = next_tool_choice
            # Reset after one use — confab retry is single-shot per turn.
            next_tool_choice = None
            response = client.messages.create(**create_kwargs)
        except Exception as exc:
            inference_elapsed = time.perf_counter() - inference_start
            print(
                f"[timing] inference turn={turn_count} "
                f"FAILED after {inference_elapsed:.2f}s "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
            # Don't re-raise into Slack as a stack trace; surface a clean
            # message so the user knows to retry.
            final_text = (
                f"(Inference failed on turn {turn_count}: {type(exc).__name__}. "
                "This is usually a transient Anthropic API issue — try the "
                "request again.)"
            )
            break

        inference_elapsed = time.perf_counter() - inference_start

        usage = getattr(response, "usage", None)
        in_tokens = getattr(usage, "input_tokens", "?") if usage else "?"
        out_tokens = getattr(usage, "output_tokens", "?") if usage else "?"
        # cache_creation_input_tokens = tokens written to cache (first turn)
        # cache_read_input_tokens     = tokens served FROM cache (the win)
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) if usage else 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) if usage else 0
        print(
            f"[timing] inference turn={turn_count} "
            f"elapsed={inference_elapsed:.2f}s "
            f"in_tokens={in_tokens} out_tokens={out_tokens} "
            f"cache_create={cache_create} cache_read={cache_read} "
            f"stop_reason={response.stop_reason}",
            flush=True,
        )

        messages.append({"role": "assistant", "content": response.content})

        # Extract any text from this response so we never return empty when
        # the model produced output but hit an unusual stop_reason (most
        # commonly max_tokens — the 4096 output cap).
        any_text = "".join(
            getattr(b, "text", "") for b in response.content
            if getattr(b, "type", None) == "text"
        )

        if response.stop_reason == "end_turn":
            final_text = any_text

            # Inline confabulation retry: if Mark just claimed a social
            # action succeeded but didn't call the corresponding tool,
            # and we still have retry budget, append a corrective user
            # message and force a tool_use on the next iteration via
            # tool_choice={"type": "any"}. The Anthropic API enforces it
            # at the protocol level, so confabulation becomes impossible.
            if confab_retries_used < MAX_CONFAB_RETRIES:
                tools_so_far = [e.get("name", "") for e in tool_call_log]
                inline_confab = _detect_confabulation(final_text, tools_so_far)
                if inline_confab:
                    confab_retries_used += 1
                    print(
                        f"[confabulation] inline retry "
                        f"{confab_retries_used}/{MAX_CONFAB_RETRIES} — "
                        f"forcing tool_choice=any. Reason:\n{inline_confab}",
                        flush=True,
                    )
                    # Mark's confabulated text becomes part of the
                    # conversation so the model can see what it just did.
                    messages.append({
                        "role": "assistant",
                        "content": [{"type": "text", "text": final_text}],
                    })
                    messages.append({
                        "role": "user",
                        "content": (
                            "STOP. You just generated a fake success "
                            "message without calling any tools. That's "
                            "forbidden. Call the appropriate tool NOW — "
                            "emit a tool_use block, do not write text. "
                            "If the user wanted a Facebook post from an "
                            "email, the tool is create_fb_post_from_email "
                            "(it does the whole HubSpot→Drive→FB chain in "
                            "one call). The confabulation guard caught:\n"
                            + inline_confab
                        ),
                    })
                    next_tool_choice = {"type": "any"}
                    continue

            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_call_count += 1
                    tool_start = time.perf_counter()
                    result = _execute_tool(block.name, block.input)
                    tool_elapsed = time.perf_counter() - tool_start
                    # Audit trail — written to the answer card so we can
                    # forensic-debug any future "wait why did Mark say X?"
                    # without having to dump session state in chat.
                    tool_call_log.append({
                        "name": block.name,
                        "input": block.input,
                        "output": result,
                        "elapsed_s": round(tool_elapsed, 3),
                    })
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unusual stop_reason — most often max_tokens. Keep whatever text
        # the model produced and tell the user the reply got truncated, so
        # they know to ask a more focused question rather than seeing a
        # silent "(no response)".
        if any_text:
            final_text = (
                any_text
                + f"\n\n_(Reply was truncated by stop_reason={response.stop_reason} "
                "— ask a more focused follow-up if you need more.)_"
            )
        else:
            final_text = (
                f"(Mark stopped with stop_reason={response.stop_reason} "
                "and no text content. Most often this means the tool result "
                "was too large to fit in context — try narrowing the query.)"
            )
        break
    else:
        final_text = "(Agent loop exited without a final text response — likely hit the turn cap.)"

    total_elapsed = time.perf_counter() - chat_start
    print(
        f"[timing] chat total={total_elapsed:.2f}s "
        f"turns={turn_count} tool_calls={tool_call_count} "
        f"conversation_id={conversation_id or 'none'}",
        flush=True,
    )

    # Confabulation guard — intercept claims of social-tool success that
    # weren't backed by a real tool call this turn. Mark has repeatedly
    # generated convincing "Done!" responses without actually calling the
    # tools (fabricated file IDs, made-up Drive URLs, wrong response
    # shapes). The prompt rule alone hasn't held; this code check is the
    # backstop. If we catch a confabulation, overwrite the response with
    # a loud warning so the user knows nothing actually shipped.
    tools_called_names = [entry.get("name", "") for entry in tool_call_log]
    confab_reason = _detect_confabulation(final_text, tools_called_names)
    if confab_reason:
        print(
            f"[confabulation] intercepted hallucinated success:\n{confab_reason}\n"
            f"tools_called={tools_called_names}",
            flush=True,
        )
        original = final_text or "(no response)"
        final_text = (
            ":warning: *Confabulation intercepted — nothing actually shipped.*\n\n"
            "I was about to claim a social action succeeded, but I never "
            "called the tool that would have performed it. Here's what I "
            "almost told you:\n\n"
            f"```\n{original.strip()[:1500]}\n```\n\n"
            "*What's actually wrong:*\n"
            f"```\n{confab_reason}\n```\n\n"
            "*Tools I called this turn:* "
            + (", ".join(tools_called_names) if tools_called_names else "(none)")
            + "\n\n"
            "This is a known failure mode where I generate a plausible "
            "success message without doing the work. Try (1) breaking the "
            "request into one tool at a time, or (2) explicitly naming the "
            "tools and asking me to paste back each raw result. If it keeps "
            "happening, flag it to the team — there's a guard for this but "
            "the underlying behavior needs a deeper fix."
        )

    if conversation_id is not None:
        # Trim oldest first if we exceed the cap.
        if len(messages) > MAX_CONVERSATION_MESSAGES:
            messages = messages[-MAX_CONVERSATION_MESSAGES:]
        _conversations[conversation_id] = messages

    # Record the answer card — full audit trail for forensic debugging
    # and future eval corpus growth. Wrapped in try/except in
    # answer_cards.record_card itself; do NOT let instrumentation break
    # the user-facing return.
    _record_answer_card(
        user_message=user_message,
        final_response=final_text or "(no response)",
        tool_calls=tool_call_log,
        turn_count=turn_count,
        tool_call_count=tool_call_count,
        started_at=chat_start_unix,
        completed_at=time.time(),
        channel=channel,
        user=user,
        conversation_id=conversation_id,
        slack_message_ts=slack_message_ts,
    )

    return final_text or "(no response)"
