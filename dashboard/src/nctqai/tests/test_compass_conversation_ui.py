"""Regression tests for the compact Compass conversation review UI."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fasthtml.common import to_xml

from compass_backend.quality.scorecard_models import ConversationTurn, Verdict
from nctqai.components.compass.conversation_detail import (
    _verdicts_for_rendered_turns,
    sticky_header,
)
from nctqai.components.compass.conversation_list import (
    ConversationListItem,
    triage_results,
)
from nctqai.components.compass.turn_card import TurnCard
from nctqai.components.nav import SECTIONS
from nctqai.models.compass import ConversationSummary


@dataclass
class _Msg:
    """Minimal stand-in for a ChatMessage row (only .content is read here)."""

    content: str
    role: str = "user"


def test_compass_subnav_exposes_conversations_tab():
    assert ("Conversations", "/compass/conversations") in SECTIONS["compass"]


def test_compass_subnav_hides_operations_tab():
    assert not any(url == "/compass/operations" for _, url in SECTIONS["compass"])


def test_conversation_list_item_only_calls_out_review_signals():
    html = str(
        ConversationListItem(
            session_id="abc",
            preview="What went wrong with this answer?",
            district_name="Anchorage School District",
            state="AK",
            thumbs_down_count=1,
        )
    )

    assert "flagged" in html
    assert "Data Lookup" not in html
    assert "compass-dot" not in html


def test_conversation_context_is_labeled_and_timestamps_name_et():
    html = to_xml(
        ConversationListItem(
            session_id="abc",
            preview="A question",
            timestamp=datetime(2026, 7, 9, 14, 30, tzinfo=UTC),
            district_name="Brevard Public Schools",
            state="FL",
        )
    )
    assert "District context: Brevard Public Schools, FL" in html
    assert "ET" in html
    assert "not the user" in html and "identity or location" in html


def test_conversation_context_uses_a_count_for_multiple_districts():
    html = to_xml(
        ConversationListItem(
            session_id="abc",
            preview="A question",
            district_name="Do not show this district",
            district_count=17,
        )
    )
    assert "17 districts in context" in html
    assert "Do not show this district" not in html


def test_conversation_list_item_wires_detail_loading_indicator():
    """The row must point hx-indicator at #compass-detail so the existing
    `.compass-detail.htmx-request` fade (theme.py §26q) fires while the detail
    fetches — otherwise the pane sits unchanged and reads as frozen on slower
    loads. Pairs with the swap target so the cue and the content land together.
    """
    html = str(ConversationListItem(session_id="abc", preview="A question"))
    assert 'hx-indicator="#compass-detail"' in html
    assert 'hx-target="#compass-detail"' in html


def test_list_item_fail_signal_is_thumbs_down_only():
    """The list row's only fail signal is the REAL thumbs-down count — there is
    no verdict-driven 'fail' badge (the dead ``verdict_status`` param was
    removed). With no thumbs-down, no fail badge and no has-fail class render.
    """
    no_signal = str(
        ConversationListItem(
            session_id="abc",
            preview="A question",
            thumbs_down_count=0,
        )
    )
    assert ">fail<" not in no_signal
    assert "compass-badge-rejected" not in no_signal
    assert "has-fail" not in no_signal

    # The real signal still fires.
    real_flag = str(
        ConversationListItem(
            session_id="abc",
            preview="A question",
            thumbs_down_count=1,
        )
    )
    assert "flagged" in real_flag
    assert "has-fail" in real_flag


def test_sticky_header_suppresses_no_verdict_badge():
    convo = ConversationSummary(
        session_id="abc",
        first_message="What was the starting salary?",
        last_verdict_approved=None,
    )

    html = str(sticky_header(convo))

    assert "What was the starting salary?" in html
    assert "No verdict" not in html


def test_turn_card_renders_markdown_table_without_pipeline_placeholder():
    # TurnCard's outer Div carries id="turn-{n}" for the navigator pip
    # anchors, and FastHTML's str() returns the id when one is present —
    # use to_xml() to get the rendered HTML.
    html = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Show the result",
            assistant_response="| District | Value |\n| --- | --- |\n| Anchorage | 10 |",
        )
    )

    assert "<table>" in html
    assert "<th>District</th>" in html
    assert "compass-pipeline" not in html
    assert 'id="turn-1"' in html


# ── W2-0/W2-2/W2-3: snapshot table, citation links, CSV download ───────────


def test_turn_card_renders_snapshot_table_from_saved_rows():
    """W2-0: the saved snapshot ``result.rows`` render as a structured table
    (readable subset of columns), additive to the prose answer."""
    html = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Which districts pay extra?",
            assistant_response="Here are the results.",
            snapshot_rows=[
                {
                    "district_name": "District of Columbia Public Schools",
                    "state": "DC",
                    "display_value": "Yes",
                    "value": "Yes",
                    "coverage_state": "covered",
                    "citation_markers": [1],
                },
                {
                    "district_name": "Chicago Public Schools",
                    "state": "IL",
                    "display_value": "No",
                    "value": "No",
                    "coverage_state": "covered",
                    "citation_markers": [2],
                },
            ],
        )
    )
    assert "<table" in html
    assert "Data table (2 rows)" in html
    assert "District of Columbia Public Schools" in html
    assert "Chicago Public Schools" in html
    # Verbatim display value, not recomputed.
    assert ">Yes<" in html
    # Empty rows → no table (and no crash).
    none_html = to_xml(
        TurnCard(turn_number=1, user_message="Q", assistant_response="A", snapshot_rows=[])
    )
    assert "compass-snapshot-table" not in none_html


def test_turn_card_linkifies_known_citation_markers_only():
    """W2-3: a ``[1]`` marker with a matching citation becomes a link to its URL;
    a marker with no citation stays plain text (no fabricated link)."""
    html = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Q",
            assistant_response="DC pays extra [1] but nothing matches [9].",
            citations=[
                {
                    "marker": 1,
                    "url": "https://nctqdocs.example/dc_salary.pdf",
                    "title": "DC Salary Schedule, 2024-2025",
                },
            ],
        )
    )
    # Known marker → anchor with the resolved href + title.
    assert 'href="https://nctqdocs.example/dc_salary.pdf"' in html
    assert "compass-citation-link" in html
    assert "DC Salary Schedule, 2024-2025" in html
    assert ">[1]</a>" in html
    # Unknown marker stays plain text — not wrapped in an anchor.
    assert "[9]" in html
    assert ">[9]</a>" not in html


def test_turn_card_unresolved_citation_title_is_unknown_source():
    """W2-3: a citation with a URL but no title surfaces 'Unknown Source' honestly
    in the link title — no invented prose. A citation with no URL stays plain."""
    titled = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Q",
            assistant_response="See [1].",
            citations=[{"marker": 1, "url": "https://example.com/doc.pdf", "title": ""}],
        )
    )
    assert 'title="Unknown Source"' in titled

    # No URL → nothing resolvable, marker stays plain text.
    no_url = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Q",
            assistant_response="See [1].",
            citations=[{"marker": 1, "url": "", "title": "Has a title"}],
        )
    )
    assert ">[1]</a>" not in no_url


def test_csv_download_link_embeds_verbatim_csv_export():
    """W2-2: the Download CSV affordance carries the saved csv_export columns and
    rows as a data: URI — exact bytes, no server route."""
    from nctqai.components.compass.conversation_detail import csv_download_link

    link = csv_download_link(
        {
            "columns": ["district_name", "state", "value"],
            "rows": [
                {"district_name": "DC Public Schools", "state": "DC", "value": "Yes"},
            ],
        },
        filename="extra-pay-turn1.csv",
    )
    assert link is not None
    html = to_xml(link)
    assert "Download CSV" in html
    assert 'download="extra-pay-turn1.csv"' in html
    assert 'href="data:text/csv' in html
    # The verbatim header + value are carried in the encoded payload (quote keeps
    # unreserved chars like underscores literal).
    assert "district_name" in html
    assert "Yes" in html


def test_csv_download_link_none_when_no_export():
    """W2-2: no columns/rows → no affordance (never an empty download)."""
    from nctqai.components.compass.conversation_detail import csv_download_link

    assert csv_download_link(None, filename="x.csv") is None
    assert csv_download_link({"columns": [], "rows": []}, filename="x.csv") is None
    assert (
        csv_download_link({"columns": ["a"], "rows": []}, filename="x.csv") is None
    )


def test_turn_card_renders_passed_csv_download_affordance():
    """The detail layer builds the per-turn CSV link; the turn card renders it."""
    from nctqai.components.compass.conversation_detail import csv_download_link

    link = csv_download_link(
        {"columns": ["state"], "rows": [{"state": "DC"}]},
        filename="q-turn1.csv",
    )
    html = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Q",
            assistant_response="A",
            csv_download=link,
        )
    )
    assert "Download CSV" in html
    assert "compass-snapshot-actions" in html


# ── #1809: saved-chart rendering in the conversation replay ────────────────

# Mirrors a real persisted single-series bar chart (ChartArtifact.model_dump):
# a ranking/count chart carries top-level ``points`` and an empty ``series``.
_SAMPLE_BAR_CHART = {
    "artifact_type": "bar_chart",
    "title": "Starting salary by district",
    "x_axis_label": "District",
    "y_axis_label": "Starting salary",
    "points": [
        {
            "label": "DC Public Schools",
            "value": 63373.0,
            "district_id": 1,
            "academic_year": None,
            "citation_markers": [1],
        },
        {
            "label": "Chicago Public Schools",
            "value": 58306.0,
            "district_id": 2,
            "academic_year": None,
            "citation_markers": [2],
        },
    ],
    "series": [],
    "show_legend": False,
}

# Mirrors a real persisted multi-metric chart: top-level ``points`` is empty and
# each metric is its own ``series`` entry, with ``show_legend`` true.
_SAMPLE_MULTI_CHART = {
    "artifact_type": "bar_chart",
    "title": "Selected metrics by district",
    "x_axis_label": "District",
    "y_axis_label": "Value",
    "points": [],
    "series": [
        {
            "label": "Starting salary",
            "metric_id": 10,
            "points": [{"label": "DC Public Schools", "value": 63373.0}],
        },
        {
            "label": "Max salary",
            "metric_id": 11,
            "points": [{"label": "DC Public Schools", "value": 99000.0}],
        },
    ],
    "show_legend": True,
}


def test_turn_card_renders_chart_from_saved_artifact():
    """#1809: a snapshot ChartArtifact renders a self-contained SVG bar chart,
    additive to the prose/table — and never drops the existing table/citation."""
    html = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Top districts by starting salary?",
            assistant_response="Here are the top districts [1].",
            chart=_SAMPLE_BAR_CHART,
            snapshot_rows=[
                {
                    "district_name": "DC Public Schools",
                    "state": "DC",
                    "display_value": "$63,373",
                    "coverage_state": "covered",
                    "citation_markers": [1],
                }
            ],
            citations=[
                {"marker": 1, "url": "https://example.com/dc.pdf", "title": "DC Schedule"}
            ],
        )
    )
    # The chart is a real SVG bar chart with the artifact's title + bars.
    assert "<svg" in html
    assert "compass-snapshot-chart" in html
    assert "Starting salary by district" in html
    assert "<rect" in html
    # The grounded label appears in a <title> tooltip (formatted verbatim value).
    assert "63,373" in html
    # No regression: the snapshot table and the citation link still render.
    assert "compass-snapshot-table" in html
    assert "compass-citation-link" in html


def test_turn_card_renders_multi_series_chart_legend():
    """#1809: a multi-metric chart renders one series per metric with a legend."""
    html = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Starting and max salary?",
            assistant_response="Here you go.",
            chart=_SAMPLE_MULTI_CHART,
        )
    )
    assert "<svg" in html
    assert "compass-chart-legend" in html
    assert "Starting salary" in html
    assert "Max salary" in html


