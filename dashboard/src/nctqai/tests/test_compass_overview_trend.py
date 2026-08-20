"""Tests for the Overview 'Activity over time' trend (U3 / TREND-R1).

The bucketing, zero-fill, and per-bucket counts live in SQL, so the pure-Python
unit tests below can only verify what's decided in Python (granularity branch,
row→TrendPoint mapping, the renderer, the empty state, and the safe() default).
The generate_series zero-fill and the actual day counts need the staging DB —
that is the live_db test at the bottom, gated by --run-live-db.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fasthtml.common import to_xml

from nctqai.components.compass.bars import layered_trend_chart, trend_chart
from nctqai.components.compass.range_pills import parse_custom_since
from nctqai.models.auth import Role
from nctqai.routes.compass import overview as overview_route
from nctqai.services import compass_stats
from nctqai.services.compass_stats import (
    SessionsOverTime,
    TrendPoint,
    get_sessions_over_time,
)


# ── Granularity branch (captured SQL, no DB) ────────────────────────


def _capture_sql(monkeypatch) -> list[str]:
    captured: list[str] = []

    def fake_run_sql(sql: str, _bind: Any = ()) -> list[dict[str, Any]]:
        captured.append(sql)
        return []

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)
    return captured


def test_short_window_uses_daily_buckets(monkeypatch) -> None:
    captured = _capture_sql(monkeypatch)
    since = datetime.now(UTC) - timedelta(days=7)
    get_sessions_over_time(since)
    assert captured, "expected a query to run"
    assert "date_trunc('day'" in captured[0]
    assert "date_trunc('week'" not in captured[0]
    assert "AT TIME ZONE 'America/New_York'" in captured[0]


def test_thirty_day_window_stays_daily(monkeypatch) -> None:
    captured = _capture_sql(monkeypatch)
    since = datetime.now(UTC) - timedelta(days=30)
    get_sessions_over_time(since)
    assert "date_trunc('day'" in captured[0]


def test_long_window_uses_weekly_buckets(monkeypatch) -> None:
    captured = _capture_sql(monkeypatch)
    since = datetime.now(UTC) - timedelta(days=90)
    get_sessions_over_time(since)
    assert "date_trunc('week'" in captured[0]


def test_all_time_since_none_uses_weekly_and_min_start(monkeypatch) -> None:
    captured = _capture_sql(monkeypatch)
    get_sessions_over_time(None)
    sql = captured[0]
    # range='all' → since is None → weekly buckets resolved from MIN(created_at)
    # (generate_series needs a concrete lower bound).
    assert "date_trunc('week'" in sql
    assert "MIN(created_at)" in sql


def test_custom_until_bounds_the_window(monkeypatch) -> None:
    """A custom from..to span (U1-forward-compat) binds `until` as the window
    end instead of now(); assert the end predicate uses the bound param."""
    captured = _capture_sql(monkeypatch)
    since = datetime(2026, 5, 1)
    until = datetime(2026, 5, 31)
    get_sessions_over_time(since, until)
    sql = captured[0]
    assert "%(until)s" in sql
    assert "now()" not in sql


# ── #6: row filter uses the RAW since, not the truncated week boundary ───


def test_row_filter_uses_raw_window_not_truncated_boundary(monkeypatch) -> None:
    """The sess/ques CTEs must filter on the raw window edges (raw_start_ts /
    raw_end_ts), never the truncated ``start_ts`` — truncating to a week
    boundary would pull in pre-window rows (bug: counts rows before 'since')."""
    captured = _capture_sql(monkeypatch)
    get_sessions_over_time(datetime.now(UTC) - timedelta(days=90))  # weekly
    sql = captured[0]
    # Both data CTEs filter on the raw edges.
    assert sql.count("s.created_at >= bounds.raw_start_ts") == 2
    assert sql.count("s.created_at < bounds.raw_end_ts") == 2
    # And never on the truncated bucket-aligned start (that is generate_series-only).
    assert "s.created_at >= bounds.start_ts" not in sql


# ── #9: effective start floored to MIN(created_at) bounds the series ─────


def test_effective_start_floored_to_min_created_at(monkeypatch) -> None:
    """A far-past custom ``from`` must not spawn ~100k empty weekly buckets:
    the effective start is GREATEST(since, MIN(created_at)), so the series is
    bounded by the actual data span."""
    captured = _capture_sql(monkeypatch)
    get_sessions_over_time(datetime(2000, 1, 1))
    sql = captured[0]
    assert "GREATEST(%(since)s::timestamp" in sql
    assert "MIN(created_at)" in sql


def test_all_time_effective_start_is_min_created_at_unfloored(monkeypatch) -> None:
    """For range='all' (since None) the effective start stays MIN(created_at)
    with no GREATEST wrapper — unchanged all-time behavior."""
    captured = _capture_sql(monkeypatch)
    get_sessions_over_time(None)
    sql = captured[0]
    assert "MIN(created_at)" in sql
    assert "GREATEST" not in sql


# ── #9b: parse_custom_since rejects absurd / out-of-range dates ──────────


def test_parse_custom_since_rejects_pre_compass_date() -> None:
    """A pre-Compass date (before the floor) returns None → caller falls back to
    a preset instead of charting an empty multi-year span."""
    assert parse_custom_since("2000-01-01") is None


def test_parse_custom_since_rejects_future_date() -> None:
    """A future lower bound matches no rows; reject it → fallback."""
    future = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    assert parse_custom_since(future) is None


def test_parse_custom_since_accepts_in_range_date() -> None:
    """An Eastern calendar date parses to its corresponding UTC instant."""
    parsed = parse_custom_since("2026-05-01")
    assert parsed == datetime(2026, 5, 1, 4, tzinfo=UTC)


# ── Row → TrendPoint mapping (no DB) ────────────────────────────────


def test_rows_map_to_trend_points_including_zero_day(monkeypatch) -> None:
    """The Python mapping must keep a zero-value bucket (the SQL zero-fills it;
    the mapper must not drop it)."""
    rows = [
        {"day": date(2026, 6, 17), "sessions": 168, "questions": 279},
        {"day": date(2026, 6, 18), "sessions": 0, "questions": 0},  # deploy gap
        {"day": date(2026, 6, 19), "sessions": 15, "questions": 18},
    ]
    monkeypatch.setattr(compass_stats, "run_sql", lambda *a, **k: rows)
    result = get_sessions_over_time(datetime.now(UTC) - timedelta(days=3))
    assert result.granularity == "day"
    assert result.points == [
        TrendPoint(date(2026, 6, 17), 168, 279),
        TrendPoint(date(2026, 6, 18), 0, 0),
        TrendPoint(date(2026, 6, 19), 15, 18),
    ]


def test_rows_with_none_counts_coerce_to_zero(monkeypatch) -> None:
    rows = [{"day": date(2026, 6, 18), "sessions": None, "questions": None}]
    monkeypatch.setattr(compass_stats, "run_sql", lambda *a, **k: rows)
    result = get_sessions_over_time(datetime.now(UTC) - timedelta(days=1))
    assert result.points[0].sessions == 0 and result.points[0].questions == 0


# ── Renderer (no DB) ────────────────────────────────────────────────


def test_trend_chart_renders_one_bar_row_per_bucket_with_commas() -> None:
    items = [
        {"label": "Jun 15", "value": 1241},
        {"label": "Jun 18", "value": 0},
    ]
    html = to_xml(trend_chart(items))
    assert html.count("bar-row") == 2
    assert "1,241" in html  # comma-formatted via bar_row
    assert "Jun 15" in html and "Jun 18" in html


def test_trend_chart_empty_input_is_empty_chart() -> None:
    html = to_xml(trend_chart([]))
    assert "bar-chart" in html
    assert "bar-row" not in html


# ── Section builder: empty state + teal scope + a11y (no DB) ─────────


def test_trend_section_empty_shows_no_activity_panel() -> None:
    html = to_xml(overview_route._trend_section(SessionsOverTime([], "day")))
    assert "No activity in this range yet." in html


def test_trend_section_with_points_renders_one_layered_chart() -> None:
    series = SessionsOverTime([TrendPoint(date(2026, 6, 20), 365, 596)], "day")
    html = to_xml(overview_route._trend_section(series))
    # One layered chart (not two side-by-side panels): both fills present, a
    # legend naming Sessions + Questions, the two counts, and the avg q/session.
    assert "bar-fill-sessions" in html and "bar-fill-questions" in html
    assert "trend-legend" in html
    assert "Sessions" in html and "Questions" in html
    assert "365" in html and "596" in html
    assert "1.6" in html  # 596 / 365 = 1.6 avg questions per session
    # Teal scope intact, no brand-blue leak into the new markup.
    assert "compass-trend" in html
    assert "brand-blue" not in html


def test_layered_trend_chart_overlays_sessions_on_questions_with_avg() -> None:
    items = [
        {"label": "Jun 20", "sessions": 365, "questions": 596, "avg": "1.6"},
        {"label": "Jun 26", "sessions": 0, "questions": 0, "avg": None},
    ]
    html = to_xml(layered_trend_chart(items))
    assert html.count("bar-row") == 2
    # The darker sessions fill is later in the DOM than the lighter questions
    # fill, so it paints on top of the questions bar's left portion.
    assert html.index("bar-fill-questions") < html.index("bar-fill-sessions")
    assert "365" in html and "596" in html and "1.6" in html
    # A bucket with no sessions shows an em dash, never a divide-by-zero or 0.0.
    assert "—" in html


def test_trend_section_weekly_buckets_say_week_not_day() -> None:
    """Long windows render weekly buckets; the subtitle and bar labels must say
    'per week' / 'Week of …', never imply daily granularity (honesty mandate)."""
    series = SessionsOverTime(
        [
            TrendPoint(date(2026, 5, 4), 8743, 9000),
            TrendPoint(date(2026, 5, 11), 2241, 2500),
        ],
        "week",
    )
    html = to_xml(overview_route._trend_section(series))
    assert "per week" in html
    assert "Week of May 4" in html
    assert "per day" not in html


def test_trend_section_single_weekly_bucket_still_says_week() -> None:
    """A weekly window that lands a SINGLE bucket must still say 'per week'.

    The old ``_is_weekly(points)`` inferred granularity from the gap between
    two points, so a one-bucket weekly window was mislabelled 'per day'. The
    granularity now comes from ``series.granularity``, so one bucket is fine.
    """
    series = SessionsOverTime([TrendPoint(date(2026, 5, 4), 8743, 9000)], "week")
    html = to_xml(overview_route._trend_section(series))
    assert "per week" in html
    assert "Week of May 4" in html
    assert "per day" not in html


def test_trend_section_daily_buckets_say_day_not_week() -> None:
    series = SessionsOverTime(
        [
            TrendPoint(date(2026, 6, 17), 168, 279),
            TrendPoint(date(2026, 6, 18), 0, 0),
        ],
        "day",
    )
    html = to_xml(overview_route._trend_section(series))
    assert "per day" in html
    assert "per week" not in html
    assert "Week of" not in html


def test_trend_section_region_is_aria_live_polite() -> None:
    series = SessionsOverTime([TrendPoint(date(2026, 6, 20), 1, 1)], "day")
    html = to_xml(overview_route._trend_section(series))
    assert 'id="compass-trend"' in html
    assert 'aria-live="polite"' in html


def test_trend_section_safe_default_empty_series_builds() -> None:
    """overview.py wraps the read in safe(..., default=SessionsOverTime([], "day"));
    the section must build from an empty series without a TypeError (the
    degradation path)."""
    # Should not raise.
    overview_route._trend_section(SessionsOverTime([], "day"))


# ── Route handler: admin gating + range threading (#10) ─────────────


class _FakeUser:
    """Stand-in for the section user.

    ``is_admin()`` drives the Quality-link branch under test; ``can_access`` and
    ``role`` are read by the shared TopNav/SubNav when the page wraps in Layout.
    A non-admin is a power_user (the role that keeps Overview but not Scorecard),
    so the SubNav itself drops the gated Scorecard tab — letting the non-admin
    test prove the *panel* link is what's absent, not just the nav tab.
    """

    name = "Test Reviewer"
    email = "reviewer@example.org"

    def __init__(self, *, admin: bool) -> None:
        self._admin = admin
        self.role = Role.ADMIN if admin else Role.POWER_USER

    def is_admin(self) -> bool:
        return self._admin

    def can_access(self, _section: str) -> bool:
        return True


class _FakeRouter:
    """Captures the handler ``@rt`` registers so we can call it directly."""

    def __init__(self) -> None:
        self.handlers: dict = {}

    def __call__(self, path):
        def deco(fn):
            self.handlers[path] = fn
            return fn

        return deco


class _FakeRequest:
    def __init__(self, query_params: dict | None = None) -> None:
        self.query_params = query_params or {}


def _overview_handler(monkeypatch, *, admin: bool, captured: dict):
    """Register the overview route with all four reads stubbed.

    ``get_engagement_stats`` and ``get_sessions_over_time`` record the ``since``
    they receive into ``captured`` so a test can prove the resolved bound (not
    the 7d default) reached the data layer. require_compass is monkeypatched to
    yield a user with the requested admin flag and no deny.
    """
    from nctqai.routes.compass import overview as overview_route

    monkeypatch.setattr(
        overview_route,
        "require_compass",
        lambda req: (_FakeUser(admin=admin), None),
    )

    def fake_engagement(since, until=None):
        captured["engagement_since"] = since
        captured["engagement_until"] = until
        return overview_route.EngagementStats(0, 0.0, 0, 0)

    def fake_trend(since, until=None):
        captured["trend_since"] = since
        captured["trend_until"] = until
        return overview_route.SessionsOverTime([], "day")

    monkeypatch.setattr(overview_route, "get_engagement_stats", fake_engagement)
    monkeypatch.setattr(overview_route, "get_sessions_over_time", fake_trend)
    monkeypatch.setattr(
        overview_route,
        "get_feedback_stats",
        lambda since, until=None: overview_route.FeedbackStats(0, 0, 0, 0, 0, 0, 0),
    )
    monkeypatch.setattr(
        overview_route, "snapshot_aggregates", lambda since, until=None: None
    )
    # G2: the Sessions-tile prior-period read. Stub it so the route never hits
    # the DB in these unit tests; 0 → no fabricated trend (the no-comparison case).
    monkeypatch.setattr(
        overview_route, "get_session_count", lambda since=None, until=None: 0
    )

    # The Umami HTTP read is async; stub it so the page builds offline. Record
    # the window it receives so a test can prove since/until reach the Umami
    # fetch (the unique-visitors tile must honor the range pill — #1807).
    async def fake_visitors(since, until):
        captured["visitors_since"] = since
        captured["visitors_until"] = until
        return None

    monkeypatch.setattr(overview_route, "_visitors_safe", fake_visitors)

    router = _FakeRouter()
    overview_route.register(router)
    return router.handlers["/compass/overview"]


def test_overview_hides_system_quality_panel_for_admin(monkeypatch) -> None:
    """The Overview "System quality" panel (the Scorecard link) is hidden for
    now — commented out in overview.py per Macon (2026-06-26).

    For an admin the SubNav still shows the Scorecard *tab*, so we pin the
    absence of the *panel* via its own class, not the page-wide href.
    """
    captured: dict = {}
    handler = _overview_handler(monkeypatch, admin=True, captured=captured)
    page = asyncio.run(handler(_FakeRequest(), range="7d"))
    html = to_xml(page)
    assert "overview-quality-link" not in html
    assert "System quality" not in html
    assert "Quality →" not in html


def test_overview_hides_system_quality_panel_for_non_admin(monkeypatch) -> None:
    """For a non-admin the panel and the gated Scorecard tab are both absent."""
    captured: dict = {}
    handler = _overview_handler(monkeypatch, admin=False, captured=captured)
    page = asyncio.run(handler(_FakeRequest(), range="7d"))
    html = to_xml(page)
    assert "System quality" not in html
    assert "overview-quality-link" not in html
    assert 'href="/compass/quality/scorecard"' not in html


def test_overview_custom_range_threads_real_bound_to_reads(monkeypatch) -> None:
    """range=custom&from=<date> must resolve a concrete lower bound and pass it
    to BOTH the engagement and trend reads — never the 7d default."""
    captured: dict = {}
    handler = _overview_handler(monkeypatch, admin=True, captured=captured)
    req = _FakeRequest({"from": "2026-05-01"})
    asyncio.run(handler(req, range="custom"))

    for key in ("engagement_since", "trend_since"):
        since = captured[key]
        assert since is not None, key
        # The custom span resolved to 2026-05-01, NOT ~7 days before now.
        assert (since.year, since.month, since.day) == (2026, 5, 1), key


def test_overview_custom_to_bound_threads_and_round_trips(monkeypatch) -> None:
    """range=custom&to=<date> must (a) thread an exclusive upper bound into the
    reads AND (b) echo the ``to`` value back into the date input so it survives a
    reload. Regression: the input was blank on reload because the route never
    passed custom_until to range_selector.
    """
    captured: dict = {}
    handler = _overview_handler(monkeypatch, admin=True, captured=captured)
    req = _FakeRequest({"from": "2026-05-01", "to": "2026-05-31"})
    page = asyncio.run(handler(req, range="custom"))
    html = to_xml(page)
    # (a) the upper bound reached the reads (inclusive day -> exclusive Jun 1).
    until = captured["engagement_until"]
    assert until is not None
    assert (until.year, until.month, until.day) == (2026, 6, 1)
    # (b) the `to` value round-trips into the control so a reload keeps it.
    assert 'name="to"' in html
    assert 'value="2026-05-31"' in html


def test_overview_rejected_custom_range_drops_upper_bound(monkeypatch) -> None:
    """range=custom with an empty `from` but a valid `to`: the custom span is
    rejected (falls back to 7d), so the upper bound must ALSO be dropped — else
    the queried window is [7d-ago, that `to`) which can be empty/backwards.
    """
    captured: dict = {}
    handler = _overview_handler(monkeypatch, admin=True, captured=captured)
    req = _FakeRequest({"from": "", "to": "2026-05-31"})
    asyncio.run(handler(req, range="custom"))
    # since fell back to the 7d preset (a real lower bound), and until is gated
    # off so it doesn't pin a stale May-31 ceiling over a rolling-7d floor.
    assert captured["engagement_since"] is not None
    assert captured["engagement_until"] is None
    assert captured["trend_until"] is None


# ── #1807: unique-visitors tile honors the range pill ───────────────


def test_overview_visitors_fetch_receives_window(monkeypatch) -> None:
    """The Umami unique-visitors read must get the SAME since/until the other
    cards use — not a hardcoded 7-day window. range=custom resolves a concrete
    lower+upper bound; both must reach the visitors fetch.
    """
    captured: dict = {}
    handler = _overview_handler(monkeypatch, admin=True, captured=captured)
    req = _FakeRequest({"from": "2026-05-01", "to": "2026-05-31"})
    asyncio.run(handler(req, range="custom"))

    since = captured["visitors_since"]
    until = captured["visitors_until"]
    assert since is not None and (since.year, since.month, since.day) == (2026, 5, 1)
    assert until is not None and (until.year, until.month, until.day) == (2026, 6, 1)


def test_overview_visitors_alltime_passes_none_since(monkeypatch) -> None:
    """range=all → the visitors fetch gets since=None (the "all time" window),
    which umami_stats floors to its far-back lower bound."""
    captured: dict = {}
    handler = _overview_handler(monkeypatch, admin=True, captured=captured)
    asyncio.run(handler(_FakeRequest(), range="all"))
    assert captured["visitors_since"] is None


def test_overview_visitors_subtitle_matches_active_window(monkeypatch) -> None:
    """The unique-visitors subtitle names the source + the active rolling window
    ("opened Compass · last 30 days"). At all-time the Umami tile shows an honest
    note instead of a number — its monthly-salt total over-counts."""
    captured: dict = {}

    async def fake_visitors(since, until):
        return overview_route.VisitorsSummary(total=99)

    handler = _overview_handler(monkeypatch, admin=True, captured=captured)
    monkeypatch.setattr(overview_route, "_visitors_safe", fake_visitors)

    page_30d = to_xml(asyncio.run(handler(_FakeRequest(), range="30d")))
    assert "opened Compass · last 30 days" in page_30d

    # All-time: the Umami tile no longer poses a (monthly-inflated) number as a
    # clean count — it shows the explanatory note and points at Chat users.
    page_all = to_xml(asyncio.run(handler(_FakeRequest(), range="all")))
    assert "Not shown for all-time" in page_all


# ── G2: prior-period sessions trend caption (pure) ──────────────────


def test_session_trend_caption_up_down_flat_and_no_comparison() -> None:
    cap = overview_route._session_trend_caption
    assert cap(120, 100) == "▲ 20% vs prior period"
    assert cap(80, 100) == "▼ 20% vs prior period"
    assert cap(100, 100) == "no change vs prior period"
    # No honest comparison when the prior window had zero sessions → no fake ∞%.
    assert cap(50, 0) is None
    assert cap(50, -1) is None


def test_sessions_trend_skips_subday_and_all_time_windows() -> None:
    """A sub-day window ("today") and the all-time range (since=None) yield no
    trend — the prior would be a misleading partial-day sliver or nonexistent."""
    # all-time: since None → None regardless of count.
    assert asyncio.run(overview_route._sessions_trend(None, None, 123)) is None
    # sub-day: since = a few hours ago, until = now → span < 1 day → None.
    since = datetime.now(UTC) - timedelta(hours=6)
    assert asyncio.run(overview_route._sessions_trend(since, None, 123)) is None


# ── C-2: ?to= upper bound resolution + threading ────────────────────


def test_resolve_until_parses_to_param_as_inclusive_end_of_day() -> None:
    """Inline fallback (sibling resolve_until not present): a ``to`` date maps to
    midnight of the NEXT day so the whole selected day is inside the half-open
    [since, until) window."""
    until = overview_route._resolve_until("2026-05-31")
    assert until == datetime(2026, 6, 1, 4, tzinfo=UTC)


def test_resolve_until_empty_or_malformed_is_none() -> None:
    assert overview_route._resolve_until("") is None
    assert overview_route._resolve_until("not-a-date") is None


def test_overview_threads_until_into_all_reads(monkeypatch) -> None:
    """``?to=`` must resolve an upper bound and reach BOTH the engagement and
    trend reads (and, by construction, feedback + snapshot)."""
    captured: dict = {}
    handler = _overview_handler(monkeypatch, admin=True, captured=captured)
    req = _FakeRequest({"from": "2026-05-01", "to": "2026-05-31"})
    asyncio.run(handler(req, range="custom"))

    for key in ("engagement_until", "trend_until"):
        until = captured[key]
        assert until is not None, key
        # Inclusive of the 31st → exclusive next-midnight (Jun 1).
        assert (until.year, until.month, until.day) == (2026, 6, 1), key


# ── Live DB correctness (gated) ─────────────────────────────────────


@pytest.mark.live_db
def test_get_sessions_over_time_live_db_zero_fills_gap(request) -> None:
    """Against staging: a 7d window returns ascending zero-filled daily buckets,
    and the 2026-06-18 deploy gap appears as a low/zero bar, not omitted.

    Run with: op run --env-file=.env.op -- \
        env PG_SCHEMA=compass PYTHONPATH=src uv run pytest \
        src/nctqai/tests/test_compass_overview_trend.py --run-live-db
    """
    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB smoke tests")

    since = datetime.now(UTC) - timedelta(days=7)
    result = get_sessions_over_time(since)
    points = result.points
    assert points, "expected at least one bucket"
    assert result.granularity == "day"
    # Ascending order, no gaps (every calendar day present via generate_series).
    days = [p.day for p in points]
    assert days == sorted(days)
    for earlier, later in zip(days, days[1:]):
        assert (later - earlier) == timedelta(days=1), "daily buckets must be contiguous"
    # Counts are non-negative ints; sessions never exceed questions only by
    # coincidence — just assert the honest shape.
    assert all(p.sessions >= 0 and p.questions >= 0 for p in points)
