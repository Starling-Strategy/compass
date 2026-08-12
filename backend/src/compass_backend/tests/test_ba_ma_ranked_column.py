"""Regression: BA+MA salary comparison must mark which column drives the order (#1220).

When a user asks for bachelor's (BA) and master's (MA) starting salaries in one
answer, Compass ranks the districts by ONE of the two figures and shows both as
columns. Because MA pay is almost always higher than BA pay, ordering the table
by BA while showing both reads as misleading — the reader cannot tell which
column the order reflects.

The ranked-lookup executor now stamps ``MetricLookupResult.ranked_by_metric_name``
with the metric the rows are ordered by, and the comparison renderer marks that
column header "(ranked)" so the ordering is honest and obvious. These tests pin
the renderer behavior on the typed signal.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    MetricLookupResult,
    MetricValueRow,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.contracts import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.rendering import render_response
from compass_backend.tests.test_mvp_rendering import _valid_report

_BA = "Average teacher starting salary (bachelor's)"
_MA = "Average teacher starting salary (master's)"


def _row(district_id: int, name: str, metric_name: str, value: str) -> MetricValueRow:
    return MetricValueRow(
        district_id=district_id,
        district_name=name,
        state="CA",
        metric_id=1000 + (0 if metric_name == _BA else 1),
        metric_name=metric_name,
        value=value,
        display_value=value,
        academic_year="2024 - 2025",
        coverage_state="covered",
        coverage_display=value,
        coverage_reason="answer_value",
    )


def _ba_ma_lookup(*, ranked_by: str | None) -> MetricLookupResult:
    # Two districts, each with a BA and an MA salary. Rows are ordered by BA
    # (Bravo $60k before Alpha $50k); MA happens to invert that order.
    rows = [
        _row(2, "Bravo", _BA, "$60,000"),
        _row(2, "Bravo", _MA, "$70,000"),
        _row(1, "Alpha", _BA, "$50,000"),
        _row(1, "Alpha", _MA, "$80,000"),
    ]
    return MetricLookupResult(
        rows=rows,
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=2, district_name="Bravo", state="CA"),
                SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
            ],
        ),
        total_considered=2,
        excluded_count=0,
        order_statement=f"Ranked by {_BA}, highest to lowest; displayed comparison metrics.",
        ranked_by_metric_name=ranked_by,
    )


def _plan() -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="Compare BA and MA starting salaries for the top districts.",
        selection=SelectionSpec(scope="named_districts", districts=["Bravo", "Alpha"]),
        metrics=[MetricSpec(name=_BA), MetricSpec(name=_MA, role="comparison")],
    )


def _header_line(body: str) -> str:
    return next(line for line in body.splitlines() if line.startswith("| District"))


def test_ranked_column_is_marked_in_comparison_header() -> None:
    manifest = render_response(_plan(), _ba_ma_lookup(ranked_by=_BA), _valid_report())

    assert manifest.status == "rendered"
    header = _header_line(manifest.body)
    # The BA column (the order driver) is marked; the MA column is not.
    assert f"{_BA} (ranked)" in header, header
    assert f"{_MA} (ranked)" not in header, header
    # Both metrics still appear as columns.
    assert _MA in header


def test_unranked_comparison_header_is_not_marked() -> None:
    manifest = render_response(_plan(), _ba_ma_lookup(ranked_by=None), _valid_report())

    assert manifest.status == "rendered"
    header = _header_line(manifest.body)
    assert "(ranked)" not in header, header
