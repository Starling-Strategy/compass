"""Tests for the dimension-bridge validator factory in validators/registry.py.

The dim_<name> validators registered via _evaluate_dimension(dim) adapt the
existing L1 suite (quality.validate_result()) into the @register pattern. Per
dimension:

  - pass case  — a plan/result that produces zero L1 findings for that label
  - fail case  — a plan/result that produces ≥1 finding tagged with that label
  - missing artifact path — ctx.plan or ctx.result is None → outcome="error"
                            (NOT "fail")

The bridge is build-once at import time. We exercise the *registered* closures
via `lookup()` so the test is faithful to what RecordDeterministicEvaluator
dispatches at runtime.

Run with:
    PYTHONPATH=src .venv/bin/python -m pytest \\
        src/compass_backend/tests/test_dimension_bridge_validators.py -v
"""

from __future__ import annotations

from collections import Counter

import pytest

from compass_backend.artifacts import (
    CitationRef,
    CoverageFrame,
    MethodologyRef,
    MetricCountResult,
    MetricRankingResult,
    RankingRow,
    ResultSelection,
    ResultSet,
    SelectedDistrict,
    ThresholdCountRow,
)
from compass_backend.contracts import (
    MetricSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.validators.registry import known_validators, lookup
from compass_backend.tests.conftest import make_evaluator_context


# ─── Shared fixtures ──────────────────────────────────────────────────────────


def _ctx(
    *,
    plan: QueryPlan | None = None,
    result: ResultSet | None = None,
    answer_text: str = "",
) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="11111111-1111-1111-1111-111111111111",
        turn_index=0,
        plan=plan,
        result=result,
        answer_text=answer_text,
    )


def _coverage_frame(rows: list) -> CoverageFrame:
    state_counts = Counter(getattr(r, "coverage_state", "covered") for r in rows)
    in_scope = (
        state_counts["covered"]
        + state_counts["ina"]
        + state_counts["na"]
        + state_counts["not_reviewed"]
    )
    real = state_counts["covered"]
    coverage_ratio = real / in_scope if in_scope else 0.0
    return CoverageFrame(
        universe_count=len(rows),
        in_scope_count=in_scope,
        addressed_count=state_counts["covered"]
        + state_counts["ina"]
        + state_counts["na"],
        real_data_count=real,
        not_reviewed_count=state_counts["not_reviewed"],
        out_of_universe_count=state_counts["out_of_universe"],
        coverage_ratio=coverage_ratio,
    )


def _ranking_row(
    *,
    district_id: int,
    district_name: str,
    value: float,
    rank: int,
    citation_markers: list[int] | None = None,
    metric_id: int = 1234,
    metric_name: str = "Average teacher starting salary",
    state: str = "CA",
    coverage_state: str = "covered",
    coverage_display: str | None = None,
) -> RankingRow:
    display = f"${value:,.0f}"
    return RankingRow(
        district_id=district_id,
        district_name=district_name,
        state=state,
        metric_id=metric_id,
        metric_name=metric_name,
        value=value,
        display_value=display,
        academic_year="2024 - 2025",
        rank=rank,
        citation_markers=citation_markers if citation_markers is not None else [rank],
        coverage_state=coverage_state,
        coverage_display=coverage_display if coverage_display is not None else display,
        coverage_reason="answer_value",
    )


def _citation(marker: int, *, district_id: int | None = None) -> CitationRef:
    return CitationRef(
        marker=marker,
        title=f"District {district_id or marker} Contract, 2024-2025",
        url=f"https://example.org/{marker}.pdf",
        page_number=marker,
        page_ref=f"p. {marker}",
        academic_year="2024 - 2025",
        document_type="Contract",
        district_id=district_id or marker,
    )


