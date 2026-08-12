"""Compass Overview — funder-reporting-focused dashboard.

Three sections grounded in NCTQ's Gates Foundation reporting needs:
  A. Engagement     — sessions, avg user questions, multi-turn rates, repeat users
  B. Feedback       — user thumbs vs. system Critic, clearly separated
  C. What people ask — top topics, districts, states (top 3 each)

Drill-down into individual conversations lives at /compass/conversations.
Cost / latency / token tiles live at /compass/operations (Starling-only).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote

from fasthtml.common import Div, H2, H3, P, Span, Style
from starlette.requests import Request

from nctqai.components import KpiCard, KpiRow
from nctqai.components.compass.bars import bar_row, layered_trend_chart
from nctqai.components.compass.range_pills import (
    parse_custom_since,
    range_selector,
    resolve_since,
)
from nctqai.layout import Layout
from nctqai.routes.compass._helpers import require_compass, run_in_thread, safe
from nctqai.services.compass_stats import (
    ArtifactCoverage,
    EngagementStats,
    FeedbackStats,
    ReturningUsers,
    SessionsOverTime,
    get_artifact_coverage,
    get_engagement_stats,
    get_feedback_stats,
    get_returning_users,
    get_session_count,
    get_sessions_over_time,
    snapshot_aggregates,
)
from nctqai.services.umami_stats import (
    VisitorsSummary,
    get_visitors,
    umami_enabled,
)
from nctqai.services.ga_stats import (
    ga_enabled,
    get_visitors as ga_get_visitors,
)

# C-2: the custom-range upper bound (``?to=YYYY-MM-DD``). A sibling slice adds
# ``resolve_until`` to range_pills on another branch; import it when present,
# otherwise the route parses ``to`` inline (see _resolve_until below).
try:
    from nctqai.components.compass.range_pills import resolve_until
except ImportError:  # pragma: no cover - exercised once the sibling slice lands
    resolve_until = None

logger = logging.getLogger(__name__)
_EASTERN = ZoneInfo("America/New_York")



def _section_heading(title: str, subtitle: str = "") -> Div:
    parts = [H2(title, cls="overview-section-title")]
    if subtitle:
        parts.append(P(subtitle, cls="overview-section-subtitle"))
    return Div(*parts, cls="overview-section-heading")


def _under_construction_card(label: str, why: str) -> Div:
    """A muted tile that names the metric but tells the user it's not live yet."""
    return Div(
        Div("—", cls="kpi-value", style="color: var(--text-faint);"),
        Div(label, cls="kpi-label"),
        Div(Span("Under construction", cls="coming-soon-badge"), cls="kpi-subtitle"),
        Div(why, cls="kpi-subtitle", style="margin-top: 4px;"),
        cls="kpi-card kpi-placeholder",
    )


# ── Section A: Engagement ───────────────────────────────────────────


def _unique_visitors_card(
    visitors: VisitorsSummary | None,
    subtitle: str = "last 7 days",
    is_all_time: bool = False,
) -> Div:
    """Site-level unique-visitor tile (Umami) over the active window.

    Umami de-duplicates a visitor only WITHIN a calendar month (its hash salt
    resets on the 1st), so the meaning of the number depends on the window:
      * 7d / 30d (and other bounded windows) are honest enough — the subtitle
        names the exact rolling window ("viewed Compass on Pathfinder · last 30 days").
      * "all time" would count a monthly regular ~12x a year, so we DON'T show a
        number there. We show a short note and point at the Chat-users tile,
        whose visitor_id count de-duplicates cleanly with no monthly reset.
    ``is_all_time`` is set by the route when the range pill is "all time". When
    Umami creds aren't configured the tile degrades to the shared placeholder.
    """
    if is_all_time:
        return Div(
            Div("—", cls="kpi-value", style="color: var(--text-faint);"),
            Div("Unique visitors", cls="kpi-label"),
            Div("Not shown for all-time", cls="kpi-subtitle"),
            Div(
                "Umami re-counts a visitor each calendar month, so the all-time "
                "total over-counts. See Chat users for the true count.",
                cls="kpi-subtitle",
                style="margin-top: 4px;",
            ),
            cls="kpi-card kpi-placeholder",
        )
    if visitors is None:
        return _under_construction_card(
            "Unique visitors",
            "Traffic stats temporarily unavailable",
        )
    return Div(
        Div(f"{visitors.total:,}", cls="kpi-value"),
        Div("Unique visitors", cls="kpi-label"),
        Div(f"viewed Compass on Pathfinder · {subtitle}", cls="kpi-subtitle"),
        cls="kpi-card",
    )


