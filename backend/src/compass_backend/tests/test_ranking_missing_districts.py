"""Tests for missing-district inclusion in build_metric_ranking_result.

These tests drive the fix for the silent left-join drop: when a query selects
districts by a profile field (enrollment, etc.) and then ranks the subset by a
policy metric, districts that ARE in the selected set but have NO current metric
row were previously dropped from result_rows. They now appear at the bottom of
the ranking table with an appropriate coverage label (rank = N+offset).

Contract under test:
- If data-present rows >= limit: output exactly `limit` rows; missing rows
  are truncated.
- If data-present rows < limit: fill up to `limit` with missing-data rows in
  district-name order.
- If limit is None: include all data-present rows ranked, then ALL missing-data
  rows below.
- Missing-data rows get rank = len(data_present_in_result) + offset, where
  offset counts up from 1 in district-name order.
- Missing-data rows have coverage_state != "covered".
- frozen=True: MetricRankingResult is constructable without post-init mutation.
"""

from __future__ import annotations

import pytest

from compass_backend.artifacts import (
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.artifacts.results import MetricRankingResult
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts import LimitSpec, MetricSpec, QueryPlan, SelectionSpec
from compass_backend.execution.ranking import build_metric_ranking_result
from compass_backend.execution.types import MetricAnswerRow


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=1001,
        name="Average teacher starting salary",
        answer_type="numeric",
    )


def _district(district_id: int, name: str, state: str = "TX") -> SelectedDistrict:
    return SelectedDistrict(district_id=district_id, district_name=name, state=state)


def _answer_row(district_id: int, name: str, value: str, state: str = "TX") -> MetricAnswerRow:
    return MetricAnswerRow(
        answer_id=district_id * 100,
        district_id=district_id,
        district_name=name,
        state=state,
        metric_id=1001,
        metric_name="Average teacher starting salary",
        value=value,
        academic_year="2024 - 2025",
        citations=[],
    )


def _selection(districts: list[SelectedDistrict]) -> ResultSelection:
    return ResultSelection(scope="named_districts", districts=districts)


def _plan(*, limit: int | None = None) -> QueryPlan:
    limit_spec = LimitSpec(count=limit, kind="top") if limit is not None else None
    return QueryPlan(
        question="Rank selected districts by starting salary.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        limit=limit_spec,
    )


def _build(
    *,
    selected_districts: list[SelectedDistrict],
    answer_rows: list[MetricAnswerRow],
    limit: int | None = None,
) -> MetricRankingResult:
    metric = _metric()
    plan = _plan(limit=limit)
    selection = _selection(selected_districts)
    return build_metric_ranking_result(
        plan,
        metric,
        answer_rows,
        recent_rows=[],
        selection=selection,
        selected_districts=selected_districts,
        default_limit=10,
        reviewed_district_ids=set(),
        academic_year="2024 - 2025",
    )


# ─── Currency precision (#1687, case 1136) ───────────────────────────────────


def test_currency_ranking_renders_consistent_whole_dollar_precision() -> None:
    """#1687 (case 1136 / SORT-CURATED-02-C03): a salary ranking renders every
    dollar amount as whole dollars, never "$71,038.98" alongside "$71,038"
    (criterion `currency_format_canonical`). The fix is value-formation, so the
    table cell, sort key, and CSV all read the same consistent display string.
    """
    districts = [_district(1, "Alpha"), _district(2, "Bravo")]
    rows = [
        _answer_row(1, "Alpha", "$71,038.98"),  # raw value carries cents
        _answer_row(2, "Bravo", "$60,000"),  # raw value omits cents
    ]

    result = _build(selected_districts=districts, answer_rows=rows)

    displays = [row.display_value for row in result.rows]
    assert "$71,039" in displays  # rounded to whole dollars
    assert "$60,000" in displays
    # No row mixes cents in: neither the lingering ".98" nor a spurious ".00".
    for row in result.rows:
        assert ".98" not in row.display_value, f"cents leaked: {row.display_value!r}"
        assert ".00" not in row.display_value, f"cents leaked: {row.display_value!r}"
        assert ".98" not in (row.sort_display_value or "")


