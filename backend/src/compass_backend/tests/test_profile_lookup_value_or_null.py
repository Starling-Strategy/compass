"""Profile lookup is value-or-null over the *resolved* selection.

The cross-turn follow-up bug: "show <profile metric> for those districts" after
comparing e.g. Denver, Dallas, Buffalo was BLOCKED with
``selection_intended_geography_mismatch``. Root cause —
``db/catalog.py::lookup_nces_profile_values`` silently drops a requested district
with 0 or >1 ``nces_districts`` name matches (Buffalo School District), so the
lookup returned only Denver + Dallas rows. ``build_profile_lookup_result`` then
built ``result.selection.districts`` from those rows alone, dropping Buffalo/NY —
and the independent ``intended_geography`` Gate-3 saw the user-named NY uncovered
and refused the whole turn, hiding Denver's and Dallas's perfectly good data.

The fix: when ``build_profile_lookup_result`` is given the resolved selection, it
emits one row per (resolved district × field) — a covered value where the lookup
found one, an honest "no NCES data" cell otherwise — so ``resolved_states`` stays
complete and the geography validator goes silent as a CONSEQUENCE (zero change to
the validator / grounding guard).

These tests verify the BUILD function and the validator behaviour it drives; they
do not re-prove that the live cross-turn ``resolve_selection`` resolves Buffalo to
NY (a different path, verified against the live DB in the issue).
"""

from __future__ import annotations

import asyncio

from compass_backend.artifacts import (
    ResultSelection,
    SelectedDistrict,
    district_coverage_summary_for_rows,
)
from compass_backend.catalog import NCESFieldCandidate
from compass_backend.catalog.resolution import ProfileFieldValueRow
from compass_backend.contracts.planning import (
    ProfileFieldSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.execution.profile import build_profile_lookup_result
from compass_backend.quality.validators import registry as _registry
from compass_backend.quality.validators.coverage import _validate_coverage_state
from compass_backend.quality.validators.intended_geography import (
    _validate_intended_geography,
)
from compass_backend.rendering.writer import _render_result
from compass_backend.tests.conftest import make_evaluator_context


# ─── Fixtures ───────────────────────────────────────────────────────────────


def _enrollment_field() -> NCESFieldCandidate:
    return NCESFieldCandidate(
        field_key="enrollment",
        label="Enrollment",
        data_type="numeric",
    )


def _profile_row(
    district_id: int,
    district_name: str,
    state: str,
    *,
    field_key: str,
    label: str,
    value: float,
    display_value: str,
) -> ProfileFieldValueRow:
    return ProfileFieldValueRow(
        requested_name=district_name,
        district_id=district_id,
        district_name=district_name,
        state=state,
        field_key=field_key,
        label=label,
        value=value,
        display_value=display_value,
        academic_year="2024 - 2025",
        source="profile_field",
        source_label="NCES district profile",
        provenance="NCES directory year: 2024",
        covered_by_compass=True,
    )


def _enrollment_row(
    district_id: int, district_name: str, state: str, value: float
) -> ProfileFieldValueRow:
    return _profile_row(
        district_id,
        district_name,
        state,
        field_key="enrollment",
        label="Enrollment",
        value=value,
        display_value=f"{int(value):,}",
    )


def _three_district_plan() -> QueryPlan:
    """A cross-turn follow-up plan whose prose unambiguously names NY (Buffalo).

    ``intended_geography`` derives NY only from the explicit ``, NY`` suffix —
    "buffalo" is intentionally NOT in its unambiguous-city map — so this phrase
    is the exact signal that BLOCKED the pre-fix turn.
    """

    return QueryPlan(
        operation="profile_lookup",
        question="How many students are enrolled in each of those districts?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver", "Dallas", "Buffalo School District, NY"],
        ),
        profile_fields=[ProfileFieldSpec(name="enrollment")],
    )


def _three_district_selection() -> ResultSelection:
    """The resolved selection the planner/resolver carried across the turn —
    all three districts WITH states, including Buffalo/NY."""

    return ResultSelection(
        scope="named_districts",
        districts=[
            SelectedDistrict(district_id=10, district_name="Denver County 1", state="CO"),
            SelectedDistrict(district_id=20, district_name="Dallas ISD", state="TX"),
            SelectedDistrict(
                district_id=30, district_name="Buffalo School District", state="NY"
            ),
        ],
    )


