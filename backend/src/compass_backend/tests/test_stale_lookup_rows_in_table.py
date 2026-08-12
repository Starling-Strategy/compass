"""Renderer regression (#1826): stale lookup rows stay IN the table.

Reporter prompt: "show me starting salaries for districts in florida"
(prod session ``63e33b20``). The state-scoped single-metric ``metric_lookup``
retrieved 15 Florida districts, but #1514's answers-only table demoted the 14
stale (prior-year) rows to prose — the rendered table showed only Broward
(the one district with a current value), reading as a 1-of-15 answer.

Fix 1 of docs/plans/2026-07-01-001: a stale row (``coverage_state='not_reviewed'``,
``coverage_reason='stale_recent_answer'``) that carries a
``coverage_prior_display_value`` renders in the lookup table, showing that prior
value with its ``coverage_prior_academic_year`` in the Academic-year column, so
staleness is annotated rather than hidden. Genuinely-empty states
(out_of_universe / not_reviewed with no prior value) keep demoting to prose.

Dimension: Coverage-State Labeling. The change moves stale rows across the four
surfaces that must agree (table, CSV data rows, manifest ``displayed_row_count``,
surface_consistency parity), so this test pins all four.
"""

from __future__ import annotations

import asyncio

from compass_backend.artifacts import (
    CitationRef,
    MetricLookupResult,
    MetricValueRow,
    ResultSelection,
    SelectedDistrict,
    coverage_label_for_stale_answer,
)
from compass_backend.contracts import (
    MetricSpec,
    QueryPlan,
    SelectionSpec,
    ValidationReport,
)
from compass_backend.quality.validators import registry as _registry  # populate registry
from compass_backend.quality.validators.surface import (
    _validate_csv_row_parity,
    _validate_markdown_table_parity,
)
from compass_backend.rendering import render_response
from compass_backend.tests.conftest import make_evaluator_context

_METRIC_ID = 89
_METRIC_NAME = "Annual base salary for a first year teacher with a bachelor's degree"
_CURRENT_YEAR = "2024 - 2025"
_PRIOR_YEAR = "2022 - 2023"

# One current district (Broward) + 14 Florida districts whose only reviewed
# figure is a prior-year value — the exact 1-of-15 shape from session 63e33b20.
_STALE_DISTRICTS: list[tuple[int, str, str]] = [
    (101, "Miami-Dade County Public Schools", "$47,500"),
    (102, "Hillsborough County Public Schools", "$46,200"),
    (103, "Orange County Public Schools", "$47,800"),
    (104, "Palm Beach County School District", "$47,000"),
    (105, "Duval County Public Schools", "$45,100"),
    (106, "Pinellas County Schools", "$45,900"),
    (107, "Polk County Public Schools", "$44,700"),
    (108, "Lee County School District", "$45,400"),
    (109, "Brevard Public Schools", "$44,200"),
    (110, "Pasco County Schools", "$44,900"),
    (111, "Seminole County Public Schools", "$46,500"),
    (112, "Volusia County Schools", "$43,800"),
    (113, "Osceola County School District", "$45,600"),
    (114, "Manatee County School District", "$44,500"),
]


def _plan() -> QueryPlan:
    return QueryPlan(
        question="show me starting salaries for districts in florida",
        selection=SelectionSpec(scope="state", states=["FL"]),
        metrics=[MetricSpec(name=_METRIC_NAME)],
    )


def _valid_report() -> ValidationReport:
    return ValidationReport(
        valid=True,
        dimensions_checked=["selection", "metric", "coverage_state"],
        findings=[],
    )


def _broward_row() -> MetricValueRow:
    return MetricValueRow(
        district_id=37,
        district_name="Broward County Public Schools",
        state="FL",
        metric_id=_METRIC_ID,
        metric_name=_METRIC_NAME,
        value="$51,402",
        display_value="$51,402",
        academic_year=_CURRENT_YEAR,
        citation_markers=[1],
        coverage_state="covered",
        coverage_display="$51,402",
        coverage_reason="answer_value",
    )


def _stale_row(district_id: int, district_name: str, prior_value: str) -> MetricValueRow:
    # Build the coverage label the same way execution/lookup.py does so the
    # row carries the authoritative stale fields (state/reason/prior_*).
    coverage = coverage_label_for_stale_answer(
        prior_value,
        district_name=district_name,
        metric_name=_METRIC_NAME,
        current_academic_year=_CURRENT_YEAR,
        prior_academic_year=_PRIOR_YEAR,
    )
    return MetricValueRow(
        district_id=district_id,
        district_name=district_name,
        state="FL",
        metric_id=_METRIC_ID,
        metric_name=_METRIC_NAME,
        value=None,
        display_value=coverage.display,
        academic_year=_CURRENT_YEAR,
        source="coverage_state",
        citation_markers=[],
        coverage_state=coverage.state,
        coverage_display=coverage.display,
        coverage_reason=coverage.reason,
        coverage_prior_academic_year=coverage.prior_academic_year,
        coverage_prior_display_value=coverage.prior_display_value,
    )


