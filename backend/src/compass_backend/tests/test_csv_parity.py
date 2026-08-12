"""W2-M5-01 (#745) — CSV export parity with chat-rendered tables.

The Ashley/Shannon Golden29 reviewer complaints reduce to two distinct
bugs:

  1. **Blank CSV** — sessions 62 / 64 / etc. downloaded a CSV with no
     rows at all. Root cause: ``CompositeRankingResult.csv_export`` is
     not populated at construction time (the envelope-level validator
     intentionally returns ``self`` with ``csv_export=None``), so the
     SSE ``data`` event ships ``csv_export: null`` to the frontend. The
     user sees a table in chat but downloads nothing.

  2. **Sources column empty** — for NCES-backed answers (#747 / W2-M5-03
     territory) the row-level ``citation_markers`` are empty because the
     NCES allowlist plumbing into citation emission isn't wired yet. The
     CSV faithfully reflects that empty state; both surfaces are in
     parity (chat shows no Sources column when no row has markers).
     Out of scope for this PR; tracked by #747.

This module asserts:

  - Every executable ``ResultSet`` variant produces a non-empty
    ``csv_export`` artifact (the "no blank CSV" invariant, including
    composite envelopes).
  - For every row that carries citation markers in the chat-rendered
    output, the CSV exports the same marker numbers in its
    ``citation_markers`` column (the "Sources column parity" invariant).
  - Composite envelopes emit one CSV row per child-row, with the
    metric_name distinguisher preserved.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    CompositeRankingResult,
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricTrendResult,
    PeerComparisonResult,
    ProfileLookupResult,
    RankingRow,
    ResultSelection,
)
from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.results import (
    CategoricalCountRow,
    CoverageDisclosure,
    MetricValueRow,
    PeerComparisonRow,
    ProfileValueRow,
)


# ─── Fixtures (small, deterministic, one row per variant) ────────────────────


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


def _ranking_result(metric_id: int = 100, metric_name: str = "salary") -> MetricRankingResult:
    return MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=metric_id,
                metric_name=metric_name,
                value=80000.0,
                display_value="$80,000",
                academic_year="2024 - 2025",
                rank=1,
                source="policy_answer",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$80,000",
                coverage_reason="answer",
            ),
        ],
        citations=[_citation(marker=1)],
        total_considered=1,
        excluded_count=0,
        order_statement=f"Ranked by {metric_name}, highest to lowest.",
    )


def _lookup_result() -> MetricLookupResult:
    return MetricLookupResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
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
                coverage_reason="answer",
            ),
        ],
        citations=[_citation(marker=1)],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up salary for Alpha.",
    )


def _profile_lookup_result() -> ProfileLookupResult:
    return ProfileLookupResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            ProfileValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                field_key="enrollment",
                label="Enrollment",
                value=12345,
                display_value="12,345",
                academic_year="2024 - 2025",
                source="profile_field",
                source_label="NCES",
                provenance="CCD 2024-25",
                covered_by_compass=True,
                metric_id=-1001,
                metric_name="Enrollment",
                coverage_state="covered",
                coverage_display="12,345",
                coverage_reason="profile_field_value",
            ),
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up enrollment for Alpha.",
    )


def _peer_result() -> PeerComparisonResult:
    return PeerComparisonResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            PeerComparisonRow(
                district_id=1,
                district_name="Alpha",
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
                coverage_reason="answer",
                peer_role="anchor",
            ),
            PeerComparisonRow(
                district_id=2,
                district_name="Beta",
                state="CA",
                metric_id=100,
                metric_name="salary",
                value=78000.0,
                display_value="$78,000",
                academic_year="2024 - 2025",
                source="policy_answer",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$78,000",
                coverage_reason="answer",
                peer_role="peer",
                peer_rank=1,
                peer_score=0.92,
            ),
        ],
        citations=[_citation(marker=1)],
        total_considered=2,
        excluded_count=0,
        order_statement="Compared Alpha against 1 peer.",
    )


def _count_result() -> MetricCountResult:
    return MetricCountResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            CategoricalCountRow(
                metric_id=100,
                metric_name="union_status",
                value="unionized",
                display_value="120",
                academic_year="2024 - 2025",
                count=120,
                denominator=150,
                percent=80.0,
                filter_statement="districts marked unionized",
                qualifying_district_ids=[1, 2, 3],
                source="policy_answer",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="120",
                coverage_reason="aggregate",
                category="unionized",
            ),
        ],
        citations=[_citation(marker=1)],
        total_considered=150,
        excluded_count=0,
        order_statement="Counted districts by union status.",
    )


def _composite_result() -> CompositeRankingResult:
    return CompositeRankingResult(
        children=[
            _ranking_result(metric_id=101, metric_name="BA salary"),
            _ranking_result(metric_id=102, metric_name="MA salary"),
        ],
        total_considered=0,  # validator rolls up from children
        excluded_count=0,
        order_statement="Two metrics, ranked highest to lowest by each.",
    )


# ─── Shape 1: ranking — CSV exists, citation markers present ────────────────


def test_ranking_csv_export_is_populated_with_citation_markers() -> None:
    result = _ranking_result()
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 1
    row = result.csv_export.rows[0]
    assert row["citation_markers"] == "1"
    assert row["source_url"] == "https://example.org/test-contract.pdf"


# ─── Shape 2: lookup ────────────────────────────────────────────────────────


def test_lookup_csv_export_is_populated_with_citation_markers() -> None:
    result = _lookup_result()
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 1
    assert result.csv_export.rows[0]["citation_markers"] == "1"
    assert (
        result.csv_export.rows[0]["source_url"]
        == "https://example.org/test-contract.pdf"
    )


def test_lookup_csv_export_has_public_download_schema_without_internal_ids() -> None:
    """#1502: downloaded CSVs should not expose Compass's internal row schema.

    #1611 re-baseline: column pruning now drops a column empty in every row.
    This single covered row has no Data Note, so the public schema is the
    12-column shape (Data Note dropped). The test's intent — no internal IDs
    leak into the public projection — is unchanged."""
    result = _lookup_result()

    assert result.csv_export is not None
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
    assert result.csv_export.columns[0] == "district_id"

    public_row = result.csv_export.public_rows[0]
    assert public_row == {
        "Export Source": "National Council on Teacher Quality (NCTQ) Compass",
        "District": "Alpha",
        "State": "CA",
        "Measure": "salary",
        "Value": "$80,000",
        "School Year": "2024 - 2025",
        "Data Status": "Available",
        "Citation Marker(s)": "1",
        "Source Document": "Test District Contract",
        "Source Type": "Contract",
        "Source Page(s)": "p. 5",
        "Source URL(s)": "https://example.org/test-contract.pdf",
    }
    assert "district_id" not in public_row
    assert "coverage_reason" not in public_row


def test_public_csv_uses_short_coverage_label_for_disclosure_rows() -> None:
    """#1514 — ranking disclosure rows (non-answer, appended after data rows)
    get a human-readable Data Status in the public CSV via _public_data_status.
    Lookup CSVs are answers-only (#1514 D2), so disclosures only appear in
    ranking public_rows."""

    base = _ranking_result()
    not_reviewed_display = (
        "NCTQ hasn't reviewed Bravo for Average teacher starting salary "
        "in 2024 - 2025 yet."
    )
    result = MetricRankingResult(
        selection=base.selection,
        rows=base.rows,
        citations=base.citations,
        coverage_frame=base.coverage_frame,
        coverage_disclosures=[
            CoverageDisclosure(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=100,
                metric_name="Average teacher starting salary",
                academic_year="2024 - 2025",
                coverage_state="not_reviewed",
                display=not_reviewed_display,
                reason="district_not_reviewed",
            ),
        ],
        total_considered=base.total_considered,
        excluded_count=base.excluded_count,
        order_statement=base.order_statement,
        methodology_codes=base.methodology_codes,
    )

    assert result.csv_export is not None
    # Data rows come first (covered answers), then disclosure rows.
    disclosure_rows = [
        r for r in result.csv_export.public_rows if r.get("District") == "Bravo"
    ]
    assert len(disclosure_rows) == 1
    public_row = disclosure_rows[0]
    assert public_row["Data Status"] == "Not reviewed"
    # Note should contain the full display string when it differs from the status.
    assert public_row["Data Note"] == not_reviewed_display


def test_ranking_public_csv_adds_rank_columns_only_when_sort_metadata_exists() -> None:
    result = MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=100,
                metric_name="BA salary",
                value=80000.0,
                display_value="$80,000",
                academic_year="2024 - 2025",
                rank=1,
                source="policy_answer",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$80,000",
                coverage_reason="answer",
                sort_metric_id=200,
                sort_metric_name="FRPL %",
                sort_value=0.9,
                sort_display_value="90%",
                sort_academic_year="2023 - 2024",
                sort_source_url="https://example.org/frpl",
            ),
        ],
        citations=[_citation(marker=1)],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by FRPL %, highest to lowest.",
    )

    assert result.csv_export is not None
    assert result.csv_export.public_columns[-4:] == [
        "Ranked By",
        "Rank Value",
        "Rank School Year",
        "Rank Source URL",
    ]
    assert result.csv_export.public_rows[0]["Ranked By"] == "FRPL %"
    assert result.csv_export.public_rows[0]["Rank Value"] == "90%"
    assert result.csv_export.public_rows[0]["Rank School Year"] == "2023 - 2024"
    assert result.csv_export.public_rows[0]["Rank Source URL"] == "https://example.org/frpl"


# ─── Shape 3: profile lookup (NCES) — CSV exists, markers may be empty ─────


def test_profile_lookup_csv_export_is_non_empty_even_without_citations() -> None:
    """Artifact-level invariant: a ProfileLookupResult with no citations
    attached still emits a non-blank CSV. (The execution-layer builder
    ``build_profile_lookup_result`` now attaches NCES citations per
    W2-M5-03 / #747; this artifact-layer fixture constructs the result
    directly without going through the builder, exercising the path
    where rows have empty citation_markers — e.g., legacy snapshots
    deserialized without per-row markers, or future variants we haven't
    wired yet.)"""
    result = _profile_lookup_result()
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 1
    # Empty markers when no citations are attached — the CSV still renders.
    assert result.csv_export.rows[0]["citation_markers"] == ""


# ─── Shape 4: peer comparison ───────────────────────────────────────────────


def test_peer_csv_export_is_populated_with_citation_markers() -> None:
    result = _peer_result()
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 2
    for csv_row in result.csv_export.rows:
        assert csv_row["citation_markers"] == "1"


# ─── Shape 5: count (categorical) ───────────────────────────────────────────


def test_count_csv_export_is_populated_with_citation_markers() -> None:
    result = _count_result()
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 1
    assert result.csv_export.rows[0]["citation_markers"] == "1"


# ─── Shape 6: composite ranking — THE BLANK CSV BUG ─────────────────────────


def test_composite_csv_export_is_not_blank() -> None:
    """The bug that caused 'CSV was BLANK - no data' on sheet rows 62/64.

    ``CompositeRankingResult.populate_artifact_surfaces`` previously
    returned ``self`` without setting ``csv_export``, so the SSE shipped
    ``csv_export: null`` to the frontend → blank download. The envelope
    must carry a CSV that aggregates rows across all children, with the
    ``metric_name`` column distinguishing rows from different children."""
    result = _composite_result()
    assert result.csv_export is not None, (
        "composite envelope must populate csv_export — blank CSV bug (#745)"
    )
    # One row per child row (both children have 1 row in the fixture)
    assert len(result.csv_export.rows) == 2
    # metric_name distinguishes the child each row came from
    metric_names = {row["metric_name"] for row in result.csv_export.rows}
    assert metric_names == {"BA salary", "MA salary"}


def test_composite_csv_export_preserves_citation_markers_per_child() -> None:
    """Each composite child's rows must carry their citation_markers
    through into the aggregated CSV. The W2-M5-04 cross-child dedup
    already ensures marker numbering is consistent across the composite;
    this test asserts the CSV consumes that consistency correctly."""
    result = _composite_result()
    assert result.csv_export is not None
    for csv_row in result.csv_export.rows:
        # Both children share the same source URL → same marker [1] across the envelope
        assert csv_row["citation_markers"] == "1"


def test_composite_zip_keeps_collapsing_child_rank_values_when_sibling_diverges() -> None:
    """#1611 regression: per-child rank-column pruning must not blank a
    collapsing child's rank cells in the aggregated envelope.

    Child A is ranked by the metric it displays, so on its own the redundant
    rank columns collapse and its per-child public_rows drop those keys. Child B
    is a genuine cross-metric sort, so over the aggregated union the rank
    columns are kept. The envelope must re-project EVERY row from the union, not
    splice in the per-child-pruned rows — otherwise child A's (now-kept) rank
    columns come out blank. Before the fix, child A's row had no Ranked By /
    Rank Value at all."""

    def _child(
        metric_name: str,
        display_value: str,
        sort_metric_name: str,
        sort_display_value: str,
        sort_year: str,
    ) -> MetricRankingResult:
        return MetricRankingResult(
            selection=ResultSelection(scope="all_covered_districts"),
            rows=[
                RankingRow(
                    district_id=1,
                    district_name="Alpha",
                    state="CA",
                    metric_id=100,
                    metric_name=metric_name,
                    value=1.0,
                    display_value=display_value,
                    academic_year="2024 - 2025",
                    rank=1,
                    source="policy_answer",
                    citation_markers=[1],
                    coverage_state="covered",
                    coverage_display=display_value,
                    coverage_reason="answer",
                    sort_metric_name=sort_metric_name,
                    sort_display_value=sort_display_value,
                    sort_academic_year=sort_year,
                ),
            ],
            citations=[_citation(marker=1)],
            total_considered=1,
            excluded_count=0,
            order_statement=f"Ranked by {sort_metric_name}, highest to lowest.",
        )

    collapsing = _child(
        metric_name="BA salary",
        display_value="$80,000",
        sort_metric_name="BA salary",  # == Measure
        sort_display_value="$80,000",  # == Value
        sort_year="2024 - 2025",  # == School Year
    )
    diverging = _child(
        metric_name="class size",
        display_value="22",
        sort_metric_name="enrollment",  # != Measure
        sort_display_value="50,000",  # != Value
        sort_year="2023 - 2024",  # != School Year
    )
    composite = CompositeRankingResult(
        children=[collapsing, diverging],
        total_considered=0,
        excluded_count=0,
        order_statement="Two metrics, ranked by each.",
    )

    assert composite.csv_export is not None
    columns = composite.csv_export.public_columns
    # The diverging sibling keeps the rank columns in the envelope.
    assert "Ranked By" in columns and "Rank Value" in columns
    # The collapsing child's row must still carry its real rank values, not blanks.
    collapsing_row = next(
        r for r in composite.csv_export.public_rows if r.get("Measure") == "BA salary"
    )
    assert collapsing_row.get("Ranked By") == "BA salary"
    assert collapsing_row.get("Rank Value") == "$80,000"


# ─── Shape 7: trend ─────────────────────────────────────────────────────────


def test_trend_csv_export_is_populated_with_citation_markers() -> None:
    """Cover the seventh result variant. Trend uses ``_trend_csv_export_for_result``
    not the default, so this is a distinct surface."""
    result = MetricTrendResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
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
                coverage_reason="answer",
                numeric_value=80000.0,
                delta_value=2000.0,
                delta_percent=2.56,
                delta_from_academic_year="2023 - 2024",
            ),
        ],
        citations=[_citation(marker=1)],
        total_considered=1,
        excluded_count=0,
        order_statement="Trend for salary in Alpha.",
    )
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 1
    assert result.csv_export.rows[0]["citation_markers"] == "1"


# ─── Whole-answer invariant ──────────────────────────────────────────────────


def test_no_executable_result_variant_emits_blank_csv() -> None:
    """Parameterised invariant: every executable ResultSet variant the
    executor can emit must produce a non-empty csv_export when the result
    has at least one row. This is the structural check that catches the
    'CSV was BLANK' bug class going forward."""
    fixtures = [
        ("ranking", _ranking_result()),
        ("lookup", _lookup_result()),
        ("profile_lookup", _profile_lookup_result()),
        ("peer", _peer_result()),
        ("count", _count_result()),
        ("composite", _composite_result()),
    ]
    blank: list[str] = []
    for label, result in fixtures:
        if result.csv_export is None or not result.csv_export.rows:
            blank.append(label)
    assert not blank, (
        f"{blank} produced blank/missing csv_export — #745 regression"
    )


# ─── Answers-only data rows (#1514 D2) ───────────────────────────────────────


def _not_reviewed_lookup_row() -> MetricValueRow:
    return MetricValueRow(
        district_id=2,
        district_name="Bravo",
        state="CA",
        metric_id=100,
        metric_name="salary",
        value=None,
        display_value="NCTQ hasn't reviewed Bravo for salary in 2024 - 2025 yet.",
        academic_year="2024 - 2025",
        source="coverage_state",
        coverage_state="not_reviewed",
        coverage_display="NCTQ hasn't reviewed Bravo for salary in 2024 - 2025 yet.",
        coverage_reason="metric_not_reviewed",
    )


def _out_of_universe_lookup_row() -> MetricValueRow:
    return MetricValueRow(
        district_id=None,
        district_name="Cincinnati, OH",
        state="OH",
        metric_id=100,
        metric_name="salary",
        value=None,
        display_value="Cincinnati, OH is not in the District Policy Pathfinder.",
        academic_year="2024 - 2025",
        source="coverage_state",
        coverage_state="out_of_universe",
        coverage_display="Cincinnati, OH is not in the District Policy Pathfinder.",
        coverage_reason="out_of_universe",
    )


def test_lookup_csv_data_rows_are_answers_only() -> None:
    """#1514 D2 — the CSV mirrors the table: data rows hold answer states only
    (covered / ina / na); not_reviewed and out_of_universe rows are voiced as
    narrative sentences in the rendered answer. The ResultSet keeps the full
    record — only the CSV projection filters."""
    base = _lookup_result()
    result = MetricLookupResult(
        selection=base.selection,
        rows=[
            base.rows[0],
            _not_reviewed_lookup_row(),
            _out_of_universe_lookup_row(),
        ],
        citations=base.citations,
        total_considered=3,
        excluded_count=0,
        order_statement=base.order_statement,
    )

    # ResultSet stays the full record (V-10 guard) …
    assert len(result.rows) == 3
    # … while the CSV projects answers only.
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 1
    assert result.csv_export.rows[0]["district_name"] == "Alpha"
    assert result.csv_export.rows[0]["coverage_state"] == "covered"


def test_trend_csv_data_rows_are_answers_only() -> None:
    """#1514 D2 — same answers-only projection on the trend CSV builder."""
    result = MetricTrendResult(
        selection=ResultSelection(scope="named_districts"),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
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
                coverage_reason="answer",
                numeric_value=80000.0,
            ),
            _not_reviewed_lookup_row(),
        ],
        citations=[_citation(marker=1)],
        total_considered=2,
        excluded_count=0,
        order_statement="Trend for salary in Alpha and Bravo.",
    )

    assert len(result.rows) == 2
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 1
    assert result.csv_export.rows[0]["coverage_state"] == "covered"


