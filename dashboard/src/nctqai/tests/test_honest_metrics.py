"""Honest-metrics regression tests (U2 / HM-R1, HM-R2).

These guard the cleanup that removed the fabricated automated-Critic tiles
(hardcoded 0% over empty tables), relabeled "Top states" -> "States
referenced", and added the user-feedback low-adoption note — while KEEPING
every metric slot per decision D (the Critic slot becomes an honest link to
the real offline-eval Scorecard, not a deleted panel).

Pure component/dataclass assertions — no DB needed (conftest only adds
sys.path + the --run-live-db marker).
"""

from fasthtml.common import to_xml

from nctqai.routes.compass.overview import (
    _engagement_section,
    _feedback_section,
    _returning_users_card,
    _unique_people_card,
    _unique_visitors_card,
    _what_people_ask_section,
)
from nctqai.services.compass_stats import (
    EngagementStats,
    FeedbackStats,
    ReturningUsers,
)
from nctqai.services.umami_stats import VisitorsSummary


def _feedback() -> FeedbackStats:
    # W1-2: the denominator is SESSIONS (conversations), not assistant messages.
    return FeedbackStats(
        sessions=1000,
        thumbs_up=8,
        thumbs_down=5,
        unrated=987,
        thumbs_up_pct=1,
        thumbs_down_pct=1,
        unrated_pct=98,
    )


# ── HM-R1: no fabricated values ─────────────────────────────────────


def test_feedback_stats_has_no_fabricated_critic_fields():
    """The hardcoded-0% critic fields are GONE, not merely zeroed."""
    fs = _feedback()
    assert not hasattr(fs, "critic_approval_rate")
    assert not hasattr(fs, "critic_revision_rate")
    assert list(FeedbackStats.__dataclass_fields__) == [
        "sessions",
        "thumbs_up",
        "thumbs_down",
        "unrated",
        "thumbs_up_pct",
        "thumbs_down_pct",
        "unrated_pct",
    ]


# ── W1-2: feedback is session-denominated, not message-denominated ──


def test_feedback_stats_denominator_field_is_sessions_not_messages():
    """W1-2 contract: the denominator field is ``sessions`` and the old
    message-grain ``assistant_messages`` field is gone."""
    fields = FeedbackStats.__dataclass_fields__
    assert "sessions" in fields
    assert "assistant_messages" not in fields


def test_feedback_section_denominates_by_sessions():
    """The user-feedback subtitle states the denominator in SESSION terms
    (conversations), never assistant responses/messages."""
    html = str(_feedback_section(_feedback(), is_admin=False))
    assert "1,000 sessions" in html
    assert "assistant responses" not in html
    assert "assistant messages" not in html


def test_feedback_low_adoption_note_names_session_counts():
    """The low-adoption note quantifies adoption from real session counts:
    rated sessions (8 up + 5 down = 13) out of all sessions (1,000)."""
    html = str(_feedback_section(_feedback(), is_admin=False))
    assert "Ratings adoption is low" in html
    assert "13 of 1,000 sessions" in html


def test_feedback_stats_seven_arg_fallback_constructs():
    """overview.py's failure fallback builds FeedbackStats positionally; the
    9->7 arg ripple must not regress into a TypeError. (Field 1 is now the
    session-grain denominator ``sessions``, per W1-2.)
    """
    fs = FeedbackStats(0, 0, 0, 0, 0, 0, 0)
    assert fs.sessions == 0
    assert fs.unrated_pct == 0


def test_feedback_section_has_no_fabricated_critic_tiles():
    html = str(_feedback_section(_feedback(), is_admin=True))
    # No fabricated critic numbers/labels (the old hardcoded-0% tiles).
    assert "Approval rate" not in html
    assert "Revision rate" not in html
    assert "automated Critic" not in html


def test_feedback_section_hides_system_quality_panel():
    """"System quality" (the Scorecard-link slot) is intentionally HIDDEN for
    now — commented out in overview.py per Macon (2026-06-26) — for both admin
    and non-admin. Restore by uncommenting the block in _feedback_section.
    """
    for is_admin in (True, False):
        html = str(_feedback_section(_feedback(), is_admin=is_admin))
        assert "System quality" not in html
        assert "/compass/quality/scorecard" not in html