def _unique_people_card(
    returning: ReturningUsers | None, subtitle: str = "last 7 days"
) -> Div:
    """De-duplicated chat-user count over the active window (#1808).

    ``returning.identified`` is COUNT(DISTINCT visitor_id) in the window — the
    number of distinct people who chatted, counted once each no matter how many
    sessions they had. This is the headline "how many real users" number NCTQ
    asked for, scoped to the 7d/30d/all-time pill (``subtitle`` names that
    window, reusing the same range-label mechanism as the unique-visitors tile).

    Deliberately labeled "Chat users" — NOT "Unique visitors" — so it is not
    confused with the site-level, monthly-resetting Umami "Unique visitors" tile
    next to it. This one is chat-level and de-duplicated since the visitor_id
    column went live. When no one is identified yet (the id is minted by the
    embed; pre-launch direct traffic carries none), it degrades to the same calm
    "begins at launch" placeholder the Returning-users tile uses rather than a
    bare 0 that would read as "nobody uses Compass".
    """
    if returning is None or returning.identified == 0:
        return _under_construction_card(
            "Chat users",
            "Counting begins at launch",
        )
    return Div(
        Div(f"{returning.identified:,}", cls="kpi-value"),
        Div("Chat users", cls="kpi-label"),
        Div(f"started a conversation · {subtitle}", cls="kpi-subtitle"),
        cls="kpi-card",
    )


def _returning_users_card(
    returning: ReturningUsers | None, subtitle: str = "last 7 days"
) -> Div:
    """Repeat-user tile from the pseudonymous visitor_id (WS-5 / G5).

    Visitors who started 2 or more conversations within the selected window.
    Honors the 7d/30d/all-time range pill like the other people tiles (the
    subtitle names the window), so it nests correctly under Chat users and
    Unique visitors rather than showing an all-time total next to windowed ones.
    Chat-level returning visitors, distinct from the site-level Unique visitors
    tile. When no one is identified in the window, shows a muted
    under-construction shape rather than a bare 0 that would read as "nobody
    returns".
    """
    if returning is None or returning.identified == 0:
        return _under_construction_card(
            "Returning users",
            "Counting begins at launch (chat-level, since deploy)",
        )
    return Div(
        Div(f"{returning.returning:,}", cls="kpi-value"),
        Div("Returning users", cls="kpi-label"),
        Div(
            f"2+ conversations · {subtitle}",
            cls="kpi-subtitle",
        ),
        cls="kpi-card",
    )


def _session_trend_caption(current: int, prior: int) -> str | None:
    """A period-over-period sessions delta vs the immediately-preceding window.

    Returns None when there is no honest comparison to make — no prior window
    (all-time range) or a prior window with zero sessions (a percentage would be
    undefined / read as a fake "▲ ∞%"). Never fabricates a trend (G2).
    """
    if prior <= 0:
        return None
    delta = round((current - prior) / prior * 100)
    if delta > 0:
        return f"▲ {delta}% vs prior period"
    if delta < 0:
        return f"▼ {abs(delta)}% vs prior period"
    return "no change vs prior period"


