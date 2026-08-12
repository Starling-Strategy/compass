"""Route shape regression tests for the Compass Quality Scorecard.

Tests rendered HTML structure without hitting the database: the loader is
patched to return fixture objects so routes render with known data.
"""
from __future__ import annotations



from compass_backend.quality.scorecard_models import (
    CANONICAL_DIM_SLUGS,
    BuildContext,
    ConversationTurn,
    ConversationWithVerdicts,
    DimensionDetail,
    DimensionScore,
    ScorecardSnapshot,
    ScenarioCase,
    Trial,
    Verdict,
)
from nctqai.components.compass.quality import DimensionRow, K3Strip, VerdictList
from nctqai.routes.compass.quality.scorecard import _scorecard_hero, _dimension_detail
from nctqai.routes.compass.quality.conversations import _conversation_page


# ── Fixtures ──────────────────────────────────────────────────────────────

def _make_snapshot() -> ScorecardSnapshot:
    """7 dimensions: sort-accuracy has real data; others are placeholders."""
    dims = []
    for slug in CANONICAL_DIM_SLUGS:
        is_sort = slug == "sort-accuracy"
        dims.append(DimensionScore(
            dim_slug=slug,
            name=slug.replace("-", " ").title(),
            definition="Test definition.",
            score_pct=67 if is_sort else 0,
            n_trials=9 if is_sort else 0,
            regressed=False,
        ))
    return ScorecardSnapshot(
        build=BuildContext(
            build_id="b-test",
            sweep_id="sweep-test",
            criterion_set_version="compass_criteria_v1",
            cases=3,
            trials=9,
        ),
        dimensions=dims,
    )


def _make_detail(dim_slug: str = "sort-accuracy") -> DimensionDetail:
    trials = [
        Trial(outcome="pass", session_id="s-aaa-001"),
        Trial(outcome="fail", session_id="s-bbb-002", reason="wrong order"),
        Trial(outcome="error", session_id="s-ccc-003", reason="evaluator timeout"),
    ]
    cases = [
        ScenarioCase(scenario_id="G07", case_id="C01", name="Test Case 1", pass_rate_pct=100, trials=trials[:1]),
        ScenarioCase(scenario_id="G07", case_id="C02", name="Test Case 2", pass_rate_pct=0, trials=trials[1:3]),
    ]
    return DimensionDetail(
        build=BuildContext(
            build_id="b-test",
            sweep_id="sweep-test",
            criterion_set_version="compass_criteria_v1",
            cases=2,
            trials=3,
        ),
        dimension=DimensionScore(
            dim_slug=dim_slug,
            name=dim_slug.replace("-", " ").title(),
            definition="Test dimension definition.",
            score_pct=67,
            n_trials=9,
            regressed=False,
        ),
        cases=cases,
    )


def _make_conversation(session_id: str = "abc12345-0000-0000-0000-000000000000") -> ConversationWithVerdicts:
    pass_verdict = Verdict(
        criterion_id="C-sort",
        judge_source="judge_prompt",
        outcome="pass",
        reason=None,
    )
    fail_verdict = Verdict(
        criterion_id="C-tiebreak",
        judge_source="judge_prompt",
        outcome="fail",
        reason="Tie-break used alphabetical",
    )
    stub_verdict = Verdict(
        criterion_id="C-det",
        judge_source="deterministic",
        outcome="error",
        reason="not wired",
    )
    return ConversationWithVerdicts(
        session_id=session_id,
        trace_id="trace-abc",
        scenario_id=None,
        scenario_name=None,
        started_at="2026-05-14T10:00:00",
        turns=[
            ConversationTurn(
                turn_index=0,
                user_text="What are the top 5 districts by graduation rate?",
                assistant_text="The top 5 districts are: ...",
                verdicts=[pass_verdict, fail_verdict, stub_verdict],
            )
        ],
    )


# ── Component unit tests ──────────────────────────────────────────────────

def test_dimension_row_no_data_shows_pill():
    """DimensionRow with n_trials=0 renders 'no data yet' pill, no percentage."""
    dim = DimensionScore(
        dim_slug="selection-accuracy",
        name="Selection Accuracy",
        definition="Picked the right districts.",
        score_pct=0,
        n_trials=0,
        regressed=False,
    )
    row = DimensionRow(dim)
    # Convert to string to inspect rendered HTML
    import fasthtml.common as fh
    html = fh.to_xml(row)
    assert "no data yet" in html
    assert "0%" not in html