def test_feedback_section_carries_low_adoption_note():
    # The note is audience-independent.
    for is_admin in (True, False):
        html = str(_feedback_section(_feedback(), is_admin=is_admin))
        assert "Ratings adoption is low" in html


# ── HM-R2: truthful labels ──────────────────────────────────────────


def test_top_states_relabeled_to_states_referenced():
    html = str(
        _what_people_ask_section(
            topics=[],
            districts=[],
            states=[{"state": "AK", "session_count": 3}],
        )
    )
    assert "Top states represented" in html
    assert "States referenced" not in html


def test_unique_visitors_label_is_kept_not_relabeled():
    """Decision-resolved 2026-06-21: the Umami card is a REAL unique-visitor
    count — keep the label, do NOT relabel it to "Visits".
    """
    card = str(_unique_visitors_card(VisitorsSummary(total=4321)))
    assert "Unique visitors" in card
    assert "4,321" in card


def test_unique_visitors_subtitle_is_explicit_site_level():
    """The windowed Umami tile names the source + the exact rolling window
    ("opened Compass · last 30 days") so it can't be confused with the chat-level
    "Chat users" tile."""
    card = str(_unique_visitors_card(VisitorsSummary(total=4321), "last 30 days"))
    assert "opened Compass · last 30 days" in card
    assert "4,321" in card


def test_unique_visitors_hidden_at_all_time():
    """All-time over-counts (Umami's hash salt resets each calendar month), so
    the tile shows an honest note + points at the Chat-users tile instead of a
    misleading number — and it is NOT the generic 'Under construction'
    placeholder."""
    card = str(_unique_visitors_card(None, "all time", is_all_time=True))
    assert "Not shown for all-time" in card
    assert "calendar month" in card
    assert "Chat users" in card
    assert "Under construction" not in card
    assert "—" in card  # no fabricated number


def test_engagement_section_hides_umami_at_all_time():
    """The all-time flag threads from the route through the section to the
    Umami tile."""
    html = str(
        _engagement_section(
            EngagementStats(1234, 2.0, 50, 20),
            None,
            visitors_is_all_time=True,
        )
    )
    assert "Not shown for all-time" in html


def test_sessions_tile_label_unchanged():
    """Sessions stays "Sessions" (not relabeled to "Visits")."""
    html = str(_engagement_section(EngagementStats(1234, 2.0, 50, 20), None))
    assert "Sessions" in html


# ── W1-5: the canonical "Multi-turn rate" tile shows the 3+ threshold ─


def test_multi_turn_tile_shows_3plus_threshold():
    """The canonical "Multi-turn rate" tile reflects the 3+ rate (the deeper
    signal), not the 2+ rate; the separate "Deep engagement"/"2+" tiles are
    gone so there is one canonical multi-turn number."""
    # Distinct 2+ (50) and 3+ (20) so the displayed value is unambiguous.
    html = str(_engagement_section(EngagementStats(1234, 2.0, 50, 20), None))
    assert "Multi-turn rate" in html
    assert "3+ user questions" in html
    # The old second tile and its 2+ subtitle are removed.
    assert "Deep engagement" not in html
    assert "2+ user questions" not in html
    # The tile renders the 3+ value (20%), not the 2+ value (50%).
    assert "20%" in html
    assert "50%" not in html


def test_returning_users_card_shows_count_and_since_launch_note():
    """W4-4 / G5: with identified visitors, the tile shows the returning count
    and an honest "since launch" caveat (the metric only counts post-deploy)."""
    html = to_xml(_returning_users_card(ReturningUsers(returning=7, identified=42)))
    assert "Returning users" in html
    assert "7" in html
    # Subtitle states the criterion (2+ conversations) + the since-launch basis,
    # instead of the old jargon "of N identified" denominator.
    assert "2+ conversations" in html
    assert "since launch" in html


def test_returning_users_card_under_construction_when_none_identified():
    """No identified visitors yet (pre-deploy / empty) → muted under-construction,
    not a bare 0 that reads as 'nobody returns'."""
    for ru in (None, ReturningUsers(returning=0, identified=0)):
        html = to_xml(_returning_users_card(ru))
        assert "Returning users" in html
        assert "Under construction" in html