def test_turn_card_omits_chart_when_absent_or_empty():
    """#1809: no chart (older pre-snapshot turn) renders nothing — never broken.
    An artifact with no plottable points is also dropped silently."""
    none_html = to_xml(
        TurnCard(turn_number=1, user_message="Q", assistant_response="A")
    )
    assert "<svg" not in none_html
    assert "compass-snapshot-chart" not in none_html

    empty_chart = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Q",
            assistant_response="A",
            chart={"artifact_type": "bar_chart", "title": "x", "points": [], "series": []},
        )
    )
    assert "<svg" not in empty_chart


def test_line_chart_artifact_renders_polyline():
    """#1809: a line_chart artifact renders a <polyline> (bar/line parity with
    the chat front-end), not bars."""
    line_chart = dict(_SAMPLE_BAR_CHART, artifact_type="line_chart")
    html = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Salary trend?",
            assistant_response="Trend below.",
            chart=line_chart,
        )
    )
    assert "<polyline" in html
    assert "<rect" not in html


def test_build_detail_forwards_saved_chart_to_turn_card(monkeypatch):
    """#1809 full wiring: ``build_detail`` extracts ``snapshot.chart`` and passes
    it to ``TurnCard`` — the exact gap the issue describes (data present in the
    snapshot, never wired through). Stubs the DB loaders so the test is pure."""
    from nctqai.components.compass import conversation_detail as cd
    from nctqai.models.compass import ChatMessage, ConversationDetail

    assistant_id = 42
    convo_detail = ConversationDetail(
        session_id="sess-1",
        messages=[
            ChatMessage(id=41, role="user", content="Top districts by salary?"),
            ChatMessage(id=assistant_id, role="assistant", content="Here you go [1]."),
        ],
    )
    monkeypatch.setattr(cd, "get_conversation", lambda sid: convo_detail)
    monkeypatch.setattr(cd, "get_session_feedback", lambda sid: [])

    def _raise_keyerror(sid):
        raise KeyError(sid)

    monkeypatch.setattr(cd, "load_conversation_with_verdicts", _raise_keyerror)
    monkeypatch.setattr(
        cd, "_snapshots_by_msg_id", lambda sid: {assistant_id: {"chart": _SAMPLE_BAR_CHART}}
    )

    html = to_xml(cd.build_detail("sess-1", convo=None))
    assert "<svg" in html
    assert "Starting salary by district" in html
    assert "<rect" in html