def _artifact_href(
    tab: str, range_: str, custom_since: str, custom_until: str, custom_rejected: bool
) -> str:
    """Drill-down URL to the Conversations list filtered to one saved artifact.

    Carries the ACTIVE range pill (and a custom from/to span) so the destination
    list scopes to the SAME window as the tile. Without it the conversations page
    defaults to its own 7d window, and clicking a 30d/all-time tile would land on
    a 7d-filtered list whose has-table count contradicts the headline (the tile
    and the filter share the ``has_table`` signal, but they only describe the
    same conversations when they share the window too).
    """
    params = [f"tab={quote(tab)}", f"range={quote(range_)}"]
    if range_ == "custom" and not custom_rejected:
        if custom_since:
            params.append(f"from={quote(custom_since)}")
        if custom_until:
            params.append(f"to={quote(custom_until)}")
    return "/compass/conversations?" + "&".join(params)


def _artifact_card(
    *,
    coverage: ArtifactCoverage | None,
    label: str,
    count: int,
    pct: int,
    href: str,
) -> Div:
    """One artifact-availability tile with the full conversation denominator."""
    if coverage is None or not coverage.available:
        return _under_construction_card(
            label,
            "Saved artifact summaries are unavailable",
        )
    if coverage.sessions == 0:
        return _under_construction_card(
            label,
            "No conversations in this window yet",
        )
    return KpiCard(
        f"{pct}%",
        label,
        subtitle=f"{count:,} of {coverage.sessions:,} conversations",
        href=href,
    )


def _artifact_cards(
    coverage: ArtifactCoverage | None,
    *,
    artifact_hrefs: dict[str, str],
) -> list[Div]:
    """Overview cards for separately saved tables, CSV exports, and charts."""
    if coverage is None:
        return [
            _artifact_card(coverage=None, label=label, count=0, pct=0, href=href)
            for label, href in (
                ("Data tables", artifact_hrefs["has-table"]),
                ("CSV exports available", artifact_hrefs["has-csv-export"]),
                ("Charts shown", artifact_hrefs["has-chart"]),
            )
        ]
    return [
        _artifact_card(
            coverage=coverage,
            label="Data tables",
            count=coverage.with_table,
            pct=coverage.table_pct,
            href=artifact_hrefs["has-table"],
        ),
        _artifact_card(
            coverage=coverage,
            label="CSV exports available",
            count=coverage.with_csv_export,
            pct=coverage.csv_export_pct,
            href=artifact_hrefs["has-csv-export"],
        ),
        _artifact_card(
            coverage=coverage,
            label="Charts shown",
            count=coverage.with_chart,
            pct=coverage.chart_pct,
            href=artifact_hrefs["has-chart"],
        ),
    ]


def _engagement_section(
    stats: EngagementStats,
    visitors: VisitorsSummary | None,
    sessions_trend: str | None = None,
    returning: ReturningUsers | None = None,
    visitors_subtitle: str = "last 7 days",
    unique_people: ReturningUsers | None = None,
    visitors_is_all_time: bool = False,
    coverage: ArtifactCoverage | None = None,
    artifact_hrefs: dict[str, str] | None = None,
) -> Div:
    artifact_hrefs = artifact_hrefs or {
        "has-table": "/compass/conversations?tab=has-table",
        "has-csv-export": "/compass/conversations?tab=has-csv-export",
        "has-chart": "/compass/conversations?tab=has-chart",
    }
    tiles = KpiRow(
        # G2: Sessions carries a context subtitle ("conversations") and, when a
        # real prior-period comparison exists, a period-over-period trend caption.
        KpiCard(
            f"{stats.sessions:,}",
            "Sessions",
            subtitle="conversations",
            trend=sessions_trend,
        ),
        KpiCard(stats.avg_user_questions, "Avg user questions", subtitle="per session"),
        # W1-5: the canonical "Multi-turn rate" tile now shows the 3+ threshold
        # (the deeper-engagement signal). The 2+ rate is still computed on
        # EngagementStats but no longer gets its own tile — one canonical number.
        KpiCard(
            f"{stats.multi_turn_3plus_rate}%",
            "Multi-turn rate",
            subtitle="3+ user questions",
        ),
        *_artifact_cards(coverage, artifact_hrefs=artifact_hrefs),
        _unique_visitors_card(
            visitors, visitors_subtitle, is_all_time=visitors_is_all_time
        ),
        # De-duplicated chat-user count for the active window — sits next to the
        # site-level Umami "Unique visitors" tile; both honor the same range
        # pill (visitors_subtitle names the window for both).
        _unique_people_card(unique_people, visitors_subtitle),
        _returning_users_card(returning, visitors_subtitle),
        # Fixed four-column grid keeps the metric tiles easy to scan and compare.
        cls="kpi-cards--grid4",
    )
    return Div(
        _section_heading(
            "Engagement",
            "How many people are using Compass and how much are they asking?",
        ),
        tiles,
        cls="overview-section",
    )