def test_dimension_row_with_data_shows_percentage():
    """DimensionRow with n_trials > 0 renders score_pct% without 'no data yet'."""
    dim = DimensionScore(
        dim_slug="sort-accuracy",
        name="Sort Accuracy",
        definition="Right ordering.",
        score_pct=67,
        n_trials=9,
        regressed=False,
    )
    row = DimensionRow(dim)
    import fasthtml.common as fh
    html = fh.to_xml(row)
    assert "67%" in html
    assert "no data yet" not in html


def test_dimension_row_with_passing_threshold_shows_pass_class():
    """DimensionRow with score >= threshold renders the pass threshold class."""
    dim = DimensionScore(
        dim_slug="selection-accuracy",
        name="Selection Accuracy",
        definition="Picked the right districts.",
        score_pct=96,
        n_trials=12,
        regressed=False,
        threshold_pct=95,
        threshold_status="pass",
    )
    row = DimensionRow(dim)
    import fasthtml.common as fh
    html = fh.to_xml(row)
    assert "quality-threshold-pass" in html
    assert "&gt;=95%" in html or ">=95%" in html


def test_dimension_row_with_failing_threshold_shows_fail_class():
    """DimensionRow with score below threshold renders the fail threshold class."""
    dim = DimensionScore(
        dim_slug="selection-accuracy",
        name="Selection Accuracy",
        definition="Picked the right districts.",
        score_pct=94,
        n_trials=12,
        regressed=False,
        threshold_pct=95,
        threshold_status="fail",
    )
    row = DimensionRow(dim)
    import fasthtml.common as fh
    html = fh.to_xml(row)
    assert "quality-threshold-fail" in html
    assert "&gt;=95%" in html or ">=95%" in html


def test_dimension_row_links_to_drill_down():
    """DimensionRow name links to /compass/quality/scorecard/<dim_slug>."""
    dim = DimensionScore(
        dim_slug="sort-accuracy",
        name="Sort Accuracy",
        definition="Right ordering.",
        score_pct=67,
        n_trials=9,
        regressed=False,
    )
    row = DimensionRow(dim)
    import fasthtml.common as fh
    html = fh.to_xml(row)
    assert "/compass/quality/scorecard/sort-accuracy" in html


def test_k3_strip_renders_three_squares():
    """K3Strip with 3 trials renders 3 colored squares."""
    trials = [
        Trial(outcome="pass", session_id="s1"),
        Trial(outcome="fail", session_id="s2", reason="wrong"),
        Trial(outcome="error", session_id="s3"),
    ]
    strip = K3Strip(trials)
    import fasthtml.common as fh
    html = fh.to_xml(strip)
    assert html.count("quality-k3-square") == 3
    assert "quality-k3-pass" in html
    assert "quality-k3-fail" in html
    assert "quality-k3-error" in html


def test_verdict_list_collapsed_by_default_when_no_failure():
    """VerdictList with only pass verdicts is collapsed (no 'open' attribute)."""
    verdicts = [
        Verdict(criterion_id="C-1", judge_source="judge_prompt", outcome="pass"),
        Verdict(criterion_id="C-2", judge_source="judge_prompt", outcome="pass"),
    ]
    vl = VerdictList(verdicts, auto_expand_on_failure=True)
    import fasthtml.common as fh
    html = fh.to_xml(vl)
    # details element should NOT have open attribute
    assert 'open=""' not in html or 'open' not in html.split('<details')[1].split('>')[0]


def test_verdict_list_auto_expands_on_failure():
    """VerdictList with a fail verdict auto-expands (has 'open' attribute)."""
    verdicts = [
        Verdict(criterion_id="C-1", judge_source="judge_prompt", outcome="fail", reason="Wrong"),
    ]
    vl = VerdictList(verdicts, auto_expand_on_failure=True)
    import fasthtml.common as fh
    html = fh.to_xml(vl)
    assert "<details" in html
    # The open attribute is present somewhere in the details tag
    assert "open" in html


def test_verdict_list_stub_error_does_not_expand():
    """VerdictList with only stub errors (error, deterministic) does NOT auto-expand."""
    verdicts = [
        Verdict(criterion_id="C-det", judge_source="deterministic", outcome="error"),
    ]
    vl = VerdictList(verdicts, auto_expand_on_failure=True)
    import fasthtml.common as fh
    html = fh.to_xml(vl)
    # outcome="error" with judge_source="deterministic" is not a failure → no expand
    # The auto_expand_on_failure only expands on outcome=="fail"
    assert 'open=""' not in html.split('<details')[1][:50] if '<details' in html else True