# ── Part A: inline L1 verdicts per turn (DASH-R5) ──────────────────────────


def _verdict(outcome: str = "pass", criterion: str = "C-sort") -> Verdict:
    return Verdict(
        criterion_id=criterion,
        judge_source="judge_prompt",
        outcome=outcome,
        reason="because",
    )


def test_verdict_alignment_survives_short_message_shift():
    """The detail view does NOT drop short (<=5 char) messages, but the backend
    verdict reader DOES drop them before pairing — which shifts which user pairs
    with which assistant. A positional counter would misattach; the content-match
    pointer must land the verdict on the correct card.

    Rendered cards: [(U1="yes", A1), (U2=long, A2)]
    Backend turns (U1 dropped): [turn1 = (U2=long, A2)]  → verdict belongs to card 2.
    """
    turns = [
        (_Msg("yes"), _Msg("Answer one", role="assistant")),
        (_Msg("What are the top 5 districts?"), _Msg("Answer two", role="assistant")),
    ]
    backend_turns = [
        ConversationTurn(
            turn_index=1,
            user_text="What are the top 5 districts?",
            assistant_text="Answer two",
            verdicts=[_verdict("fail")],
        )
    ]

    aligned = _verdicts_for_rendered_turns(turns, backend_turns)

    assert len(aligned) == 2
    # Card 1 (the short-message turn the backend dropped) gets no verdicts.
    assert aligned[0] is None
    # Card 2 (the real backend turn 1) carries the fail verdict — NOT off by one.
    assert aligned[1] is not None
    assert any(v.outcome == "fail" for v in aligned[1])


