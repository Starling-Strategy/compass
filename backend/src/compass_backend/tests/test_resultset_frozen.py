"""Lock in `frozen=True` on ResultSet variants + row types + surface artifacts.

If anyone reverts `frozen=True` to make a mutation work, these tests fail
loudly. The surface_consistency invariants below this layer assume the
canonical ResultSet is faux-immutable after construction.

`model_copy(update={...})` continues to work — it produces a new instance
rather than mutating the original. That's the documented escape hatch for
"I really need a modified copy".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.artifacts.results import (
    ChartArtifact,
    CsvExportArtifact,
    MetricRankingResult,
    RankingRow,
)


def _row(district_id: int = 1, value: int = 80000) -> RankingRow:
    # `coverage_state="covered"` is required for `_chart_for_ranking` to
    # include the row in the derived chart artifact — without it the chart
    # is None and the chart-immutability test below has nothing to freeze.
    return RankingRow(
        district_id=district_id,
        district_name=f"District {district_id}",
        state="CA",
        metric_id=100,
        metric_name="Starting salary",
        value=value,
        display_value=f"${value:,}",
        academic_year="2024 - 2025",
        rank=district_id,
        coverage_state="covered",
        coverage_display=f"${value:,}",
        coverage_reason="answer_value",
    )


def _result() -> MetricRankingResult:
    return MetricRankingResult(
        rows=[_row(1, 80000), _row(2, 90000), _row(3, 70000)],
        total_considered=3,
        excluded_count=0,
        order_statement="Sorted by Starting salary descending.",
    )


def test_ranking_row_rejects_attribute_assignment() -> None:
    """Row mutation must raise — even small drifts compound across surfaces."""
    row = _row()
    with pytest.raises(ValidationError, match="frozen"):
        row.state = "NY"  # type: ignore[misc]


def test_metric_ranking_result_rejects_attribute_assignment() -> None:
    """Top-level result is also frozen — a renderer can't re-point
    `csv_export` to a different artifact mid-render."""
    result = _result()
    with pytest.raises(ValidationError, match="frozen"):
        result.order_statement = "Different order"  # type: ignore[misc]


def test_csv_export_artifact_rejects_attribute_assignment() -> None:
    result = _result()
    assert result.csv_export is not None
    with pytest.raises(ValidationError, match="frozen"):
        result.csv_export.artifact_type = "csv_table"  # type: ignore[misc]


def test_chart_artifact_rejects_attribute_assignment() -> None:
    result = _result()
    assert result.chart is not None
    with pytest.raises(ValidationError, match="frozen"):
        result.chart.title = "Different title"  # type: ignore[misc]


def test_model_copy_with_update_still_produces_new_instance() -> None:
    """The escape hatch: `model_copy(update=...)` works on a frozen model."""
    row = _row()
    modified = row.model_copy(update={"state": "NY"})

    assert row.state == "CA"
    assert modified.state == "NY"
    assert row is not modified


def test_populate_artifact_surfaces_runs_during_construction() -> None:
    """Even though ResultSet is frozen, the post-init validator still
    derives `csv_export` / `chart` (via `object.__setattr__` inside the
    validator, which Pydantic permits during construction)."""
    result = _result()

    assert isinstance(result.csv_export, CsvExportArtifact)
    assert isinstance(result.chart, ChartArtifact)
    # Re-validation: the surfaces should reflect this result's row count.
    assert len(result.csv_export.rows) == 3
    assert len(result.chart.points) == 3