def test_unique_people_card_shows_identified_count_and_window(monkeypatch):
    """#1808: the Chat-users tile renders ``identified`` (distinct chat users),
    labels it distinctly from the Umami "Unique visitors" tile, and names the
    active window in its subtitle."""
    html = to_xml(
        _unique_people_card(ReturningUsers(returning=7, identified=42), "last 30 days")
    )
    assert "Chat users" in html
    # The value is `identified` (42, distinct people) — NOT `returning` (7).
    assert "42" in html
    # Subtitle says what a "chat user" did (started a conversation) — distinct
    # from the Umami site-level "opened Compass" tile so the two aren't conflated.
    assert "started a conversation" in html
    # Subtitle reflects the active window (responds to the date pill).
    assert "last 30 days" in html


def test_unique_people_card_uses_windowed_value():
    """The tile shows whatever windowed `identified` it is handed, so a narrower
    pill (smaller count) and a wider pill (larger count) render different numbers
    — i.e. it responds to since/until rather than a fixed all-time number."""
    narrow = to_xml(_unique_people_card(ReturningUsers(returning=1, identified=5)))
    wide = to_xml(_unique_people_card(ReturningUsers(returning=9, identified=120)))
    assert "kpi-value\">5<" in narrow and "120" not in narrow
    assert "kpi-value\">120<" in wide


def test_unique_people_card_placeholder_when_none_identified():
    """No one identified yet (pre-launch / empty window) → calm placeholder with
    a 'begins at launch' note, never a scary bare 0."""
    for ru in (None, ReturningUsers(returning=0, identified=0)):
        html = to_xml(_unique_people_card(ru))
        assert "Chat users" in html
        assert "Under construction" in html
        assert "begins at launch" in html


def test_engagement_section_includes_unique_people_tile():
    """The windowed Chat-users tile is wired into the engagement KPI row."""
    html = to_xml(
        _engagement_section(
            EngagementStats(100, 2.0, 40, 20),
            None,
            None,
            ReturningUsers(returning=3, identified=20),
            visitors_subtitle="last 7 days",
            unique_people=ReturningUsers(returning=4, identified=37),
        )
    )
    assert "Chat users" in html
    assert "37" in html


def test_engagement_section_includes_returning_users_tile():
    html = to_xml(
        _engagement_section(
            EngagementStats(100, 2.0, 40, 20),
            None,
            None,
            ReturningUsers(returning=3, identified=20),
        )
    )
    assert "Returning users" in html


def test_get_returning_users_parses_counts(monkeypatch):
    """The query returns identified + returning(>1 session); the dataclass mirrors
    them. Mocks run_sql so no DB is needed."""
    from nctqai.services import compass_stats

    # The module-level TTL cache persists across tests; clear it so this read
    # actually hits the (stubbed) query rather than a value another test cached.
    compass_stats._RETURNING_USERS_CACHE.clear()
    captured = {}

    def fake_run_sql(sql, binds):
        captured["sql"] = sql
        return [{"identified": 20, "returning": 3}]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)
    result = compass_stats.get_returning_users()
    assert result.identified == 20 and result.returning == 3
    # Counts returning = visitors with MORE THAN ONE session, and never the
    # anonymous NULL tail.
    assert "sessions > 1" in captured["sql"]
    assert "visitor_id IS NOT NULL" in captured["sql"]


def test_get_returning_users_caches_within_ttl(monkeypatch):
    """#1808: get_returning_users caches by normalized since~until, so a second
    call with equivalent bounds within the TTL reuses the result instead of
    re-querying. This is what collapses the Overview's two identical all-time
    reads into one DB hit."""
    from nctqai.services import compass_stats

    compass_stats._RETURNING_USERS_CACHE.clear()
    calls = {"n": 0}

    def fake_run_sql(sql, binds):
        calls["n"] += 1
        return [{"identified": 20, "returning": 3}]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)
    first = compass_stats.get_returning_users(None, None)
    second = compass_stats.get_returning_users(None, None)
    assert first == second
    # The second call within the TTL must not re-query.
    assert calls["n"] == 1


def test_sessions_tile_renders_trend_when_provided():
    """G2: a provided prior-period caption renders on the Sessions tile; when
    omitted (e.g. all-time range) no fabricated trend appears."""
    with_trend = str(
        _engagement_section(
            EngagementStats(1234, 2.0, 50, 20), None, "▲ 12% vs prior period"
        )
    )
    assert "▲ 12% vs prior period" in with_trend

    without_trend = str(_engagement_section(EngagementStats(1234, 2.0, 50, 20), None))
    assert "vs prior period" not in without_trend