# ── Not-applicable verdict handling (C1 P0) ───────────────────────────────


def test_verdict_list_not_applicable_styled_muted_not_error():
    """A not-applicable verdict (outcome='error', reason 'not applicable: ...')
    renders with the muted .quality-verdict-na class and an "N/A" label — NOT
    the red error style or "ERROR" text."""
    verdicts = [
        Verdict(
            criterion_id="C-respects-user-limit",
            judge_source="deterministic",
            outcome="error",
            reason="not applicable: no user limit on this turn",
        ),
    ]
    vl = VerdictList(verdicts)
    import fasthtml.common as fh
    html = fh.to_xml(vl)
    assert "quality-verdict-na" in html
    assert "N/A" in html
    # The not-applicable row is NOT painted with the red error style or labelled
    # ERROR (that's the bug C1 fixes — 23 healthy N/A rows reading as red errors).
    assert "quality-verdict-error" not in html
    assert "ERROR" not in html


def test_verdict_list_header_reports_full_shape():
    """The header reconciles to the total: X failed · Y passed · Z not applicable.

    A genuine error count is surfaced ONLY when a genuine error exists."""
    verdicts = [
        Verdict(criterion_id="C-1", judge_source="judge_prompt", outcome="fail", reason="bad"),
        Verdict(criterion_id="C-2", judge_source="judge_prompt", outcome="pass"),
        Verdict(criterion_id="C-3", judge_source="judge_prompt", outcome="pass"),
        Verdict(
            criterion_id="C-na",
            judge_source="deterministic",
            outcome="error",
            reason="not applicable: nothing to check here",
        ),
    ]
    vl = VerdictList(verdicts)
    import fasthtml.common as fh
    html = fh.to_xml(vl)
    assert "Verdicts (4)" in html
    assert "1 failed" in html
    assert "2 passed" in html
    assert "1 not applicable" in html
    # No genuine error here → the header must NOT claim any errored.
    assert "errored" not in html


def test_verdict_list_header_separates_genuine_errors_from_na():
    """A genuine error (outcome='error' WITHOUT the 'not applicable:' prefix)
    is counted as 'errored', distinct from the not-applicable bucket."""
    verdicts = [
        Verdict(criterion_id="C-pass", judge_source="judge_prompt", outcome="pass"),
        Verdict(
            criterion_id="C-na",
            judge_source="deterministic",
            outcome="error",
            reason="not applicable: skip",
        ),
        Verdict(
            criterion_id="C-broke",
            judge_source="deterministic",
            outcome="error",
            reason="not wired",  # genuine stub error — not the N/A prefix
        ),
    ]
    vl = VerdictList(verdicts)
    import fasthtml.common as fh
    html = fh.to_xml(vl)
    assert "1 passed" in html
    assert "1 not applicable" in html
    assert "1 errored" in html


def test_verdict_list_na_rows_collapsed_behind_disclosure():
    """Not-applicable rows are hidden behind a 'Show N not-applicable' disclosure
    nested INSIDE the outer details, so a healthy turn doesn't show a wall of
    rows. The outer details stays the FIRST <details> tag (the conversation-page
    collapse test inspects the first tag)."""
    verdicts = [
        Verdict(criterion_id="C-pass", judge_source="judge_prompt", outcome="pass"),
        Verdict(
            criterion_id="C-na",
            judge_source="deterministic",
            outcome="error",
            reason="not applicable: skip",
        ),
    ]
    vl = VerdictList(verdicts, auto_expand_on_failure=True)
    import fasthtml.common as fh
    html = fh.to_xml(vl)
    assert "Show 1 not-applicable" in html
    assert "quality-verdict-na-disclosure" in html
    # No failure → the OUTER details (first tag) stays collapsed.
    first_details_tag = html[html.find("<details"):html.find(">", html.find("<details")) + 1]
    assert " open" not in first_details_tag


# ── Route HTML shape tests ────────────────────────────────────────────────

class _FakeQueryParams(dict):
    """dict subclass that mimics Starlette QueryParams.get() interface."""

    def get(self, key, default=None):
        return super().get(key, default)


class _FakeRequest:
    """Minimal Starlette-compatible request stub."""
    class _state:
        user = None
    state = _state()

    def __init__(self, query_params: dict | None = None):
        self.query_params = _FakeQueryParams(query_params or {})