def _florida_lookup_result() -> MetricLookupResult:
    rows = [_broward_row()] + [
        _stale_row(district_id, name, prior_value)
        for district_id, name, prior_value in _STALE_DISTRICTS
    ]
    selection = ResultSelection(
        scope="state",
        states=["FL"],
        districts=[
            SelectedDistrict(
                district_id=row.district_id,
                district_name=row.district_name,
                state=row.state,
            )
            for row in rows
        ],
    )
    citations = [
        CitationRef(
            marker=1,
            title="Broward salary scale 2024-2025",
            url="https://example.org/broward.pdf",
            academic_year=_CURRENT_YEAR,
            district_id=37,
        )
    ]
    return MetricLookupResult(
        selection=selection,
        rows=rows,
        citations=citations,
        total_considered=15,
        excluded_count=0,
        order_statement="Looked up the selected metric for Florida districts.",
    )


def _table_rows(body: str) -> list[str]:
    """Markdown data rows (lines starting with '| ' after the header/separator)."""
    lines = [line for line in body.splitlines() if line.startswith("| ")]
    # Drop the header row and the '| --- |' separator row.
    return list(lines[2:]) if len(lines) > 2 else []


def test_all_florida_districts_render_as_table_rows() -> None:
    result = _florida_lookup_result()
    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.status == "rendered"
    body = manifest.body

    # All 15 districts appear as table rows — Broward plus the 14 stale ones.
    assert "| Broward County Public Schools" in body
    for _district_id, name, _prior in _STALE_DISTRICTS:
        assert f"| {name}" in body, f"stale district missing from table: {name}"

    data_rows = _table_rows(body)
    assert len(data_rows) == 15, f"expected 15 table rows, got {len(data_rows)}"

    # Broward shows its current value + current year.
    broward_line = next(r for r in data_rows if "Broward" in r)
    assert "$51,402" in broward_line
    assert _CURRENT_YEAR in broward_line

    # Each stale district shows its PRIOR value under its PRIOR academic year —
    # the value is in the table, not buried in a "NCTQ last reviewed …" sentence.
    for _district_id, name, prior_value in _STALE_DISTRICTS:
        line = next(r for r in data_rows if name in r)
        assert prior_value in line, f"{name}: prior value {prior_value} not in table cell"
        assert _PRIOR_YEAR in line, f"{name}: prior academic year not in table cell"

    # The stale narrative sentence no longer appears — the value moved in-table.
    assert "NCTQ last reviewed Miami-Dade" not in body


def test_stale_lookup_manifest_and_csv_count_all_fifteen() -> None:
    result = _florida_lookup_result()
    manifest = render_response(_plan(), result, _valid_report())

    # displayed_row_count re-anchors to the extended (stale-inclusive) subset.
    assert manifest.metadata["displayed_row_count"] == 15

    # The CSV export ships all 15 rows as data rows (raw record — the stale
    # rows keep their current academic_year plus the coverage_prior_* columns).
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 15
    csv_names = {row["district_name"] for row in result.csv_export.rows}
    assert "Broward County Public Schools" in csv_names
    for _district_id, name, _prior in _STALE_DISTRICTS:
        assert name in csv_names


def test_stale_lookup_chart_stays_current_values_only() -> None:
    """The chart plots current reviewed values only — stale rows (value=None)
    are NOT charted, so no chart-vs-table divergence guard can fire. With one
    covered district (below the render threshold) the chart is simply absent;
    the point is that staleness lives in the table, never as a phantom bar."""
    result = _florida_lookup_result()
    assert result.chart is None


def test_stale_lookup_passes_surface_consistency_parity() -> None:
    """The parity guard must not fire: table, CSV, and manifest agree on 15 rows."""
    result = _florida_lookup_result()
    manifest = render_response(_plan(), result, _valid_report())

    markdown_findings = _validate_markdown_table_parity(_plan(), result, manifest.body)
    assert markdown_findings == [], markdown_findings

    csv_findings = _validate_csv_row_parity(_plan(), result)
    assert csv_findings == [], csv_findings


def test_stale_lookup_l2_csv_parity_counts_all_fifteen() -> None:
    """The L2 ``csv_parity_matches_rendered_rows`` validator anchors chat rows to
    the same stale-inclusive subset — it counts 15 (not 1), so it actually
    validates the 14 stale rows against the CSV instead of ignoring them."""
    result = _florida_lookup_result()
    validator = _registry.lookup("csv_parity_matches_rendered_rows")
    assert validator is not None
    ctx = make_evaluator_context(
        session_id="sess-stale-lookup",
        turn_index=0,
        answer_text="placeholder",
        result=result,
    )
    outcome = asyncio.run(validator(ctx, {}))
    assert outcome.outcome == "pass", outcome.reason
    assert outcome.evidence["chat_row_count"] == 15
