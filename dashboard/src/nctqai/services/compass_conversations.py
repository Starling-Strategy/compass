"""Conversations service — Postgres-backed reads for /compass/conversations.

The dashboard pulls conversation metadata, messages, verdicts, and feedback
straight from the canonical compass.* schema rather than going through the
HTTP API. This keeps the conversations view independent of backend API
availability and avoids a network hop on the same Tailscale subnet.

Companion to ``compass_stats.py``; everything here is per-session detail
(or per-session list) rather than aggregate KPI counts.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from nctqai.db import COMPASS_SCHEMA, artifact_summary_v2_available, run_sql
from nctqai.models.compass import (
    ChatMessage,
    ConversationDetail,
    ConversationSummary,
    MessageFeedback,
)
from nctqai.services.compass_snapshot_memory import (
    fetch_latest_snapshot_rows,
    memory_by_session,
    normalize_since as _normalize_since,
)

logger = logging.getLogger(__name__)


# UUID v4 regex — used by the top-of-page smart search to detect a session ID
# pasted anywhere (raw, in a URL, or surrounded by other text). Lowercase only;
# we lowercase the input before matching to stay tolerant of pasted casing.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def extract_session_id(text: str) -> str | None:
    """Return the first UUID-looking substring in ``text``, or None.

    Matches both bare IDs and IDs embedded in URLs or surrounding text. Casing
    is normalized to lowercase since session_id values land in the DB
    lowercased by the chat backend.
    """
    if not text:
        return None
    match = _UUID_RE.search(text.lower())
    return match.group(0) if match else None


def session_exists(session_id: str) -> bool:
    """Cheap existence check used by the smart-search redirect path."""
    if not session_id:
        return False
    rows = run_sql(
        f"SELECT 1 AS x FROM {COMPASS_SCHEMA}.chat_sessions WHERE session_id = %s LIMIT 1",
        (session_id,),
    )
    return bool(rows)


# ── Shared FROM/JOIN block — reused by list + single-summary queries ───
#
# Both list_conversations and get_conversation_summary need the same
# per-session rollup: first user message, primary district, and feedback
# counts. Defining the LATERAL joins once keeps the two queries in
# lockstep — change one column here and both call sites pick it up.

_SESSION_SUMMARY_FROM = f"""
    FROM {COMPASS_SCHEMA}.chat_sessions s
    LEFT JOIN {COMPASS_SCHEMA}.chat_messages m ON m.session_id = s.session_id
    LEFT JOIN LATERAL (
        SELECT content
        FROM {COMPASS_SCHEMA}.chat_messages
        WHERE session_id = s.session_id
          AND role = 'user'
          AND content NOT LIKE '[%%'
          AND content != 'Final result processed.'
          AND length(content) > 5
        ORDER BY timestamp
        LIMIT 1
    ) first_message ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            COUNT(*) FILTER (WHERE rating = 1) AS thumbs_up_count,
            COUNT(*) FILTER (WHERE rating = -1) AS thumbs_down_count
        FROM {COMPASS_SCHEMA}.message_feedback
        WHERE session_id = s.session_id
    ) fb ON TRUE
"""

_SESSION_SUMMARY_PROJECTION = """
    s.session_id,
    s.created_at,
    COUNT(DISTINCT m.id) AS message_count,
    MAX(first_message.content) AS first_message,
    COALESCE(MAX(fb.thumbs_up_count), 0) AS thumbs_up_count,
    COALESCE(MAX(fb.thumbs_down_count), 0) AS thumbs_down_count
