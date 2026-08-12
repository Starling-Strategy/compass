"""#1611 — public CSV column pruning in `_public_csv_export`.

PR #1537 curated the internal CSV dump into a fixed ~13-column public schema
plus 4 rank columns, with friendly headers — but it never pruned, so a column
blank or redundant across every row still emitted (case 1051: 17 columns for a
query the on-screen table renders in 5). This replaces the all-or-nothing
`include_sort_columns` toggle with two general, per-column rules applied to the
*built* public rows:

  1. drop a column empty ("" / None) in every row;
  2. drop a rank column equal to its base column in every row
     (Ranked By↔Measure, Rank Value↔Value, Rank School Year↔School Year,
     Rank Source URL↔Source URL(s)).

Comparisons run on the public projected values. The redundant-rank collapse
fires when a query ranks by the metric it displays, but a genuine cross-metric
sort — where the rank values differ from the displayed values — keeps the rank
columns. The empty-rows guard never emits a zero-column CSV.

The pruning helper `_prune_public_columns` is pure (built columns + projected
rows → pruned pair), so the cross-metric and empty-rows cases drive
`_public_csv_export` directly with the list-of-dict rows it consumes; the rest
build real ResultSets, mirroring `test_csv_parity.py`.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    MetricRankingResult,
    RankingRow,
    ResultSelection,
)
from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.results import (
    CoverageDisclosure,
    _public_csv_export,
)


# ─── Fixtures (mirror test_csv_parity.py) ────────────────────────────────────


def _citation(marker: int = 1) -> CitationRef:
    return CitationRef(
        marker=marker,
        title="Test District Contract",
        url="https://example.org/test-contract.pdf",
        page_number=5,
        page_ref="p. 5",
        academic_year="2024 - 2025",
        document_type="Contract",
        district_id=1,
    )


def _ranked_by_displayed_metric_result() -> MetricRankingResult:
    """Case 1051 shape: ranked by the metric it displays. The sort metadata
    duplicates the displayed value/measure/year, and there is no rank source
    URL — so all 4 rank columns are noise (3 redundant, 1 empty)."""
    return MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=100,
                metric_name="salary",
                value=80000.0,
                display_value="$80,000",
                academic_year="2024 - 2025",
                rank=1,
                source="policy_answer",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$80,000",
                coverage_reason="answer",
                # Ranked by the displayed metric → sort_* mirrors the base values.
                sort_metric_name="salary",
                sort_value=80000.0,
                sort_display_value="$80,000",
                sort_academic_year="2024 - 2025",
            ),
        ],
        citations=[_citation(marker=1)],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by salary, highest to lowest.",
    )


# ─── Rule 1: empty-column drop ───────────────────────────────────────────────


def test_empty_columns_are_dropped() -> None:
    """A column blank in every row is removed from both public_columns and
    every public_row. Here the single covered answer row has no Data Note
    (covered → no note) and no rank source URL, so both drop."""
    result = _ranked_by_displayed_metric_result()
    assert result.csv_export is not None

    assert "Data Note" not in result.csv_export.public_columns
    assert "Rank Source URL" not in result.csv_export.public_columns
    for public_row in result.csv_export.public_rows:
        assert "Data Note" not in public_row
        assert "Rank Source URL" not in public_row


# ─── Rule 2: redundant-rank collapse (ranked by displayed metric) ────────────


def test_redundant_rank_columns_collapse_when_ranked_by_displayed_metric() -> None:
    """When a query ranks by the metric it displays, every rank column equals
    its base column in every row — so all 4 rank columns drop (case 1051:
    17 → 12 columns). The real per-row signal columns stay."""
    result = _ranked_by_displayed_metric_result()
    assert result.csv_export is not None

    for rank_column in ("Ranked By", "Rank Value", "Rank School Year", "Rank Source URL"):
        assert rank_column not in result.csv_export.public_columns

    # The lean, legitimate columns survive.
    assert result.csv_export.public_columns == [
        "Export Source",
        "District",
        "State",
        "Measure",
        "Value",
        "School Year",
        "Data Status",
        "Citation Marker(s)",
        "Source Document",
        "Source Type",
        "Source Page(s)",
        "Source URL(s)",
    ]


# ─── Rule 2: a genuine cross-metric sort KEEPS the rank columns ──────────────


def test_cross_metric_sort_keeps_rank_columns() -> None:
    """A cross-metric sort — e.g. "class size for the 10 highest-paying
    districts" — has rank values that differ from the displayed values, so the
    rank columns carry information and are kept. Driven through
    _public_csv_export directly with the list-of-dict rows it consumes, for
    precise control over the rank↔base divergence."""
    rows = [
        {
            "district_name": "Alpha",
            "state": "CA",
            "metric_name": "class size",
            "display_value": "22",
            "academic_year": "2024 - 2025",
            "coverage_state": "covered",
            "coverage_display": "22",
            "coverage_reason": "answer",
            "citation_markers": "1",
            "source_document": "Test District Contract",
            "source_document_type": "Contract",
            "source_page": "p. 5",
            "source_urls": "https://example.org/class-size",
            # Sorted by a DIFFERENT metric — values diverge from the displayed ones.
            "sort_metric_name": "starting salary",
            "sort_display_value": "$80,000",
            "sort_academic_year": "2023 - 2024",
            "sort_source_url": "https://example.org/salary",
        },
    ]

    columns, public_rows = _public_csv_export(rows)

    for rank_column in ("Ranked By", "Rank Value", "Rank School Year", "Rank Source URL"):
        assert rank_column in columns
    public_row = public_rows[0]
    assert public_row["Measure"] == "class size"
    assert public_row["Ranked By"] == "starting salary"
    assert public_row["Value"] == "22"
    assert public_row["Rank Value"] == "$80,000"
    assert public_row["School Year"] == "2024 - 2025"
    assert public_row["Rank School Year"] == "2023 - 2024"
    assert public_row["Source URL(s)"] == "https://example.org/class-size"
    assert public_row["Rank Source URL"] == "https://example.org/salary"


# ─── Rule 2: per-pair, every-row independence (multi-row) ────────────────────


def test_rank_pairs_are_pruned_independently_per_pair_across_rows() -> None:
    """Each rank↔base pair is judged on its own, over EVERY row.

    Two rows where the pairs disagree:
      - Ranked By == Measure in both rows           → drop (redundant)
      - Rank School Year == School Year in both rows → drop (redundant)
      - Rank Value == Value in row 1 but NOT row 2   → keep (not equal in every row)
      - Rank Source URL differs from Source URL(s)   → keep (non-empty, divergent)

    This is the case the single-row tests can't reach: it fails if the
    every-row quantifier is weakened to "any row" (Rank Value would wrongly
    collapse) or if the pairs are pruned as a group rather than independently
    (Ranked By would wrongly survive alongside Rank Value)."""
    rows = [
        {
            "district_name": "Alpha",
            "state": "CA",
            "metric_name": "salary",
            "display_value": "$80,000",
            "academic_year": "2024 - 2025",
            "coverage_state": "covered",
            "coverage_display": "$80,000",
            "coverage_reason": "answer",
            "citation_markers": "1",
            "source_document": "Doc A",
            "source_document_type": "Contract",
            "source_page": "p. 5",
            "source_urls": "https://example.org/a",
            "sort_metric_name": "salary",  # == Measure
            "sort_display_value": "$80,000",  # == Value (this row only)
            "sort_academic_year": "2024 - 2025",  # == School Year
            "sort_source_url": "https://example.org/rank-a",  # != Source URL(s)
        },
        {
            "district_name": "Bravo",
            "state": "CA",
            "metric_name": "salary",
            "display_value": "$70,000",
            "academic_year": "2024 - 2025",
            "coverage_state": "covered",
            "coverage_display": "$70,000",
            "coverage_reason": "answer",
            "citation_markers": "2",
            "source_document": "Doc B",
            "source_document_type": "Contract",
            "source_page": "p. 6",
            "source_urls": "https://example.org/b",
            "sort_metric_name": "salary",  # == Measure
            "sort_display_value": "$99,999",  # != Value → Rank Value must be KEPT
            "sort_academic_year": "2024 - 2025",  # == School Year
            "sort_source_url": "https://example.org/rank-b",  # != Source URL(s)
        },
    ]

    columns, public_rows = _public_csv_export(rows)

    assert "Ranked By" not in columns  # redundant every row → dropped
    assert "Rank School Year" not in columns  # redundant every row → dropped
    assert "Rank Value" in columns  # diverges in row 2 → kept
    assert "Rank Source URL" in columns  # divergent + non-empty → kept
    assert public_rows[0]["Rank Value"] == "$80,000"
    assert public_rows[1]["Rank Value"] == "$99,999"


# ─── Rule 1: coverage variation KEEPS Data Note ──────────────────────────────


def test_coverage_variation_keeps_data_note() -> None:
    """When coverage varies — a "not reviewed" disclosure row carries the full
    display string as its Data Note — the column is non-empty somewhere and is
    kept, preserving parity. (Contrast the all-covered case above, where it
    drops.)"""
    base = _ranked_by_displayed_metric_result()
    not_reviewed_display = (
        "NCTQ hasn't reviewed Bravo for salary in 2024 - 2025 yet."
    )
    result = MetricRankingResult(
        selection=base.selection,
        rows=base.rows,
        citations=base.citations,
        coverage_disclosures=[
            CoverageDisclosure(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=100,
                metric_name="salary",
                academic_year="2024 - 2025",
                coverage_state="not_reviewed",
                display=not_reviewed_display,
                reason="district_not_reviewed",
            ),
        ],
        total_considered=base.total_considered,
        excluded_count=base.excluded_count,
        order_statement=base.order_statement,
    )

    assert result.csv_export is not None
    assert "Data Note" in result.csv_export.public_columns
    disclosure_rows = [
        r for r in result.csv_export.public_rows if r.get("District") == "Bravo"
    ]
    assert len(disclosure_rows) == 1
    assert disclosure_rows[0]["Data Note"] == not_reviewed_display


# ─── Edge: empty input rows return base columns unpruned ─────────────────────


def test_empty_rows_return_base_columns_unpruned() -> None:
    """Empty input rows must never produce a zero-column CSV — return the base
    public schema unpruned (no rank columns, since no row proves sort
    metadata). The existing parity suite asserts a non-empty csv_export
    elsewhere; this guards the projection helper itself."""
    columns, public_rows = _public_csv_export([])

    assert public_rows == []
    assert columns == [
        "Export Source",
        "District",
        "State",
        "Measure",
        "Value",
        "School Year",
        "Data Status",
        "Data Note",
        "Citation Marker(s)",
        "Source Document",
        "Source Type",
        "Source Page(s)",
        "Source URL(s)",
    ]
