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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from anthropic import Anthropic
from dotenv import find_dotenv, load_dotenv

from email_mark.hubspot_marketing import (
    clone_marketing_email,
    get_email_body_text,
    get_email_statistics,
    list_marketing_emails,
    update_email_body,
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
MAX_AGENT_TURNS = 10  # Hard cap so a runaway loop can't burn through tokens.
HUBSPOT_PORTAL_ID = "8614495"  # Glowforge HubSpot portal — used for UI URLs.

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


SYSTEM_PROMPT = (
    """You are an AI coworker for the Glowforge marketing team, available in Slack.

You help with lifecycle marketing tasks: drafting emails, exploring data, proposing
audiences, and answering questions about marketing performance. Keep responses
friendly and concise; use Slack-style formatting (no Markdown headers; light
use of *bold*; bullet points are fine).

You have tools to look up real data in HubSpot and to create draft emails.
Use them rather than guessing. When a tool returns data, summarize in plain
language — never paste raw JSON.

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

PRIVACY AND SENSITIVE DATA — strict rules:
You are operating in a Slack workspace that contains HubSpot data. Treat all
customer information as sensitive. Specifically:

- NEVER share individual customer email addresses, phone numbers, or
  postal addresses. If a tool returns these, don't include them in your reply
  unless the user is explicitly asking about themselves or their own account.
- NEVER share full names paired with behavioral data, lifecycle status, deal
  status, or financial information. "Jane Doe is at risk of churning" — bad.
  "8% of subscribers are at risk of churning" — fine.
- NEVER share deal amounts, revenue numbers, or pipeline values for individual
  customers or deals.
- NEVER export or paste lists of contacts, even if the user asks. Refuse
  politely and suggest they export from HubSpot directly if they need that.
- DO share aggregate statistics, counts, percentages, distributions, and
  patterns. Marketing performance numbers (sends, opens, clicks, unsubscribes)
  at the campaign or audience level are fine.
- DO share email content (subjects, body copy) that's already drafted or sent
  marketing material. It's marketing copy, not PII.
- If asked to do something that requires sharing individual PII, refuse
  politely and explain the rule. Offer the aggregate version if possible.

If you're unsure whether something is sensitive, default to NOT sharing it
and ask the user to confirm whether the request is appropriate.
"""
    + _brand_voice_section()
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
    "get_email_body": _tool_get_email_body,
    "get_marketing_email_stats": _tool_get_marketing_email_stats,
    "create_email_draft": _tool_create_email_draft,
    "get_subscription_distribution": _tool_get_subscription_distribution,
    "count_inactive_users": _tool_count_inactive_users,
    "get_print_recency_buckets": _tool_get_print_recency_buckets,
    "run_warehouse_query": _tool_run_warehouse_query,
    "describe_table": _tool_describe_table,
    "lookup_slack_user": _tool_lookup_slack_user,
    "send_slack_dm": _tool_send_slack_dm,
}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def _execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return handler(args)
    except Exception as exc:
        return {"error": f"Tool {name} failed: {exc}"}


def reset_conversation(conversation_id: str) -> None:
    """Wipe the stored history for a single conversation."""
    _conversations.pop(conversation_id, None)


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
        messages: List[Dict[str, Any]] = list(_conversations.get(conversation_id, []))
    else:
        messages = []

    messages.append({"role": "user", "content": user_message})

    final_text = ""
    for _ in range(MAX_AGENT_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            final_text = "".join(
                getattr(b, "text", "") for b in response.content
                if getattr(b, "type", None) == "text"
            )
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
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

        break
    else:
        final_text = "(Agent loop exited without a final text response — likely hit the turn cap.)"

    if conversation_id is not None:
        # Trim oldest first if we exceed the cap.
        if len(messages) > MAX_CONVERSATION_MESSAGES:
            messages = messages[-MAX_CONVERSATION_MESSAGES:]
        _conversations[conversation_id] = messages

    return final_text or "(no response)"