def _ranking_result(rows: list[RankingRow]) -> ResultSet:
    return MetricRankingResult(
        rows=rows,
        citations=[_citation(r.rank, district_id=r.district_id) for r in rows],
        total_considered=len(rows),
        excluded_count=0,
        coverage_frame=_coverage_frame(rows),
        order_statement="Ranked by starting salary, highest to lowest.",
        source_notes=[],
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def _ranking_plan(
    *, scope: str = "all_covered_districts", states: list[str] | None = None
) -> QueryPlan:
    return QueryPlan(
        question="Rank covered districts by starting salary.",
        selection=SelectionSpec(scope=scope, states=states or []),
        metrics=[MetricSpec(name="starting salary")],
    )


def _clean_ranking_plan_and_result() -> tuple[QueryPlan, ResultSet]:
    """Plan + result that produces zero findings across all 8 dimensions."""
    rows = [
        _ranking_row(district_id=1, district_name="Alpha", value=70000.0, rank=1),
        _ranking_row(district_id=2, district_name="Bravo", value=60000.0, rank=2),
    ]
    return _ranking_plan(), _ranking_result(rows)


# ─── Registration sanity ──────────────────────────────────────────────────────


def test_all_eight_dim_bridges_registered() -> None:
    """Import time should register exactly the eight planned dim_* validators."""
    expected = {
        "dim_selection",
        "dim_metric",
        "dim_coverage_state",
        "dim_citation_coverage",
        "dim_denominator",
        "dim_data_fidelity",
        "dim_numeric_token_provenance",
        "dim_filter",
    }
    assert expected.issubset(known_validators())


def test_intentionally_unregistered_dimensions_are_absent() -> None:
    """sort_order must NOT have a dim_* bridge.

    `dim_surface_consistency` was registered when the L1 surface validators
    landed in `validators/surface.py` (markdown_cell_drift, csv_cell_drift,
    artifact_id_drift).
    """
    known = known_validators()
    # dim_sort_order would duplicate the existing sort_descending validator.
    assert "dim_sort_order" not in known
    # Lock in: surface_consistency IS registered now. Guards against
    # accidental removal during future refactors.
    assert "dim_surface_consistency" in known


# ─── Missing-artifact paths (one parametrized across all eight bridges) ──────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dim_name",
    [
        "dim_selection",
        "dim_metric",
        "dim_coverage_state",
        "dim_citation_coverage",
        "dim_denominator",
        "dim_data_fidelity",
        "dim_numeric_token_provenance",
        "dim_filter",
    ],
)
async def test_missing_plan_returns_error_not_fail(dim_name: str) -> None:
    fn = lookup(dim_name)
    assert fn is not None
    _plan, result = _clean_ranking_plan_and_result()
    ctx = _ctx(plan=None, result=result)
    outcome = await fn(ctx, {})
    assert outcome.outcome == "error", outcome.reason
    assert "plan" in outcome.reason
    # Use the `not applicable:` reason convention so the Scorecard's
    # _is_excluded_from_score filter drops these trials from both numerator
    # and denominator (direct-route + clarification turns naturally have no
    # plan; that's over-selection, not a bridge failure).
    assert outcome.reason.startswith("not applicable:"), outcome.reason
    assert outcome.evidence["dimension"] == dim_name.removeprefix("dim_")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dim_name",
    [
        "dim_selection",
        "dim_metric",
        "dim_coverage_state",
        "dim_citation_coverage",
        "dim_denominator",
        "dim_data_fidelity",
        "dim_numeric_token_provenance",
        "dim_filter",
    ],
)
async def test_missing_result_returns_error_not_fail(dim_name: str) -> None:
    fn = lookup(dim_name)
    assert fn is not None
    plan, _result = _clean_ranking_plan_and_result()
    ctx = _ctx(plan=plan, result=None)
    outcome = await fn(ctx, {})
    assert outcome.outcome == "error", outcome.reason
    assert "result" in outcome.reason
    assert outcome.reason.startswith("not applicable:"), outcome.reason


# ─── dim_selection ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dim_selection_pass_on_consistent_ranking() -> None:
    fn = lookup("dim_selection")
    assert fn is not None
    plan, result = _clean_ranking_plan_and_result()
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "pass", outcome.reason
    assert outcome.evidence["dimension"] == "selection"


@pytest.mark.asyncio
async def test_dim_selection_fail_when_row_state_outside_request() -> None:
    """Plan requests CA only; result has a TX row → selection mismatch."""
    fn = lookup("dim_selection")
    assert fn is not None
    plan = _ranking_plan(scope="state", states=["CA"])
    rows = [
        _ranking_row(
            district_id=1,
            district_name="Alpha",
            value=70000.0,
            rank=1,
            state="CA",
        ),
        # Out-of-state row triggers selection_state_mismatch (selection.py)
        _ranking_row(
            district_id=2,
            district_name="Bravo",
            value=60000.0,
            rank=2,
            state="TX",
        ),
    ]
    outcome = await fn(_ctx(plan=plan, result=_ranking_result(rows)), {})
    assert outcome.outcome == "fail", outcome.reason
    assert outcome.evidence["dimension"] == "selection"
    assert outcome.evidence["violation_count"] >= 1


