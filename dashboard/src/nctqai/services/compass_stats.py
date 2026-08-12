"""Compass dashboard stats — aggregation queries for /compass/overview and /compass/operations.

All queries hit the canonical Compass schema on the staging database. Every query takes an
optional `since: datetime | None` (inclusive lower bound) and `until: datetime | None`
(exclusive upper bound) so the range pill on the dashboard (Today / 7d / 30d / All time)
and a custom from..to span scope everything consistently.

Sections:
  - Engagement: sessions, avg user questions, multi-turn rates (2+ and 3+)
  - Feedback: thumbs up/down/unrated as % of sessions (conversations)
  - Topics & districts: top N topics, districts, states
  - Operations (Starling-only): cost, latency, cost by phase
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from time import monotonic

from nctqai.db import (
    COMPASS_SCHEMA,
    artifact_summary_v2_available,
    run_sql,
    snapshot_summary_column_available,
)
from nctqai.services.compass_snapshot_memory import (
    SNAPSHOT_KEY,
    normalize_since as _normalize_since,
)

logger = logging.getLogger(__name__)

# ── In-process TTL caches ─────────────────────────────────────────────
# The snapshot_summary + SQL aggregation work killed the 4s pole, but
# every Overview load still hits Postgres for engagement + feedback.
# These are cheap queries (~10 ms each), but eliminating the round-trip
# under concurrent load compounds — and the server-side cache works even
# after the HTTP cache expires.

_ENGAGEMENT_TTL_SECONDS = 60
_ENGAGEMENT_CACHE: dict[str, tuple[float, "EngagementStats"]] = {}

_FEEDBACK_TTL_SECONDS = 60
_FEEDBACK_CACHE: dict[str, tuple[float, "FeedbackStats"]] = {}

_SNAPSHOT_AGGREGATES_TTL_SECONDS = 60
_SNAPSHOT_AGGREGATES_CACHE: dict[
    str, tuple[float, "SnapshotAggregates"]
] = {}
# Cache a few more than the 3 shown so get_top_*(limit>3) still slices correctly.
_SNAPSHOT_AGG_LIMIT = 10

# get_returning_users is read twice per Overview load — once all-time for the
# cumulative "Returning users" tile and once windowed for the "Chat users" tile.
# Those two reads are dispatched concurrently (asyncio.gather → to_thread), so on
# a cold cache they both miss and each still issues its own query. The win is
# ACROSS loads: at the all-time range both reads key to the same normalized
# (None, None) bounds, so repeated loads within the TTL reuse one cached value
# instead of re-querying. Same 60s bound as the snapshot cache above.
_RETURNING_USERS_TTL_SECONDS = 60
_RETURNING_USERS_CACHE: dict[str, tuple[float, "ReturningUsers"]] = {}


# ── Engagement ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class EngagementStats:
    sessions: int
    avg_user_questions: float  # user messages / sessions, 1 decimal
    multi_turn_2plus_rate: int  # % of sessions with ≥2 user messages
    multi_turn_3plus_rate: int  # % of sessions with ≥3 user messages


def _scope_clause(
    since: datetime | None, until: datetime | None, *, alias: str = "s", col: str = "created_at"
) -> tuple[str, list]:
    """Build a shared ``[since, until)`` WHERE clause + positional binds.

    ``since`` is an inclusive lower bound, ``until`` an exclusive upper bound —
    the same half-open window the trend query uses, so every Overview read scopes
    identically. Returns ("" , []) for the unbounded (all-time) case.
    """
    clauses: list[str] = []
    binds: list = []
    if since:
        clauses.append(f"{alias}.{col} >= %s")
        binds.append(since)
    if until:
        clauses.append(f"{alias}.{col} < %s")
        binds.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, binds


def _cache_key(since: datetime | None, until: datetime | None) -> str:
    """Hour-grain cache key so the TTL gets real hits.

    ``since`` is typically ``now - 7 days``, which changes every second. A
    minute-precision key rotated every clock-minute and the 60 s TTL never
    got a hit (each real load paid a cold miss). Hour grain lets requests
    within the same hour share a slot; the TTL still bounds staleness to 60 s.
    Same pattern as ``snapshot_aggregates``.
    """
    since_key = since.isoformat(timespec="hours") if since else "all"
    until_key = until.isoformat(timespec="hours") if until else "now"
    return f"{since_key}~{until_key}"


def get_engagement_stats(
    since: datetime | None = None, until: datetime | None = None
) -> EngagementStats:
    since = _normalize_since(since)
    until = _normalize_since(until)

    # Check in-process cache before hitting Postgres.
    key = _cache_key(since, until)
    now = monotonic()
    cached = _ENGAGEMENT_CACHE.get(key)
    if cached and now - cached[0] < _ENGAGEMENT_TTL_SECONDS:
        return cached[1]

    where_session, binds = _scope_clause(since, until)

    sql = f"""
        WITH scoped AS (
            SELECT s.session_id
            FROM {COMPASS_SCHEMA}.chat_sessions s
            {where_session}
        ),
        user_msgs AS (
            SELECT m.session_id, COUNT(*) AS n
            FROM {COMPASS_SCHEMA}.chat_messages m
            JOIN scoped ON scoped.session_id = m.session_id
            WHERE m.role = 'user'
            GROUP BY m.session_id
        )
        SELECT
            (SELECT COUNT(*) FROM scoped) AS sessions,
            COALESCE(SUM(n), 0) AS user_messages,
            COUNT(*) FILTER (WHERE n >= 2) AS multi_2plus,
            COUNT(*) FILTER (WHERE n >= 3) AS multi_3plus
        FROM user_msgs
    """
    rows = run_sql(sql, tuple(binds))
    row = rows[0] if rows else {}
    sessions = int(row.get("sessions") or 0)
    user_messages = int(row.get("user_messages") or 0)
    multi_2 = int(row.get("multi_2plus") or 0)
    multi_3 = int(row.get("multi_3plus") or 0)

    # W1-3: divide by ALL scoped sessions (the same denominator the Sessions
    # tile shows), not by "sessions that have ≥1 user message". The old split
    # denominator made "Multi-turn rate" a percentage of a smaller, invisible
    # base than the Sessions count it sat next to — inconsistent and misleading.
    avg = round(user_messages / sessions, 1) if sessions else 0.0
    rate2 = round(multi_2 / sessions * 100) if sessions else 0
    rate3 = round(multi_3 / sessions * 100) if sessions else 0
    result = EngagementStats(
        sessions=sessions,
        avg_user_questions=avg,
        multi_turn_2plus_rate=rate2,
        multi_turn_3plus_rate=rate3,
    )
    _ENGAGEMENT_CACHE[key] = (now, result)
    return result


def get_session_count(
    since: datetime | None = None, until: datetime | None = None
) -> int:
    """Plain session count over a ``[since, until)`` window.

    Used by the Overview route for the Sessions tile's prior-period trend (G2):
    a cheap COUNT over the immediately-preceding equal-length window, kept
    separate from ``get_engagement_stats`` so the trend's prior read never
    re-runs (or overwrites the captured bound of) the current-window read.
    """
    since = _normalize_since(since)
    until = _normalize_since(until)
    where, binds = _scope_clause(since, until)
    rows = run_sql(
        f"SELECT COUNT(*) AS n FROM {COMPASS_SCHEMA}.chat_sessions s {where}",
        tuple(binds),
    )
    return int(rows[0].get("n") or 0) if rows else 0


# ── Artifact coverage: tables, CSVs, and charts ──────────────────────


@dataclass(frozen=True)
class ArtifactCoverage:
    """Conversation-level coverage of saved result artifacts over a window.

    Each count uses all scoped conversations as its denominator and records a
    conversation once when any saved assistant turn contains that artifact.
    """

    sessions: int
    with_table: int
    table_pct: int
    with_csv_export: int = 0
    csv_export_pct: int = 0
    with_chart: int = 0
    chart_pct: int = 0
    available: bool = True


def get_artifact_coverage(
    since: datetime | None = None, until: datetime | None = None
) -> ArtifactCoverage:
    """Count saved tables, CSV exports, and charts in ``[since, until)``.

    The three signals are kept distinct. A CSV is availability in a saved result,
    not evidence that someone downloaded it.
    """
    since = _normalize_since(since)
    until = _normalize_since(until)
    where_session, binds = _scope_clause(since, until)
    artifacts_available = artifact_summary_v2_available()
    if artifacts_available:
        sql = f"""
            WITH scoped AS (
                SELECT s.session_id
                FROM {COMPASS_SCHEMA}.chat_sessions s
                {where_session}
            ),
            table_sessions AS (
                SELECT DISTINCT m.session_id
                FROM {COMPASS_SCHEMA}.chat_messages m
                JOIN scoped ON scoped.session_id = m.session_id
                WHERE m.role = 'assistant'
                  AND (m.snapshot_summary ->> 'has_table') = 'true'
            ),
            csv_sessions AS (
                SELECT DISTINCT m.session_id
                FROM {COMPASS_SCHEMA}.chat_messages m
                JOIN scoped ON scoped.session_id = m.session_id
                WHERE m.role = 'assistant'
                  AND (m.snapshot_summary ->> 'has_csv_export') = 'true'
            ),
            chart_sessions AS (
                SELECT DISTINCT m.session_id
                FROM {COMPASS_SCHEMA}.chat_messages m
                JOIN scoped ON scoped.session_id = m.session_id
                WHERE m.role = 'assistant'
                  AND (m.snapshot_summary ->> 'has_chart') = 'true'
            )
            SELECT
                (SELECT COUNT(*) FROM scoped) AS sessions,
                (SELECT COUNT(*) FROM table_sessions) AS with_table,
                (SELECT COUNT(*) FROM csv_sessions) AS with_csv_export,
                (SELECT COUNT(*) FROM chart_sessions) AS with_chart
        """
    else:
        # Migration 208 is absent or still backfilling. Preserve the denominator
        # for diagnostics, but mark the counts unavailable so a missing v2 key
        # cannot masquerade as a genuine 0% CSV/chart rate.
        sql = f"""
            SELECT
                COUNT(*) AS sessions,
                0 AS with_table,
                0 AS with_csv_export,
                0 AS with_chart
            FROM {COMPASS_SCHEMA}.chat_sessions s
            {where_session}
        """
    rows = run_sql(sql, tuple(binds))
    row = rows[0] if rows else {}
    sessions = int(row.get("sessions") or 0)
    with_table = int(row.get("with_table") or 0)
    with_csv_export = int(row.get("with_csv_export") or 0)
    with_chart = int(row.get("with_chart") or 0)
    table_pct = round(with_table / sessions * 100) if sessions else 0
    return ArtifactCoverage(
        sessions=sessions,
        with_table=with_table,
        table_pct=table_pct,
        with_csv_export=with_csv_export,
        csv_export_pct=round(with_csv_export / sessions * 100) if sessions else 0,
        with_chart=with_chart,
        chart_pct=round(with_chart / sessions * 100) if sessions else 0,
        available=artifacts_available,
    )


# ── Returning users (WS-5 / G5) ─────────────────────────────────────


@dataclass(frozen=True)
class ReturningUsers:
    """Repeat-user counts derived from the pseudonymous ``visitor_id`` (WS-5).

    ``returning`` = visitor_ids seen in MORE THAN ONE distinct session in the
    window. ``identified`` = distinct visitor_ids in the window (the honest
    denominator). Both ignore the anonymous NULL tail (pre-launch sessions and
    any turn that arrived without an id), so the metric only reflects traffic
    since the visitor_id column went live — it begins at launch and grows.
    """

    returning: int
    identified: int


def get_returning_users(
    since: datetime | None = None, until: datetime | None = None
) -> ReturningUsers:
    """Count returning vs. identified visitors over a ``[since, until)`` window.

    A "returning" visitor has >1 distinct session. The query rides the partial
    index on ``visitor_id`` (migration 161) and never counts NULL/anonymous rows.
    """
    since = _normalize_since(since)
    until = _normalize_since(until)
    # Normalize BEFORE keying so the cache key is stable across requests with
    # equivalent bounds (the all-time read keys to "None~None"). TTL bounds
    # staleness to 60s, matching the snapshot cache.
    key = f"{since}~{until}"
    now = monotonic()
    cached = _RETURNING_USERS_CACHE.get(key)
    if cached and now - cached[0] < _RETURNING_USERS_TTL_SECONDS:
        return cached[1]

    where, binds = _scope_clause(since, until)
    not_null = "s.visitor_id IS NOT NULL"
    where_sql = f"{where} AND {not_null}" if where else f"WHERE {not_null}"
    sql = f"""
        WITH per_visitor AS (
            SELECT s.visitor_id, COUNT(DISTINCT s.session_id) AS sessions
            FROM {COMPASS_SCHEMA}.chat_sessions s
            {where_sql}
            GROUP BY s.visitor_id
        )
        SELECT
            COUNT(*) AS identified,
            COUNT(*) FILTER (WHERE sessions > 1) AS returning
        FROM per_visitor
    """
    rows = run_sql(sql, tuple(binds))
    row = rows[0] if rows else {}
    result = ReturningUsers(
        returning=int(row.get("returning") or 0),
        identified=int(row.get("identified") or 0),
    )
    _RETURNING_USERS_CACHE[key] = (now, result)
    return result


# ── Activity over time ──────────────────────────────────────────────
#
# /compass/overview shows every metric as a single point-in-time number;
# the one genuinely-missing data-backed capability is a sessions/questions
# OVER-TIME chart scoped to the same range. ``get_sessions_over_time``
# returns one zero-filled bucket per day (or per week for long windows) so a
# traffic gap (e.g. the 2026-06-18 deploy day with 3 sessions) is shown
# honestly as a low bar, never silently omitted.

# Daily buckets up to this window length; weekly beyond so "All time" doesn't
# render hundreds of bar rows.
_TREND_DAILY_MAX_DAYS = 31

# Even bucketed weekly, "All time" can emit 60+ bars back to the first session.
# Cap the weekly series to the most-recent N weeks (the SQL takes the newest N,
# Python reverses them back to oldest→newest). Daily windows are unaffected.
_TREND_WEEKLY_MAX_BARS = 12


@dataclass(frozen=True)
class TrendPoint:
    day: date
    sessions: int
    questions: int


@dataclass(frozen=True)
class SessionsOverTime:
    """A trend series plus the granularity it was actually bucketed at.

    ``granularity`` is "day" or "week" — the real value the query used, so the
    Overview labels ("per day" / "per week", "Week of …") never have to *infer*
    it from point spacing (which mislabels a single-bucket weekly window).
    """

    points: list[TrendPoint]
    granularity: str  # "day" or "week"


def get_sessions_over_time(
    since: datetime | None = None, until: datetime | None = None
) -> SessionsOverTime:
    """Sessions and user questions per time bucket within [since, until).

    - ``since`` is the lower bound (None for "all time" → resolved in-SQL via
      MIN(created_at), which ``generate_series`` needs as a concrete start).
    - ``until`` is the window END; None means "up to now". The route only ever
      passes ``since`` today (lower-bound-only MVP), but the ``until`` arg keeps
      a custom from..to span a one-line change later.
    - Bucket width is decided here in Python from the resolved span: daily for
      windows ≤ ~31 days, weekly beyond (so "All time" stays bounded). The
      decision drives ``date_trunc('day'|'week', …)`` in the query and is
      returned as ``SessionsOverTime.granularity`` so the labels stay honest.
    - The effective start is floored to ``MAX(since, MIN(created_at))`` in SQL,
      so a far-past custom ``from`` (e.g. year 2000) can't spawn ~100k empty
      weekly buckets — the series is bounded by the actual data span.
    - Days/weeks with zero sessions are zero-filled via a generate_series
      LEFT JOIN so a gap shows as a zero bar, not a missing row.

    Sessions are COUNT(*) over chat_sessions in the bucket; questions are
    role='user' chat_messages joined to those sessions. Both are attributed to
    the *session's* start day (s.created_at), matching engagement semantics,
    not the individual message timestamp. ``created_at`` is a naive UTC
    timestamp, so the SQL converts it to Eastern local time before assigning the
    daily or weekly bucket. That matches the range control and the ET timestamps
    shown to reviewers.
    """
    since = _normalize_since(since)
    until = _normalize_since(until)

    # Decide granularity from the resolved span. For "all time" (since is None)
    # the span is the whole history → always weekly.
    if since is None:
        bucket = "week"
    else:
        # ``since`` is naive UTC (via _normalize_since); keep the span math naive.
        end = until or datetime.now(UTC).replace(tzinfo=None)
        span_days = (end - since).days
        bucket = "day" if span_days <= _TREND_DAILY_MAX_DAYS else "week"
    step = "1 day" if bucket == "day" else "1 week"

    # Effective start floored to the first session: GREATEST(since, MIN) keeps a
    # far-past custom ``from`` from generating tens of thousands of empty
    # buckets (bug: unbounded generate_series). For "all time" (since is None)
    # the effective start is just MIN(created_at) — unchanged behavior.
    min_created = f"(SELECT MIN(created_at) FROM {COMPASS_SCHEMA}.chat_sessions)"
    if since is not None:
        eff_start = f"GREATEST(%(since)s::timestamp, {min_created})"
    else:
        eff_start = min_created
    end_expr = "%(until)s::timestamp" if until is not None else "now() at time zone 'utc'"
    eastern_start = (
        f"({eff_start} AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')"
    )
    eastern_end = (
        f"({end_expr} AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')"
    )
    eastern_created_at = (
        "(s.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')"
    )

    # Weekly windows ("All time"/long spans) keep only the most-recent N buckets so
    # the trend never balloons into 60+ bar divs; take the newest N in SQL, then
    # reverse to oldest→newest in Python. Daily windows return every bucket as-is.
    order_limit = (
        f"ORDER BY b.bucket DESC LIMIT {_TREND_WEEKLY_MAX_BARS}"
        if bucket == "week"
        else "ORDER BY b.bucket"
    )

    # ``raw_start_ts`` / ``raw_end_ts`` are the *exact* window edges used to
    # filter rows, so the first bucket counts only sessions on/after ``since``
    # (truncating to a week boundary would have pulled in pre-window rows).
    # ``start_ts`` (truncated) only aligns generate_series onto bucket edges.
    sql = f"""
        WITH bounds AS (
            SELECT
                {eff_start} AS raw_start_ts,
                {end_expr} AS raw_end_ts,
                date_trunc('{bucket}', {eastern_start}) AS start_ts,
                date_trunc('{bucket}', {eastern_end}) + interval '{step}' AS end_ts
        ),
        buckets AS (
            SELECT generate_series(
                (SELECT start_ts FROM bounds),
                (SELECT end_ts FROM bounds) - interval '{step}',
                interval '{step}'
            )::date AS bucket
        ),
        sess AS (
            SELECT date_trunc('{bucket}', {eastern_created_at})::date AS bucket,
                   COUNT(*) AS sessions
            FROM {COMPASS_SCHEMA}.chat_sessions s, bounds
            WHERE s.created_at >= bounds.raw_start_ts AND s.created_at < bounds.raw_end_ts
            GROUP BY 1
        ),
        ques AS (
            SELECT date_trunc('{bucket}', {eastern_created_at})::date AS bucket,
                   COUNT(*) AS questions
            FROM {COMPASS_SCHEMA}.chat_messages m
            JOIN {COMPASS_SCHEMA}.chat_sessions s ON s.session_id = m.session_id, bounds
            WHERE m.role = 'user'
              AND s.created_at >= bounds.raw_start_ts AND s.created_at < bounds.raw_end_ts
            GROUP BY 1
        )
        SELECT
            b.bucket AS day,
            COALESCE(sess.sessions, 0) AS sessions,
            COALESCE(ques.questions, 0) AS questions
        FROM buckets b
        LEFT JOIN sess ON sess.bucket = b.bucket
        LEFT JOIN ques ON ques.bucket = b.bucket
        {order_limit}
    """
    bind: dict = {}
    if since is not None:
        bind["since"] = since
    if until is not None:
        bind["until"] = until

    rows = run_sql(sql, bind)
    if bucket == "week":
        # SQL returned the newest N weeks (DESC LIMIT); flip back to chronological.
        rows = list(reversed(rows))
    points = [
        TrendPoint(
            day=row["day"],
            sessions=int(row.get("sessions") or 0),
            questions=int(row.get("questions") or 0),
        )
        for row in rows
    ]
    return SessionsOverTime(points=points, granularity=bucket)


# ── Feedback ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FeedbackStats:
    # W1-2: the denominator is now SESSIONS (conversations), not assistant
    # messages. ``sessions`` is every scoped conversation; thumbs_up/down/unrated
    # are mutually-exclusive *session* counts (a session with any 👎 counts as
    # down, any 👍 with no 👎 as up, otherwise unrated). Percentages are of
    # ``sessions``. This makes "% rated" a share of conversations, the unit a
    # funder cares about, and matches the Sessions tile's denominator.
    sessions: int
    thumbs_up: int
    thumbs_down: int
    unrated: int
    thumbs_up_pct: int
    thumbs_down_pct: int
    unrated_pct: int


def get_feedback_stats(
    since: datetime | None = None, until: datetime | None = None
) -> FeedbackStats:
    since = _normalize_since(since)
    until = _normalize_since(until)

    # Check in-process cache before hitting Postgres.
    key = _cache_key(since, until)
    now = monotonic()
    cached = _FEEDBACK_CACHE.get(key)
    if cached and now - cached[0] < _FEEDBACK_TTL_SECONDS:
        return cached[1]

    where_session, binds = _scope_clause(since, until)

    # One row per session that received any rating, with its up/down tallies, so
    # we can classify each session once and count sessions (not messages).
    counts_sql = f"""
        WITH scoped AS (
            SELECT s.session_id
            FROM {COMPASS_SCHEMA}.chat_sessions s
            {where_session}
        ),
        session_ratings AS (
            SELECT a.session_id,
                   COUNT(*) FILTER (WHERE f.rating = 1) AS ups,
                   COUNT(*) FILTER (WHERE f.rating = -1) AS downs
            FROM {COMPASS_SCHEMA}.chat_messages a
            JOIN scoped ON scoped.session_id = a.session_id
            JOIN {COMPASS_SCHEMA}.message_feedback f ON f.message_id = a.id
            WHERE a.role = 'assistant'
            GROUP BY a.session_id
        )
        SELECT
            (SELECT COUNT(*) FROM scoped) AS sessions,
            COUNT(*) FILTER (WHERE downs > 0) AS thumbs_down,
            COUNT(*) FILTER (WHERE ups > 0 AND downs = 0) AS thumbs_up
        FROM session_ratings
    """
    rows = run_sql(counts_sql, tuple(binds))
    row = rows[0] if rows else {}
    sessions = int(row.get("sessions") or 0)
    thumbs_up = int(row.get("thumbs_up") or 0)
    thumbs_down = int(row.get("thumbs_down") or 0)
    unrated = max(sessions - thumbs_up - thumbs_down, 0)

    if sessions:
        up_pct = round(thumbs_up / sessions * 100)
        down_pct = round(thumbs_down / sessions * 100)
        unrated_pct = max(100 - up_pct - down_pct, 0)
    else:
        up_pct = down_pct = unrated_pct = 0

    result = FeedbackStats(
        sessions=sessions,
        thumbs_up=thumbs_up,
        thumbs_down=thumbs_down,
        unrated=unrated,
        thumbs_up_pct=up_pct,
        thumbs_down_pct=down_pct,
        unrated_pct=unrated_pct,
    )
    _FEEDBACK_CACHE[key] = (now, result)
    return result


# ── What people ask ─────────────────────────────────────────────────
#
# /compass/overview asks three questions of the same data — policy areas,
# districts, and states — all sourced from every saved fresh snapshot in each
# conversation. ``snapshot_aggregates`` runs their shared SQL query once.


@dataclass(frozen=True)
class SnapshotAggregates:
    topics: Counter
    districts: Counter
    district_meta: dict
    states: Counter


def _query_snapshot_aggregates(
    since: datetime | None, until: datetime | None
) -> SnapshotAggregates:
    """Top policy areas, districts, and states from all saved turns in SQL.

    A metric reference is joined to ``policy_questions`` by metric ID, so the
    Overview reports NCTQ policy areas rather than free-form metric names. Every
    entity is deduplicated once per conversation before ranking. The query reads
    the compact ``snapshot_summary`` column, never the full saved result blob.
    """
    if not snapshot_summary_column_available():
        # Migration 163 unapplied: nothing to aggregate from a column that does
        # not exist. Return empty aggregates so "What people ask" renders empty
        # instead of 500-ing.
        return SnapshotAggregates(
            topics=Counter(),
            districts=Counter(),
            district_meta={},
            states=Counter(),
        )
    # Retain the fresh-snapshot predicate so the dashboard partial index still
    # serves all-time reads. ``snapshot_summary`` alone covers many unrelated
    # assistant messages and forces a costly table scan.
    where = [
        "m.role = 'assistant'",
        f"m.message_data ? '{SNAPSHOT_KEY}'",
        "m.snapshot_summary IS NOT NULL",
    ]
    binds: dict = {"lim": _SNAPSHOT_AGG_LIMIT}
    if since is not None:
        where.append("s.created_at >= %(since)s")
        binds["since"] = since
    if until is not None:
        where.append("s.created_at < %(until)s")
        binds["until"] = until
    where_sql = " AND ".join(where)

    sql = f"""
        WITH turns AS (
            SELECT s.session_id,
                   m.snapshot_summary->'memory'->'latest_query_context' AS lqc
            FROM {COMPASS_SCHEMA}.chat_sessions s
            JOIN {COMPASS_SCHEMA}.chat_messages m ON m.session_id = s.session_id
            WHERE {where_sql}
        ),
        sess_topic AS (
            SELECT DISTINCT turns.session_id, pq.topic_name AS label
            FROM turns
            CROSS JOIN LATERAL jsonb_array_elements(
                COALESCE(turns.lqc->'result_metrics', '[]'::jsonb)
            ) AS met
            JOIN {COMPASS_SCHEMA}.policy_questions pq
              ON pq.metric_id::text = met->>'metric_id'
            WHERE pq.topic_name IS NOT NULL AND pq.topic_name <> ''
        ),
        district_refs AS (
            SELECT turns.session_id,
                   (d->>'district_id')::int AS did,
                   d->>'district_name' AS dname,
                   d->>'state' AS dstate
            FROM turns,
                 jsonb_array_elements(COALESCE(lqc->'result_districts', '[]'::jsonb)) AS d
            WHERE d->>'district_id' ~ '^-?[0-9]+$'
              AND d->>'district_name' IS NOT NULL
        ),
        sess_district AS (
            SELECT DISTINCT session_id, did
            FROM district_refs
        ),
        district_meta AS (
            SELECT did, max(dname) AS dname, max(dstate) AS dstate
            FROM district_refs
            GROUP BY did
        ),
        sess_state AS (
            SELECT DISTINCT session_id, dstate
            FROM district_refs
            WHERE dstate IS NOT NULL AND dstate <> ''
        )
        (SELECT 'topic' AS kind, label, NULL::int AS did,
                NULL::text AS name, NULL::text AS state, count(*) AS c
           FROM sess_topic GROUP BY label ORDER BY c DESC, label LIMIT %(lim)s)
        UNION ALL
        (SELECT 'district' AS kind, NULL AS label, sd.did,
                dm.dname AS name, dm.dstate AS state, count(*) AS c
           FROM sess_district sd
           JOIN district_meta dm ON dm.did = sd.did
           GROUP BY sd.did, dm.dname, dm.dstate ORDER BY c DESC, dm.dname LIMIT %(lim)s)
        UNION ALL
        (SELECT 'state' AS kind, dstate AS label, NULL::int AS did,
                NULL::text AS name, NULL::text AS state, count(*) AS c
           FROM sess_state
           GROUP BY dstate ORDER BY c DESC LIMIT %(lim)s)
    """
    topics: Counter[str] = Counter()
    districts: Counter[int] = Counter()
    district_meta: dict[int, dict[str, object]] = {}
    states: Counter[str] = Counter()
    for row in run_sql(sql, binds):
        kind = row.get("kind")
        count = int(row.get("c") or 0)
        if kind == "topic" and row.get("label"):
            topics[row["label"]] = count
        elif kind == "district" and row.get("did") is not None:
            did = int(row["did"])
            districts[did] = count
            district_meta[did] = {
                "district_id": did,
                "district_name": row.get("name"),
                "state": row.get("state"),
            }
        elif kind == "state" and row.get("label"):
            states[row["label"]] = count
    return SnapshotAggregates(
        topics=topics,
        districts=districts,
        district_meta=district_meta,
        states=states,
    )


def snapshot_aggregates(
    since: datetime | None = None, until: datetime | None = None
) -> SnapshotAggregates:
    since = _normalize_since(since)
    until = _normalize_since(until)
    key = _cache_key(since, until)
    now = monotonic()
    cached = _SNAPSHOT_AGGREGATES_CACHE.get(key)
    if cached and now - cached[0] < _SNAPSHOT_AGGREGATES_TTL_SECONDS:
        return cached[1]

    aggregates = _query_snapshot_aggregates(since, until)
    _SNAPSHOT_AGGREGATES_CACHE[key] = (now, aggregates)
    return aggregates


def get_top_topics(limit: int = 3, since: datetime | None = None) -> list[dict]:
    return [
        {"topic": topic, "session_count": count}
        for topic, count in snapshot_aggregates(since).topics.most_common(limit)
    ]


def get_top_districts(limit: int = 3, since: datetime | None = None) -> list[dict]:
    aggregates = snapshot_aggregates(since)
    return [
        {**aggregates.district_meta[district_id], "session_count": count}
        for district_id, count in aggregates.districts.most_common(limit)
    ]


def get_top_states(limit: int = 3, since: datetime | None = None) -> list[dict]:
    return [
        {"state": state, "session_count": count}
        for state, count in snapshot_aggregates(since).states.most_common(limit)
    ]


# ── Operations (Starling-only /compass/operations) ──────────────────


@dataclass(frozen=True)
class CostStats:
    total_cost: float
    cost_per_session: float
    cost_per_turn: float
    avg_tokens_per_session: int
    avg_tokens_per_turn: int
    avg_rounds_per_session: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    requests: int
    sessions: int
    turns: int
    cache_hit_rate: float
    cache_savings: float
    unpriced_rows: int
    unpriced_tokens: int
    unpriced_token_share: float


@dataclass(frozen=True)
class ApiKeyUsageSummary:
    key_id: str
    name: str
    owner_email: str
    last_used_at: datetime | None
    request_count: int


def _api_key_scope(
    api_key_id: str | None,
    *,
    alias: str = "u",
) -> tuple[str, tuple]:
    if not api_key_id:
        return "", ()
    return f"AND {alias}.api_key_id = %s", (api_key_id,)


def get_cost_stats(
    since: datetime | None = None,
    *,
    api_key_id: str | None = None,
) -> CostStats:
    """Cost rollup over current chat-sourced llm_usage rows."""
    since = _normalize_since(since)
    usage_since = "AND u.created_at >= %s" if since else ""
    api_key_clause, api_key_bind = _api_key_scope(api_key_id)
    bind: tuple = ((since,) if since else ()) + api_key_bind
    rows = run_sql(
        f"""
        WITH scoped_usage AS (
            SELECT *
            FROM {COMPASS_SCHEMA}.llm_usage u
            WHERE u.source = 'chat'
            {usage_since}
            {api_key_clause}
        )
        SELECT
            COALESCE(SUM(cost_usd) FILTER (WHERE pricing_status = 'priced'), 0) AS total_cost,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
            COALESCE(SUM(requests), 0) AS requests,
            COUNT(DISTINCT session_id) AS sessions,
            COUNT(DISTINCT message_id) FILTER (WHERE message_id IS NOT NULL) AS turns,
            COALESCE(
                SUM(
                    CASE
                        WHEN pricing_meta ? 'cache_savings_usd'
                        THEN (pricing_meta->>'cache_savings_usd')::numeric
                        ELSE 0
                    END
                ),
                0
            ) AS cache_savings,
            COUNT(*) FILTER (WHERE pricing_status IS DISTINCT FROM 'priced') AS unpriced_rows,
            COALESCE(
                SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens)
                    FILTER (WHERE pricing_status IS DISTINCT FROM 'priced'),
                0
            ) AS unpriced_tokens
        FROM scoped_usage
        """,
        bind,
    )
    row = rows[0] if rows else {}
    total_cost = float(row.get("total_cost") or 0)
    sessions = int(row.get("sessions") or 0)
    turns = int(row.get("turns") or 0)
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    cache_read_tokens = int(row.get("cache_read_tokens") or 0)
    cache_write_tokens = int(row.get("cache_write_tokens") or 0)
    total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
    cache_denominator = cache_read_tokens + input_tokens + cache_write_tokens
    cost_per_session = round(total_cost / sessions, 3) if sessions else 0.0
    cost_per_turn = round(total_cost / turns, 3) if turns else 0.0
    avg_tokens_per_session = round(total_tokens / sessions) if sessions else 0
    avg_tokens_per_turn = round(total_tokens / turns) if turns else 0
    avg_rounds_per_session = round(turns / sessions, 1) if sessions else 0.0
    cache_hit_rate = (
        round(cache_read_tokens / cache_denominator * 100, 1)
        if cache_denominator
        else 0.0
    )
    unpriced_tokens = int(row.get("unpriced_tokens") or 0)
    unpriced_token_share = (
        round(unpriced_tokens / total_tokens * 100, 1) if total_tokens else 0.0
    )

    return CostStats(
        total_cost=total_cost,
        cost_per_session=cost_per_session,
        cost_per_turn=cost_per_turn,
        avg_tokens_per_session=avg_tokens_per_session,
        avg_tokens_per_turn=avg_tokens_per_turn,
        avg_rounds_per_session=avg_rounds_per_session,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        requests=int(row.get("requests") or 0),
        sessions=sessions,
        turns=turns,
        cache_hit_rate=cache_hit_rate,
        cache_savings=float(row.get("cache_savings") or 0),
        unpriced_rows=int(row.get("unpriced_rows") or 0),
        unpriced_tokens=unpriced_tokens,
        unpriced_token_share=unpriced_token_share,
    )


def get_cost_by_phase(
    since: datetime | None = None,
    *,
    api_key_id: str | None = None,
) -> list[dict]:
    since = _normalize_since(since)
    api_key_clause, api_key_bind = _api_key_scope(api_key_id)
    where = (
        "WHERE u.source = 'chat'"
        + (" AND u.created_at >= %s" if since else "")
        + f" {api_key_clause}"
    )
    sql = f"""
        SELECT
            u.phase,
            SUM(u.cost_usd) FILTER (WHERE pricing_status = 'priced') AS cost,
            SUM(u.input_tokens) AS input_tokens,
            SUM(u.output_tokens) AS output_tokens,
            SUM(u.cache_read_tokens) AS cache_read_tokens,
            SUM(u.cache_write_tokens) AS cache_write_tokens,
            SUM(u.requests) AS requests
        FROM {COMPASS_SCHEMA}.llm_usage u
        {where}
        GROUP BY u.phase
        ORDER BY cost DESC NULLS LAST
    """
    bind: tuple = ((since,) if since else ()) + api_key_bind
    return run_sql(sql, bind)


def get_cost_by_model(
    since: datetime | None = None,
    *,
    api_key_id: str | None = None,
) -> list[dict]:
    since = _normalize_since(since)
    api_key_clause, api_key_bind = _api_key_scope(api_key_id)
    where = (
        "WHERE u.source = 'chat'"
        + (" AND u.created_at >= %s" if since else "")
        + f" {api_key_clause}"
    )
    sql = f"""
        SELECT
            COALESCE(u.model_actual, u.model, 'unknown') AS model,
            SUM(u.cost_usd) FILTER (WHERE pricing_status = 'priced') AS cost,
            SUM(u.input_tokens) AS input_tokens,
            SUM(u.output_tokens) AS output_tokens,
            SUM(u.cache_read_tokens) AS cache_read_tokens,
            SUM(u.cache_write_tokens) AS cache_write_tokens,
            SUM(u.requests) AS requests
        FROM {COMPASS_SCHEMA}.llm_usage u
        {where}
        GROUP BY COALESCE(u.model_actual, u.model, 'unknown')
        ORDER BY cost DESC NULLS LAST
    """
    bind: tuple = ((since,) if since else ()) + api_key_bind
    return run_sql(sql, bind)


def get_cost_trend(
    since: datetime | None = None,
    *,
    api_key_id: str | None = None,
) -> list[dict]:
    since = _normalize_since(since)
    api_key_clause, api_key_bind = _api_key_scope(api_key_id)
    where = (
        "WHERE u.source = 'chat'"
        + (" AND u.created_at >= %s" if since else "")
        + f" {api_key_clause}"
    )
    sql = f"""
        SELECT
            date_trunc('day', u.created_at)::date AS day,
            SUM(u.cost_usd) FILTER (WHERE pricing_status = 'priced') AS cost,
            SUM(u.input_tokens + u.output_tokens + u.cache_read_tokens + u.cache_write_tokens) AS tokens
        FROM {COMPASS_SCHEMA}.llm_usage u
        {where}
        GROUP BY day
        ORDER BY day
    """
    bind: tuple = ((since,) if since else ()) + api_key_bind
    return [
        {
            "label": f"{row['day']:%b} {row['day'].day}" if row.get("day") else "",
            "cost": float(row.get("cost") or 0),
            "tokens": int(row.get("tokens") or 0),
        }
        for row in run_sql(sql, bind)
    ]


def get_observed_model_config(*, api_key_id: str | None = None) -> list[dict]:
    api_key_clause, api_key_bind = _api_key_scope(api_key_id)
    sql = f"""
        SELECT DISTINCT ON (u.phase)
            u.phase,
            u.configured_model,
            u.model_actual,
            u.provider_id,
            u.created_at
        FROM {COMPASS_SCHEMA}.llm_usage u
        WHERE u.source = 'chat'
        {api_key_clause}
        ORDER BY u.phase, u.created_at DESC
    """
    return run_sql(sql, api_key_bind)


def get_recent_api_keys(
    limit: int = 5,
    *,
    key_id: str | None = None,
) -> list[ApiKeyUsageSummary]:
    where = "WHERE revoked_at IS NULL"
    bind: tuple = (limit,)
    if key_id:
        where += " AND key_id = %s"
        bind = (key_id, limit)
    rows = run_sql(
        f"""
        SELECT
            key_id,
            name,
            owner_email,
            last_used_at,
            COALESCE(request_count, 0) AS request_count
        FROM {COMPASS_SCHEMA}.api_keys
        {where}
        ORDER BY last_used_at DESC NULLS LAST, created_at DESC
        LIMIT %s
        """,
        bind,
    )
    return [
        ApiKeyUsageSummary(
            key_id=str(row.get("key_id") or ""),
            name=str(row.get("name") or ""),
            owner_email=str(row.get("owner_email") or ""),
            last_used_at=row.get("last_used_at"),
            request_count=int(row.get("request_count") or 0),
        )
        for row in rows
    ]


@dataclass(frozen=True)
class LatencyStats:
    avg_total_ms: int
    avg_generator_ms: int
    avg_critic_ms: int


def get_latency_stats(since: datetime | None = None) -> LatencyStats:
    return LatencyStats(
        avg_total_ms=0,
        avg_generator_ms=0,
        avg_critic_ms=0,
    )
