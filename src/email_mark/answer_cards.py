"""Answer card persistence — Phase 1 of the eval/instrumentation work.

Every substantive Mark response gets logged as an "answer card":
the user question, the full tool call sequence, the final response,
timestamps, channel/user. Stored in SQLite on Render's persistent disk
(same `/var/data/` mount as the lessons file) so cards survive deploys.

What this enables:
  - Forensic debugging: pull up any past answer and see EXACTLY what
    Mark did to produce it (which tools, what args, what they returned).
  - Cache + sanity check: when a similar question comes up, surface
    the prior answer for re-use rather than re-running everything.
  - Eval corpus growth: verified-correct cards become test cases for
    the Phase 2 eval harness automatically.
  - Drift detection: nightly job re-runs sample of past correct
    answers, compares results, alerts on divergence.

Schema deliberately keeps tool_calls as a single JSON blob (denormalized)
because we want the FULL audit trail readable in one row. Forensic
queries that want to drill into tool args run JSON path queries via
sqlite's `json_extract` — fast enough for the volume we expect (handfuls
of cards per day, not millions).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


# Schema version — bump if you change the columns. We don't have a
# migration framework; treat existing dbs as the schema they were
# created with.
SCHEMA_VERSION = 1


_DDL = """
CREATE TABLE IF NOT EXISTS answer_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Slack-side identity
    channel TEXT,
    user TEXT,
    conversation_id TEXT,
    -- Question + answer
    user_message TEXT NOT NULL,
    final_response TEXT,
    -- Full tool-call audit trail. JSON list of
    --   {"name": str, "input": <args>, "output": <result>, "elapsed_s": float}
    -- in the order they were called.
    tool_calls_json TEXT,
    -- Per-turn stats
    turn_count INTEGER,
    tool_call_count INTEGER,
    -- Timing
    started_at TEXT NOT NULL,    -- ISO 8601 UTC
    completed_at TEXT NOT NULL,  -- ISO 8601 UTC
    elapsed_seconds REAL,
    -- Reaction signals (Phase 2)
    -- 'thumbs_up' | 'thumbs_down' | 'correction' | NULL
    reaction TEXT,
    reaction_notes TEXT,
    -- Slack message ts so reactions can be attributed back
    slack_message_ts TEXT
);