def test_verdict_alignment_orphan_turn_gets_no_verdicts():
    """An orphan user message (no assistant) renders as a card but matches no
    backend turn → None, so VerdictList is not rendered for it."""
    turns = [
        (_Msg("Hello there friend"), _Msg("Hi", role="assistant")),
        (_Msg("Dangling question"), None),
    ]
    backend_turns = [
        ConversationTurn(
            turn_index=1,
            user_text="Hello there friend",
            assistant_text="Hi",
            verdicts=[_verdict("pass")],
        )
    ]
    aligned = _verdicts_for_rendered_turns(turns, backend_turns)
    assert aligned[0] is not None and aligned[0][0].outcome == "pass"
    assert aligned[1] is None


def test_verdict_join_is_coupled_on_exact_string_equality():
    """Coupling guard for the inline-verdict join (#12).

    The detail pane and the backend verdict reader are SEPARATE readers; they
    pair a verdict to a card only when their user/assistant strings are EXACTLY
    equal (``==``). Feed both the same raw strings: the exact pair attaches its
    verdict; a near-match (one trailing space — a stray whitespace divergence
    between the two readers' filters) attaches None. This pins the join to exact
    equality, so any future loosening (strip/normalize on one side only) that
    would silently misattach trips this test.
    """
    user_raw = "Which districts pay the most for a master's degree?"
    asst_raw = "Here are the top districts by master's-degree pay supplement."

    # Both readers agree exactly → the verdict attaches.
    exact_turns = [(_Msg(user_raw), _Msg(asst_raw, role="assistant"))]
    backend = [
        ConversationTurn(
            turn_index=1,
            user_text=user_raw,
            assistant_text=asst_raw,
            verdicts=[_verdict("fail")],
        )
    ]
    exact = _verdicts_for_rendered_turns(exact_turns, backend)
    assert exact[0] is not None
    assert any(v.outcome == "fail" for v in exact[0])

    # One reader has a trailing space the other doesn't → no attach (None),
    # NOT a wrong/misattached verdict. (Same backend turns reused.)
    near_turns = [(_Msg(user_raw + " "), _Msg(asst_raw, role="assistant"))]
    near = _verdicts_for_rendered_turns(near_turns, backend)
    assert near[0] is None