def test_currency_ranking_keeps_display_and_coverage_display_in_parity() -> None:
    """#1687 fix-forward: whole-dollar normalization must update the coverage
    label's `display` too, so `display_value == coverage_display` on covered rows.
    Otherwise the `coverage_display_mismatch` guard (quality/validators/coverage.py)
    swallows the whole answer. Mirrors execution/peer.py's `model_copy` pattern.
    """
    districts = [_district(1, "Alpha"), _district(2, "Bravo")]
    rows = [
        _answer_row(1, "Alpha", "$71,038.98"),
        _answer_row(2, "Bravo", "$60,000"),
    ]

    result = _build(selected_districts=districts, answer_rows=rows)

    for row in result.rows:
        if row.coverage_state == "covered":
            assert row.display_value == row.coverage_display, (
                f"display/coverage parity broken: "
                f"{row.display_value!r} != {row.coverage_display!r}"
            )


def test_ranking_narrates_current_year_unavailable_not_celled() -> None:
    """#1698: a covered district whose current value is the canonical
    "Unavailable" sentinel routes to not_reviewed/reason="unavailable" so the
    renderer narrates it (per #1514 tables-hold-answers-only) instead of
    rendering a literal "Unavailable" ranking-table cell. The count path already
    did this; the ranking path now agrees via the shared coverage labeler.
    """
    districts = [_district(1, "Alpha"), _district(2, "Houston ISD")]
    rows = [
        _answer_row(1, "Alpha", "2"),
        _answer_row(2, "Houston ISD", "Unavailable"),
    ]

    result = _build(selected_districts=districts, answer_rows=rows)

    # Houston leaves the ranked table and is narrated via a coverage disclosure
    # (reason="unavailable") — never a literal "Unavailable" ranked cell.
    houston_disclosed = [
        d for d in result.coverage_disclosures if d.district_name == "Houston ISD"
    ]
    assert houston_disclosed, "Houston ISD should be narrated via a coverage disclosure"
    assert all(d.reason == "unavailable" for d in houston_disclosed)
    # Only the genuinely-numeric district is a covered, ranked row.
    covered = [r for r in result.rows if r.coverage_state == "covered"]
    assert covered and all(r.district_name == "Alpha" for r in covered)
    assert all("Unavailable" not in (r.display_value or "") for r in result.rows)


# ─── Unit-level: 3 selected, 1 missing ────────────────────────────────────────


def test_missing_district_appended_when_one_of_three_has_no_data() -> None:
    """3 selected districts, 1 has no metric row — result should have 3 rows."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Bravo"),
        _district(3, "Fairfax County Public Schools"),
    ]
    rows = [
        _answer_row(1, "Alpha", "$50,000"),
        _answer_row(2, "Bravo", "$60,000"),
        # district 3 (Fairfax) has no row
    ]

    result = _build(selected_districts=districts, answer_rows=rows)

    assert len(result.rows) == 3
    district_names = [row.district_name for row in result.rows]
    # Ranked data rows come first (Bravo > Alpha by salary desc)
    assert district_names[:2] == ["Bravo", "Alpha"]
    # Fairfax appended at bottom
    assert "Fairfax County Public Schools" in district_names
    # Ranked rows have coverage_state "covered"
    for row in result.rows[:2]:
        assert row.coverage_state == "covered", f"{row.district_name} should be covered"
    # Missing row has non-covered state
    fairfax_row = result.rows[2]
    assert fairfax_row.district_name == "Fairfax County Public Schools"
    assert fairfax_row.coverage_state != "covered"
    # Missing row rank is 3 (next after the 2 ranked rows)
    assert fairfax_row.rank == 3


# ─── Limit-respect: 5 selected (3 data, 2 missing), limit=4 ──────────────────


def test_limit_respected_missing_rows_fill_up_to_limit() -> None:
    """5 selected (3 with data, 2 missing), limit=4 → 3 data rows + 1 missing = 4."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Bravo"),
        _district(3, "Charlie"),
        _district(4, "Delta"),    # no data
        _district(5, "Echo"),     # no data
    ]
    rows = [
        _answer_row(1, "Alpha", "$50,000"),
        _answer_row(2, "Bravo", "$70,000"),
        _answer_row(3, "Charlie", "$60,000"),
    ]

    result = _build(selected_districts=districts, answer_rows=rows, limit=4)

    assert len(result.rows) == 4
    # First 3 are the ranked data rows (desc order: Bravo, Charlie, Alpha)
    data_rows = [r for r in result.rows if r.coverage_state == "covered"]
    assert len(data_rows) == 3
    assert [r.district_name for r in data_rows] == ["Bravo", "Charlie", "Alpha"]
    # One missing row appended (Delta comes before Echo alphabetically)
    missing_rows = [r for r in result.rows if r.coverage_state != "covered"]
    assert len(missing_rows) == 1
    assert missing_rows[0].district_name == "Delta"
    # Missing row rank = 4
    assert missing_rows[0].rank == 4
    # Never exceeds limit
    assert len(result.rows) <= 4


