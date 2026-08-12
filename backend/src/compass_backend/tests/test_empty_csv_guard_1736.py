"""#1736 — never emit a literally-empty CSV when every row is not_reviewed.

Bug #1736: a comparison where every named district is ``not_reviewed`` for the
asked metric produced a header-only CSV — columns, zero data rows — because the
CSV data-row gate keeps answer states only (``is_rendered_answer_state``:
covered/ina/na; ``not_reviewed`` is voiced as a narrative sentence, never a data
point). For peer/lookup/profile/trend the builder appends no re-derived
disclosures (only ranking does, via ``promoted_row_disclosures``), so the built
artifact's ``rows`` list is empty — the #1222 blank-file symptom.

The decision-independent fix injects ZERO data rows. It only suppresses the
empty artifact: ``populate_artifact_surfaces`` assigns ``csv_export`` only when
the built artifact has data rows, otherwise leaves it as its default ``None``.
``None`` flips ``has_csv_export`` (computed as ``csv_export is not None`` in
rendering/writer.py and session/memory.py) to False, so the stylist promises no
download, SSE ships ``csv_export: None``, and the on-screen not_reviewed
narrative is the clean message. This is the zero-ROW twin of the existing
zero-COLUMN guard in ``_public_csv_export``.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    MetricLookupResult,
    PeerComparisonResult,
    ResultSelection,
)
from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.results import (
    MetricValueRow,
    PeerComparisonRow,
)


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


def _not_reviewed_lookup_row(district_id: int, district_name: str) -> MetricValueRow:
    return MetricValueRow(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=100,
        metric_name="salary",
        value=None,
        display_value=(
            f"NCTQ hasn't reviewed {district_name} for salary "
            "(2024 - 2025 data not yet reviewed)."
        ),
        academic_year="2024 - 2025",
        source="coverage_state",
        coverage_state="not_reviewed",
        coverage_display=(
            f"NCTQ hasn't reviewed {district_name} for salary "
            "(2024 - 2025 data not yet reviewed)."
        ),
        coverage_reason="metric_not_reviewed",
    )


def _covered_lookup_row(district_id: int, district_name: str) -> MetricValueRow:
    return MetricValueRow(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=100,
        metric_name="salary",
        value=80000.0,
        display_value="$80,000",
        academic_year="2024 - 2025",
        source="policy_answer",
        citation_markers=[1],
        coverage_state="covered",
        coverage_display="$80,000",
        coverage_reason="answer_value",
    )


def _not_reviewed_peer_row(
    district_id: int, district_name: str, peer_role: str, peer_rank: int | None = None
) -> PeerComparisonRow:
    return PeerComparisonRow(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=100,
        metric_name="salary",
        value=None,
        display_value=(
            f"NCTQ hasn't reviewed {district_name} for salary "
            "(2024 - 2025 data not yet reviewed)."
        ),
        academic_year="2024 - 2025",
        source="coverage_state",
        coverage_state="not_reviewed",
        coverage_display=(
            f"NCTQ hasn't reviewed {district_name} for salary "
            "(2024 - 2025 data not yet reviewed)."
        ),
        coverage_reason="metric_not_reviewed",
        peer_role=peer_role,
        peer_rank=peer_rank,
    )


def _covered_peer_row(
    district_id: int, district_name: str, peer_role: str, peer_rank: int | None = None
) -> PeerComparisonRow:
    return PeerComparisonRow(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=100,
        metric_name="salary",
        value=80000.0,
        display_value="$80,000",
        academic_year="2024 - 2025",
        source="policy_answer",
        citation_markers=[1],
        coverage_state="covered",
        coverage_display="$80,000",
        coverage_reason="answer_value",
        peer_role=peer_role,
        peer_rank=peer_rank,
    )


# ─── The bug: every row not_reviewed → no CSV (was a header-only empty file) ──


def test_all_not_reviewed_peer_comparison_has_no_csv_export() -> None:
    """A peer comparison whose every row is not_reviewed yields csv_export=None,
    never a header-only empty CSV (#1736)."""
    result = PeerComparisonResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            _not_reviewed_peer_row(1, "Alpha", peer_role="anchor"),
            _not_reviewed_peer_row(2, "Beta", peer_role="peer", peer_rank=1),
        ],
        citations=[],
        total_considered=2,
        excluded_count=0,
        order_statement="Compared Alpha against 1 peer.",
    )

    # ResultSet keeps the full record (the not_reviewed rows are voiced as
    # narrative) …
    assert len(result.rows) == 2
    # … and the CSV is suppressed entirely rather than emitted header-only.
    assert result.csv_export is None
    # has_csv_export (computed as `csv_export is not None` in writer.py /
    # memory.py) is therefore False.
    assert (result.csv_export is not None) is False


def test_all_not_reviewed_metric_lookup_has_no_csv_export() -> None:
    """Same guard on the lookup builder (#1736)."""
    result = MetricLookupResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            _not_reviewed_lookup_row(1, "Alpha"),
            _not_reviewed_lookup_row(2, "Beta"),
        ],
        citations=[],
        total_considered=2,
        excluded_count=0,
        order_statement="Lookup for salary in Alpha and Beta.",
    )

    assert len(result.rows) == 2
    assert result.csv_export is None
    assert (result.csv_export is not None) is False


# ─── Counter-test: ≥1 covered row still produces a CSV with that row ──────────


def test_one_covered_row_peer_comparison_keeps_csv_export() -> None:
    """A peer comparison with at least one covered row still yields a non-None
    csv_export carrying that data row — the guard only suppresses the all-empty
    case (#1736)."""
    result = PeerComparisonResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            _covered_peer_row(1, "Alpha", peer_role="anchor"),
            _not_reviewed_peer_row(2, "Beta", peer_role="peer", peer_rank=1),
        ],
        citations=[_citation(marker=1)],
        total_considered=2,
        excluded_count=0,
        order_statement="Compared Alpha against 1 peer.",
    )

    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 1
    assert result.csv_export.rows[0]["district_name"] == "Alpha"
    assert result.csv_export.rows[0]["coverage_state"] == "covered"


def test_one_covered_row_metric_lookup_keeps_csv_export() -> None:
    """Same counter-test on the lookup builder (#1736)."""
    result = MetricLookupResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            _covered_lookup_row(1, "Alpha"),
            _not_reviewed_lookup_row(2, "Beta"),
        ],
        citations=[_citation(marker=1)],
        total_considered=2,
        excluded_count=0,
        order_statement="Lookup for salary in Alpha and Beta.",
    )

    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 1
    assert result.csv_export.rows[0]["district_name"] == "Alpha"
    assert result.csv_export.rows[0]["coverage_state"] == "covered"