# ─── The bug: one resolved district has no NCES row ─────────────────────────


def test_missing_district_becomes_no_data_row_and_unblocks_geography() -> None:
    """Buffalo's NCES row was dropped; it must still appear (value-or-null) so the
    resolved NY geography is covered and the turn is NOT blocked."""

    # Lookup returned only Denver + Dallas — Buffalo dropped by the name-match.
    rows = [
        _enrollment_row(10, "Denver County 1", "CO", 90000),
        _enrollment_row(20, "Dallas ISD", "TX", 145000),
    ]
    result = build_profile_lookup_result(
        _three_district_plan(),
        _enrollment_field(),
        rows,
        academic_year="2024 - 2025",
        resolved_selection=_three_district_selection(),
    )

    # The selection carries ALL THREE resolved districts (so resolved_states is
    # complete: CO, TX, NY) — not just the two the lookup returned.
    selected_states = {d.state for d in result.selection.districts}
    assert selected_states == {"CO", "TX", "NY"}
    assert len(result.selection.districts) == 3

    # One row per (district × field): Denver + Dallas covered, Buffalo no-data.
    by_district = {row.district_name: row for row in result.rows}
    assert set(by_district) == {"Denver County 1", "Dallas ISD", "Buffalo School District"}

    denver = by_district["Denver County 1"]
    assert denver.value == 90000
    assert denver.coverage_state == "covered"
    assert denver.citation_markers  # NCES citation attached

    buffalo = by_district["Buffalo School District"]
    assert buffalo.value is None
    assert buffalo.coverage_state != "covered"
    assert buffalo.coverage_reason == "unavailable"
    assert buffalo.display_value == "No NCES data"
    assert buffalo.coverage_display == buffalo.display_value  # coverage-state validator
    assert buffalo.citation_markers == []
    assert buffalo.covered_by_compass is False

    # The whole point: the geography Gate-3 no longer fires — NY is covered by a
    # resolved district — and the coverage-state frame/breakdown invariants hold.
    plan = _three_district_plan()
    assert _validate_intended_geography(plan, result) == []
    assert _validate_coverage_state(result) == []


def test_no_data_row_renders_honest_cell() -> None:
    """The renderer shows the missing district with a clear no-data cell — it does
    not crash, drop the district, or imply data exists."""

    rows = [
        _enrollment_row(10, "Denver County 1", "CO", 90000),
        _enrollment_row(20, "Dallas ISD", "TX", 145000),
    ]
    result = build_profile_lookup_result(
        _three_district_plan(),
        _enrollment_field(),
        rows,
        academic_year="2024 - 2025",
        resolved_selection=_three_district_selection(),
    )

    body = _render_result(_three_district_plan(), result)
    assert "Buffalo School District" in body
    assert "No NCES data" in body
    # Denver's real figure is still rendered (not hidden by the block).
    assert "90,000" in body


# ─── Multi-field: one district covered for field A, no-data for field B ─────