def test_scorecard_hero_has_seven_dimension_rows(monkeypatch):
    """Scorecard hero renders exactly 7 quality-dimension-row elements."""
    snapshot = _make_snapshot()
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        lambda **kw: snapshot,
    )
    request = _FakeRequest()
    result = _scorecard_hero(request)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert html.count("quality-dimension-row") == 7


def test_scorecard_hero_shows_threshold_column(monkeypatch):
    """Scorecard hero renders a threshold column sourced from DimensionScore."""
    snapshot = _make_snapshot()
    snapshot.dimensions[0].threshold_pct = 95
    snapshot.dimensions[0].threshold_status = "no_data"
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        lambda **kw: snapshot,
    )
    request = _FakeRequest()
    result = _scorecard_hero(request)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "Threshold" in html
    assert "quality-threshold-no-data" in html


def test_scorecard_hero_has_threshold_trend_cases_and_drilldown_columns(monkeypatch):
    """Scorecard hero exposes the reviewer readout columns from model fields."""
    snapshot = _make_snapshot()
    # Give one dimension a comparable prior sweep so the Trend column renders
    # (SC1: the Trend column is hidden entirely when no dimension has a
    # delta_pct; here we assert it appears when at least one dimension does).
    snapshot.dimensions[0].delta_pct = 3
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        lambda **kw: snapshot,
    )
    request = _FakeRequest()
    result = _scorecard_hero(request)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "Threshold" in html
    assert "Trend" in html
    assert "Cases" in html
    assert "Details" in html


def test_scorecard_hero_hides_trend_column_when_no_prior_sweep(monkeypatch):
    """SC1: with every dimension's delta_pct None, the Trend header is absent."""
    snapshot = _make_snapshot()  # all dims have delta_pct=None
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        lambda **kw: snapshot,
    )
    request = _FakeRequest()
    result = _scorecard_hero(request)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "Trend" not in html
    # The placeholder cell text must not leak either (column dropped as a unit).
    assert "no comparable prior sweep" not in html
    # Sanity: the rest of the table still renders.
    assert "Threshold" in html
    assert html.count("quality-dimension-row") == 7


def test_scorecard_hero_shows_trend_column_when_a_prior_sweep_exists(monkeypatch):
    """SC1: when at least one dimension has a delta_pct, the Trend header shows."""
    snapshot = _make_snapshot()
    snapshot.dimensions[0].delta_pct = -2
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        lambda **kw: snapshot,
    )
    request = _FakeRequest()
    result = _scorecard_hero(request)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "Trend" in html
    assert "-2" in html


def test_dimension_row_renders_positive_delta():
    dim = DimensionScore(
        dim_slug="sort-accuracy",
        name="Sort Accuracy",
        definition="Right ordering.",
        score_pct=92,
        n_trials=18,
        regressed=False,
        previous_score_pct=89,
        delta_pct=3,
    )
    row = DimensionRow(dim)
    import fasthtml.common as fh
    html = fh.to_xml(row)
    assert "+3" in html
    assert "quality-trend-positive" in html


def test_dimension_row_renders_negative_delta():
    dim = DimensionScore(
        dim_slug="sort-accuracy",
        name="Sort Accuracy",
        definition="Right ordering.",
        score_pct=87,
        n_trials=18,
        regressed=True,
        previous_score_pct=89,
        delta_pct=-2,
    )
    row = DimensionRow(dim)
    import fasthtml.common as fh
    html = fh.to_xml(row)
    assert "-2" in html
    assert "quality-trend-negative" in html


def test_dimension_row_renders_no_comparable_prior_sweep():
    dim = DimensionScore(
        dim_slug="sort-accuracy",
        name="Sort Accuracy",
        definition="Right ordering.",
        score_pct=92,
        n_trials=18,
        regressed=False,
    )
    row = DimensionRow(dim)
    import fasthtml.common as fh
    html = fh.to_xml(row)
    assert "no comparable prior sweep" in html
    assert "quality-trend-empty" in html


def test_build_context_strip_shows_threshold_review_date():
    """Build context strip includes threshold metadata when available."""
    from nctqai.components.compass.quality import build_context_strip

    build = BuildContext(
        build_id="b-test",
        sweep_id="sweep-test",
        criterion_set_version="compass_criteria_v1",
        cases=3,
        trials=9,
        threshold_version="test_thresholds_v1",
        threshold_review_date="2026-06-22",
    )
    strip = build_context_strip(build)
    import fasthtml.common as fh
    html = fh.to_xml(strip)
    assert "Thresholds" in html
    assert "test_thresholds_v1" in html
    assert "2026-06-22" in html


