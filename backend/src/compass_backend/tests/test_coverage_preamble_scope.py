"""Scope-aware coverage preamble (#1827).

The #1514 D9 availability preamble ("Of the N districts you asked about, …")
hardcoded "you asked about" even when the population was *system-derived* — a
top-N size cut, a whole state, or the full covered universe. The reported bug
(session ``7302e429``, "How often do large districts evaluate teachers?") shows
a top-10 ``largest_districts`` selection narrated as "Of the 10 districts you
asked about…", crediting the user with a list they never named.

These tests pin the phrasing branch per ``ResultSelection.scope``: the derived
scopes name how they were derived, and ``named_districts`` keeps the original
"you asked about" wording untouched.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    CoverageFrame,
    MetricLookupResult,
    MetricValueRow,
    ResultSelection,
    SelectedDistrict,
    district_coverage_summary_for_rows,
)
from compass_backend.contracts import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.rendering.composer import compose_response


def _lookup_result(selection: ResultSelection) -> MetricLookupResult:
    """A two-district lookup: one current, one not-reviewed, so the preamble
    fires (``districts_not_reviewed > 0``) and the derived summary is honest.
    """

    districts = list(selection.districts) or [
        SelectedDistrict(district_id=1, district_name="Alpha", state="FL"),
        SelectedDistrict(district_id=2, district_name="Bravo", state="FL"),
    ]
    rows = [
        MetricValueRow(
            district_id=districts[0].district_id,
            district_name=districts[0].district_name,
            state=districts[0].state,
            metric_id=39,
            metric_name="Minimum number of formal observations",
            value="3",
            display_value="3",
            academic_year="2024 - 2025",
            source="policy_answer",
            coverage_state="covered",
            coverage_display="3",
            coverage_reason="answer_value",
        ),
        MetricValueRow(
            district_id=districts[1].district_id,
            district_name=districts[1].district_name,
            state=districts[1].state,
            metric_id=39,
            metric_name="Minimum number of formal observations",
            value=None,
            display_value="Not reviewed",
            academic_year="2024 - 2025",
            source="coverage_state",
            citation_markers=[],
            coverage_state="not_reviewed",
            coverage_display="Not reviewed",
            coverage_reason="metric_not_reviewed",
        ),
    ]
    result = MetricLookupResult(
        selection=selection,
        rows=rows,
        citations=[],
        total_considered=len(rows),
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=1,
            real_data_count=1,
            not_reviewed_count=1,
            out_of_universe_count=0,
            coverage_ratio=0.5,
        ),
        order_statement="Looked up.",
    )
    return result.model_copy(
        update={
            "district_coverage": district_coverage_summary_for_rows(
                result.rows, result.selection
            )
        }
    )


def _lookup_plan(selection: SelectionSpec) -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="How often do large districts evaluate teachers?",
        selection=selection,
        metrics=[MetricSpec(name="formal observations")],
    )


def test_largest_districts_preamble_names_derived_scope() -> None:
    """#1827 regression: a system-derived ``largest_districts`` selection must
    NOT be narrated as "districts you asked about" — the reported bug."""

    selection = ResultSelection(
        scope="largest_districts",
        districts=[
            SelectedDistrict(district_id=1, district_name="Alpha", state="FL"),
            SelectedDistrict(district_id=2, district_name="Bravo", state="FL"),
        ],
        states=["FL"],
    )
    plan = _lookup_plan(SelectionSpec(scope="largest_districts", states=["FL"]))

    lead = compose_response(plan, _lookup_result(selection)).lead_lines[0]

    assert "you asked about" not in lead
    assert "largest districts" in lead
    assert lead.startswith("Of the 2 largest districts,")


def test_state_scope_preamble_names_the_state() -> None:
    """A ``state`` selection reads "in {state}", not "you asked about"."""

    selection = ResultSelection(scope="state", states=["FL"])
    plan = _lookup_plan(SelectionSpec(scope="state", states=["FL"]))

    lead = compose_response(plan, _lookup_result(selection)).lead_lines[0]

    assert "you asked about" not in lead
    assert "in Florida" in lead


def test_all_covered_districts_preamble_credits_compass() -> None:
    """An ``all_covered_districts`` selection reads "Compass covers"."""

    selection = ResultSelection(scope="all_covered_districts", states=[])
    plan = _lookup_plan(SelectionSpec(scope="all_covered_districts"))

    lead = compose_response(plan, _lookup_result(selection)).lead_lines[0]

    assert "you asked about" not in lead
    assert "Compass covers" in lead


def test_named_districts_preamble_unchanged() -> None:
    """The pre-#1827 wording is preserved for a genuinely user-named selection —
    the default path must not regress."""

    selection = ResultSelection(
        scope="named_districts",
        districts=[
            SelectedDistrict(district_id=1, district_name="Alpha", state="FL"),
            SelectedDistrict(district_id=2, district_name="Bravo", state="FL"),
        ],
    )
    plan = _lookup_plan(
        SelectionSpec(scope="named_districts", districts=["Alpha", "Bravo"])
    )

    lead = compose_response(plan, _lookup_result(selection)).lead_lines[0]

    assert lead.startswith("Of the 2 districts you asked about,")