def test_multi_field_partial_coverage_per_cell_no_data() -> None:
    """A multi-field follow-up emits a no-data cell only for the missing
    (district, field) pair — covered cells keep their value and shared NCES
    citation, and the mixed-reason coverage breakdown stays internally valid."""

    plan = QueryPlan(
        operation="profile_lookup",
        question="Show enrollment and pupil/teacher ratio for those districts.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver", "Buffalo School District, NY"],
        ),
        profile_fields=[
            ProfileFieldSpec(name="enrollment"),
            ProfileFieldSpec(name="pupil teacher ratio"),
        ],
    )
    selection = ResultSelection(
        scope="named_districts",
        districts=[
            SelectedDistrict(district_id=10, district_name="Denver County 1", state="CO"),
            SelectedDistrict(
                district_id=30, district_name="Buffalo School District", state="NY"
            ),
        ],
    )
    fields = [
        NCESFieldCandidate(field_key="enrollment", label="Enrollment", data_type="numeric"),
        NCESFieldCandidate(
            field_key="pupil_teacher_ratio",
            label="Pupil/teacher ratio",
            data_type="numeric",
        ),
    ]
    # Denver covered for BOTH fields; Buffalo covered for enrollment only (no
    # pupil/teacher-ratio row came back).
    rows = [
        _enrollment_row(10, "Denver County 1", "CO", 90000),
        _profile_row(
            10, "Denver County 1", "CO",
            field_key="pupil_teacher_ratio", label="Pupil/teacher ratio",
            value=15.2, display_value="15.2",
        ),
        _enrollment_row(30, "Buffalo School District", "NY", 31000),
    ]
    result = build_profile_lookup_result(
        plan, fields, rows, academic_year="2024 - 2025", resolved_selection=selection
    )

    # 2 districts × 2 fields = 4 cells, one of them no-data.
    assert len(result.rows) == 4
    cells = {(row.district_name, row.field_key): row for row in result.rows}

    missing = cells[("Buffalo School District", "pupil_teacher_ratio")]
    assert missing.value is None
    assert missing.coverage_state != "covered"
    assert missing.coverage_reason == "unavailable"
    assert missing.display_value == "No NCES data"

    # Buffalo's enrollment cell is still a real covered value.
    buffalo_enroll = cells[("Buffalo School District", "enrollment")]
    assert buffalo_enroll.value == 31000
    assert buffalo_enroll.coverage_state == "covered"

    # enrollment + pupil/teacher-ratio share the one ELSI page → one citation,
    # every covered cell references it; the no-data cell references none.
    assert len(result.citations) == 1
    shared_marker = result.citations[0].marker
    for (name, field_key), row in cells.items():
        if row.coverage_state == "covered":
            assert row.citation_markers == [shared_marker], (name, field_key)
        else:
            assert row.citation_markers == []

    assert _validate_intended_geography(plan, result) == []
    assert _validate_coverage_state(result) == []


# ─── Guardrail: the all-covered case is unchanged ───────────────────────────


def test_all_covered_selection_has_no_no_data_rows() -> None:
    """When every resolved district has an NCES row, the result is all covered —
    no spurious no-data rows, validators silent."""

    rows = [
        _enrollment_row(10, "Denver County 1", "CO", 90000),
        _enrollment_row(20, "Dallas ISD", "TX", 145000),
        _enrollment_row(30, "Buffalo School District", "NY", 31000),
    ]
    result = build_profile_lookup_result(
        _three_district_plan(),
        _enrollment_field(),
        rows,
        academic_year="2024 - 2025",
        resolved_selection=_three_district_selection(),
    )

    assert len(result.rows) == 3
    assert all(row.coverage_state == "covered" for row in result.rows)
    assert all(row.value is not None for row in result.rows)
    assert {d.state for d in result.selection.districts} == {"CO", "TX", "NY"}
    assert _validate_intended_geography(_three_district_plan(), result) == []
    assert _validate_coverage_state(result) == []


# ─── The no-data row must not break Citation or overstate coverage ──────────


def _buffalo_no_data_result():
    rows = [
        _enrollment_row(10, "Denver County 1", "CO", 90000),
        _enrollment_row(20, "Dallas ISD", "TX", 145000),
    ]
    return build_profile_lookup_result(
        _three_district_plan(),
        _enrollment_field(),
        rows,
        academic_year="2024 - 2025",
        resolved_selection=_three_district_selection(),
    )


def test_no_data_row_passes_the_nces_citation_criterion() -> None:
    """The reject-severity ``profile_rows_carry_nces_citation`` criterion must NOT
    flag the no-data row for lacking a citation — only COVERED profile rows must
    cite NCES. Without the covered-row carve-out this fired on the PR's own
    cross-turn scenario (a wrong Citation FAIL)."""

    result = _buffalo_no_data_result()
    validator = _registry.lookup("profile_rows_carry_nces_citation")
    ctx = make_evaluator_context(result=result)
    outcome = asyncio.run(validator(ctx, {}))
    assert outcome.outcome == "pass", outcome.reason


def test_no_data_district_is_not_counted_as_having_current_data() -> None:
    """The availability summary must not overstate coverage: the no-data district
    (reason="unavailable") is a GAP, not "current reviewed data" — so the D9
    sentence stays honest ("2 have current reviewed data; 1 hasn't been
    reviewed"), never "3 have current reviewed data"."""

    result = _buffalo_no_data_result()
    summary = district_coverage_summary_for_rows(result.rows, result.selection)
    assert summary.districts_asked == 3
    assert summary.districts_with_current_data == 2  # NOT 3 — Buffalo has no data
    assert summary.districts_not_reviewed == 1