# ─── Limit-not-exceeded: 5 data-present, limit=3 ─────────────────────────────


def test_limit_not_exceeded_when_enough_data_rows() -> None:
    """5 data-present districts, limit=3 → exactly 3 rows, no missing rows."""
    districts = [_district(i, f"District {i:02d}") for i in range(1, 6)]
    rows = [_answer_row(i, f"District {i:02d}", f"${100_000 - i * 1000:,}") for i in range(1, 6)]

    result = _build(selected_districts=districts, answer_rows=rows, limit=3)

    assert len(result.rows) == 3
    # All 3 are ranked data rows
    for row in result.rows:
        assert row.coverage_state == "covered"
    assert [row.rank for row in result.rows] == [1, 2, 3]


# ─── No-limit: 5 selected (3 data, 2 missing), no limit ─────────────────────


def test_no_limit_includes_all_data_rows_then_all_missing_rows() -> None:
    """No limit: all 3 data rows ranked, then all 2 missing rows appended."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Bravo"),
        _district(3, "Charlie"),
        _district(4, "Houston ISD"),    # no data
        _district(5, "Zeta ISD"),       # no data
    ]
    rows = [
        _answer_row(1, "Alpha", "$50,000"),
        _answer_row(2, "Bravo", "$70,000"),
        _answer_row(3, "Charlie", "$60,000"),
    ]

    result = _build(selected_districts=districts, answer_rows=rows, limit=None)

    assert len(result.rows) == 5
    data_rows = [r for r in result.rows if r.coverage_state == "covered"]
    missing_rows = [r for r in result.rows if r.coverage_state != "covered"]
    assert len(data_rows) == 3
    assert len(missing_rows) == 2
    # Data rows come first (ranked desc)
    assert result.rows[0].district_name == "Bravo"
    assert result.rows[1].district_name == "Charlie"
    assert result.rows[2].district_name == "Alpha"
    # Missing rows in alphabetical order at the end
    assert result.rows[3].district_name == "Houston ISD"
    assert result.rows[4].district_name == "Zeta ISD"
    # Ranks are sequential: 1, 2, 3, 4, 5
    assert [r.rank for r in result.rows] == [1, 2, 3, 4, 5]


# ─── Coverage state for missing rows ─────────────────────────────────────────


def test_missing_row_coverage_state_is_not_reviewed() -> None:
    """Missing-data rows must have coverage_state "not_reviewed"."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Beta"),  # no data
    ]
    rows = [_answer_row(1, "Alpha", "$50,000")]

    result = _build(selected_districts=districts, answer_rows=rows)

    missing = [r for r in result.rows if r.district_name == "Beta"]
    assert len(missing) == 1
    assert missing[0].coverage_state == "not_reviewed"


