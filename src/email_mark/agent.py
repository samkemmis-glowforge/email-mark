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
from email_mark.hubspot_crm import (
    list_contact_properties,
    search_contacts,
)
from email_mark.hubspot_marketing import (
    clone_marketing_email,
    get_contact_email_events,
    get_email_body_text,
    get_email_engagement_contacts,
    get_email_engagers_via_list,
    get_email_statistics,
    get_email_widget_html,
    get_email_widget_structure,
    get_workflow_details,
    get_workflow_enrollments,
    list_marketing_emails,
    list_workflows,
    update_email_body,
    update_email_by_widget_map,
    update_marketing_email,
)
from email_mark.slack_helpers import (
    lookup_user as slack_lookup_user,
    send_dm as slack_send_dm,
)
from email_mark.warehouse import (
    count_inactive_users,
    describe_table,
    get_print_recency_buckets,
    get_subscription_distribution,
    run_warehouse_query,
)

load_dotenv(find_dotenv())

MODEL = "claude-sonnet-4-5"
MAX_AGENT_TURNS = 25  # Hard cap so a runaway loop can't burn through tokens.
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
# Lessons-learned loading: domain knowledge captured from past conversations.
# An editable file at prompts/lessons_learned.md gets injected into the system
# prompt at startup. Add entries as we discover gotchas about data sources,
# tool behavior, business rules, etc. — see the file's header for the format.
# ---------------------------------------------------------------------------


def _lessons_file_path() -> Path:
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


def remember_lesson(heading: str, lesson: str) -> Dict[str, Any]:
    """Append a lesson to prompts/lessons_learned.md under the given heading.

    If a section with the matching heading already exists, append the
    lesson as a new bullet under it. Otherwise create a new section at
    the bottom of the file.

    On Render, the file lives in the deployed-code path which gets reset
    on each new deploy — so lessons saved at runtime persist until the
    next push. The response includes a `permanence_note` reminding the
    user to commit to git for durable storage.
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
    # Match the heading and everything up to the next ## section (or EOF).
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

    lessons_file.write_text(new_content)

    return {
        "saved": True,
        "heading": heading_clean,
        "lesson": lesson_clean,
        "date": today,
        "section_action": section_action,
        "permanence_note": (
            "Lesson saved to the local file. On Render this resets to the "
            "git version on every deploy — share the lesson in chat so the "
            "user can commit it for permanent storage."
        ),
    }


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

You have tools to look up real data in HubSpot and to create draft emails.
Use them rather than guessing. When a tool returns data, summarize in plain
language — never paste raw JSON.

CAPTURING LESSONS — save them yourself, don't ask:
When the user corrects you about something durable — a data source quirk,
an undocumented tool behavior, a business rule that differs from your
assumptions — call the remember_lesson tool to save it directly. Don't
just propose the lesson in chat and wait for the user to paste it; that
hasn't been working and you keep making the same mistakes.

After saving, briefly mention in chat that you saved it (one sentence),
include the lesson text, and remind the user to commit the file to git
so it survives the next deploy. Example: "Saved a lesson to lessons_
learned.md: 'BQ hubspot.email_events only goes back to Dec 2024.' Worth
committing to git so it persists across deploys."

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

DRAFTING EMAILS — workflow:
1. Write the subject + body in chat for the user to review.
2. AFTER presenting drafts, ALWAYS proactively ask whether to create them
   as drafts in HubSpot. Don't wait for the user to think to ask. Phrase it
   like: "Want me to create these in HubSpot as drafts? If yes, which existing
   email should I clone as the template?" If you have a sensible template
   guess from prior context, suggest it.
3. Iterate based on user feedback (tone, length, structure).
4. Once the user gives explicit approval ("yes," "create them," "go ahead"),
   call create_email_draft for each one — passing the FULL body_text you wrote
   in chat (subject, name, AND body all go in one call).
5. create_email_draft clones a template, updates the subject/name, and replaces
   the largest body text widget with your body_text. Other template modules
   (header image, CTA button, footer) carry over unchanged. Always share the
   edit_url back so the user can review.
6. The body_update field in the response tells you whether body replacement
   succeeded. If it failed, surface that to the user honestly so they know
   to paste the body manually.
7. If you don't know which template to use, call search_marketing_emails to
   suggest 2-3 candidates and let the user pick.

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
- Prebuilt aggregate tools: get_subscription_distribution, count_inactive_users,
  get_print_recency_buckets. Use these first when the question fits.
- Ad-hoc SQL: run_warehouse_query lets you write your own BigQuery SELECT for
  questions the prebuilt tools can't answer (joins, custom aggregations,
  funnel analysis). Use describe_table first if you're unsure about columns.

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
"""
    + _brand_voice_section()
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