@pytest.mark.live_db
def test_verdict_join_couples_on_real_staging_strings(request):
    """Against staging: the two readers' real strings DO line up, so a session
    with turn verdicts attaches at least one verdict end-to-end (the coupling
    that the unit test pins is also true on live data, not just fixtures).

    Discovers the session at runtime (no hardcoded ID to drift) — the top
    verdict-bearing session in compass.verdicts.

    Run with: op run --env-file=.env.op -- \
        env PG_SCHEMA=compass PYTHONPATH=src uv run pytest \
        src/nctqai/tests/test_compass_conversation_ui.py --run-live-db
    """
    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB smoke tests")

    from nctqai.components.compass.conversation_detail import group_into_turns
    from nctqai.db import COMPASS_SCHEMA, run_sql
    from nctqai.services.compass_conversations import get_conversation
    from nctqai.services.compass_quality.loaders import (
        load_conversation_with_verdicts,
    )

    rows = run_sql(
        f"""
        SELECT v.session_id::text AS session_id, COUNT(*) AS n
          FROM {COMPASS_SCHEMA}.verdicts v
          JOIN {COMPASS_SCHEMA}.chat_sessions s
            ON s.session_id::text = v.session_id::text
         WHERE v.session_id IS NOT NULL
         GROUP BY v.session_id
         ORDER BY n DESC
         LIMIT 1
        """
    )
    assert rows, "expected at least one verdict-bearing session on staging"
    session_id = rows[0]["session_id"]

    convo = get_conversation(session_id)
    assert convo is not None
    turns = group_into_turns(convo.messages)
    backend = load_conversation_with_verdicts(session_id).turns

    aligned = _verdicts_for_rendered_turns(turns, backend)
    # The exact-equality coupling holds on real strings: at least one rendered
    # turn matched a backend turn and carries its verdict list.
    assert any(v is not None for v in aligned), (
        "no rendered turn matched a backend turn — the two readers' strings "
        "diverged (the coupling the unit test pins broke on live data)"
    )


