"""Tests for threshold_hint on FilterSpec (PR 2D — vague-quantifier transparency).

TDD sequence:
  Step 1 — contract: FilterSpec accepts threshold_hint, validates it
  Step 2 — renderer: rank/lookup prose surfaces the hint when present
  Step 3 — renderer: hint absent when threshold_hint=None
  Step 4 — planner prompt: worked examples present in PLANNER_INSTRUCTIONS
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.artifacts import (
    CoverageBreakdown,
    CoverageFrame,
    MethodologyRef,
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricValueRow,
    RankingRow,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.contracts import FilterSpec, MetricSpec, QueryPlan, SelectionSpec
from compass_backend.rendering.composer import _ranking_coverage_label, compose_response


# ---------------------------------------------------------------------------
# Step 1 — Contract: FilterSpec accepts and validates threshold_hint
# ---------------------------------------------------------------------------


def test_filter_spec_accepts_threshold_hint() -> None:
    """FilterSpec with a threshold_hint string round-trips correctly."""
    spec = FilterSpec(
        field="observation frequency",
        operator="greater_than_or_equal",
        value=2,
        threshold_hint="regularly",
    )
    assert spec.threshold_hint == "regularly"
    assert spec.value == 2


def test_filter_spec_default_threshold_hint_is_none() -> None:
    """FilterSpec.threshold_hint defaults to None when not supplied."""
    spec = FilterSpec(
        field="teacher workdays",
        operator="greater_than",
        value=190,
    )
    assert spec.threshold_hint is None


def test_filter_spec_strips_whitespace_in_hint() -> None:
    """FilterSpec.threshold_hint validator strips leading/trailing whitespace."""
    spec = FilterSpec(
        field="bonuses",
        operator="greater_than_or_equal",
        value=2,
        threshold_hint="  a couple  ",
    )
    assert spec.threshold_hint == "a couple"


def test_filter_spec_rejects_empty_hint_after_strip() -> None:
    """FilterSpec.threshold_hint validator rejects empty string after stripping."""
    with pytest.raises(ValidationError, match="threshold_hint must not be empty"):
        FilterSpec(
            field="bonuses",
            operator="greater_than_or_equal",
            value=2,
            threshold_hint="   ",
        )


def test_filter_spec_rejects_empty_hint() -> None:
    """FilterSpec.threshold_hint validator rejects empty string."""
    with pytest.raises(ValidationError, match="threshold_hint must not be empty"):
        FilterSpec(
            field="bonuses",
            operator="greater_than_or_equal",
            value=2,
            threshold_hint="",
        )


def test_filter_spec_max_length_enforced() -> None:
    """FilterSpec.threshold_hint rejects strings longer than 80 characters."""
    long_hint = "a" * 81
    with pytest.raises(ValidationError):
        FilterSpec(
            field="bonuses",
            operator="greater_than_or_equal",
            value=2,
            threshold_hint=long_hint,
        )


def test_filter_spec_accepts_exactly_80_chars() -> None:
    """FilterSpec.threshold_hint accepts exactly 80 characters."""
    hint_80 = "a" * 80
    spec = FilterSpec(
        field="bonuses",
        operator="greater_than_or_equal",
        value=2,
        threshold_hint=hint_80,
    )
    assert spec.threshold_hint == hint_80


def test_filter_spec_json_roundtrip_with_threshold_hint() -> None:
    """FilterSpec serialises/deserialises threshold_hint correctly."""
    spec = FilterSpec(
        field="observation frequency",
        operator="greater_than_or_equal",
        value=2,
        threshold_hint="regularly",
    )
    json_str = spec.model_dump_json()
    restored = FilterSpec.model_validate_json(json_str)
    assert restored.threshold_hint == "regularly"
    assert restored.value == 2


def test_filter_spec_json_roundtrip_null_threshold_hint() -> None:
    """Existing JSON without threshold_hint is accepted (backward-compat)."""
    json_str = '{"field": "teacher workdays", "operator": "greater_than", "value": 190}'
    spec = FilterSpec.model_validate_json(json_str)
    assert spec.threshold_hint is None


# ---------------------------------------------------------------------------
# Step 2 — Renderer: rank prose surfaces threshold_hint when present
# ---------------------------------------------------------------------------


def _ranking_plan_with_hint(hint: str | None) -> QueryPlan:
    """Build a rank plan with an observation-frequency filter."""
    filters = [
        FilterSpec(
            field="observation frequency",
            operator="greater_than_or_equal",
            value=2,
            threshold_hint=hint,
        )
    ]
    return QueryPlan(
        operation="rank",
        question="Which districts observe teachers regularly?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="observation frequency")],
        filters=filters,
    )


def _simple_ranking_result() -> MetricRankingResult:
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=100,
                metric_name="Number of formal classroom observations",
                value=3.0,
                display_value="3",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[],
            ),
        ],
        citations=[],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by observation frequency, highest to lowest.",
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def test_rank_filter_prose_surfaces_threshold_hint() -> None:
    """Rank response prose names the concrete cutoff (#1651), not just the phrase.

    The observation-frequency rows display a count (not a dollar value), so the
    cutoff renders as "2 or more" — the FilterSpec.value formatted to match the
    column it filters.
    """
    plan = _ranking_plan_with_hint("regularly")
    result = _simple_ranking_result()

    composition = compose_response(plan, result)

    full_lead = " ".join(composition.lead_lines)
    assert "interpreting 'regularly' as 2 or more" in full_lead


def test_rank_filter_prose_omits_hint_when_none() -> None:
    """Rank response prose does NOT include interpreting phrase when hint is None."""
    plan = _ranking_plan_with_hint(None)
    result = _simple_ranking_result()

    composition = compose_response(plan, result)

    full_lead = " ".join(composition.lead_lines)
    assert "interpreting" not in full_lead


# ---------------------------------------------------------------------------
# Step 3 — Renderer: lookup prose surfaces threshold_hint when present
# ---------------------------------------------------------------------------


def _lookup_plan_with_hint(hint: str | None) -> QueryPlan:
    """Build a lookup plan with a bonus threshold filter."""
    filters = [
        FilterSpec(
            field="performance pay bonuses",
            operator="greater_than_or_equal",
            value=500,
            threshold_hint=hint,
        )
    ]
    return QueryPlan(
        operation="lookup",
        question="Districts with real performance bonuses.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="performance pay bonuses")],
        filters=filters,
    )


def _simple_lookup_result() -> MetricLookupResult:
    return MetricLookupResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="TX"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="TX",
                metric_id=200,
                metric_name="Performance pay bonus amount",
                value="$1,000",
                display_value="$1,000",
                academic_year="2024 - 2025",
                citation_markers=[],
            ),
        ],
        citations=[],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up performance pay bonuses for covered districts.",
        methodology_codes=[
            MethodologyRef(code="lookup_default_district_order")
        ],
    )


def test_lookup_filter_prose_surfaces_threshold_hint() -> None:
    """Lookup response prose names the dollar cutoff (#1651), not just the phrase.

    The bonus rows display "$1,000" — a dollar value — so the cutoff renders as
    "$500 or more" (FilterSpec.value=500 formatted as currency to match the
    column).
    """
    plan = _lookup_plan_with_hint("real (not token)")
    result = _simple_lookup_result()

    composition = compose_response(plan, result)

    full_lead = " ".join(composition.lead_lines)
    assert "interpreting 'real (not token)' as $500 or more" in full_lead


def test_lookup_filter_prose_omits_hint_when_none() -> None:
    """Lookup response prose does NOT include interpreting phrase when hint is None."""
    plan = _lookup_plan_with_hint(None)
    result = _simple_lookup_result()

    composition = compose_response(plan, result)

    full_lead = " ".join(composition.lead_lines)
    assert "interpreting" not in full_lead


def test_multiple_filters_surfaces_first_hint() -> None:
    """When multiple filters carry threshold_hint, the first non-None hint is used."""
    plan = QueryPlan(
        operation="rank",
        question="Districts that observe regularly and have real bonuses.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="observation frequency")],
        filters=[
            FilterSpec(
                field="observation frequency",
                operator="greater_than_or_equal",
                value=2,
                threshold_hint="regularly",
            ),
            FilterSpec(
                field="performance pay bonuses",
                operator="greater_than_or_equal",
                value=500,
                threshold_hint="real (not token)",
            ),
        ],
    )
    result = _simple_ranking_result()

    composition = compose_response(plan, result)

    full_lead = " ".join(composition.lead_lines)
    # First hint wins, now naming its concrete cutoff (#1651). The ranking rows
    # display a count, so the cutoff renders as "2 or more".
    assert "interpreting 'regularly' as 2 or more" in full_lead
    # Second hint is NOT surfaced
    assert "real (not token)" not in full_lead


def test_state_only_filter_no_hint() -> None:
    """State-only filter with no threshold_hint produces no interpreting line."""
    plan = QueryPlan(
        operation="rank",
        question="California districts by observation frequency.",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="observation frequency")],
        filters=[
            FilterSpec(field="state", operator="equals", value="CA"),
        ],
    )
    result = _simple_ranking_result()

    composition = compose_response(plan, result)

    full_lead = " ".join(composition.lead_lines)
    assert "interpreting" not in full_lead


# ---------------------------------------------------------------------------
# Step 4 — Planner prompt: worked examples present in PLANNER_INSTRUCTIONS
# ---------------------------------------------------------------------------


def test_planner_instructions_contain_threshold_hint_paragraph() -> None:
    """PLANNER_INSTRUCTIONS includes the vague-quantifier threshold_hint paragraph."""
    from compass_backend.planning.planner import PLANNER_INSTRUCTIONS

    assert "threshold_hint" in PLANNER_INSTRUCTIONS


def test_planner_instructions_contain_regularly_example() -> None:
    """PLANNER_INSTRUCTIONS includes worked example for 'regularly' phrase."""
    from compass_backend.planning.planner import PLANNER_INSTRUCTIONS

    assert "regularly" in PLANNER_INSTRUCTIONS
    assert "threshold_hint" in PLANNER_INSTRUCTIONS


def test_planner_instructions_contain_a_couple_example() -> None:
    """PLANNER_INSTRUCTIONS includes worked example for 'a couple' phrase."""
    from compass_backend.planning.planner import PLANNER_INSTRUCTIONS

    assert "a couple" in PLANNER_INSTRUCTIONS


def test_planner_instructions_contain_real_not_token_example() -> None:
    """PLANNER_INSTRUCTIONS includes worked example for 'real (not token)' phrase."""
    from compass_backend.planning.planner import PLANNER_INSTRUCTIONS

    assert "real (not token)" in PLANNER_INSTRUCTIONS


def test_planner_instructions_no_prose_dispatch_on_hint() -> None:
    """PLANNER_INSTRUCTIONS does not branch on threshold_hint content — routing only in planner."""
    from compass_backend.planning.planner import PLANNER_INSTRUCTIONS

    # The instructions should instruct the planner to SET the hint, not branch on it
    # If the word 'if hint ==' appears, that would be wrong (prose-dispatch in planner)
    assert 'if hint ==' not in PLANNER_INSTRUCTIONS
    assert 'if threshold_hint ==' not in PLANNER_INSTRUCTIONS


# ---------------------------------------------------------------------------
# PR B Item 2 — count response prose surfaces threshold_hint
# ---------------------------------------------------------------------------


def _count_plan_with_hint(hint: str | None) -> QueryPlan:
    """Build a count plan with a metric filter that carries a vague-quantifier hint."""
    filters = [
        FilterSpec(
            field="observation frequency",
            operator="greater_than_or_equal",
            value=2,
            threshold_hint=hint,
        )
    ]
    return QueryPlan(
        operation="count",
        question="How many districts observe teachers regularly?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="observation frequency")],
        filters=filters,
    )


def _simple_count_result() -> MetricCountResult:
    from compass_backend.artifacts.results import ThresholdCountRow  # noqa: PLC0415

    return MetricCountResult(
        rows=[
            ThresholdCountRow(
                metric_id=100,
                metric_name="Number of formal classroom observations",
                display_value="42",
                academic_year="2024 - 2025",
                count=42,
                denominator=100,
                percent=42.0,
                filter_statement="observed at least 2 times",
            ),
        ],
        citations=[],
        total_considered=100,
        excluded_count=0,
        order_statement="Counted qualifying covered districts.",
        methodology_codes=[],
    )


def test_compose_count_surfaces_threshold_hint() -> None:
    """Count operations also surface the threshold_hint for vague-quantifier queries.

    Regression guard for composer.py _compose_count accepting plan: QueryPlan
    and appending hint_line (PR B Item 2).
    """
    plan = _count_plan_with_hint("regularly")
    result = _simple_count_result()

    composition = compose_response(plan, result)

    full_lead = " ".join(composition.lead_lines)
    assert "interpreting 'regularly' as 2 or more" in full_lead, (
        f"Count with threshold_hint='regularly' must surface the concrete cutoff "
        f"(#1651). Lead lines: {composition.lead_lines!r}"
    )


def test_compose_count_omits_hint_when_none() -> None:
    """Count response prose does NOT include interpreting phrase when hint is None."""
    plan = _count_plan_with_hint(None)
    result = _simple_count_result()

    composition = compose_response(plan, result)

    full_lead = " ".join(composition.lead_lines)
    assert "interpreting" not in full_lead, (
        f"Count without threshold_hint must not include interpreting phrase. "
        f"Lead lines: {composition.lead_lines!r}"
    )


def test_compose_count_leads_with_match_denominator() -> None:
    """Count lead states the denominator and match share in plain English.

    Regression guard for C1 (#1413) + C3 (#1415): the deterministic count answer
    leads with "Of N covered districts with data, X (Y%) match your criteria." The
    count, denominator, and (now that C3 grounds it on the row) the percent are
    all real artifact values, replacing the operator-flavored jargon lead.
    """
    plan = _count_plan_with_hint(None)
    result = _simple_count_result()  # count=42, denominator=100, percent=42.0

    composition = compose_response(plan, result)

    full_lead = " ".join(composition.lead_lines)
    assert (
        "Of 100 covered districts with data, 42 (42.0%) match your criteria." in full_lead
    ), (
        f"Count lead must surface denominator + grounded match percent. "
        f"Lead lines: {composition.lead_lines!r}"
    )
    assert "I counted qualifying covered districts" not in full_lead, (
        f"Operator-flavored jargon lead must be gone. "
        f"Lead lines: {composition.lead_lines!r}"
    )


# ---------------------------------------------------------------------------
# PR B Item 5 — coverage_rendered_decision log emission
# ---------------------------------------------------------------------------


def test_coverage_rendered_decision_logged_when_below_threshold() -> None:
    """_coverage_summary logs coverage_rendered_decision with rendered=True
    when coverage ratio is BELOW the threshold (line will emit).

    Verifies log_info is called with the right keys by mocking log_info.
    """
    import unittest.mock  # noqa: PLC0415
    from compass_backend.artifacts.coverage import (  # noqa: PLC0415
        CoverageBreakdown,
        CoverageFrame,
    )

    # Build a result with low coverage (2 of 10 = 20%, below 70% threshold)
    low_coverage_frame = CoverageFrame(
        universe_count=10,
        in_scope_count=10,
        addressed_count=2,
        real_data_count=2,
        not_reviewed_count=8,
        out_of_universe_count=0,
        breakdown=CoverageBreakdown(
            answer_value_count=2,
            metric_not_reviewed_count=8,
        ),
    )
    result = MetricLookupResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="TX"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="TX",
                metric_id=100,
                metric_name="Observation frequency",
                value="3",
                display_value="3",
                academic_year="2024 - 2025",
                citation_markers=[],
            ),
        ],
        citations=[],
        total_considered=10,
        excluded_count=0,
        order_statement="Looked up observation frequency.",
        methodology_codes=[],
        coverage_frame=low_coverage_frame,
    )
    plan = QueryPlan(
        operation="lookup",
        question="Show observation frequency.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="observation frequency")],
    )

    with unittest.mock.patch(
        "compass_backend.rendering.composer.log_info"
    ) as mock_log:
        composition = compose_response(plan, result)
        _ = composition  # result not checked here; we only check the log call

        # Verify log_info was called with coverage_rendered_decision
        calls = [c for c in mock_log.call_args_list if c.args and c.args[0] == "coverage_rendered_decision"]
        assert calls, (
            "log_info('coverage_rendered_decision', ...) must be called when "
            "coverage ratio is below threshold. "
            f"All log_info calls: {mock_log.call_args_list}"
        )
        call_kwargs = calls[0].kwargs
        assert call_kwargs.get("rendered") is True, (
            f"rendered=True expected for below-threshold coverage. Got: {call_kwargs}"
        )
        assert "coverage_ratio" in call_kwargs
        assert "threshold" in call_kwargs


# ---------------------------------------------------------------------------
# _ranking_coverage_label bug fix (#1478) — neutral noun for mixed-state pool
# ---------------------------------------------------------------------------


def _ranking_result_with_mixed_coverage() -> MetricRankingResult:
    """A ranking result whose in_scope_count > real_data_count.

    real_data_count=5 (covered), in_scope_count=8 (covered + not-reviewed +
    ina). The old label "N of M covered districts" was wrong because the
    denominator M includes rows that are NOT covered.
    """
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=i,
                district_name=f"District {i}",
                state="TX",
                metric_id=100,
                metric_name="Observation frequency",
                value=float(i),
                display_value=str(i),
                academic_year="2024 - 2025",
                rank=i,
                citation_markers=[],
            )
            for i in range(1, 6)
        ],
        citations=[],
        total_considered=8,
        excluded_count=0,
        order_statement="Ranked by observation frequency, highest to lowest.",
        methodology_codes=[],
        coverage_frame=CoverageFrame(
            universe_count=8,
            in_scope_count=8,
            addressed_count=5,
            real_data_count=5,
            not_reviewed_count=3,
            out_of_universe_count=0,
            breakdown=CoverageBreakdown(
                answer_value_count=5,
                metric_not_reviewed_count=3,
            ),
        ),
    )


def test_ranking_coverage_label_no_states_is_neutral() -> None:
    """_ranking_coverage_label returns a neutral noun, not 'covered districts'.

    Bug #1478: in_scope_count includes not-reviewed/ina/na rows, so the
    denominator in the ranking lead sentence is NOT exclusively covered rows.
    Calling it 'covered districts' mis-labels the pool. The fix returns
    'districts NCTQ tracks' (neutral) instead.
    """
    plan = QueryPlan(
        operation="rank",
        question="Which TX districts observe teachers most?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="observation frequency")],
    )
    result = _ranking_result_with_mixed_coverage()

    label = _ranking_coverage_label(plan, result)

    assert "covered districts" not in label, (
        f"_ranking_coverage_label must not claim the pool is exclusively covered. "
        f"Got: {label!r}"
    )
    assert "NCTQ tracks" in label, (
        f"_ranking_coverage_label must use the neutral noun 'NCTQ tracks'. "
        f"Got: {label!r}"
    )


def test_ranking_coverage_label_with_states_is_neutral() -> None:
    """_ranking_coverage_label with states uses neutral noun, not 'covered <state> districts'.

    Same bug #1478: when states are resolved the old code returned
    'covered TX districts' — still wrong for the same reason.
    """
    plan = QueryPlan(
        operation="rank",
        question="Which TX districts observe teachers most?",
        selection=SelectionSpec(scope="state", states=["TX"]),
        metrics=[MetricSpec(name="observation frequency")],
    )
    # Build a result that already carries a state-scoped selection so
    # _result_or_plan_states returns ["TX"] (MetricRankingResult is frozen).
    result = MetricRankingResult(
        selection=ResultSelection(
            scope="state",
            states=["TX"],
            districts=[
                SelectedDistrict(district_id=i, district_name=f"District {i}", state="TX")
                for i in range(1, 6)
            ],
        ),
        rows=[
            RankingRow(
                district_id=i,
                district_name=f"District {i}",
                state="TX",
                metric_id=100,
                metric_name="Observation frequency",
                value=float(i),
                display_value=str(i),
                academic_year="2024 - 2025",
                rank=i,
                citation_markers=[],
            )
            for i in range(1, 6)
        ],
        citations=[],
        total_considered=8,
        excluded_count=0,
        order_statement="Ranked by observation frequency, highest to lowest.",
        methodology_codes=[],
        coverage_frame=CoverageFrame(
            universe_count=8,
            in_scope_count=8,
            addressed_count=5,
            real_data_count=5,
            not_reviewed_count=3,
            out_of_universe_count=0,
            breakdown=CoverageBreakdown(
                answer_value_count=5,
                metric_not_reviewed_count=3,
            ),
        ),
    )

    label = _ranking_coverage_label(plan, result)

    assert "covered TX districts" not in label, (
        f"_ranking_coverage_label must not say 'covered TX districts'. Got: {label!r}"
    )
    assert "NCTQ tracks" in label, (
        f"_ranking_coverage_label must include 'NCTQ tracks'. Got: {label!r}"
    )
    assert "TX" in label, (
        f"_ranking_coverage_label must still name the state. Got: {label!r}"
    )


def test_ranking_lead_uses_neutral_noun_in_full_composition() -> None:
    """compose_response ranking lead does not say 'covered districts' for the denominator.

    End-to-end regression guard: the coverage_disclosures path in
    _compose_ranking calls _ranking_coverage_label; confirm the full lead
    sentence uses the neutral noun when coverage_disclosures are present.
    """
    from compass_backend.artifacts import CoverageDisclosure  # noqa: PLC0415

    plan = QueryPlan(
        operation="rank",
        question="Which districts observe teachers most?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="observation frequency")],
    )
    # Build the result with coverage_disclosures baked in (frozen model).
    result = MetricRankingResult(
        rows=[
            RankingRow(
                district_id=i,
                district_name=f"District {i}",
                state="TX",
                metric_id=100,
                metric_name="Observation frequency",
                value=float(i),
                display_value=str(i),
                academic_year="2024 - 2025",
                rank=i,
                citation_markers=[],
            )
            for i in range(1, 6)
        ],
        coverage_disclosures=[
            CoverageDisclosure(
                district_id=10,
                district_name="Missing District",
                state="TX",
                metric_id=100,
                metric_name="Observation frequency",
                academic_year="2024 - 2025",
                coverage_state="not_reviewed",
                reason="metric_not_reviewed",
                display="Not reviewed",
            )
        ],
        citations=[],
        total_considered=8,
        excluded_count=0,
        order_statement="Ranked by observation frequency, highest to lowest.",
        methodology_codes=[],
        coverage_frame=CoverageFrame(
            universe_count=8,
            in_scope_count=8,
            addressed_count=5,
            real_data_count=5,
            not_reviewed_count=3,
            out_of_universe_count=0,
            breakdown=CoverageBreakdown(
                answer_value_count=5,
                metric_not_reviewed_count=3,
            ),
        ),
    )

    composition = compose_response(plan, result)
    full_lead = " ".join(composition.lead_lines)

    assert "covered districts" not in full_lead, (
        f"Ranking lead must not say 'covered districts' as denominator noun. "
        f"Lead: {full_lead!r}"
    )
    assert "NCTQ tracks" in full_lead, (
        f"Ranking lead must use 'NCTQ tracks' neutral noun. Lead: {full_lead!r}"
    )


# ---------------------------------------------------------------------------
# #1651 — transparency note names the concrete cutoff + survives the voice pass
# ---------------------------------------------------------------------------


def test_threshold_hint_line_states_cutoff_number_one_arg() -> None:
    """_threshold_hint_line names the FilterSpec.value cutoff, not just the phrase.

    This is the #1651 headline regression. Called one-arg (no result), so it runs
    on both main and the branch with the SAME signature — on main it returns the
    phrase-only "from your question" form and the number "5000" is absent (red);
    on the branch it names "5,000 or more" (green). A real, non-vacuous assertion.
    """
    from compass_backend.rendering.composer import _threshold_hint_line  # noqa: PLC0415

    plan = _lookup_plan_with_hint("real (not token)")
    # value=500 on this fixture; assert the planner-chosen number reaches the line.
    line = _threshold_hint_line(plan)

    assert line is not None
    assert "500" in line, (
        f"#1651: the transparency note must state the concrete cutoff number the "
        f"planner applied, not only the user's phrase. Got: {line!r}"
    )
    assert "or more" in line, f"Cutoff direction must be stated. Got: {line!r}"


def test_threshold_hint_line_formats_dollar_cutoff_from_rows() -> None:
    """A dollar-valued column renders the cutoff as currency ("$500 or more").

    Currency-ness is read from the rendered rows' display_value, so the cutoff's
    format matches the column it filters.
    """
    from compass_backend.rendering.composer import _threshold_hint_line  # noqa: PLC0415

    plan = _lookup_plan_with_hint("real (not token)")
    result = _simple_lookup_result()  # display_value="$1,000" → currency

    line = _threshold_hint_line(plan, result)

    assert line == "(interpreting 'real (not token)' as $500 or more)", (
        f"Dollar column must render the cutoff as currency. Got: {line!r}"
    )


def test_filter_prevalence_path_surfaces_cutoff_with_denominator() -> None:
    """The filter_prevalence lookup lead carries BOTH the cutoff and the denominator.

    A single metric-value threshold filter is exactly what sets filter_prevalence,
    and that compose path took an early return that previously skipped the hint
    line entirely (#1651). It already states the denominator ("Of N … match the
    filter"); now it also names the cutoff, so the reader gets both on one path.
    """
    from compass_backend.artifacts.results import FilterPrevalenceSummary  # noqa: PLC0415

    plan = _lookup_plan_with_hint("real (not token)")  # value=500
    result = MetricLookupResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="TX"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="TX",
                metric_id=200,
                metric_name="Performance pay bonus amount",
                value="$1,000",
                display_value="$1,000",
                academic_year="2024 - 2025",
                citation_markers=[],
            ),
        ],
        citations=[],
        total_considered=10,
        excluded_count=0,
        order_statement="Looked up performance pay bonuses for covered districts.",
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
        filter_prevalence=FilterPrevalenceSummary(
            matched=7,
            denominator=23,
            percent=30.4,
            not_reviewed_count=0,
            na_count=0,
        ),
    )

    composition = compose_response(plan, result)
    full_lead = " ".join(composition.lead_lines)

    # Denominator (the path's own lead) and the cutoff (the #1651 addition).
    assert "match your criteria" in full_lead, (
        f"filter_prevalence path must keep its denominator lead. Lead: {full_lead!r}"
    )
    assert "interpreting 'real (not token)' as $500 or more" in full_lead, (
        f"filter_prevalence path must also name the cutoff (#1651). Lead: {full_lead!r}"
    )


def test_threshold_hint_line_degrades_to_phrase_without_scalar_value() -> None:
    """When the filter carries no scalar value, fall back to the phrase-only form.

    The renderer must never invent a number it was not given — an is_missing /
    anchor filter has value=None, so the note degrades gracefully.
    """
    from compass_backend.rendering.composer import _threshold_hint_line  # noqa: PLC0415

    plan = QueryPlan(
        operation="lookup",
        question="Districts with real performance bonuses.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="performance pay bonuses")],
        filters=[
            FilterSpec(
                field="performance pay bonuses",
                operator="is_not_missing",
                threshold_hint="real (not token)",
            )
        ],
    )

    line = _threshold_hint_line(plan)

    assert line == "(interpreting 'real (not token)' from your question)", (
        f"Valueless filter must degrade to the phrase-only note. Got: {line!r}"
    )


def test_threshold_hint_line_is_sealed_against_voice_pass() -> None:
    """The emitted note is caught by the answer-layer caveat seal (#1651 leak 2).

    Couples the seal marker to the ACTUAL emitted line (not just the marker list),
    so the voice pass cannot silently delete the transparency note and a future
    reword can't drift the line out from under the marker. Deterministic — no LLM.
    """
    from compass_backend.answer_layer.briefs import (  # noqa: PLC0415
        canonical_caveat_fragments,
    )
    from compass_backend.rendering.composer import _threshold_hint_line  # noqa: PLC0415

    plan = _lookup_plan_with_hint("real (not token)")
    result = _simple_lookup_result()
    line = _threshold_hint_line(plan, result)
    assert line is not None

    body = (
        "I looked up Maximum annual performance pay bonus for covered districts.\n"
        f"{line}\n"
    )
    sealed = canonical_caveat_fragments(body)

    assert line in sealed, (
        f"The transparency note must be sealed so the voice pass preserves it. "
        f"Emitted line: {line!r}; sealed fragments: {sealed!r}"
    )


def test_provenance_guard_allows_threshold_cutoff_token() -> None:
    """The provenance guard must not reject the cutoff number the note surfaces.

    The cutoff (FilterSpec.value) lives in the PLAN, not the result artifact, so
    without the #1651 allowance "$5,000" trips the numeric-token-provenance guard
    (severity=error → degraded render, no voice pass). RED on main: the guard sees
    "5000" in the body, never in result.model_dump_json(), and flags it.
    """
    from compass_backend.quality.validators.coverage import (  # noqa: PLC0415
        _validate_numeric_token_provenance,
    )

    plan = QueryPlan(
        operation="lookup",
        question="Districts with real performance pay - not just token bonuses",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Maximum annual performance pay bonus, if eligible")],
        filters=[
            FilterSpec(
                field="Maximum annual performance pay bonus, if eligible",
                operator="greater_than_or_equal",
                value=5000,
                kind="metric_value",
                threshold_hint="real (not token)",
            )
        ],
    )
    # A result whose only rows are ABOVE the cutoff — 5000 itself is NOT in the
    # artifact, so the token's only provenance is the plan filter.
    result = MetricLookupResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=200,
                metric_name="Maximum annual performance pay bonus, if eligible",
                value="$15,560.76",
                display_value="$15,560.76",
                academic_year="2024 - 2025",
                citation_markers=[],
            ),
        ],
        citations=[],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up maximum performance pay bonus for covered districts.",
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
    )
    body = (
        "I looked up Maximum annual performance pay bonus for covered districts.\n"
        "(interpreting 'real (not token)' as $5,000 or more)\n"
        "| District | $15,560.76 |\n"
    )

    # Without the plan (the main-HEAD behavior), the cutoff IS rejected — the
    # token's only provenance is the plan filter, so this proves the guard really
    # would block the rendered note and the allowance is doing real work.
    flagged_no_plan = {
        token
        for finding in _validate_numeric_token_provenance(result, body)
        for token in finding.metadata.get("tokens", [])
    }
    assert "5000" in flagged_no_plan, (
        f"Sanity: without the plan, the cutoff has no artifact provenance and the "
        f"guard must flag it. Flagged: {flagged_no_plan!r}"
    )

    findings = _validate_numeric_token_provenance(result, body, plan)
    flagged = {
        token
        for finding in findings
        for token in finding.metadata.get("tokens", [])
    }
    assert "5000" not in flagged, (
        f"#1651: the planner-chosen cutoff the note surfaces must be a licensed "
        f"token. Flagged: {flagged!r}"
    )

    # The allowance is one-to-one: an ungrounded number still trips the guard.
    body_bad = body + "\nSome stray 99999 token.\n"
    findings_bad = _validate_numeric_token_provenance(result, body_bad, plan)
    flagged_bad = {
        token
        for finding in findings_bad
        for token in finding.metadata.get("tokens", [])
    }
    assert "99999" in flagged_bad, (
        f"The guard must still reject genuinely ungrounded tokens. "
        f"Flagged: {flagged_bad!r}"
    )
