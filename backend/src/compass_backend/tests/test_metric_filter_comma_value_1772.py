"""Regression for #1772: a numeric metric filter whose value carries a thousands
separator (a currency anchor value like "45,602") must not crash the turn.

Before the fix, ``_row_passes_metric_filter`` parsed the ROW value with the
comma-stripping canonical parser (``parse_compass_numeric_value``) but the FILTER
value with a raw ``float()`` — so ``float("45,602")`` raised ``ValueError`` and
orchestration surfaced it as a user-facing 500. This bites the equality-anchor
path ("districts with the same starting salary as Portland, ME"), where the
filter value is the anchor district's rendered cell.
"""

from __future__ import annotations

from compass_backend.db.rows import MetricAnswerRow
from compass_backend.execution.filters import ResolvedMetricFilter
from compass_backend.execution.operations import _row_passes_metric_filter


def _row(value: str) -> MetricAnswerRow:
    return MetricAnswerRow(
        district_id=1,
        district_name="Test District",
        metric_id=7,
        metric_name="Annual base salary for a first year teacher",
        value=value,
        academic_year="2024 - 2025",
    )


def test_comma_formatted_equality_filter_compares_not_crashes() -> None:
    # Equality-anchor on a currency metric: f.value is the anchor's rendered cell.
    f = ResolvedMetricFilter(
        metric_id=7, operator="equals", value="45,602", value_kind="numeric"
    )
    assert _row_passes_metric_filter(_row("45,602"), f) is True
    assert _row_passes_metric_filter(_row("45,601"), f) is False


def test_comma_formatted_threshold_filter_compares_not_crashes() -> None:
    f = ResolvedMetricFilter(
        metric_id=7,
        operator="greater_than_or_equal",
        value="45,000",
        value_kind="numeric",
    )
    assert _row_passes_metric_filter(_row("45,602"), f) is True
    assert _row_passes_metric_filter(_row("44,000"), f) is False


def test_unparseable_filter_value_excludes_rather_than_raises() -> None:
    f = ResolvedMetricFilter(
        metric_id=7, operator="equals", value="not a number", value_kind="numeric"
    )
    assert _row_passes_metric_filter(_row("45,602"), f) is False