def test_turn_card_renders_inline_verdict_list_for_paired_turn():
    """A paired turn (l1_verdicts is a list, even empty) shows VerdictList; a
    failing verdict auto-expands. An unmatched card (None) shows no verdict block."""
    with_verdicts = to_xml(
        TurnCard(
            turn_number=1,
            user_message="Q",
            assistant_response="A",
            l1_verdicts=[_verdict("fail")],
        )
    )
    assert "quality-verdict-list" in with_verdicts
    assert "compass-turn-verdicts" in with_verdicts
    assert "open" in with_verdicts  # auto-expanded on fail

    # Empty list = paired turn with zero verdicts → honest empty state, shown.
    empty = to_xml(
        TurnCard(turn_number=1, user_message="Q", assistant_response="A", l1_verdicts=[])
    )
    assert "No verdicts recorded for this turn." in empty

    # None = unmatched/orphan card → no verdict block at all.
    none_block = to_xml(
        TurnCard(turn_number=1, user_message="Q", assistant_response="A", l1_verdicts=None)
    )
    assert "compass-turn-verdicts" not in none_block
    assert "quality-verdict-list" not in none_block


# ── Part B: triage quick-filter tabs (TRIAGE-R1) ───────────────────────────


def test_triage_tabs_render_with_live_counts_and_aria():
    html = to_xml(
        triage_results(
            tab_strip_items=[],
            tab_counts={
                "all": 2229,
                "thumbs-down": 0,
                "has-table": 1213,
                "has-csv-export": 1209,
                "has-chart": 246,
                "unreviewed": 433,
            },
            active_tab="unreviewed",
            filter_state={},
        )
    )
    # Every tab drives the list partial with its own saved-artifact signal.
    for key in (
        "all",
        "thumbs-down",
        "has-table",
        "has-csv-export",
        "has-chart",
        "unreviewed",
    ):
        assert f"/compass/conversations/list?tab={key}" in html
    # Live counts render comma-formatted.
    assert "1,213" in html
    assert "2,229" in html
    # The active tab is marked.
    assert "triage-tab active" in html
    assert 'aria-pressed="true"' in html
    # The swap region announces to screen readers.
    assert 'id="compass-convo-results"' in html
    assert 'aria-live="polite"' in html
    # Teal accent only — no brand-blue token leaks into the new markup.
    assert "brand-blue" not in html
    # The active tab is carried in a hidden input inside the swapped region.
    assert 'name="tab"' in html


def test_triage_tab_with_none_count_renders_no_chip():
    """has-table over 'All time' is too costly to count live → None, so the tab
    shows no chip number rather than a fabricated one (HM-R1: never fake)."""
    html = to_xml(
        triage_results(
            tab_strip_items=[],
        tab_counts={
            "all": 5,
            "thumbs-down": 1,
            "has-table": None,
            "has-csv-export": None,
            "has-chart": None,
            "unreviewed": 2,
        },
            active_tab="all",
            filter_state={},
        )
    )
    assert "Has data table" in html  # the tab still renders and is usable
    # Artifact tabs omit all-time chips rather than fabricate counts.
    assert html.count("triage-tab-count") == 3