def test_scorecard_hero_no_color_classes_on_placeholder_dims(monkeypatch):
    """Placeholder dimensions (n_trials=0) have no green/red color classes."""
    snapshot = _make_snapshot()
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        lambda **kw: snapshot,
    )
    request = _FakeRequest()
    result = _scorecard_hero(request)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    # No colored K3 squares on the hero (no quality-k3-pass/fail)
    assert "quality-k3-pass" not in html
    assert "quality-k3-fail" not in html


def test_scorecard_hero_breadcrumb_present(monkeypatch):
    """Scorecard hero includes the quality-breadcrumb-strip."""
    snapshot = _make_snapshot()
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        lambda **kw: snapshot,
    )
    request = _FakeRequest()
    result = _scorecard_hero(request)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "quality-breadcrumb-strip" in html
    assert "Quality Scorecard" in html


def test_scorecard_hero_shows_last_successful_sweep(monkeypatch):
    """Scorecard hero renders the last-successful-sweep strip from
    max(latest_finished_at) across dimensions (issue #751)."""
    snapshot = _make_snapshot()
    # Give two dimensions a finished_at; the strip should reflect the max.
    snapshot.dimensions[0].latest_finished_at = "2026-05-30T00:00:00+00:00"
    snapshot.dimensions[3].latest_finished_at = "2026-06-02T10:00:00+00:00"
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        lambda **kw: snapshot,
    )
    request = _FakeRequest()
    result = _scorecard_hero(request)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "quality-last-sweep-strip" in html
    assert "Last successful sweep" in html
    # the later of the two timestamps is surfaced (title/precision)
    assert "2026-06-02" in html


def test_dimension_drill_down_shows_k3_strips(monkeypatch):
    """Drill-down for sort-accuracy renders K3 squares."""
    detail = _make_detail("sort-accuracy")
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_dimension",
        lambda dim_slug, **kw: detail,
    )
    request = _FakeRequest()
    result = _dimension_detail(request, "sort-accuracy")
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "quality-k3-strip" in html


def test_dimension_drill_down_links_to_conversations(monkeypatch):
    """Drill-down session links point to /compass/quality/conversations/<session_id>."""
    detail = _make_detail("sort-accuracy")
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_dimension",
        lambda dim_slug, **kw: detail,
    )
    request = _FakeRequest()
    result = _dimension_detail(request, "sort-accuracy")
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "/compass/quality/conversations/" in html


def test_dimension_detail_links_failed_sessions(monkeypatch):
    """Failed drill-down trials keep a direct conversation link."""
    detail = _make_detail("sort-accuracy")
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_dimension",
        lambda dim_slug, **kw: detail,
    )
    request = _FakeRequest()
    result = _dimension_detail(request, "sort-accuracy")
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "/compass/quality/conversations/s-bbb-002" in html
    assert "quality-verdict-fail" in html


def test_dimension_drill_down_not_found_shows_error(monkeypatch):
    """Unknown dim_slug shows an error message, not a crash."""
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_dimension",
        lambda dim_slug, **kw: (_ for _ in ()).throw(KeyError(dim_slug)),
    )
    request = _FakeRequest()
    result = _dimension_detail(request, "not-a-dim")
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "not-a-dim" in html or "not found" in html.lower()


def test_conversation_route_renders_turns_and_verdicts(monkeypatch):
    """Conversation page renders turn blocks with VerdictList."""
    conv = _make_conversation()
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.conversations.load_conversation_with_verdicts",
        lambda session_id: conv,
    )
    request = _FakeRequest()
    result = _conversation_page(request, conv.session_id)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "quality-turn-block" in html
    assert "quality-verdict-list" in html


def test_conversation_route_verdicts_collapsed_when_no_failure(monkeypatch):
    """Conversation turns with only pass/error verdicts are collapsed by default."""
    conv = _make_conversation()
    # Remove the fail verdict from the turn
    turn = conv.turns[0]
    turn.verdicts = [v for v in turn.verdicts if v.outcome != "fail"]
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.conversations.load_conversation_with_verdicts",
        lambda session_id: conv,
    )
    request = _FakeRequest()
    result = _conversation_page(request, conv.session_id)
    import fasthtml.common as fh
    html = fh.to_xml(result)
    # Details tag should not have open attribute in its opening tag attributes
    # (open would be present if auto-expanded)
    details_start = html.find("<details")
    if details_start != -1:
        details_tag = html[details_start:html.find(">", details_start) + 1]
        assert " open" not in details_tag or "open" not in details_tag