def _categorical_bucket(
    *,
    metric_id: int,
    metric_name: str,
    category: str,
    count: int,
    denominator: int,
    qualifying_district_ids: list[int],
    coverage_state: str = "covered",
    coverage_reason: str = "categorical_value_count",
    citation_markers: list[int] | None = None,
) -> CategoricalCountRow:
    display_value = f"{count} of {denominator} covered districts"
    return CategoricalCountRow(
        metric_id=metric_id,
        metric_name=metric_name,
        value=category,
        category=category,
        display_value=display_value,
        academic_year="2024 - 2025",
        count=count,
        denominator=denominator,
        percent=round(count / denominator * 100, 1),
        filter_statement="grouped current categorical values",
        qualifying_district_ids=qualifying_district_ids,
        source="policy_answer",
        citation_markers=citation_markers or [],
        coverage_state=coverage_state,
        coverage_display=display_value,
        coverage_reason=coverage_reason,
    )


def test_count_csv_data_rows_are_answers_only() -> None:
    """#1514 D12 (Fix B blocker B1) — the count CSV mirrors the table and
    chart: "Not reviewed"/"Unavailable" categorical buckets leave the CSV the
    same way they left the other two surfaces; their counts are voiced as the
    rule-2 narrative sentence. Reviewer repro shape: a multi-metric
    categorical count whose MIDDLE row is metric 1's not_reviewed bucket —
    pre-fix the builder emitted all three rows while the parity validator
    zipped against answers only, shifting the zip and error-failing the
    healthy answer on the live chat path."""
    result = MetricCountResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            _categorical_bucket(
                metric_id=100,
                metric_name="union_status",
                category="Yes",
                count=2,
                denominator=3,
                qualifying_district_ids=[1, 2],
                citation_markers=[1],
            ),
            _categorical_bucket(
                metric_id=100,
                metric_name="union_status",
                category="Not reviewed",
                count=1,
                denominator=3,
                qualifying_district_ids=[3],
                coverage_state="not_reviewed",
                coverage_reason="metric_not_reviewed",
            ),
            _categorical_bucket(
                metric_id=200,
                metric_name="board_approval",
                category="No",
                count=3,
                denominator=3,
                qualifying_district_ids=[1, 2, 3],
                citation_markers=[1],
            ),
        ],
        citations=[_citation(marker=1)],
        total_considered=6,
        excluded_count=0,
        order_statement="Counted categorical value distribution.",
    )

    # ResultSet stays the full record (V-10 guard) …
    assert len(result.rows) == 3
    # … while the CSV mirrors table + chart: answer buckets only, in order.
    assert result.csv_export is not None
    assert [
        (row["metric_id"], row["coverage_state"]) for row in result.csv_export.rows
    ] == [(100, "covered"), (200, "covered")]


