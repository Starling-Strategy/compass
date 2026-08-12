"""Tests for tie-at-the-cutoff expansion and disclosure (#1471).

05-accuracy-sort.md commits to disclosing ties at the cutoff rather than
silently resolving them: a "top 5" with a tie at position 5 returns 6 rows and
states the tie. ``build_metric_ranking_result`` previously hard-sliced
``candidates[:limit]`` with a silent alphabetical tie-break, dropping every
district tied with the cutoff value below the cut. These tests pin:

- **Small tie** — every district tied at the cutoff value is included and the
  tie is disclosed in the methodology note.
- **Clean cutoff** — when the row below the cutoff has a different value, the
  slice is exactly ``limit`` rows with no tie note.
- **Large tie** — a pathological tie (many districts sharing the cutoff value)
  is capped at ``limit + _MAX_TIE_EXPANSION`` and the count of tied districts
  not shown is disclosed.
"""

from __future__ import annotations

from compass_backend.artifacts import ResultSelection, SelectedDistrict
from compass_backend.artifacts.results import MetricRankingResult
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts import LimitSpec, MetricSpec, QueryPlan, SelectionSpec
from compass_backend.execution.ranking import _MAX_TIE_EXPANSION, build_metric_ranking_result
from compass_backend.execution.types import MetricAnswerRow


def _metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=1001,
        name="Average teacher starting salary",
        answer_type="numeric",
    )


def _answer_row(district_id: int, name: str, value: str) -> MetricAnswerRow:
    return MetricAnswerRow(
        answer_id=district_id * 100,
        district_id=district_id,
        district_name=name,
        state="TX",
        metric_id=1001,
        metric_name="Average teacher starting salary",
        value=value,
        academic_year="2024 - 2025",
        citations=[],
    )


def _build(rows: list[MetricAnswerRow], *, limit: int) -> MetricRankingResult:
    districts = [
        SelectedDistrict(district_id=r.district_id, district_name=r.district_name, state="TX")
        for r in rows
    ]
    plan = QueryPlan(
        question="Rank covered districts by starting salary, highest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        limit=LimitSpec(count=limit, kind="top"),
    )
    return build_metric_ranking_result(
        plan,
        _metric(),
        rows,
        recent_rows=[],
        selection=ResultSelection(scope="all_covered_districts", districts=districts),
        selected_districts=districts,
        default_limit=10,
        reviewed_district_ids=set(),
        academic_year="2024 - 2025",
    )


def test_small_tie_at_cutoff_expands_and_discloses() -> None:
    """Top 3 with a 2-way tie at position 3 returns 4 rows and states the tie."""

    rows = [
        _answer_row(1, "Alpha", "$90,000"),
        _answer_row(2, "Bravo", "$80,000"),
        _answer_row(3, "Charlie", "$70,000"),  # tie at cutoff value
        _answer_row(4, "Delta", "$70,000"),  # tied with Charlie below the cut
        _answer_row(5, "Echo", "$60,000"),
    ]

    result = _build(rows, limit=3)

    names = [row.district_name for row in result.rows]
    # Both tied districts appear — Delta is no longer silently dropped.
    assert names == ["Alpha", "Bravo", "Charlie", "Delta"]
    tie_notes = [note for note in result.source_notes if "tie at position 3" in note]
    assert tie_notes, result.source_notes
    assert "all tied districts are shown" in tie_notes[0]
    assert "$70,000" in tie_notes[0]


def test_clean_cutoff_does_not_expand_or_disclose() -> None:
    """When the row below the cutoff differs in value, slice is exactly limit."""

    rows = [
        _answer_row(1, "Alpha", "$90,000"),
        _answer_row(2, "Bravo", "$80,000"),
        _answer_row(3, "Charlie", "$70,000"),
        _answer_row(4, "Delta", "$60,000"),  # distinct value below cutoff
    ]

    result = _build(rows, limit=3)

    assert [row.district_name for row in result.rows] == ["Alpha", "Bravo", "Charlie"]
    assert not any("tie at position" in note for note in result.source_notes)


def test_large_tie_is_capped_and_discloses_unshown_count() -> None:
    """A tie larger than the expansion cap stops growing and discloses the rest."""

    rows = [_answer_row(1, "Alpha", "$90,000"), _answer_row(2, "Bravo", "$80,000")]
    # 10 districts all tied at the #3 cutoff value.
    tied_count = 10
    for index in range(tied_count):
        rows.append(_answer_row(100 + index, f"Tie {index:02d}", "$70,000"))

    limit = 3
    result = _build(rows, limit=limit)

    # Never explodes past the cap.
    assert len(result.rows) <= limit + _MAX_TIE_EXPANSION
    tie_notes = [note for note in result.source_notes if "tie at position 3" in note]
    assert tie_notes, result.source_notes
    assert "not shown here but are in the export" in tie_notes[0]
    # Discloses the full tied-group size.
    assert str(tied_count) in tie_notes[0]