# ─── dim_metric ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dim_metric_pass_on_consistent_ranking() -> None:
    fn = lookup("dim_metric")
    assert fn is not None
    plan, result = _clean_ranking_plan_and_result()
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "pass", outcome.reason


@pytest.mark.asyncio
async def test_dim_metric_fail_on_metric_id_mismatch_between_rows() -> None:
    """Ranking rows must all share the same metric_id; mismatch → fail."""
    fn = lookup("dim_metric")
    assert fn is not None
    plan = _ranking_plan()
    rows = [
        _ranking_row(
            district_id=1,
            district_name="Alpha",
            value=70000.0,
            rank=1,
            metric_id=1234,
        ),
        _ranking_row(
            district_id=2,
            district_name="Bravo",
            value=60000.0,
            rank=2,
            metric_id=9999,
            metric_name="Different metric",
        ),
    ]
    outcome = await fn(_ctx(plan=plan, result=_ranking_result(rows)), {})
    assert outcome.outcome == "fail", outcome.reason
    assert outcome.evidence["dimension"] == "metric"


# ─── dim_coverage_state ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dim_coverage_state_pass_on_consistent_ranking() -> None:
    fn = lookup("dim_coverage_state")
    assert fn is not None
    plan, result = _clean_ranking_plan_and_result()
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "pass", outcome.reason


@pytest.mark.asyncio
async def test_dim_coverage_state_fail_when_display_value_mismatches_coverage_display() -> None:
    """coverage_display != display_value → coverage_display_mismatch."""
    fn = lookup("dim_coverage_state")
    assert fn is not None
    plan = _ranking_plan()
    rows = [
        _ranking_row(
            district_id=1,
            district_name="Alpha",
            value=70000.0,
            rank=1,
            coverage_display="$70,001",  # Off-by-one — triggers mismatch.
        ),
        _ranking_row(district_id=2, district_name="Bravo", value=60000.0, rank=2),
    ]
    outcome = await fn(_ctx(plan=plan, result=_ranking_result(rows)), {})
    assert outcome.outcome == "fail", outcome.reason
    assert outcome.evidence["dimension"] == "coverage_state"


# ─── dim_citation_coverage ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dim_citation_coverage_pass_on_consistent_ranking() -> None:
    fn = lookup("dim_citation_coverage")
    assert fn is not None
    plan, result = _clean_ranking_plan_and_result()
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "pass", outcome.reason


@pytest.mark.asyncio
async def test_dim_citation_coverage_fail_when_row_references_missing_marker() -> None:
    """Row references citation marker 99 that isn't in result.citations."""
    fn = lookup("dim_citation_coverage")
    assert fn is not None
    plan = _ranking_plan()
    rows = [
        _ranking_row(
            district_id=1,
            district_name="Alpha",
            value=70000.0,
            rank=1,
            citation_markers=[99],  # Not in citations list.
        ),
        _ranking_row(district_id=2, district_name="Bravo", value=60000.0, rank=2),
    ]
    outcome = await fn(_ctx(plan=plan, result=_ranking_result(rows)), {})
    assert outcome.outcome == "fail", outcome.reason
    assert outcome.evidence["dimension"] == "citation_coverage"


# ─── dim_denominator + dim_filter (count-only) ────────────────────────────────


def _count_plan() -> QueryPlan:
    return QueryPlan(
        operation="count",
        question="How many covered districts review starting salary?",
        selection=SelectionSpec(scope="all_covered_districts", states=["TX"]),
        metrics=[MetricSpec(name="starting salary")],
    )


def _count_selection(district_ids: list[int]) -> ResultSelection:
    return ResultSelection(
        scope="all_covered_districts",
        states=["TX"],
        districts=[
            SelectedDistrict(
                district_id=did,
                district_name=f"District {did}",
                state="TX",
            )
            for did in district_ids
        ],
    )


def _count_result(
    *,
    count: int,
    denominator: int,
    qualifying_district_ids: list[int],
    selection_district_ids: list[int],
    filter_statement: str = "reviewed current value",
) -> ResultSet:
    display = f"{count} of {denominator} covered districts"
    row = ThresholdCountRow(
        metric_id=1234,
        metric_name="Average teacher starting salary",
        value=count,
        display_value=display,
        academic_year="2024 - 2025",
        count=count,
        denominator=denominator,
        filter_statement=filter_statement,
        qualifying_district_ids=qualifying_district_ids,
        coverage_state="covered",
        coverage_display=display,
        coverage_reason="count_summary",
    )
    return MetricCountResult(        selection=_count_selection(selection_district_ids),
        rows=[row],
        total_considered=len(selection_district_ids),
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=len(selection_district_ids),
            in_scope_count=len(selection_district_ids),
            addressed_count=len(selection_district_ids),
            real_data_count=denominator,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement="Counted qualifying districts.",
        source_notes=[],
        methodology_codes=[
            MethodologyRef(code="count_denominator_current_reviewed_rows"),
        ],
    )