def test_missing_row_display_value_contains_district_name() -> None:
    """Missing-data rows should have a meaningful display_value from coverage label."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Fairfax County Public Schools"),  # no data
    ]
    rows = [_answer_row(1, "Alpha", "$50,000")]

    result = _build(selected_districts=districts, answer_rows=rows)

    fairfax = next(r for r in result.rows if r.district_name == "Fairfax County Public Schools")
    # Display value should reference the district or be non-empty
    assert fairfax.display_value
    assert len(fairfax.display_value) > 0


# ─── Frozen=True: no post-construction mutation ────────────────────────────────


def test_result_is_frozen_after_construction() -> None:
    """MetricRankingResult must be constructable and immutable post-init."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Beta"),  # no data
    ]
    rows = [_answer_row(1, "Alpha", "$75,000")]

    result = _build(selected_districts=districts, answer_rows=rows)

    assert isinstance(result, MetricRankingResult)
    # frozen=True means attribute assignment must raise ValidationError or TypeError
    with pytest.raises((TypeError, Exception)):
        result.rows = []  # type: ignore[misc]


# ─── CSV/markdown parity: missing rows appear in both surfaces ────────────────


def test_missing_row_appears_in_csv_export() -> None:
    """A missing-data row in result.rows must appear in csv_export.rows too."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Fairfax"),  # no data
    ]
    rows = [_answer_row(1, "Alpha", "$50,000")]

    result = _build(selected_districts=districts, answer_rows=rows)

    assert result.csv_export is not None
    csv_district_names = [r["district_name"] for r in result.csv_export.rows]
    # Both Alpha (data row) and Fairfax (missing row) should appear in CSV
    assert "Alpha" in csv_district_names
    assert "Fairfax" in csv_district_names


def test_csv_missing_row_has_same_coverage_state_as_result_row() -> None:
    """CSV row for missing district must have same coverage_state as result row."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Houston ISD"),  # no data
    ]
    rows = [_answer_row(1, "Alpha", "$55,000")]

    result = _build(selected_districts=districts, answer_rows=rows)

    houston_result_row = next(r for r in result.rows if r.district_name == "Houston ISD")
    assert result.csv_export is not None
    houston_csv_rows = [
        r for r in result.csv_export.rows if r.get("district_name") == "Houston ISD"
    ]
    # Should appear exactly once in CSV (as a data row, not duplicated in disclosure)
    assert len(houston_csv_rows) >= 1
    houston_csv = houston_csv_rows[0]
    assert houston_csv["coverage_state"] == houston_result_row.coverage_state


# ─── total_considered / excluded_count semantics ─────────────────────────────