# ── Part B: triage-tab service predicates (SQL composition, no DB) ──────────


def _capture_sql(monkeypatch):
    """Patch run_sql in the conversations service; capture (sql, binds)."""
    from nctqai.services import compass_conversations as svc

    calls: list[tuple[str, tuple]] = []

    def fake_run_sql(sql, binds=()):
        calls.append((sql, binds))
        return []  # empty result; we only assert on the composed SQL

    monkeypatch.setattr(svc, "run_sql", fake_run_sql)
    # The list path enriches rows via snapshot memory; stub it to a no-op.
    monkeypatch.setattr(
        svc, "_add_primary_district_from_snapshot_memory", lambda rows: rows
    )
    # These SQL-shape tests assert the migration-208 artifact path; treat the
    # v2 summary as available so the capability guard does not degrade to FALSE.
    monkeypatch.setattr(svc, "artifact_summary_v2_available", lambda: True)
    return svc, calls


def test_list_conversations_has_table_tab_adds_snapshot_predicate(monkeypatch):
    svc, calls = _capture_sql(monkeypatch)
    svc.list_conversations(tab="has-table", limit=10)
    sql = calls[-1][0]
    # PR-2 (#1788): the has-table predicate now probes the precomputed
    # snapshot_summary->>'has_table' boolean (migration 163) instead of
    # detoasting the full snapshot JSONB and measuring result.rows length.
    assert "snapshot_summary->>'has_table'" in sql


def test_list_conversations_artifact_tabs_use_their_own_snapshot_signals(monkeypatch):
    svc, calls = _capture_sql(monkeypatch)
    svc.list_conversations(tab="has-csv-export", limit=10)
    csv_sql = calls[-1][0]
    svc.list_conversations(tab="has-chart", limit=10)
    chart_sql = calls[-1][0]
    assert "snapshot_summary->>'has_csv_export'" in csv_sql
    assert "snapshot_summary->>'has_chart'" in chart_sql


def test_list_conversations_unreviewed_tab_excludes_verdicts_and_thumbs(monkeypatch):
    svc, calls = _capture_sql(monkeypatch)
    svc.list_conversations(tab="unreviewed", limit=10)
    sql = calls[-1][0]
    # No turn verdict (NOT EXISTS over verdicts) AND no thumbs. Both are now
    # session-level WHERE predicates: the feedback rollup is a one-row LATERAL,
    # so its columns filter directly — no GROUP BY / HAVING (perf: pick-then-enrich).
    assert "NOT EXISTS" in sql
    assert "scope = 'turn'" in sql
    assert "fb.thumbs_up_count = 0 AND fb.thumbs_down_count = 0" in sql
    # The uuid cast is CASE-guarded (never casts the non-UUID legacy ids).
    assert "CASE" in sql and "ELSE NULL" in sql


def test_list_conversations_thumbs_down_tab_filters_on_feedback(monkeypatch):
    svc, calls = _capture_sql(monkeypatch)
    svc.list_conversations(tab="thumbs-down", limit=10)
    sql = calls[-1][0]
    # Thumbs-down is now a WHERE on the one-row feedback LATERAL (was a HAVING).
    assert "fb.thumbs_down_count > 0" in sql


def test_list_conversations_picks_newest_then_enriches(monkeypatch):
    """Perf: pick the newest sessions by created_at FIRST (index-driven), then
    enrich only those — instead of aggregating all ~45k sessions and sorting by
    MAX(m.timestamp). The list orders by s.created_at (consistent with the range
    pill); message_count + first_message are computed over the picked set."""
    svc, calls = _capture_sql(monkeypatch)
    svc.list_conversations(limit=10)
    sql = calls[-1][0]
    assert "WITH picked AS" in sql
    assert "ORDER BY s.created_at DESC" in sql  # index-driven pick
    assert "FROM picked p" in sql               # enrichment over the picked set
    # the old all-table aggregate sort by last-message-time is gone
    assert "MAX(m.timestamp)" not in sql