@pytest.mark.asyncio
async def test_dim_denominator_pass_on_consistent_count() -> None:
    fn = lookup("dim_denominator")
    assert fn is not None
    plan = _count_plan()
    result = _count_result(
        count=2,
        denominator=2,
        qualifying_district_ids=[101, 102],
        selection_district_ids=[101, 102],
    )
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "pass", outcome.reason


@pytest.mark.asyncio
async def test_dim_denominator_fail_when_count_exceeds_denominator() -> None:
    fn = lookup("dim_denominator")
    assert fn is not None
    plan = _count_plan()
    # count=3 but denominator=2 → fail (count_qualifying_ids_mismatch + denominator_mismatch)
    result = _count_result(
        count=3,
        denominator=2,
        qualifying_district_ids=[101, 102, 103],
        selection_district_ids=[101, 102, 103],
    )
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "fail", outcome.reason
    assert outcome.evidence["dimension"] == "denominator"


@pytest.mark.asyncio
async def test_dim_filter_pass_on_consistent_count() -> None:
    fn = lookup("dim_filter")
    assert fn is not None
    plan = _count_plan()
    result = _count_result(
        count=2,
        denominator=2,
        qualifying_district_ids=[101, 102],
        selection_district_ids=[101, 102],
    )
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "pass", outcome.reason


@pytest.mark.asyncio
async def test_dim_filter_fail_on_filter_statement_mismatch() -> None:
    """Row carries a filter_statement that doesn't match the planner's filters."""
    fn = lookup("dim_filter")
    assert fn is not None
    plan = _count_plan()
    # Plan has no value filters → expected statement is "reviewed current value".
    # Force a different actual to trigger filter_statement_mismatch.
    result = _count_result(
        count=2,
        denominator=2,
        qualifying_district_ids=[101, 102],
        selection_district_ids=[101, 102],
        filter_statement="some other interpretation",
    )
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "fail", outcome.reason
    assert outcome.evidence["dimension"] == "filter"


# ─── dim_data_fidelity (trend / year-anchored) ────────────────────────────────


@pytest.mark.asyncio
async def test_dim_data_fidelity_pass_on_consistent_ranking() -> None:
    """Ranking turns produce zero data_fidelity findings (it's a trend-side check)."""
    fn = lookup("dim_data_fidelity")
    assert fn is not None
    plan, result = _clean_ranking_plan_and_result()
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "pass", outcome.reason


@pytest.mark.asyncio
async def test_dim_data_fidelity_fail_when_row_year_mismatches_requested_year() -> None:
    """Plan requests 2023-2024 specifically; row carries 2024-2025 → fail."""
    from compass_backend.contracts.planning import TemporalSpec

    fn = lookup("dim_data_fidelity")
    assert fn is not None
    plan = QueryPlan(
        question="Lookup starting salary for Alpha in 2023-2024.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="starting salary")],
        temporal=TemporalSpec(intent="specific_year", academic_year="2023 - 2024"),
    )
    rows = [
        _ranking_row(
            district_id=1,
            district_name="Alpha",
            value=70000.0,
            rank=1,
        ),
    ]  # academic_year defaults to 2024-2025 in helper
    outcome = await fn(_ctx(plan=plan, result=_ranking_result(rows)), {})
    assert outcome.outcome == "fail", outcome.reason
    assert outcome.evidence["dimension"] == "data_fidelity"


# ─── dim_numeric_token_provenance (rendered-body only) ────────────────────────


@pytest.mark.asyncio
async def test_dim_numeric_token_provenance_pass_when_no_rendered_body() -> None:
    """When validate_result is called without rendered_body the dim check is a
    no-op (it returns no findings), so the bridge passes by construction."""
    fn = lookup("dim_numeric_token_provenance")
    assert fn is not None
    plan, result = _clean_ranking_plan_and_result()
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "pass", outcome.reason


# ─── Confirm bridges actually invoke validate_result ─────────────────────────