# ── Section A2: Activity over time ──────────────────────────────────


def _trend_legend() -> Div:
    """Key for the layered trend bars (darker Sessions over lighter Questions)."""
    return Div(
        Span(
            Span(cls="trend-legend-swatch trend-legend-sessions"),
            "Sessions",
            cls="trend-legend-item",
        ),
        Span(
            Span(cls="trend-legend-swatch trend-legend-questions"),
            "Questions",
            cls="trend-legend-item",
        ),
        Span("avg = questions per session", cls="trend-legend-note"),
        cls="trend-legend",
    )


def _trend_section(series: SessionsOverTime) -> Div:
    """Sessions + questions over time, scoped to the selected range.

    One layered bar per bucket: a lighter Questions bar with the darker Sessions
    bar overlaid (questions are always >= sessions), plus the avg questions per
    session — one compact chart instead of two side-by-side panels.

    The section body carries id='compass-trend' + aria-live='polite' so an
    assistive reader announces it; Overview is full-page URL-driven navigation
    (no HTMX swap on this surface per the resolved decision), so no aria-busy
    handler is wired — there is no swap to drive it.

    Long windows are weekly-bucketed by the service; the subtitle and bar
    labels say "per week" / "Week of …" so the chart never claims daily
    granularity it doesn't have (honesty mandate). The granularity comes from
    ``series.granularity`` — the value the query actually used — so even a
    single-bucket weekly window is labelled correctly.
    """
    points = series.points
    if points:
        weekly = series.granularity == "week"
        subtitle = (
            "Sessions and user questions per week in the selected range."
            if weekly
            else "Sessions and user questions per day in the selected range."
        )
        items = [
            {
                "label": (
                    f"Week of {p.day.strftime('%b %-d')}" if weekly
                    else p.day.strftime("%b %-d")
                ),
                "sessions": p.sessions,
                "questions": p.questions,
                # avg questions per session for the bucket; None when no sessions
                # (avoids a divide-by-zero and a meaningless "0.0" on empty days).
                "avg": f"{p.questions / p.sessions:.1f}" if p.sessions else None,
            }
            for p in points
        ]
        body = Div(
            _trend_legend(),
            layered_trend_chart(items),
            cls="overview-panel",
        )
    else:
        subtitle = "Sessions and user questions over time in the selected range."
        body = P("No activity in this range yet.", cls="text-muted text-sm")
    return Div(
        _section_heading("Activity over time", subtitle),
        Div(body, id="compass-trend", aria_live="polite", cls="compass-trend"),
        cls="overview-section",
    )


# ── Section B: Feedback ─────────────────────────────────────────────