"""


# Triage quick-filter tabs (TRIAGE-R1). "all" is the no-op default. The values
# double as the ``tab`` URL/query param and the keys in count_conversations_by_tab.
TRIAGE_TABS: tuple[str, ...] = (
    "all",
    "thumbs-down",
    "has-table",
    "has-csv-export",
    "has-chart",
    "unreviewed",
)

# Only the well-formed UUID session_ids can be cast to uuid for the verdicts
# join (compass.verdicts.session_id is uuid; chat_sessions.session_id is text and
# holds a handful of non-UUID legacy values). Guarding the cast keeps the EXISTS
# sargable on the uuid index AND avoids a cast error on the bad rows.
_UUID_SQL_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# A session "has a table" when some saved assistant snapshot carries a non-empty
# result.rows array. Migration 163 precomputes that condition into the tiny
# snapshot_summary->>'has_table' boolean (kept current by the chat_messages
# trigger), so this probe reads a small column instead of detoasting the full
# snapshot JSONB per assistant message — and rides idx_chat_messages_has_table.
# Correlated by s.session_id so it composes with the date/search predicates.
_HAS_TABLE_EXISTS = f"""EXISTS (
    SELECT 1 FROM {COMPASS_SCHEMA}.chat_messages tm
     WHERE tm.session_id = s.session_id
       AND tm.role = 'assistant'
       AND (tm.snapshot_summary->>'has_table') = 'true'
)"""


def _artifact_exists(summary_key: str) -> str:
    return f"""EXISTS (
        SELECT 1 FROM {COMPASS_SCHEMA}.chat_messages tm
         WHERE tm.session_id = s.session_id
           AND tm.role = 'assistant'
           AND (tm.snapshot_summary->>'{summary_key}') = 'true'
    )"""


_ARTIFACT_TAB_PREDICATES = {
    "has-table": _HAS_TABLE_EXISTS,
    "has-csv-export": _artifact_exists("has_csv_export"),
    "has-chart": _artifact_exists("has_chart"),
}

# A session has at least one real L1 turn verdict in the ledger. The ::uuid cast
# is wrapped in a CASE that yields NULL for non-UUID legacy session_ids: a bare
# `regex AND ::uuid` is NOT safe because Postgres may reorder the AND and cast
# the bad value first (it does — verified 2026-06-21, it raised on the literal
# "None"). The CASE guarantees the cast only runs on the matching branch, and
# the planner still drives it through the uuid index (Index Only Scan).
_HAS_TURN_VERDICT_EXISTS = f"""EXISTS (
    SELECT 1 FROM {COMPASS_SCHEMA}.verdicts v
     WHERE v.session_id = CASE
             WHEN s.session_id ~ '{_UUID_SQL_RE}'
             THEN s.session_id::uuid ELSE NULL END
       AND v.scope = 'turn'
)"""


# ── List conversations ─────────────────────────────────────────────────


def _base_where(
    since: datetime | None, until: datetime | None, search: str
) -> tuple[list[str], list[Any]]:
    """Build the date + free-text WHERE predicates shared by the list and the
    tab-count queries, so both apply identical date/search scoping (the counts
    can never disagree with the rows on these filters).

    ``until`` is the EXCLUSIVE upper bound (``created_at < until``) — a "through
    this day" date already converted to the following midnight by
    ``range_pills.resolve_until``. Together with ``since`` it forms the half-open
    window [since, until) the weekly-review use case needs.
    """
    clauses: list[str] = []
    binds: list[Any] = []
    if since is not None:
        clauses.append("s.created_at >= %s")
        binds.append(since)
    if until is not None:
        clauses.append("s.created_at < %s")
        binds.append(until)
    if search:
        # Multi-word AND — split the query on whitespace, require each term to
        # appear in *some* message in the session (user OR assistant). Each term
        # gets its own correlated EXISTS; strips legacy tool-marker rows the same
        # way get_conversation does.
        for term in search.split():
            clauses.append(
                f"""EXISTS (
                    SELECT 1 FROM {COMPASS_SCHEMA}.chat_messages sm
                     WHERE sm.session_id = s.session_id
                       AND sm.content NOT LIKE '[%%'
                       AND sm.content != 'Final result processed.'
                       AND sm.content ILIKE %s
                )"""
            )
            binds.append(f"%{term}%")
    return clauses, binds


def list_conversations(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    feedback: str = "all",
    intent: str = "all",
    search: str = "",
    scenario_id: int | None = None,
    tab: str = "all",
    limit: int = 100,
) -> list[ConversationSummary]:
    """Return recent conversations with quality + feedback rollups.

    Args:
        since: Only sessions created at/after this UTC bound. None = no bound.
        until: Exclusive upper bound (created_at < until). None = no bound.
        feedback: One of "all", "thumbs_up", "thumbs_down", "unrated".
        intent: Accepted for backwards-compatibility with existing callers; no longer filters.
        search: ILIKE-substring match against any message in the session.
        scenario_id: Accepted for backwards-compatibility with existing callers; no longer filters.
        tab: Triage quick-filter — one of TRIAGE_TABS. "thumbs-down" reuses the
            thumbs-down feedback rollup; "has-table" keeps only sessions with a
            saved table; "unreviewed" keeps sessions with no thumbs rating AND
            no L1 turn verdict. "all" is the no-op default.
        limit: Max rows returned.
    """
    since = _normalize_since(since)
    until = _normalize_since(until)

    # PICK-THEN-ENRICH. Every filter here is SESSION-level, so we can pick the
    # newest ``limit`` sessions FIRST (ORDER BY created_at DESC, rides
    # idx_chat_sessions_created_at) and enrich only those — message_count and
    # first_message are computed for ~100 rows, not all ~45k. The old query
    # joined every message + ran the first_message LATERAL per session and only
    # then sorted/limited, because ORDER BY MAX(m.timestamp) blocked the LIMIT
    # from pushing down — ~2.5s at "all time". The per-session feedback rollup is
    # a LATERAL that returns exactly one row, so its columns filter directly in
    # WHERE (no GROUP BY / HAVING needed).
    #
    # Ordering note: the list now sorts by ``s.created_at`` (session start),
    # consistent with the range pill (which scopes on created_at) — not by last
    # message time. created_at is what an index can serve; MAX(timestamp) cannot.
    where_clauses, where_binds = _base_where(since, until, search)

    # Artifact tabs and the verdict half of unreviewed are correlated EXISTS;
    # thumbs conditions read the one-row feedback LATERAL (fb) directly.
    if tab in _ARTIFACT_TAB_PREDICATES:
        where_clauses.append(
            _ARTIFACT_TAB_PREDICATES[tab]
            if artifact_summary_v2_available()
            else "FALSE"
        )
    elif tab == "unreviewed":
        where_clauses.append(f"NOT {_HAS_TURN_VERDICT_EXISTS}")
        where_clauses.append("fb.thumbs_up_count = 0 AND fb.thumbs_down_count = 0")
    elif tab == "thumbs-down":
        where_clauses.append("fb.thumbs_down_count > 0")

    if feedback == "thumbs_up":
        where_clauses.append("fb.thumbs_up_count > 0")
    elif feedback == "thumbs_down":
        where_clauses.append("fb.thumbs_down_count > 0")
    elif feedback == "unrated":
        where_clauses.append("fb.thumbs_up_count = 0 AND fb.thumbs_down_count = 0")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        WITH picked AS (
            SELECT
                s.session_id,
                s.created_at,
                fb.thumbs_up_count,
                fb.thumbs_down_count
            FROM {COMPASS_SCHEMA}.chat_sessions s
            LEFT JOIN LATERAL (
                SELECT
                    COUNT(*) FILTER (WHERE rating = 1) AS thumbs_up_count,
                    COUNT(*) FILTER (WHERE rating = -1) AS thumbs_down_count
                FROM {COMPASS_SCHEMA}.message_feedback
                WHERE session_id = s.session_id
            ) fb ON TRUE
            {where_sql}
            ORDER BY s.created_at DESC
            LIMIT %s
        )
        SELECT
            p.session_id,
            p.created_at,
            (
                SELECT COUNT(*)
                FROM {COMPASS_SCHEMA}.chat_messages m
                WHERE m.session_id = p.session_id
            ) AS message_count,
            first_message.content AS first_message,
            COALESCE(p.thumbs_up_count, 0) AS thumbs_up_count,
            COALESCE(p.thumbs_down_count, 0) AS thumbs_down_count
        FROM picked p
        LEFT JOIN LATERAL (
            SELECT content
            FROM {COMPASS_SCHEMA}.chat_messages
            WHERE session_id = p.session_id
              AND role = 'user'
              AND content NOT LIKE '[%%'
              AND content != 'Final result processed.'
              AND length(content) > 5
            ORDER BY timestamp
            LIMIT 1
        ) first_message ON TRUE
        ORDER BY p.created_at DESC
    """
    bind = [*where_binds, limit]
    rows = run_sql(sql, tuple(bind))
    return [
        ConversationSummary.model_validate(r)
        for r in _add_primary_district_from_snapshot_memory(rows)
    ]