# ─── Coverage disclosure rows — stale-recent-answer filter (#1435) ───────────


def _disclosure(reason: str, district_id: int, district_name: str) -> CoverageDisclosure:
    return CoverageDisclosure(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=100,
        metric_name="salary",
        academic_year="2024 - 2025",
        coverage_state="covered",
        display="$70,000",
        reason=reason,
        prior_academic_year="2022 - 2023",
        prior_display_value="$70,000",
    )


def test_stale_recent_answer_disclosures_are_dropped_from_csv() -> None:
    """#1435 — a ``stale_recent_answer`` disclosure carries a prior-year value
    as its ``display``. Re-enumerating it inline in the CSV next to the ranked
    set invites misreading the stale value as current. The CSV must drop stale
    disclosures (they live only in the markdown supplementary block); non-stale
    coverage disclosures still appear."""
    base = _ranking_result()
    # Build via the constructor (not model_copy) so the per-variant
    # populate_artifact_surfaces validator re-derives csv_export with the
    # disclosures attached.
    result = MetricRankingResult(
        selection=base.selection,
        rows=base.rows,
        citations=base.citations,
        total_considered=base.total_considered,
        excluded_count=base.excluded_count,
        order_statement=base.order_statement,
        coverage_disclosures=[
            _disclosure("stale_recent_answer", 2, "Stale District"),
            _disclosure("no_policy_answer", 3, "Disclosed District"),
        ],
    )
    assert result.csv_export is not None
    csv_rows = result.csv_export.rows

    # Ranked rows (1) + non-stale disclosure rows (1) == 2; stale dropped.
    assert len(csv_rows) == len(result.rows) + 1

    csv_names = {row["district_name"] for row in csv_rows}
    assert "Stale District" not in csv_names
    assert "Disclosed District" in csv_names
    assert not any(
        row["coverage_reason"] == "stale_recent_answer" for row in csv_rows
    )