CREATE INDEX IF NOT EXISTS idx_answer_cards_started_at
    ON answer_cards(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_answer_cards_conversation_id
    ON answer_cards(conversation_id);
CREATE INDEX IF NOT EXISTS idx_answer_cards_channel
    ON answer_cards(channel);
"""


def _db_path() -> Path:
    """Resolve the SQLite file path.

    Prefer ANSWER_CARDS_DB_PATH env var (point at /var/data/ on Render so
    cards persist across deploys). Fall back to a repo-local path during
    development so tests can run without env setup.
    """
    override = os.environ.get("ANSWER_CARDS_DB_PATH")
    if override:
        path = Path(override)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    # Dev fallback: repo-local file. Not persistent across deploys but
    # fine for local testing.
    repo_root = Path(__file__).resolve().parent.parent.parent
    fallback = repo_root / "answer_cards.db"
    return fallback


_initialized: bool = False


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    with _connect() as conn:
        conn.executescript(_DDL)
    _initialized = True


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """SQLite connection with sane defaults for our usage."""
    conn = sqlite3.connect(str(_db_path()), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_card(
    *,
    user_message: str,
    final_response: str,
    tool_calls: List[Dict[str, Any]],
    turn_count: int,
    tool_call_count: int,
    started_at: float,  # time.time() float
    completed_at: float,
    channel: Optional[str] = None,
    user: Optional[str] = None,
    conversation_id: Optional[str] = None,
    slack_message_ts: Optional[str] = None,
) -> int:
    """Persist an answer card. Returns the new card id.

    `tool_calls` is the full audit trail — each entry is
        {"name": str, "input": <whatever>, "output": <whatever>,
         "elapsed_s": float}
    Mark's chat() loop accumulates these as it dispatches tools.

    Designed to NEVER raise — recording is instrumentation, not a
    user-facing operation. Failures are swallowed and logged so a
    broken DB doesn't take down Mark's main response path.
    """
    try:
        _ensure_initialized()
        started_iso = _iso(started_at)
        completed_iso = _iso(completed_at)
        elapsed = max(0.0, completed_at - started_at)
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO answer_cards (
                    channel, user, conversation_id,
                    user_message, final_response,
                    tool_calls_json,
                    turn_count, tool_call_count,
                    started_at, completed_at, elapsed_seconds,
                    slack_message_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel,
                    user,
                    conversation_id,
                    user_message,
                    final_response,
                    json.dumps(tool_calls, default=str),
                    turn_count,
                    tool_call_count,
                    started_iso,
                    completed_iso,
                    elapsed,
                    slack_message_ts,
                ),
            )
            card_id = cur.lastrowid
        return card_id or 0
    except Exception as exc:
        # Never let instrumentation break the user-facing flow.
        print(
            f"[answer_cards] failed to record card: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return 0


def get_recent_cards(
    *,
    limit: int = 5,
    channel: Optional[str] = None,
    user: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the N most recent cards, optionally scoped to channel/user.

    Scoping by channel is the default for Slack lookup commands: keep
    DM history out of public channels.
    """
    _ensure_initialized()
    where_clauses = []
    params: List[Any] = []
    if channel:
        where_clauses.append("channel = ?")
        params.append(channel)
    if user:
        where_clauses.append("user = ?")
        params.append(user)
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM answer_cards
            {where_sql}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_cards(
    needle: str,
    *,
    limit: int = 10,
    channel: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Substring-match against the user_message and final_response.

    Simple LIKE-based search. Good enough at the volume we expect
    (handfuls of cards per day). If/when we want semantic search,
    embed user_message at write time and add a vector column.
    """
    _ensure_initialized()
    pattern = f"%{needle}%"
    where_extra = " AND channel = ?" if channel else ""
    params = [pattern, pattern]
    if channel:
        params.append(channel)
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM answer_cards
            WHERE (user_message LIKE ? OR final_response LIKE ?)
            {where_extra}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_card_by_id(card_id: int) -> Optional[Dict[str, Any]]:
    _ensure_initialized()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM answer_cards WHERE id = ?",
            (card_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def set_reaction(
    card_id: int,
    reaction: str,
    notes: Optional[str] = None,
) -> bool:
    """Set a reaction signal on a card. Used by Phase 2/3 for eval
    corpus growth and drift detection. `reaction` is one of
    'thumbs_up', 'thumbs_down', 'correction', or other free-form."""
    _ensure_initialized()
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE answer_cards SET reaction = ?, reaction_notes = ? WHERE id = ?",
                (reaction, notes, card_id),
            )
            return cur.rowcount > 0
    except Exception as exc:
        print(
            f"[answer_cards] failed to set reaction: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return False


def _iso(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    # Parse the tool_calls JSON for caller convenience.
    raw = d.get("tool_calls_json")
    if raw:
        try:
            d["tool_calls"] = json.loads(raw)
        except Exception:
            d["tool_calls"] = []
    else:
        d["tool_calls"] = []
    return d


def format_card_for_slack(card: Dict[str, Any], *, full: bool = False) -> str:
    """Render an answer card as a Slack-formatted string.

    `full=True` includes the entire tool_calls sequence with args and
    outputs. `full=False` (default) shows just tool names + a short
    response preview, for list views.
    """
    started = card.get("started_at") or "?"
    user = card.get("user") or "?"
    q = (card.get("user_message") or "").strip()
    if len(q) > 200:
        q = q[:200] + "…"
    resp = (card.get("final_response") or "").strip()
    if not full and len(resp) > 300:
        resp = resp[:300] + "…"

    tools = card.get("tool_calls") or []
    tool_names = [t.get("name") for t in tools]

    parts = [
        f"*Card #{card.get('id')}* — {started} — by <@{user}>",
        f"*Q:* {q}",
        f"*Tools used ({len(tools)}):* {', '.join(tool_names) or '(none)'}",
    ]
    if full:
        parts.append("*Full tool sequence:*")
        for i, t in enumerate(tools, 1):
            args_str = json.dumps(t.get("input"), default=str)
            if len(args_str) > 400:
                args_str = args_str[:400] + "…"
            out_str = json.dumps(t.get("output"), default=str)
            if len(out_str) > 600:
                out_str = out_str[:600] + "…"
            parts.append(
                f"  {i}. `{t.get('name')}` "
                f"({t.get('elapsed_s', 0):.2f}s)\n"
                f"     args: `{args_str}`\n"
                f"     result: `{out_str}`"
            )
    parts.append(f"*Response:* {resp}")
    return "\n".join(parts)