@pytest.mark.asyncio
async def test_bridge_invokes_validate_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: the bridge calls quality.validation.validate_result with ctx.plan
    and ctx.result. Patch validate_result and assert the call signature."""

    calls: list[tuple] = []

    from compass_backend.quality import validation as validation_module
    from compass_backend.contracts.validation import ValidationReport

    def _fake_validate_result(*, plan, result, rendered_body=None):
        calls.append((plan, result, rendered_body))
        return ValidationReport(valid=True, dimensions_checked=[], findings=[])

    monkeypatch.setattr(
        validation_module, "validate_result", _fake_validate_result
    )

    fn = lookup("dim_selection")
    assert fn is not None
    plan, result = _clean_ranking_plan_and_result()
    outcome = await fn(_ctx(plan=plan, result=result), {})
    assert outcome.outcome == "pass"
    assert len(calls) == 1
    # Empty answer_text maps to None so body-gated validators short-circuit.
    assert calls[0] == (plan, result, None)


# ---------------------------------------------------------------------------
# C4 (#1416) — the bridge passes the final answer text, so rendered-body-gated
# criteria actually run at L2 (they silently always-passed before)
# ---------------------------------------------------------------------------


def _count_plan_and_result():
    from compass_backend.artifacts import (
        MetricCountResult,
        ResultSelection,
        SelectedDistrict,
        ThresholdCountRow,
    )
    from compass_backend.contracts import MetricSpec, SelectionSpec

    plan = QueryPlan(
        operation="count",
        question="How many districts require formal observations?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations")],
    )
    result = MetricCountResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
            ],
        ),
        rows=[
            ThresholdCountRow(
                metric_id=39,
                metric_name="Minimum number of formal observations",
                value=2,
                display_value="2 of 3 covered districts",
                academic_year="2024 - 2025",
                count=2,
                denominator=3,
                filter_statement="value >= 3",
                qualifying_district_ids=[1, 2],
                coverage_state="covered",
                coverage_display="2 of 3 covered districts",
                coverage_reason="count_summary",
            )
        ],
        citations=[],
        total_considered=3,
        excluded_count=0,
        order_statement="Counted qualifying districts.",
        methodology_codes=[],
    )
    return plan, result


@pytest.mark.asyncio
async def test_dim_fact_coverage_fails_on_lossy_answer_text() -> None:
    """The eval criterion catches a final answer that dropped computed facts.

    Because the bridge passes ctx.answer_text (the POST-stylist text), this
    catches drops by the voice pass too — not only renderer regressions.
    """
    fn = lookup("dim_fact_coverage")
    assert fn is not None
    plan, result = _count_plan_and_result()
    lossy = "Of 3 covered districts with data, 2 match your criteria."

    outcome = await fn(_ctx(plan=plan, result=result, answer_text=lossy), {})

    # Warning-severity findings surface as a calibration-mode PASS: visible in
    # the reason + evidence (sweep reasons table, compass.verdicts) without
    # depressing the headline score. Promotion to fail = raising the L1
    # finding severity to "error" once calibrated.
    assert outcome.outcome == "pass"
    assert outcome.reason.startswith("calibration:")
    assert outcome.evidence["first_code"] == "count_names_not_surfaced"
    assert outcome.evidence["codes"] == ["count_names_not_surfaced"]


@pytest.mark.asyncio
async def test_dim_fact_coverage_passes_on_complete_answer_text() -> None:
    fn = lookup("dim_fact_coverage")
    assert fn is not None
    plan, result = _count_plan_and_result()
    complete = (
        "Of 3 covered districts with data, 2 match your criteria.\n\n"
        "Matching districts (2): Alpha (CA) and Bravo (TX)."
    )

    outcome = await fn(_ctx(plan=plan, result=result, answer_text=complete), {})

    assert outcome.outcome == "pass"


@pytest.mark.asyncio
async def test_dim_numeric_token_provenance_actually_fires_now() -> None:
    """Regression proof for the always-pass bug: an ungrounded number in the
    final answer text now fails the provenance criterion at L2."""
    fn = lookup("dim_numeric_token_provenance")
    assert fn is not None
    plan, result = _count_plan_and_result()
    invented = "Of 3 covered districts with data, 2 match. Some 99,999 students agree."

    outcome = await fn(_ctx(plan=plan, result=result, answer_text=invented), {})

    assert outcome.outcome == "fail"
    assert outcome.evidence["first_code"] == "numeric_token_not_in_artifact"