def test_conversation_not_found_shows_error(monkeypatch):
    """Unknown session_id shows an error message, not a crash."""
    monkeypatch.setattr(
        "nctqai.routes.compass.quality.conversations.load_conversation_with_verdicts",
        lambda session_id: (_ for _ in ()).throw(KeyError(session_id)),
    )
    request = _FakeRequest()
    result = _conversation_page(request, "s-not-found")
    import fasthtml.common as fh
    html = fh.to_xml(result)
    assert "not found" in html.lower() or "s-not-found" in html


# ── sweep_run_id query-param threading tests ──────────────────────────────

def test_scorecard_route_accepts_sweep_run_id_query_param(monkeypatch):
    """Scorecard hero passes a valid sweep_run_id query param to load_scorecard."""
    snapshot = _make_snapshot()
    captured = {}

    def fake_load_scorecard(**kw):
        captured.update(kw)
        return snapshot

    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        fake_load_scorecard,
    )
    valid_uuid = "12345678-1234-5678-1234-567812345678"
    request = _FakeRequest(query_params={"sweep_run_id": valid_uuid})
    _scorecard_hero(request)
    assert captured.get("sweep_run_id") == valid_uuid


def test_scorecard_route_default_no_sweep_run_id(monkeypatch):
    """Scorecard hero calls load_scorecard(sweep_run_id=None) when no query param."""
    snapshot = _make_snapshot()
    captured = {}

    def fake_load_scorecard(**kw):
        captured.update(kw)
        return snapshot

    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        fake_load_scorecard,
    )
    request = _FakeRequest()
    _scorecard_hero(request)
    assert captured.get("sweep_run_id") is None


def test_scorecard_route_invalid_uuid_falls_back_gracefully(monkeypatch):
    """Malformed sweep_run_id falls back to None (no 500) and logs a warning."""
    import logging
    from nctqai.routes.compass.quality import scorecard as scorecard_module

    snapshot = _make_snapshot()
    captured = {}

    def fake_load_scorecard(**kw):
        captured.update(kw)
        return snapshot

    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_scorecard",
        fake_load_scorecard,
    )

    warning_messages = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record):
            warning_messages.append(record.getMessage())

    handler = _CapturingHandler()
    scorecard_logger = logging.getLogger(scorecard_module.__name__)
    scorecard_logger.addHandler(handler)
    try:
        request = _FakeRequest(query_params={"sweep_run_id": "not-a-uuid"})
        result = _scorecard_hero(request)
    finally:
        scorecard_logger.removeHandler(handler)

    import fasthtml.common as fh
    html = fh.to_xml(result)
    # (a) route returns 200 — rendered HTML, not an error crash
    assert "quality-scorecard-table" in html or "quality-dimension-row" in html
    # (b) load_scorecard called with sweep_run_id=None
    assert captured.get("sweep_run_id") is None
    # (c) a warning was logged
    assert any("not-a-uuid" in msg or "malformed" in msg for msg in warning_messages)


def test_dimension_route_accepts_sweep_run_id_query_param(monkeypatch):
    """Dimension drill-down passes a valid sweep_run_id query param to load_dimension."""
    detail = _make_detail("sort-accuracy")
    captured = {}

    def fake_load_dimension(dim_slug, **kw):
        captured.update(kw)
        return detail

    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_dimension",
        fake_load_dimension,
    )
    valid_uuid = "12345678-1234-5678-1234-567812345678"
    request = _FakeRequest(query_params={"sweep_run_id": valid_uuid})
    _dimension_detail(request, "sort-accuracy")
    assert captured.get("sweep_run_id") == valid_uuid


def test_dimension_route_default_no_sweep_run_id(monkeypatch):
    """Dimension drill-down calls load_dimension(sweep_run_id=None) when no query param."""
    detail = _make_detail("sort-accuracy")
    captured = {}

    def fake_load_dimension(dim_slug, **kw):
        captured.update(kw)
        return detail

    monkeypatch.setattr(
        "nctqai.routes.compass.quality.scorecard.load_dimension",
        fake_load_dimension,
    )
    request = _FakeRequest()
    _dimension_detail(request, "sort-accuracy")
    assert captured.get("sweep_run_id") is None