def _tool_lookup_slack_user(args: Dict[str, Any]) -> Dict[str, Any]:
    matches = slack_lookup_user(args.get("query", ""))
    return {"matches": matches[:10], "total_matches": len(matches)}


def _tool_send_slack_dm(args: Dict[str, Any]) -> Dict[str, Any]:
    return slack_send_dm(str(args["user_id"]), str(args["text"]))


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
            "Save a durable lesson to your lessons_learned.md file so you "
            "won't make the same mistake next time. Use this when the user "
            "corrects you about something that's likely to recur: a data "
            "source quirk (e.g. a BigQuery table is stale or incomplete), "
            "an undocumented tool behavior, a business rule, a definition "
            "that differs from your default assumption, or a workflow "
            "gotcha. The lesson takes effect on your next inference call.\n\n"
            "Only save lessons for DURABLE truths — gotchas about systems "
            "that will still be true next month. Do NOT save lessons for "
            "one-off preferences, momentary mistakes, or tone/style "
            "feedback (those go in the system prompt directly).\n\n"
            "When you save a lesson, also briefly tell the user in chat: "
            "(1) that you saved it, (2) what it says, and (3) that they "
            "should commit it to git for permanent storage — on Render the "
            "file resets on every deploy."
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
]

TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "search_marketing_emails": _tool_search_marketing_emails,
    "fetch_forum_post": _tool_fetch_forum_post,
    "remember_lesson": _tool_remember_lesson,
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
    "get_subscription_distribution": _tool_get_subscription_distribution,
    "count_inactive_users": _tool_count_inactive_users,
    "get_print_recency_buckets": _tool_get_print_recency_buckets,
    "run_warehouse_query": _tool_run_warehouse_query,
    "describe_table": _tool_describe_table,
    "search_hubspot_contacts": _tool_search_hubspot_contacts,
    "list_contact_properties": _tool_list_contact_properties,
    "lookup_slack_user": _tool_lookup_slack_user,
    "send_slack_dm": _tool_send_slack_dm,
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


def chat(
    user_message: str,
    *,
    conversation_id: Optional[str] = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    """Run an agent loop until Claude produces a final text response.

    If conversation_id is provided, prior messages from that conversation
    are loaded as context, and the updated history is saved back at the end.
    Without conversation_id, every call is a fresh conversation.
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
    turn_count = 0
    tool_call_count = 0

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
    for turn_idx in range(MAX_AGENT_TURNS):
        turn_count = turn_idx + 1

        # Log BEFORE the call so we can see in-flight inferences in Render
        # logs even when the API hangs.
        in_flight_msg_count = len(messages)
        print(
            f"[timing] inference turn={turn_count} "
            f"starting (messages_in_history={in_flight_msg_count})",
            flush=True,
        )

        inference_start = time.perf_counter()
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_blocks,
                tools=tools_for_call,
                messages=messages,
                timeout=INFERENCE_TIMEOUT_SECONDS,
            )
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
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_call_count += 1
                    result = _execute_tool(block.name, block.input)
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

    if conversation_id is not None:
        # Trim oldest first if we exceed the cap.
        if len(messages) > MAX_CONVERSATION_MESSAGES:
            messages = messages[-MAX_CONVERSATION_MESSAGES:]
        _conversations[conversation_id] = messages

    return final_text or "(no response)"
