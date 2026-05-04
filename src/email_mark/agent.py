"""Tool-using agent loop.

Runs a multi-turn conversation with Claude where Claude can call tools
to look things up in HubSpot (and eventually BigQuery, etc.) before
producing a final text response.

Public entrypoint:
    chat(user_message: str) -> str
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

from anthropic import Anthropic
from dotenv import find_dotenv, load_dotenv

from email_mark.hubspot_marketing import (
    get_email_statistics,
    list_marketing_emails,
)

load_dotenv(find_dotenv())

MODEL = "claude-sonnet-4-5"
MAX_AGENT_TURNS = 10  # Hard cap so a runaway loop can't burn through tokens.

_client: Optional[Anthropic] = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


SYSTEM_PROMPT = """You are an AI coworker for the Glowforge marketing team, available in Slack.

You help with lifecycle marketing tasks: drafting emails, exploring data, proposing
audiences, and answering questions about marketing performance. Keep responses
friendly and concise; use Slack-style formatting (no Markdown headers; light
use of *bold*; bullet points are fine).

You have tools to look up real data in HubSpot — use them when the user asks
about specific campaigns, emails, or A/B test results rather than guessing or
asking for screenshots. When you call a tool, summarize what came back in
plain language, not raw JSON. If a tool returns no data or fails, say so
clearly and suggest what to try.

What you don't have yet (be honest about gaps):
- Direct access to the BigQuery data warehouse (in progress)
- The ability to send emails or create lists (read-only for now)
- Access to forum/community data
"""


# ---------------------------------------------------------------------------
# Tool definitions: schema for Claude + Python handler.
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
    emails = list_marketing_emails(name_contains=name_contains, limit=limit)
    return {
        "found": len(emails),
        "emails": [
            {
                "id": e.get("id"),
                "name": e.get("name"),
                "state": e.get("state"),
                "subject": e.get("subject"),
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


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_marketing_emails",
        "description": (
            "Search HubSpot marketing emails by name substring (case-insensitive). "
            "Returns matching emails with id, name, state, and subject line. "
            "Use this first when the user asks about a specific campaign, email, "
            "or A/B test by name. The official HubSpot connector cannot query "
            "marketing emails — only this tool can."
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
            },
            "required": ["name_contains"],
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
]

TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "search_marketing_emails": _tool_search_marketing_emails,
    "get_marketing_email_stats": _tool_get_marketing_email_stats,
}


# ---------------------------------------------------------------------------
# Agent loop.
# ---------------------------------------------------------------------------


def _execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return handler(args)
    except Exception as exc:
        return {"error": f"Tool {name} failed: {exc}"}


def chat(user_message: str, *, system_prompt: str = SYSTEM_PROMPT) -> str:
    """Run an agent loop until Claude produces a final text response."""
    client = _get_client()
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": user_message}
    ]

    for _ in range(MAX_AGENT_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        # Always echo the assistant's full content back into the conversation.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return "".join(
                getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"
            )

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

        # Unexpected stop reason — give up cleanly.
        break

    return "(Agent loop exited without producing a final text response — likely hit the turn cap.)"