def test_count_by_tab_all_time_omits_has_table_count(monkeypatch):
    """With since=None (All time), has-table is too costly to count live → None,
    and the SQL must NOT run the snapshot scan (FALSE AS has_table)."""
    svc, calls = _capture_sql(monkeypatch)
    counts = svc.count_conversations_by_tab(since=None)
    sql = calls[-1][0]
    assert "FALSE AS has_table" in sql
    assert "snapshot_summary->>'has_table'" not in sql
    assert counts["has-table"] is None


def test_count_by_tab_bounded_range_computes_has_table(monkeypatch):
    from datetime import datetime

    svc, calls = _capture_sql(monkeypatch)
    svc.count_conversations_by_tab(since=datetime(2026, 6, 1))
    sql = calls[-1][0]
    assert "snapshot_summary->>'has_table'" in sql
    assert "FALSE AS has_table" not in sql


def test_sidebar_filters_target_the_atomic_results_region():
    """Every filter/range/tab trigger must swap #compass-convo-results (strip +
    list together) — never just the inner list, or the partial's full wrapper
    would nest inside the list and the counts would go stale.
    """
    from nctqai.components.compass.conversation_list import ConversationSidebar

    html = to_xml(
        ConversationSidebar(
            conversations=[],
            range_value="7d",
            tab_value="all",
            tab_counts={"all": 0, "thumbs-down": 0, "has-table": 0, "unreviewed": 0},
        )
    )
    # The list partial returns #compass-convo-results, so every hx_target that
    # drives it must point there. No trigger should target the inner list.
    assert "#compass-convo-results" in html
    assert 'hx-target="#compass-convo-list"' not in html


def test_smart_search_bar_uses_live_sidebar_filter_controls():
    """Submitting a search must NOT reset the date window or triage tab.

    The top search form itself stays small; the live sidebar controls are
    associated to it by ``form=...`` so HTMX swaps can update the range/date/tab
    fields without re-rendering the search bar.
    """
    from nctqai.components.compass.conversation_list import (
        SMART_SEARCH_FORM_ID,
        ConversationSidebar,
    )
    from nctqai.routes.compass.conversations import _smart_search_bar

    search_html = to_xml(
        _smart_search_bar(
            current_value="salary",
            not_found_uuid=None,
        )
    )
    assert f'id="{SMART_SEARCH_FORM_ID}"' in search_html
    assert 'name="q"' in search_html
    # No stale hidden copies in the top bar. These values change inside the
    # HTMX-swapped sidebar region.
    assert 'name="range"' not in search_html
    assert 'name="tab"' not in search_html
    assert 'name="from"' not in search_html
    assert 'name="to"' not in search_html

    sidebar_html = to_xml(
        ConversationSidebar(
            conversations=[],
            feedback_value="thumbs_down",
            range_value="30d",
            custom_since="2026-05-01",
            custom_until="2026-05-31",
            tab_value="has-table",
            tab_counts={"all": 0, "has-table": 0},
        )
    )
    assert f'form="{SMART_SEARCH_FORM_ID}"' in sidebar_html
    assert 'name="feedback"' in sidebar_html and 'value="thumbs_down"' in sidebar_html
    assert 'name="range"' in sidebar_html and 'value="30d"' in sidebar_html
    assert 'name="tab"' in sidebar_html and 'value="has-table"' in sidebar_html
    assert 'name="from"' in sidebar_html and 'value="2026-05-01"' in sidebar_html
    assert 'name="to"' in sidebar_html and 'value="2026-05-31"' in sidebar_html