def _feedback_section(stats: FeedbackStats, is_admin: bool = False) -> Div:
    # W1-2: the denominator is SESSIONS (conversations), not assistant messages.
    bars = Div(
        bar_row(
            "Thumbs up",
            stats.thumbs_up,
            stats.sessions,
            suffix=f" ({stats.thumbs_up_pct}%)",
        ),
        bar_row(
            "Thumbs down",
            stats.thumbs_down,
            stats.sessions,
            suffix=f" ({stats.thumbs_down_pct}%)",
        ),
        bar_row(
            "Not rated",
            stats.unrated,
            stats.sessions,
            suffix=f" ({stats.unrated_pct}%)",
        ),
        cls="bar-chart",
    )

    # Only call adoption "low" when the real unrated share backs it up; word it
    # from the actual numbers (rated sessions out of all sessions) rather than
    # asserting "most answers are unrated" unconditionally (false once adoption
    # climbs).
    rated_sessions = stats.thumbs_up + stats.thumbs_down
    user_panel_children = [
        H3("User feedback", cls="overview-subsection-title"),
        P(
            f"% of {stats.sessions:,} sessions (conversations) rated up or down",
            cls="overview-section-subtitle",
        ),
    ]
    if stats.unrated_pct >= 50:
        user_panel_children.append(
            P(
                f"Ratings adoption is low — only {rated_sessions:,} of "
                f"{stats.sessions:,} sessions ({100 - stats.unrated_pct}%) "
                "have been rated.",
                cls="overview-section-subtitle",
            )
        )
    user_panel_children.append(bars)
    user_panel = Div(*user_panel_children, cls="overview-panel")

    # The automated-Critic Approval/Revision tiles used to live here, both
    # hardcoded to 0% over empty tables (compass.conversation_evaluations and
    # critic_guidance have 0 rows). Per decision D the quality slot stays, but
    # showing a fabricated 0% is dishonest, so it becomes a link to the real
    # offline-eval Scorecard rather than a fake live number.
    #
    # The Scorecard route is admin-only (nav.py _TAB_ROLES), while Overview is
    # the power_user-visible funder view — so the "Quality →" link is rendered
    # ONLY for admins; a non-admin would just 403. Non-admins get the same
    # honest copy without a dead-end link.
    # ── "System quality" panel — HIDDEN FOR NOW (per Macon, 2026-06-26) ──
    # The Overview's quality slot linked to the offline-eval Scorecard
    # ("Compass answers are graded against B-spine test cases…"). Commented out
    # for now so the Feedback section shows user thumbs only. To restore:
    # uncomment the block below, re-add `A` to the fasthtml import, put
    # `quality_panel` back in the grid, and restore the "…and how Compass grades
    # itself" half of the section subtitle.
    #
    # quality_children: list = [
    #     H3("System quality", cls="overview-subsection-title"),
    # ]
    # if is_admin:
    #     quality_children.append(
    #         P(
    #             "Compass answers are graded against B-spine test cases in an "
    #             "offline eval sweep. See the latest results on the Scorecard.",
    #             cls="overview-section-subtitle",
    #         )
    #     )
    #     quality_children.append(
    #         A(
    #             "Quality →",
    #             href="/compass/quality/scorecard",
    #             cls="overview-quality-link",
    #         )
    #     )
    # else:
    #     quality_children.append(
    #         P(
    #             "Compass answers are graded against B-spine test cases in an "
    #             "offline eval sweep. The full Scorecard is available to admins.",
    #             cls="overview-section-subtitle",
    #         )
    #     )
    # quality_panel = Div(*quality_children, cls="overview-panel")

    return Div(
        _section_heading(
            "Feedback",
            "How users rate answers.",
        ),
        # "System quality" panel hidden for now — feedback panel only.
        Div(user_panel, cls="overview-grid-2"),
        cls="overview-section",
    )


# ── Section C: What people ask ──────────────────────────────────────


def _ranked_panel(title: str, items: list[dict], label_key: str, count_key: str) -> Div:
    if not items:
        body = P("No data yet.", cls="text-muted text-sm")
    else:
        max_val = max(item[count_key] for item in items) or 1
        rows = []
        for rank, item in enumerate(items, start=1):
            value = int(item[count_key])
            pct = round(value / max_val * 100) if max_val else 0
            rows.append(
                Div(
                    Span(str(rank), cls="ask-rank"),
                    Div(str(item[label_key]), cls="bar-label ask-label"),
                    Div(
                        Div(cls="bar-fill", style=f"width: {pct}%;"),
                        cls="bar-track ask-track",
                    ),
                    Div(f"{value:,}", cls="bar-value ask-count"),
                    cls="bar-row ask-row",
                )
            )
        body = Div(*rows, cls="bar-chart")
    return Div(H3(title, cls="overview-subsection-title"), body, cls="overview-panel")