def count_conversations_by_tab(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    search: str = "",
    feedback: str = "all",
) -> dict[str, int | None]:
    """Return the live triage-tab counts for the active range/search/feedback scope.

    Counts share the SAME date+search predicates as ``list_conversations`` (via
    ``_base_where``) AND the same feedback filter, so a tab's chip can never
    disagree with the rows it shows. (Before W2-4 the feedback dropdown narrowed
    the list but not the chips, so e.g. "👎 Thumbs down" showed 12 rows under an
    "All 700" chip — the count ignored the active feedback filter.)

    Performance note: all / thumbs-down / unreviewed are cheap (~0.5s even at 30
    days — feedback table is tiny and the verdict EXISTS rides the uuid index).
    ``has-table`` historically had to detoast the snapshot JSONB per in-range
    assistant message (~3s at 7d, a minute at "all time"); since migration 163 it
    probes the precomputed ``snapshot_summary->>'has_table'`` boolean over
    ``idx_chat_messages_has_table``, so it is now cheap at every window too. The
    ``compute_has_table`` guard below is kept as a conservative carry-over (no
    longer the hard necessity it was): for "All time" (since is None) it still
    returns ``None`` — the tab works as a filter, it just shows no chip number.
    Honest, never fabricated.
    """
    since = _normalize_since(since)
    until = _normalize_since(until)
    base_clauses, base_binds = _base_where(since, until, search)

    # W2-4: mirror list_conversations' feedback filter so the chips count exactly
    # the sessions the list would show. No GROUP BY here — the per-session
    # feedback LATERAL returns one row, so its columns filter directly in WHERE.
    feedback_where = {
        "thumbs_up": "fb.thumbs_up_count > 0",
        "thumbs_down": "fb.thumbs_down_count > 0",
        "unrated": "fb.thumbs_up_count = 0 AND fb.thumbs_down_count = 0",
    }.get(feedback)
    where_clauses = [*base_clauses]
    if feedback_where:
        where_clauses.append(feedback_where)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Artifact counts use the precomputed snapshot summary. Keep all-time chips
    # blank rather than make a potentially expensive count part of every load.
    compute_artifact_counts = since is not None and artifact_summary_v2_available()
    artifact_exprs = {
        key: predicate if compute_artifact_counts else "FALSE"
        for key, predicate in _ARTIFACT_TAB_PREDICATES.items()
    }

    # No GROUP BY: the feedback LATERAL returns one row per session and the
    # has-table / has-verdict signals are per-session EXISTS, so the base set is
    # already one row per session. Dropping the (pointless) GROUP BY s.session_id
    # halves the all-time cost (~1.2s -> ~0.6s) by removing the HashAggregate
    # over 45k rows. The verdict EXISTS stays a per-session probe — it
    # short-circuits on the uuid index (a one-shot DISTINCT over the 440k-row
    # turn-verdict index was measured ~5x SLOWER, so it is deliberately avoided).
    sql = f"""
        WITH base AS (
            SELECT
                fb.thumbs_up_count AS up,
                fb.thumbs_down_count AS down,
                {artifact_exprs['has-table']} AS has_table,
                {artifact_exprs['has-csv-export']} AS has_csv_export,
                {artifact_exprs['has-chart']} AS has_chart,
                {_HAS_TURN_VERDICT_EXISTS} AS has_verdict
            FROM {COMPASS_SCHEMA}.chat_sessions s
            LEFT JOIN LATERAL (
                SELECT
                    COUNT(*) FILTER (WHERE rating = 1) AS thumbs_up_count,
                    COUNT(*) FILTER (WHERE rating = -1) AS thumbs_down_count
                FROM {COMPASS_SCHEMA}.message_feedback
                WHERE session_id = s.session_id
            ) fb ON TRUE
            {where_sql}
        )
        SELECT
            COUNT(*) AS all_count,
            COUNT(*) FILTER (WHERE down > 0) AS thumbs_down,
            COUNT(*) FILTER (WHERE has_table) AS has_table,
            COUNT(*) FILTER (WHERE has_csv_export) AS has_csv_export,
            COUNT(*) FILTER (WHERE has_chart) AS has_chart,
            COUNT(*) FILTER (WHERE up = 0 AND down = 0 AND NOT has_verdict)
                AS unreviewed
        FROM base
    """
    rows = run_sql(sql, tuple(base_binds))
    row = rows[0] if rows else {}
    return {
        "all": int(row.get("all_count") or 0),
        "thumbs-down": int(row.get("thumbs_down") or 0),
        "has-table": int(row.get("has_table") or 0) if compute_artifact_counts else None,
        "has-csv-export": int(row.get("has_csv_export") or 0)
        if compute_artifact_counts
        else None,
        "has-chart": int(row.get("has_chart") or 0)
        if compute_artifact_counts
        else None,
        "unreviewed": int(row.get("unreviewed") or 0),
    }


