"""Regression: "the N largest by [metric]" ranks the N largest *with data*
and names the larger ones excluded — a consistent rule (#1468).

Scenario context: SORT-MIGRATED-144 (sc.459) — *"Which of the 10 largest
districts pay the highest starting salary for a first-year teacher with a
bachelor's degree?"*. The SCENARIO_FIT judge failed the prior behavior not on
wording but on selection: the system returned the 10 largest districts
*overall* and then padded the table with the ones lacking data, so the "10
shown" were not genuinely comparable.

Settled product rule (issue #1468):

1. For a ``largest_districts`` ranking, show only the districts that carry a
   comparable numeric value — the comparable table is never padded with
   missing-data rows (that is the named-/state-scope behavior, left unchanged).
2. Name the *larger* districts left out, split into two honest buckets that are
   never lumped together:
   - reviewed-but-no-figure (``ina`` "issue not addressed" / ``na``) — a real
     NCTQ finding, NOT a data gap;
   - ``not_reviewed`` — a genuine data gap.
3. The renderer names up to 3 per bucket, then "(and X more)".

These are unit tests against ``build_metric_ranking_result`` plus the renderer
composer, the two owning boundaries (Selection picks the with-data set; the
Renderer discloses the exclusions). The B-spine replay of SORT-MIGRATED-144 is
the closure evidence and is captured in the PR's VALIDATION PENDING block.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.artifacts.results import MetricRankingResult
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts import LimitSpec, MetricSpec, QueryPlan, SelectionSpec
from compass_backend.execution.ranking import build_metric_ranking_result
from compass_backend.execution.types import MetricAnswerRow
from compass_backend.rendering.composer import compose_response


def _metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=1001,
        name="Average teacher starting salary",
        answer_type="numeric",
    )


def _district(district_id: int, name: str, state: str = "TX") -> SelectedDistrict:
    return SelectedDistrict(district_id=district_id, district_name=name, state=state)


def _answer_row(
    district_id: int, name: str, value: str, state: str = "TX"
) -> MetricAnswerRow:
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


def _largest_plan(*, limit: int | None = None) -> QueryPlan:
    limit_spec = LimitSpec(count=limit, kind="top") if limit is not None else None
    return QueryPlan(
        question="Which of the largest districts pay the highest starting salary?",
        selection=SelectionSpec(scope="largest_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        limit=limit_spec,
    )


def _build_largest(
    *,
    selected_districts: list[SelectedDistrict],
    answer_rows: list[MetricAnswerRow],
    limit: int | None = None,
) -> MetricRankingResult:
    """Build a ``largest_districts`` ranking.

    ``selected_districts`` arrive in size order (largest first), exactly as
    ``select_largest_districts`` returns them.
    """

    return build_metric_ranking_result(
        _largest_plan(limit=limit),
        _metric(),
        answer_rows,
        recent_rows=[],
        selection=ResultSelection(
            scope="largest_districts", districts=selected_districts
        ),
        selected_districts=selected_districts,
        default_limit=10,
        reviewed_district_ids={d.district_id for d in selected_districts},
        academic_year="2024 - 2025",
    )


# ─── The comparable table is not padded with missing-data rows ────────────────


def test_largest_scope_does_not_pad_table_with_missing_data() -> None:
    """Only the with-numeric-data districts appear as ranked rows (#1468 rule 1).

    Three "largest" districts; the two biggest lack a figure (one INA, one not
    reviewed), only the third has a salary. The table must show exactly that one
    comparable row — never padded with the data-less larger ones.
    """

    districts = [
        _district(1, "Mega ISD"),  # largest, INA
        _district(2, "Giant ISD"),  # second, not reviewed
        _district(3, "Big ISD"),  # third, has data
    ]
    rows = [
        _answer_row(1, "Mega ISD", "ina"),
        # Giant ISD has no row at all -> not_reviewed
        _answer_row(3, "Big ISD", "$60,000"),
    ]

    result = _build_largest(selected_districts=districts, answer_rows=rows)

    assert [row.district_name for row in result.rows] == ["Big ISD"]
    assert result.rows[0].coverage_state == "covered"


# ─── The excluded-larger callout splits INA from not_reviewed ─────────────────


def test_largest_excluded_splits_reviewed_finding_from_gap() -> None:
    """INA/NA land in ``reviewed`` (a finding); absent rows land in ``not_reviewed``."""

    districts = [
        _district(1, "Mega ISD"),  # INA — reviewed finding
        _district(2, "Huge ISD"),  # N/A — reviewed finding
        _district(3, "Giant ISD"),  # no row — genuine gap
        _district(4, "Big ISD"),  # has data
    ]
    rows = [
        _answer_row(1, "Mega ISD", "ina"),
        _answer_row(2, "Huge ISD", "N/A - no such program"),
        _answer_row(4, "Big ISD", "$60,000"),
    ]

    result = _build_largest(selected_districts=districts, answer_rows=rows)

    assert result.largest_excluded is not None
    reviewed_names = [d.district_name for d in result.largest_excluded.reviewed]
    gap_names = [d.district_name for d in result.largest_excluded.not_reviewed]
    # INA + N/A are reviewed findings, NOT gaps.
    assert reviewed_names == ["Mega ISD", "Huge ISD"]
    # Only the genuinely-unreviewed district is a gap.
    assert gap_names == ["Giant ISD"]
    # The reviewed-finding districts must never be miscounted as gaps.
    assert "Mega ISD" not in gap_names
    assert "Huge ISD" not in gap_names


def test_largest_excluded_preserves_largest_first_order() -> None:
    """Excluded districts keep the size order they were selected in."""

    districts = [
        _district(1, "AAA ISD"),  # largest, no data
        _district(2, "ZZZ ISD"),  # second, no data
        _district(3, "Big ISD"),  # has data
    ]
    rows = [_answer_row(3, "Big ISD", "$60,000")]

    result = _build_largest(selected_districts=districts, answer_rows=rows)

    assert result.largest_excluded is not None
    # Largest-first (selection order), NOT alphabetical.
    assert [d.district_name for d in result.largest_excluded.not_reviewed] == [
        "AAA ISD",
        "ZZZ ISD",
    ]


# ─── No excluded-larger callout when all the largest have data ────────────────


def test_largest_excluded_absent_when_all_have_data() -> None:
    """The rule produces no callout when every selected district is comparable."""

    districts = [_district(1, "Alpha"), _district(2, "Bravo")]
    rows = [
        _answer_row(1, "Alpha", "$50,000"),
        _answer_row(2, "Bravo", "$70,000"),
    ]

    result = _build_largest(selected_districts=districts, answer_rows=rows)

    assert result.largest_excluded is None
    assert {row.district_name for row in result.rows} == {"Alpha", "Bravo"}


def test_named_scope_carries_no_largest_excluded() -> None:
    """The rule fires only for ``largest_districts`` — named scope is untouched."""

    districts = [_district(1, "Alpha"), _district(2, "Beta")]
    rows = [_answer_row(1, "Alpha", "$50,000")]  # Beta missing
    result = build_metric_ranking_result(
        QueryPlan(
            question="Rank Alpha and Beta by salary.",
            selection=SelectionSpec(scope="named_districts", districts=["Alpha", "Beta"]),
            metrics=[MetricSpec(name="Average teacher starting salary")],
        ),
        _metric(),
        rows,
        recent_rows=[],
        selection=ResultSelection(scope="named_districts", districts=districts),
        selected_districts=districts,
        default_limit=10,
        reviewed_district_ids={1, 2},
        academic_year="2024 - 2025",
    )

    assert result.largest_excluded is None
    # Named scope still pads the missing district into the table (unchanged).
    assert {row.district_name for row in result.rows} == {"Alpha", "Beta"}


# ─── Renderer: ≤3 named per bucket + "(and X more)" ───────────────────────────


def test_renderer_caps_names_and_rolls_up_remainder() -> None:
    """The composed callout names ≤3 then "(and X more)", split by bucket (#1468)."""

    districts = [_district(i, f"Gap ISD {i:02d}") for i in range(1, 6)] + [
        _district(99, "Big ISD")
    ]
    rows = [_answer_row(99, "Big ISD", "$60,000")]  # 5 larger, all not reviewed

    result = _build_largest(selected_districts=districts, answer_rows=rows)
    composition = compose_response(_largest_plan(), result)

    callout = "\n".join(composition.largest_excluded_lines)
    assert "not yet reviewed" in callout
    # First three named, remaining two rolled up.
    assert "Gap ISD 01, Gap ISD 02, Gap ISD 03" in callout
    assert "(and 2 more)" in callout
    # No fourth/fifth district named inline.
    assert "Gap ISD 04" not in callout
    assert "Gap ISD 05" not in callout


def test_renderer_labels_reviewed_finding_distinctly() -> None:
    """The INA bucket is phrased as a reviewed finding, not a data gap."""

    districts = [_district(1, "Mega ISD"), _district(2, "Big ISD")]
    rows = [
        _answer_row(1, "Mega ISD", "ina"),
        _answer_row(2, "Big ISD", "$60,000"),
    ]

    result = _build_largest(selected_districts=districts, answer_rows=rows)
    composition = compose_response(_largest_plan(), result)

    callout = "\n".join(composition.largest_excluded_lines)
    assert "Mega ISD" in callout
    assert "not addressed" in callout
    # The INA finding must not be described as a data gap.
    assert "not yet reviewed" not in callout