# O2: the shared ``.bar-label`` rule truncates with an ellipsis (white-space:
# nowrap; overflow: hidden; — theme.py), which clipped long topic names like
# "Minimum number of fo…". Inside this section we let the label wrap onto two
# lines instead. Scoped to ``.overview-ask`` so no other bar chart is affected,
# and emitted as a Style node (the same inline-CSS pattern scenarios.py uses)
# because the global class lives in theme.py, which this slice does not own.
_ASK_LABEL_CSS = Style(
    # Long topic names wrap onto two lines instead of clipping with an ellipsis.
    ".overview-ask .bar-label {"
    " white-space: normal; overflow: visible; text-overflow: clip;"
    " flex-basis: 170px; line-height: 1.25;"
    " }"
    # A small teal rank badge (1/2/3) gives each top-3 list a tidy leaderboard
    # feel; the count gets room for big values like "7,639".
    ".overview-ask .ask-row { align-items: center; gap: 8px; }"
    ".overview-ask .ask-rank {"
    " flex: 0 0 auto; width: 18px; height: 18px; border-radius: 50%;"
    " background: var(--compass-teal-bg); color: var(--compass-teal-dark);"
    " font-size: 10px; font-weight: 700; line-height: 18px; text-align: center;"
    " }"
    ".overview-ask .ask-count { min-width: 52px; color: var(--text-secondary);"
    " font-weight: 600; }"
)


def _what_people_ask_section(
    topics: list[dict], districts: list[dict], states: list[dict]
) -> Div:
    district_items = [
        {"label": f"{d['district_name']} ({d['state']})", "count": d["session_count"]}
        for d in districts
    ]
    return Div(
        _ASK_LABEL_CSS,
        _section_heading(
            "What conversations covered",
            "Counts each policy area, district, or state once per conversation "
            "from Compass's saved metric and district context (top 3 each).",
        ),
        Div(
            _ranked_panel("Top policy areas", topics, "topic", "session_count"),
            _ranked_panel("Top districts referenced or matched", district_items, "label", "count"),
            _ranked_panel("Top states represented", states, "state", "session_count"),
            cls="overview-grid-2 overview-ask",
            style="grid-template-columns: 1fr 1fr 1fr;",
        ),
        cls="overview-section",
    )


# ── Route ───────────────────────────────────────────────────────────


async def _visitors_safe(
    since: datetime | None, until: datetime | None
) -> VisitorsSummary | None:
    # Window the Umami read to the same since/until the other cards use so the
    # unique-visitors tile honors the 7d/30d pill (#1807).
    # All-time: don't fetch a number. Umami's hash salt resets each calendar
    # month, so the lifetime unique-visitor total over-counts (a monthly regular
    # counts ~12x a year). The card renders an explanatory note instead
    # (is_all_time), and the Chat-users tile carries the honest all-time
    # de-duplicated count. (since is None only for the all-time range.)
    # Google Analytics is the visitor-tracking source when configured. Unlike
    # Umami, GA4 dedups by client id across all time, so it answers every range
    # (including all-time) with a meaningful number. Falls back to Umami only
    # when GA is not set up.
    if ga_enabled():
        try:
            return await ga_get_visitors(since, until)
        except Exception:
            logger.exception("get_visitors failed (GA4)")
            return None
    if since is None:
        return None
    # Expected in envs without Umami creds (local/staging): skip the call so the
    # tile degrades quietly to "under construction" instead of logging an error
    # traceback on every overview load. A real fetch failure still logs loudly.
    if not umami_enabled():
        return None
    try:
        return await get_visitors(since, until)
    except Exception:
        logger.exception("get_visitors failed (Umami)")
        return None