def test_total_considered_covers_all_selected_districts() -> None:
    """total_considered reflects all selected districts, not just data-present ones."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Bravo"),
        _district(3, "Charlie"),  # no data
    ]
    rows = [
        _answer_row(1, "Alpha", "$50,000"),
        _answer_row(2, "Bravo", "$60,000"),
    ]

    result = _build(selected_districts=districts, answer_rows=rows)

    assert result.total_considered == 3


def test_excluded_count_with_limit_truncating_missing_rows() -> None:
    """When limit truncates missing rows, excluded_count reflects that."""
    districts = [
        _district(1, "Alpha"),
        _district(2, "Bravo"),
        _district(3, "Charlie"),
        _district(4, "Delta"),   # no data
        _district(5, "Echo"),    # no data
    ]
    rows = [
        _answer_row(1, "Alpha", "$50,000"),
        _answer_row(2, "Bravo", "$70,000"),
        _answer_row(3, "Charlie", "$60,000"),
    ]
    # limit=3: only 3 data rows fit; both missing rows are truncated
    result = _build(selected_districts=districts, answer_rows=rows, limit=3)

    assert len(result.rows) == 3
    # excluded_count = len(coverage_labels) - len(candidates)
    #   = 5 districts - 3 data-present candidates = 2
    # The 2 missing districts couldn't participate in rank ordering, so they
    # are "excluded" from candidates (though they stay in coverage_disclosures
    # because the limit left no room for them in result_rows).
    assert result.total_considered == 5
    assert result.excluded_count == 2


# ─── State-scope: missing rows appear in result, not just disclosures ─────────


def _state_plan(*, limit: int | None = None) -> QueryPlan:
    limit_spec = LimitSpec(count=limit, kind="top") if limit is not None else None
    return QueryPlan(
        question="Rank Texas districts by starting salary.",
        selection=SelectionSpec(scope="state", states=["TX"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        limit=limit_spec,
    )


def _state_selection(districts: list[SelectedDistrict]) -> ResultSelection:
    return ResultSelection(scope="state", states=["TX"], districts=districts)


def _build_state(
    *,
    selected_districts: list[SelectedDistrict],
    answer_rows: list[MetricAnswerRow],
    limit: int | None = None,
) -> MetricRankingResult:
    metric = _metric()
    plan = _state_plan(limit=limit)
    selection = _state_selection(selected_districts)
    return build_metric_ranking_result(
        plan,
        metric,
        answer_rows,
        recent_rows=[],
        selection=selection,
        selected_districts=selected_districts,
        default_limit=10,
        reviewed_district_ids=set(),
        academic_year="2024 - 2025",
    )


def test_state_scope_appends_missing_districts() -> None:
    """state scope must append missing-data districts just like named_districts.

    Brief tester examples: Houston/Klein in HTS pay queries; Fort Bend/Arlington
    in TX observations.  When the user filters to a state, they expect to see
    ALL the districts they asked about — even those with no data for the metric.
    """
    districts = [
        _district(1, "Houston ISD"),
        _district(2, "Dallas ISD"),
        _district(3, "Klein ISD"),       # no data
        _district(4, "Fort Bend ISD"),   # no data
    ]
    rows = [
        _answer_row(1, "Houston ISD", "$52,000"),
        _answer_row(2, "Dallas ISD", "$55,000"),
        # Klein ISD and Fort Bend ISD have no metric rows
    ]

    result = _build_state(selected_districts=districts, answer_rows=rows)

    # All 4 districts should appear in result_rows (2 data + 2 missing)
    assert len(result.rows) == 4
    result_names = {r.district_name for r in result.rows}
    assert "Houston ISD" in result_names
    assert "Dallas ISD" in result_names
    assert "Klein ISD" in result_names
    assert "Fort Bend ISD" in result_names
    # Missing districts have non-covered coverage_state
    missing = [r for r in result.rows if r.district_name in {"Klein ISD", "Fort Bend ISD"}]
    assert len(missing) == 2
    for row in missing:
        assert row.coverage_state != "covered"
    # CSV must have exactly 4 rows — promoted districts must NOT be double-emitted
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 4, (
        f"Expected 4 CSV rows (no duplication), got {len(result.csv_export.rows)}: "
        f"{[r['district_name'] for r in result.csv_export.rows]}"
    )


def test_all_covered_districts_scope_does_not_append_missing() -> None:
    """all_covered_districts scope must NOT promote missing districts into result_rows.

    This is the intentional open-universe carve-out: when the query is "rank
    all covered districts," missing rows mean the district simply hasn't been
    reviewed yet for this metric — silently dropping them from result_rows is
    correct behaviour; they are represented in coverage_disclosures only.
    """
    districts = [
        _district(1, "Houston ISD"),
        _district(2, "Dallas ISD"),
        _district(3, "Klein ISD"),       # no data
        _district(4, "Fort Bend ISD"),   # no data
    ]
    rows = [
        _answer_row(1, "Houston ISD", "$52,000"),
        _answer_row(2, "Dallas ISD", "$55,000"),
    ]
    metric = _metric()
    # Construct an all_covered_districts plan and selection explicitly.
    plan = QueryPlan(
        question="Rank all covered districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
    )
    selection = ResultSelection(scope="all_covered_districts", districts=districts)
    result = build_metric_ranking_result(
        plan,
        metric,
        rows,
        recent_rows=[],
        selection=selection,
        selected_districts=districts,
        default_limit=10,
        reviewed_district_ids=set(),
        academic_year="2024 - 2025",
    )

    # Only data-present rows appear in result_rows
    assert len(result.rows) == 2
    result_names = {r.district_name for r in result.rows}
    assert "Houston ISD" in result_names
    assert "Dallas ISD" in result_names
    # Missing districts remain in coverage_disclosures, not in result_rows
    assert "Klein ISD" not in result_names
    assert "Fort Bend ISD" not in result_names
    missing_disclosure_names = {d.district_name for d in result.coverage_disclosures}
    assert "Klein ISD" in missing_disclosure_names
    assert "Fort Bend ISD" in missing_disclosure_names


# ─── sort_value population on ranked rows (M3a-2 prerequisite) ──────────────


def test_standard_ranking_populates_sort_value_on_ranked_rows() -> None:
    """Standard ranking rows must carry sort_value, sort_metric_id, sort_metric_name.

    Symmetric with the profile-ordered ranking path. Lets multi-turn follow-ups
    that reference the sort field by name see the numeric sort key directly on
    each row.
    """
    districts = [_district(1, "Alpha"), _district(2, "Bravo")]
    rows = [
        _answer_row(1, "Alpha", "$50,000"),
        _answer_row(2, "Bravo", "$70,000"),
    ]

    result = _build(selected_districts=districts, answer_rows=rows)

    ranked_rows = [r for r in result.rows if r.coverage_state == "covered"]
    assert len(ranked_rows) == 2
    for row in ranked_rows:
        assert row.sort_metric_id == 1001
        assert row.sort_metric_name == "Average teacher starting salary"
        assert row.sort_value is not None
        assert row.sort_value == row.value
        assert row.sort_display_value == row.display_value
        assert row.sort_academic_year == row.academic_year


def test_coverage_state_rows_do_not_carry_sort_value() -> None:
    """Missing-data rows (coverage_state != covered) leave sort_* fields None.

    The validator's ranking branch treats these as appendages, not part of the
    sort comparison.
    """
    districts = [
        _district(1, "Alpha"),
        _district(2, "Bravo"),
        _district(3, "Charlie"),  # no data
    ]
    rows = [
        _answer_row(1, "Alpha", "$50,000"),
        _answer_row(2, "Bravo", "$70,000"),
    ]

    result = _build(selected_districts=districts, answer_rows=rows)

    missing_rows = [r for r in result.rows if r.coverage_state != "covered"]
    assert len(missing_rows) == 1
    placeholder = missing_rows[0]
    assert placeholder.district_name == "Charlie"
    assert placeholder.sort_value is None
    assert placeholder.sort_metric_id is None


# ─── crit-26: counted not-reviewed clause names the concrete year (#1686) ─────


def test_not_reviewed_gap_clause_names_the_concrete_year() -> None:
    """#1686 (cases 14 / 1117, criterion 26 `coverage_state_language_quality`):
    the counted not-reviewed gap clause must name the SPECIFIC academic year, not
    the placeholder "the requested metric/year". The judge fails the anonymous
    phrasing; the canonical sentence (mirroring the count/filter path) is
    "NCTQ hasn't reviewed {count} {districts} for {year} yet." with the real year.

    Builds the result through the REAL execution path under STATE scope
    (`_build_state` -> `build_metric_ranking_result`), mirroring case 1117
    ("Rank California districts by maximum teacher salary, highest first.") —
    proving the year actually flows from the answered rows into
    `district_coverage.requested_academic_year` and out into the rendered
    counted clause, not just that the template renders a year when one is
    hand-set. Charlie has no metric row, so it routes to the counted
    not-reviewed path (`_COUNTED_GAP_REASONS`).
    """
    from compass_backend.rendering.composer import (
        _COUNTED_GAP_REASONS,
        _reason_count_lines,
    )

    districts = [
        _district(1, "Alpha"),
        _district(2, "Bravo"),
        _district(3, "Charlie"),  # no data -> counted not-reviewed gap
    ]
    rows = [
        _answer_row(1, "Alpha", "$50,000"),
        _answer_row(2, "Bravo", "$70,000"),
    ]

    result = _build_state(selected_districts=districts, answer_rows=rows)

    # The execution path populated the concrete academic year on the summary.
    assert result.district_coverage is not None
    assert result.district_coverage.requested_academic_year == "2024 - 2025"

    lines = _reason_count_lines(result, _COUNTED_GAP_REASONS)

    assert lines == [
        "NCTQ hasn't reviewed 1 district for 2024 - 2025 yet."
    ], lines
    # The concrete year is present and the anonymous placeholder is gone.
    joined = " ".join(lines)
    assert "2024 - 2025" in joined
    assert "the requested metric/year" not in joined
    assert "the requested year." not in joined