# ── Conversation detail ────────────────────────────────────────────────


def get_conversation_summary(session_id: str) -> ConversationSummary | None:
    """Single-session lookup for the detail-pane sticky header.

    Uses the same projection as ``list_conversations`` so the sticky header
    shows the same district and thumbs data as the matching sidebar row,
    even if that row is outside the current filter.
    """
    sql = f"""
        SELECT
            {_SESSION_SUMMARY_PROJECTION}
        {_SESSION_SUMMARY_FROM}
        WHERE s.session_id = %s
        GROUP BY s.session_id, s.created_at
    """
    rows = run_sql(sql, (session_id,))
    if not rows:
        return None
    [row] = _add_primary_district_from_snapshot_memory(rows)
    return ConversationSummary.model_validate(row)


def get_conversation(session_id: str) -> ConversationDetail | None:
    """Return one conversation with its messages, oldest first.

    Filters out the legacy pipeline-marker rows (`[resolve_metric]`,
    `Final result processed.`) that polluted history before commit 6694cb0.
    """
    session_rows = run_sql(
        f"""
        SELECT session_id, created_at, owner_email
          FROM {COMPASS_SCHEMA}.chat_sessions
         WHERE session_id = %s
        """,
        (session_id,),
    )
    if not session_rows:
        return None
    session = session_rows[0]

    msg_rows = run_sql(
        f"""
        SELECT id, session_id, role, content, timestamp, trace_id
          FROM {COMPASS_SCHEMA}.chat_messages
         WHERE session_id = %s
           AND content NOT LIKE '[%%'
           AND content != 'Final result processed.'
         ORDER BY timestamp, id
        """,
        (session_id,),
    )
    messages = [ChatMessage.model_validate(r) for r in msg_rows]

    return ConversationDetail(
        session_id=session["session_id"],
        created_at=session["created_at"],
        messages=messages,
    )


def _add_primary_district_from_snapshot_memory(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    session_ids = [str(row.get("session_id")) for row in rows if row.get("session_id")]
    memories = memory_by_session(fetch_latest_snapshot_rows(session_ids=session_ids))
    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        session_id = str(item.get("session_id") or "")
        memory = memories.get(session_id)
        if memory is not None and memory.districts:
            district = memory.districts[0]
            item["district_count"] = len(memory.districts)
            if len(memory.districts) == 1:
                item["district_id"] = district.district_id
                item["district_name"] = district.district_name
                item["state"] = district.state
        enriched.append(item)
    return enriched


def get_session_feedback(session_id: str) -> list[MessageFeedback]:
    """Return per-message thumbs feedback rows for a session."""
    rows = run_sql(
        f"""
        SELECT id, session_id, message_id, rating,
               feedback_tags, feedback_text, created_at, updated_at
          FROM {COMPASS_SCHEMA}.message_feedback
         WHERE session_id = %s
         ORDER BY created_at
        """,
        (session_id,),
    )
    return [MessageFeedback.model_validate(r) for r in rows]