def _resolve_until(to_param: str) -> datetime | None:
    """Resolve the custom-range upper bound from ``?to=YYYY-MM-DD`` (C-2).

    Prefer the sibling slice's ``resolve_until`` when it has landed; otherwise
    parse inline. The bound is EXCLUSIVE and set to midnight of the day *after*
    ``to`` so the whole selected ``to`` day is included (the service uses
    ``created_at < until``). Returns None for empty/malformed input so a bad
    ``to`` simply means "no upper bound" rather than a 500.
    """
    to_param = (to_param or "").strip()
    if not to_param:
        return None
    if resolve_until is not None:
        return resolve_until(to_param)
    try:
        parsed = date.fromisoformat(to_param)
    except (TypeError, ValueError):
        return None
    following_day = parsed + timedelta(days=1)
    return datetime(
        following_day.year,
        following_day.month,
        following_day.day,
        tzinfo=_EASTERN,
    ).astimezone(UTC)


def _as_aware(dt: datetime) -> datetime:
    """Coerce a possibly-naive datetime to aware UTC for span arithmetic."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _sessions_trend(
    since: datetime | None, until: datetime | None, current_sessions: int
) -> str | None:
    """Build the Sessions tile's prior-period trend caption, or None (G2).

    The prior window is the equal-length span immediately before ``since``. We
    skip it for the all-time range (no prior) and for any sub-day window such as
    "today" (its prior is a partial-day sliver that produces a misleading
    percentage on tiny N). The prior read is wrapped in ``safe`` so a failed
    count degrades to "no trend", never a 500 or a fabricated number.
    """
    if since is None:
        return None
    start = _as_aware(since)
    end = _as_aware(until) if until is not None else datetime.now(UTC)
    span = end - start
    if span < timedelta(days=1):
        return None
    prior_since = start - span
    prior_until = start
    prior = await run_in_thread(
        safe,
        lambda: get_session_count(prior_since, prior_until),
        0,
        name="prior_session_count",
    )
    return _session_trend_caption(current_sessions, prior)


def register(rt):

    @rt("/compass/overview")
    async def get_overview_page(
        request: Request, range: str = "7d", from_: str = ""
    ):
        user, deny = require_compass(request)
        if deny:
            return deny

        # ``from``/``to`` are Python keywords, so Starlette can't bind them to
        # params; read the custom bounds straight off the query string.
        custom_since = request.query_params.get("from", from_)
        custom_until = request.query_params.get("to", "")
        range_label, since = resolve_since(range, custom_since)
        # `to` is part of the custom span. If the span was rejected (a bad/empty
        # `from` made resolve_since fall back to the 7d preset), drop the upper
        # bound too, so the queried window matches the 7d pill the selector shows
        # — symmetric with the lower-bound degradation in resolve_since, and with
        # range_selector clearing the echoed `to` value in the same case.
        custom_rejected = range == "custom" and parse_custom_since(custom_since) is None
        until = None if custom_rejected else _resolve_until(custom_until)

        # Fan out: three Postgres reads + one HTTP read in parallel. Sync
        # psycopg2 calls are pushed to threads so they don't park the event
        # loop. Each branch is wrapped in ``safe`` so a single failure shows
        # a zero tile instead of a 500. ``until`` is threaded into every read
        # so the trend, engagement, feedback, and snapshot windows all agree.
        (
            engagement,
            trend,
            feedback,
            aggregates,
            visitors,
            returning,
            unique_people,
            coverage,
        ) = (
            await asyncio.gather(
            run_in_thread(
                safe,
                lambda: get_engagement_stats(since, until),
                EngagementStats(0, 0.0, 0, 0),
                name="get_engagement_stats",
            ),
            run_in_thread(
                safe,
                lambda: get_sessions_over_time(since, until),
                SessionsOverTime([], "day"),
                name="get_sessions_over_time",
            ),
            run_in_thread(
                safe,
                lambda: get_feedback_stats(since, until),
                FeedbackStats(0, 0, 0, 0, 0, 0, 0),
                name="get_feedback_stats",
            ),
            run_in_thread(
                safe,
                lambda: snapshot_aggregates(since, until),
                None,
                name="snapshot_aggregates",
            ),
            _visitors_safe(since, until),
            run_in_thread(
                safe,
                # Returning users honors the active range pill like the other
                # people tiles: visitors who started 2+ conversations WITHIN the
                # selected window (since, until). This keeps it nesting correctly
                # under Chat users and Unique visitors instead of showing an
                # all-time total next to windowed numbers (NCTQ feedback: an
                # all-time returning count read as larger than a 7-day visitor
                # count). Same (since, until) as the windowed chat-users read, so
                # the two share one cached query.
                lambda: get_returning_users(since, until),
                ReturningUsers(0, 0),
                name="get_returning_users",
            ),
            run_in_thread(
                safe,
                # Chat-users tile (#1808): the WINDOWED de-duplicated chat-user
                # count (COUNT(DISTINCT visitor_id) over the resolved since/until)
                # so it responds to the 7d/30d/all-time pill. Distinct call from
                # the all-time returning-users read above; get_returning_users
                # now caches by since~until (TTL). These two reads run concurrently
                # (asyncio.gather), so a cold load still issues both queries — the
                # payoff is ACROSS loads: at the all-time range both reads key to
                # the same (None, None) bounds, so repeated loads within the TTL
                # reuse one cached value instead of re-querying.
                lambda: get_returning_users(since, until),
                ReturningUsers(0, 0),
                name="get_returning_users_windowed",
            ),
            run_in_thread(
                safe,
                # Data-table coverage (#1821): the WINDOWED share of conversations
                # whose answer returned a data table, off the precomputed
                # has_table signal. Honors the same since/until as the rest of
                # the row, so the tile matches the active range pill.
                lambda: get_artifact_coverage(since, until),
                ArtifactCoverage(0, 0, 0, available=False),
                name="get_artifact_coverage",
            ),
            )
        )

        # G2: a real prior-period sessions comparison for the Sessions tile.
        # Only for a multi-day bounded window — "all time" has no prior, and
        # "today" would compare against a partial-day sliver and read as a
        # misleading "▲ 200%" on tiny N, so it is intentionally skipped.
        sessions_trend = await _sessions_trend(since, until, engagement.sessions)

        if aggregates is None:
            topics, districts, states = [], [], []
        else:
            topics = [
                {"topic": t, "session_count": c}
                for t, c in aggregates.topics.most_common(3)
            ]
            districts = [
                {**aggregates.district_meta[did], "session_count": c}
                for did, c in aggregates.districts.most_common(3)
            ]
            states = [
                {"state": s, "session_count": c}
                for s, c in aggregates.states.most_common(3)
            ]

        content = Div(
            Div(
                range_selector(range, "/compass/overview", custom_since, custom_until),
                cls="strip-range",
            ),
            _engagement_section(
                engagement,
                visitors,
                sessions_trend,
                returning,
                # Lower-case the resolved range label ("Last 7 days" → "last 7
                # days", "All time" → "all time", "Since 2026-05-01" → …) so the
                # unique-visitors subtitle names the window the pill selected.
                visitors_subtitle=range_label.lower(),
                # Windowed de-duplicated chat-user count for the Unique-people
                # tile; shares the range-label subtitle with unique visitors.
                unique_people=unique_people,
                # All-time: only the legacy Umami source hides its number (its
                # monthly-salt total over-counts). Google Analytics dedups by
                # client id across all time, so when GA is the source it shows a
                # real all-time number like every other range.
                visitors_is_all_time=(since is None) and not ga_enabled(),
                coverage=coverage,
                artifact_hrefs={
                    tab: _artifact_href(
                        tab, range, custom_since, custom_until, custom_rejected
                    )
                    for tab in ("has-table", "has-csv-export", "has-chart")
                },
            ),
            _trend_section(trend),
            # "What people ask" (the *what* of the sessions) sits above Feedback.
            _what_people_ask_section(topics, districts, states),
            _feedback_section(feedback, is_admin=bool(user and user.is_admin())),
            cls="compass-overview-wrap",
        )

        return Layout(
            "Compass — Overview",
            "",
            content,
            section="compass",
            sub_nav="/compass/overview",
            user=user,
            show_heading=False,
        )
