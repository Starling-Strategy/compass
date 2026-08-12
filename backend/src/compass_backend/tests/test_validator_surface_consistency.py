"""Focused tests for the cross-surface consistency validators.

These lock in the three drift checks added in the surface_consistency
implementation:

- `markdown_cell_drift`: rendered table cells must equal
  `format_cell_value(row, column)` for stable columns (District, State,
  Sources, Academic year).
- `csv_cell_drift`: csv_export.rows[i][col] must equal the corresponding
  row attribute when the CSV projects the column.
- `artifact_id_drift`: `manifest.metadata['artifact_id']` must match
  `compute_artifact_id(result)` re-derived at validation time.

Doctrine: validators run in `validate_result()`; tests construct a typed
result + manifest_metadata + rendered_body and assert the returned
`ValidationReport.findings` either include or exclude the relevant code.
"""

from __future__ import annotations

from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.identity import compute_artifact_id
from compass_backend.artifacts.results import (
    CategoricalCountRow,
    CoverageFrame,
    CsvExportArtifact,
    MethodologyRef,
    MetricCountResult,
    MetricLookupResult,
    MetricValueRow,
    MetricRankingResult,
    PeerComparisonResult,
    PeerComparisonRow,
    RankingRow,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.contracts.planning import (
    LimitSpec,
    MetricSpec,
    OutputSpec,
    QueryPlan,
    SelectionSpec,
    SortSpec,
    TemporalSpec,
)
from compass_backend.contracts.validation import ValidationReport
from compass_backend.quality.validation import validate_result
from compass_backend.rendering.writer import render_response


# ── fixtures ─────────────────────────────────────────────────────────────────


def _ranking_plan() -> QueryPlan:
    return QueryPlan(
        operation="rank",
        question="rank California districts by starting salary",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="Starting salary")],
        output=OutputSpec(format="table"),
        temporal=TemporalSpec(intent="current", academic_year="2024 - 2025"),
        sort=SortSpec(field="Starting salary", direction="desc"),
    )


def _ranking_result(
    *,
    rows: list[RankingRow] | None = None,
    citations: list[CitationRef] | None = None,
) -> MetricRankingResult:
    return MetricRankingResult(
        rows=rows
        or [
            RankingRow(
                district_id=1,
                district_name="Alpha ISD",
                state="CA",
                metric_id=100,
                metric_name="Starting salary",
                value=80000,
                display_value="$80,000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$80,000",
                coverage_reason="answer_value",
            ),
            RankingRow(
                district_id=2,
                district_name="Beta USD",
                state="CA",
                metric_id=100,
                metric_name="Starting salary",
                value=70000,
                display_value="$70,000",
                academic_year="2024 - 2025",
                rank=2,
                citation_markers=[2],
                coverage_state="covered",
                coverage_display="$70,000",
                coverage_reason="answer_value",
            ),
        ],
        citations=citations
        or [
            CitationRef(marker=1, title="Alpha ISD salary schedule"),
            CitationRef(marker=2, title="Beta USD salary schedule"),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Sorted by Starting salary descending.",
    )


def _markdown_body(*, alpha_name: str = "Alpha ISD") -> str:
    """Render a small markdown table matching the default _ranking_result."""
    return (
        "Top California districts by Starting salary.\n"
        "\n"
        "| Rank | District | State | Starting salary | Sources |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"| 1 | {alpha_name} | CA | $80,000 | [1] |\n"
        "| 2 | Beta USD | CA | $70,000 | [2] |\n"
    )


def _lookup_plan() -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="Compare Alpha ISD and Unknown District on Metric A and Metric B.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Alpha ISD", "Unknown District"],
        ),
        metrics=[MetricSpec(name="Metric A"), MetricSpec(name="Metric B")],
        output=OutputSpec(format="table"),
        temporal=TemporalSpec(intent="current", academic_year="2024 - 2025"),
    )


def _lookup_result() -> MetricLookupResult:
    return MetricLookupResult(
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha ISD",
                state="CA",
                metric_id=10,
                metric_name="Metric A",
                value=10,
                display_value="10 days",
                academic_year="2024 - 2025",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="10 days",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=1,
                district_name="Alpha ISD",
                state="CA",
                metric_id=11,
                metric_name="Metric B",
                value="No",
                display_value="No",
                academic_year="2024 - 2025",
                citation_markers=[2],
                coverage_state="covered",
                coverage_display="No",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=None,
                district_name="Unknown District",
                state="",
                metric_id=10,
                metric_name="Metric A",
                value=None,
                display_value=(
                    "Unknown District is not in the District Policy Pathfinder."
                ),
                academic_year="2024 - 2025",
                source="coverage_state",
                coverage_state="out_of_universe",
                coverage_display=(
                    "Unknown District is not in the District Policy Pathfinder."
                ),
                coverage_reason="out_of_universe",
            ),
            MetricValueRow(
                district_id=None,
                district_name="Unknown District",
                state="",
                metric_id=11,
                metric_name="Metric B",
                value=None,
                display_value=(
                    "Unknown District is not in the District Policy Pathfinder."
                ),
                academic_year="2024 - 2025",
                source="coverage_state",
                coverage_state="out_of_universe",
                coverage_display=(
                    "Unknown District is not in the District Policy Pathfinder."
                ),
                coverage_reason="out_of_universe",
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha Metric A policy",
                url="https://example.test/alpha-a.pdf",
                page_ref="p. 4",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            ),
            CitationRef(
                marker=2,
                title="Alpha Metric B policy",
                url="https://example.test/alpha-b.pdf",
                page_number=8,
                academic_year="2024 - 2025",
                document_type="Board Policy",
                district_id=1,
            ),
        ],
        total_considered=4,
        excluded_count=0,
        order_statement="Looked up selected districts and metrics.",
    )


def _render_lookup_body(result: MetricLookupResult | None = None) -> str:
    manifest = render_response(
        _lookup_plan(),
        result or _lookup_result(),
        ValidationReport(valid=True, dimensions_checked=[], findings=[]),
    )
    return manifest.body


# ── markdown_cell_drift ──────────────────────────────────────────────────────


def test_markdown_table_parity_passes_when_cells_match() -> None:
    """No findings when every markdown cell matches the formatter output."""
    result = _ranking_result()
    report = validate_result(
        _ranking_plan(), result, rendered_body=_markdown_body()
    )

    codes = {f.code for f in report.findings}
    assert "markdown_cell_drift" not in codes


def _profile_sort_ranking_plan() -> QueryPlan:
    """Rank BY a profile field (FRPL %), DISPLAY a policy metric (salary)."""
    return QueryPlan(
        operation="rank",
        question="starting salaries for districts with the highest FRPL share",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Starting salary")],
        output=OutputSpec(format="table"),
        temporal=TemporalSpec(intent="current", academic_year="2024 - 2025"),
        sort=SortSpec(field="FRPL %", direction="desc"),
    )


def _profile_sort_ranking_result() -> MetricRankingResult:
    """A profile-ordered-metric-display result: the sort column is headed by the
    human label ("FRPL %") while sort_metric_name carries the machine field_key
    ("frpl_pct"). Mirrors case 13 / SORT-MIGRATED-284 (#1721)."""
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha ISD",
                state="CA",
                metric_id=89,
                metric_name="Starting salary",
                value=55309,
                display_value="55,309",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="55,309",
                coverage_reason="answer_value",
                sort_metric_id=-1002,
                sort_metric_name="frpl_pct",
                sort_metric_label="FRPL %",
                sort_value=74.0,
                sort_display_value="74%",
                sort_academic_year="2024 - 2025",
            ),
        ],
        citations=[CitationRef(marker=1, title="Alpha ISD salary schedule")],
        total_considered=1,
        excluded_count=0,
        order_statement="Sorted by FRPL % descending.",
    )


def test_profile_field_sort_column_no_cell_drift() -> None:
    """#1721: rank-by-profile-field, show-the-metric. The renderer headers the
    sort column with the label ("FRPL %") and shows the FRPL value (74%) there
    and the salary (55,309) in the metric column. The validator must map the
    "FRPL %" header to sort_display_value (not fall through to the salary
    display_value) — else every such answer fails markdown_cell_drift and
    rescues before rendering."""
    plan = _profile_sort_ranking_plan()
    result = _profile_sort_ranking_result()
    body = render_response(
        plan,
        result,
        ValidationReport(valid=True, dimensions_checked=[], findings=[]),
    ).body
    report = validate_result(plan, result, rendered_body=body)

    drift = [f for f in report.findings if f.code == "markdown_cell_drift"]
    assert not drift, f"unexpected cell drift: {[f.metadata for f in drift]}"


def test_markdown_table_parity_catches_district_name_drift() -> None:
    """Rendering a different district name than the row's district_name fails."""
    result = _ranking_result()
    body = _markdown_body(alpha_name="WRONG DISTRICT NAME")
    report = validate_result(_ranking_plan(), result, rendered_body=body)

    findings_by_code = {f.code: f for f in report.findings}
    assert "markdown_cell_drift" in findings_by_code
    finding = findings_by_code["markdown_cell_drift"]
    assert finding.metadata["column"] == "District"
    assert finding.metadata["rendered_cell"] == "WRONG DISTRICT NAME"
    assert finding.metadata["expected_cell"] == "Alpha ISD"


def test_markdown_table_parity_fails_loudly_when_table_absent() -> None:
    """#1514 Fix B — answer rows expected but the body holds NO markdown
    table: that is a dropped table, not a legitimate skip. (Re-specs the
    pre-Fix-B ``test_markdown_table_parity_skips_when_no_table_present``,
    which pinned the silent-pass hole.)"""
    result = _ranking_result()
    report = validate_result(
        _ranking_plan(),
        result,
        rendered_body="Just a prose response with no table.",
    )

    findings_by_code = {f.code: f for f in report.findings}
    assert "markdown_row_count_mismatch" in findings_by_code
    assert findings_by_code["markdown_row_count_mismatch"].metadata == {
        "rendered_row_count": 0,
        "expected_row_count": 2,
        "table_absent": True,
    }


def test_markdown_table_parity_allows_narrative_only_body_for_non_answer_rows() -> None:
    """When every row is a non-answer, the renderer's #1514 empty-table guard
    legitimately emits a narrative-only body — the expected answers-only row
    set is empty, so table absence produces no finding."""
    result = _ranking_result(rows=[_not_reviewed_placeholder_row()])
    report = validate_result(
        _ranking_plan(),
        result,
        rendered_body=(
            "NCTQ hasn't reviewed Gamma USD for Starting salary in "
            "2024 - 2025 yet."
        ),
    )

    codes = {f.code for f in report.findings}
    assert "markdown_row_count_mismatch" not in codes
    assert "markdown_cell_drift" not in codes


def test_lookup_pivot_parity_fails_loudly_when_table_absent() -> None:
    """Pivot path of the same #1514 Fix B hole: Alpha ISD has answer cells,
    so a table-less body must fail with the table-absent row-count finding
    (one expected pivot row — answerless Unknown District drops, D3)."""
    report = validate_result(
        _lookup_plan(),
        _lookup_result(),
        rendered_body="Prose only, no table.",
    )

    findings_by_code = {f.code: f for f in report.findings}
    assert "markdown_row_count_mismatch" in findings_by_code
    metadata = findings_by_code["markdown_row_count_mismatch"].metadata
    assert metadata["expected_row_count"] == 1
    assert metadata["table_absent"] is True


def test_markdown_table_parity_skips_pre_render() -> None:
    """When `rendered_body` is None (pre-render path), the validator skips."""
    result = _ranking_result()
    report = validate_result(_ranking_plan(), result, rendered_body=None)

    codes = {f.code for f in report.findings}
    assert "markdown_cell_drift" not in codes


def _not_reviewed_placeholder_row() -> RankingRow:
    """A promoted not_reviewed placeholder (trails the ranked block, D4)."""
    return RankingRow(
        district_id=3,
        district_name="Gamma USD",
        state="CA",
        metric_id=100,
        metric_name="Starting salary",
        value=None,
        display_value=(
            "NCTQ hasn't reviewed Gamma USD for Starting salary in "
            "2024 - 2025 yet."
        ),
        academic_year="2024 - 2025",
        rank=3,
        source="coverage_state",
        citation_markers=[],
        coverage_state="not_reviewed",
        coverage_display=(
            "NCTQ hasn't reviewed Gamma USD for Starting salary in "
            "2024 - 2025 yet."
        ),
        coverage_reason="metric_not_reviewed",
    )


def test_ranking_markdown_parity_aligns_to_answer_rows_only() -> None:
    """#1514: a promoted not_reviewed placeholder stays in ``result.rows`` (the
    full record) but never renders as a table row — the expected list anchors
    to the answers-only subset, so the 2-row table matches a 3-row artifact."""
    base = _ranking_result()
    result = _ranking_result(
        rows=[*base.rows, _not_reviewed_placeholder_row()],
        citations=base.citations,
    )

    report = validate_result(
        _ranking_plan(), result, rendered_body=_markdown_body()
    )

    codes = {f.code for f in report.findings}
    assert "markdown_row_count_mismatch" not in codes
    assert "markdown_cell_drift" not in codes
    assert "csv_cell_drift" not in codes


def test_ranking_markdown_parity_fails_loudly_when_table_drops_a_row() -> None:
    """A table missing an answer row must fail with a row-count finding.

    The pre-#1514 guard (`len(table.rows) < len(result.rows)`) returned []
    silently here — and once non-answer rows stopped rendering, it would have
    disabled the whole check for any answer carrying a placeholder row.
    Parity now compares against the answers-only count and fails loudly."""
    result = _ranking_result()
    body = (
        "Top California districts by Starting salary.\n"
        "\n"
        "| Rank | District | State | Starting salary | Sources |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 1 | Alpha ISD | CA | $80,000 | [1] |\n"
    )

    report = validate_result(_ranking_plan(), result, rendered_body=body)

    findings_by_code = {f.code: f for f in report.findings}
    assert "markdown_row_count_mismatch" in findings_by_code
    assert findings_by_code["markdown_row_count_mismatch"].metadata == {
        "rendered_row_count": 1,
        "expected_row_count": 2,
    }


def test_lookup_pivot_markdown_parity_checks_metric_cells() -> None:
    """Multi-metric lookup pivot cells must be proven against ResultSet rows."""
    result = _lookup_result()

    report = validate_result(
        _lookup_plan(),
        result,
        rendered_body=_render_lookup_body(result),
    )

    codes = {f.code for f in report.findings}
    assert "markdown_cell_drift" not in codes
    assert "markdown_row_count_mismatch" not in codes
    assert "markdown_column_missing" not in codes
    assert "surface_consistency" in report.dimensions_checked


def test_lookup_pivot_markdown_parity_catches_metric_cell_drift() -> None:
    """A changed pivot metric cell fails instead of being skipped."""
    result = _lookup_result()
    body = _render_lookup_body(result).replace("10 days", "99 days", 1)

    report = validate_result(_lookup_plan(), result, rendered_body=body)

    findings_by_code = {f.code: f for f in report.findings}
    assert "markdown_cell_drift" in findings_by_code
    finding = findings_by_code["markdown_cell_drift"]
    assert finding.metadata["column"] == "Metric A"
    assert finding.metadata["rendered_cell"] == "99 days"
    assert finding.metadata["expected_cell"] == "10 days"


def test_lookup_pivot_markdown_parity_catches_missing_metric_column() -> None:
    """A pivot table that drops one metric column is not surface-consistent."""
    body = (
        "Compared selected districts.\n\n"
        "Unknown District is not in the District Policy Pathfinder.\n\n"
        "| District | State | Metric A | Sources |\n"
        "| --- | --- | --- | --- |\n"
        "| Alpha ISD | CA | 10 days | [1] [2] |\n"
    )

    report = validate_result(_lookup_plan(), _lookup_result(), rendered_body=body)

    findings_by_code = {f.code: f for f in report.findings}
    assert "markdown_column_missing" in findings_by_code
    assert findings_by_code["markdown_column_missing"].metadata["column"] == "Metric B"


def test_lookup_pivot_markdown_parity_drops_out_of_universe_rows() -> None:
    """#1514: an out-of-universe district holds zero answer cells, so it drops
    from the pivot table (one canonical narrative sentence instead) and is no
    longer an expected pivot row — the 1-row table matches a 4-row artifact."""
    result = _lookup_result()
    body = _render_lookup_body(result)
    assert body.count("| Unknown District |") == 0
    assert "Unknown District is not in the District Policy Pathfinder." in body

    report = validate_result(_lookup_plan(), result, rendered_body=body)

    codes = {finding.code for finding in report.findings}
    assert "markdown_row_count_mismatch" not in codes
    assert "markdown_cell_drift" not in codes


def _mixed_coverage_lookup_result() -> MetricLookupResult:
    """Alpha ISD holds an answer for Metric A but is not reviewed for Metric B
    — the #1514 D3 mixed-coverage shape: the district keeps its pivot row and
    the answerless Metric B cell renders (and is expected as) ``""``."""
    base = _lookup_result()
    not_reviewed_b = MetricValueRow(
        district_id=1,
        district_name="Alpha ISD",
        state="CA",
        metric_id=11,
        metric_name="Metric B",
        value=None,
        display_value=(
            "NCTQ hasn't reviewed Alpha ISD for Metric B in 2024 - 2025 yet."
        ),
        academic_year="2024 - 2025",
        source="coverage_state",
        coverage_state="not_reviewed",
        coverage_display=(
            "NCTQ hasn't reviewed Alpha ISD for Metric B in 2024 - 2025 yet."
        ),
        coverage_reason="metric_not_reviewed",
    )
    return base.model_copy(
        update={
            "rows": [base.rows[0], not_reviewed_b],
            "citations": [base.citations[0]],
            "total_considered": 2,
        }
    )


def test_lookup_pivot_markdown_parity_mixed_coverage_expects_empty_cell() -> None:
    """#1514 D3: a district with at least one answer cell keeps its row; the
    parity validator expects ``""`` in its answerless metric cells."""
    result = _mixed_coverage_lookup_result()
    body = _render_lookup_body(result)

    report = validate_result(_lookup_plan(), result, rendered_body=body)

    codes = {finding.code for finding in report.findings}
    assert "markdown_cell_drift" not in codes
    assert "markdown_row_count_mismatch" not in codes


def test_lookup_pivot_markdown_parity_catches_value_in_answerless_cell() -> None:
    """A value smuggled into a cell the artifact holds no answer for must
    fail loudly against the expected ``""`` (D3)."""
    result = _mixed_coverage_lookup_result()
    body = _render_lookup_body(result).replace(
        "| 10 days |  |", "| 10 days | SMUGGLED |", 1
    )

    report = validate_result(_lookup_plan(), result, rendered_body=body)

    findings_by_code = {f.code: f for f in report.findings}
    assert "markdown_cell_drift" in findings_by_code
    finding = findings_by_code["markdown_cell_drift"]
    assert finding.metadata["column"] == "Metric B"
    assert finding.metadata["rendered_cell"] == "SMUGGLED"
    assert finding.metadata["expected_cell"] == ""


def test_lookup_pivot_ranked_column_header_passes_parity() -> None:
    """#1220 adds '(ranked)' to the sorted metric column header.

    The validator must accept that suffix — a plain metric name in
    required_columns must match its '(ranked)' rendered header, AND the
    per-cell value lookup must find the ranked column's cells.  Regression for
    case 1006 (REGR-M1-GOLDEN-24): the ranked-column suffix caused first a
    false-positive markdown_column_missing and then a markdown_cell_drift
    (rendered_cell=None against the plain-name lookup), each of which swallowed
    a valid two-metric answer (salary + workdays).
    """
    result = _lookup_result().model_copy(
        update={"ranked_by_metric_name": "Metric A"}
    )
    body = _render_lookup_body(result)

    assert "Metric A (ranked)" in body, "renderer must mark the sorted column"

    report = validate_result(_lookup_plan(), result, rendered_body=body)

    codes = {f.code for f in report.findings}
    assert "markdown_column_missing" not in codes
    assert "markdown_row_count_mismatch" not in codes
    # The ranked column's cells must resolve against the plain metric name —
    # otherwise the value reads as drifted (None != "10 days") and the answer
    # is swallowed.
    assert "markdown_cell_drift" not in codes


# ── peer_comparison wide-pivot parity (#1645) ────────────────────────────────


def _peer_plan() -> QueryPlan:
    return QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's peers and how do sick-leave policies compare?",
        selection=SelectionSpec(
            scope="named_districts", districts=["Denver Public Schools"]
        ),
        metrics=[MetricSpec(name="Sick leave days")],
    )


def _peer_bundle_result() -> PeerComparisonResult:
    """A 2-district, 2-metric peer comparison — the wide-pivot shape (#1645)."""

    def _row(
        district_id, name, state, metric_id, metric_name, value, role, rank, enr, marker
    ) -> PeerComparisonRow:
        return PeerComparisonRow(
            district_id=district_id,
            district_name=name,
            state=state,
            metric_id=metric_id,
            metric_name=metric_name,
            value=value,
            display_value=value,
            academic_year="2024 - 2025",
            citation_markers=[marker],
            coverage_state="covered",
            coverage_display=value,
            coverage_reason="answer_value",
            peer_role=role,
            peer_rank=rank,
            peer_score=None,
            peer_reason=(
                "Anchor district selected by the user."
                if role == "anchor"
                else "Similar enrollment and urbanicity."
            ),
            peer_enrollment=enr,
            peer_urbanicity="City: Large",
        )

    rows = [
        _row(26, "Denver Public Schools", "CO", 198, "Sick leave days", "10", "anchor", None, 87883, 1),
        _row(26, "Denver Public Schools", "CO", 201, "Carries over", "Yes", "anchor", None, 87883, 1),
        _row(24, "Aurora Public Schools", "CO", 198, "Sick leave days", "12", "peer", 1, 38135, 2),
        _row(24, "Aurora Public Schools", "CO", 201, "Carries over", "No", "peer", 1, 38135, 2),
    ]
    return PeerComparisonResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=26, district_name="Denver Public Schools", state="CO"),
                SelectedDistrict(district_id=24, district_name="Aurora Public Schools", state="CO"),
            ],
        ),
        rows=rows,
        citations=[
            CitationRef(marker=1, title="Denver Agreement", district_id=26),
            CitationRef(marker=2, title="Aurora Agreement", district_id=24),
        ],
        coverage_frame=CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=2,
            real_data_count=2,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        total_considered=2,
        excluded_count=0,
        order_statement="Compared selected policy metrics for the anchor and 1 peer district.",
        methodology_codes=[MethodologyRef(code="peer_selection_nces_profiles")],
    )


def _render_peer_body(result: PeerComparisonResult) -> str:
    manifest = render_response(
        _peer_plan(),
        result,
        ValidationReport(valid=True, dimensions_checked=[], findings=[]),
    )
    return manifest.body


def test_peer_pivot_markdown_parity_passes_on_wide_table() -> None:
    """The wide peer table (one row per district) is surface-consistent."""
    result = _peer_bundle_result()

    report = validate_result(
        _peer_plan(), result, rendered_body=_render_peer_body(result)
    )

    codes = {f.code for f in report.findings}
    assert "markdown_cell_drift" not in codes
    assert "markdown_row_count_mismatch" not in codes
    assert "markdown_column_missing" not in codes
    assert "surface_consistency" in report.dimensions_checked


def test_peer_pivot_markdown_parity_fails_loudly_on_long_table() -> None:
    """A regression to the long layout (one row per district-metric) renders
    more table rows than there are districts — the shape guard must catch it."""
    result = _peer_bundle_result()
    long_body = (
        "Compared selected peer districts.\n\n"
        "| Role | Peer Rank | District | State | Enrollment | Urbanicity | "
        "Sick leave days | Carries over | Peer Rationale | Sources |\n"
        "| --- | ---: | --- | --- | ---: | --- | --- | --- | --- | --- |\n"
        "| anchor |  | Denver Public Schools | CO | 87,883 | City: Large | 10 |  | Anchor district selected by the user. | [1] |\n"
        "| anchor |  | Denver Public Schools | CO | 87,883 | City: Large |  | Yes | Anchor district selected by the user. | [1] |\n"
        "| peer | 1 | Aurora Public Schools | CO | 38,135 | City: Large | 12 |  | Similar enrollment and urbanicity. | [2] |\n"
        "| peer | 1 | Aurora Public Schools | CO | 38,135 | City: Large |  | No | Similar enrollment and urbanicity. | [2] |\n"
    )

    report = validate_result(_peer_plan(), result, rendered_body=long_body)

    findings_by_code = {f.code: f for f in report.findings}
    assert "markdown_row_count_mismatch" in findings_by_code
    finding = findings_by_code["markdown_row_count_mismatch"]
    assert finding.metadata["rendered_row_count"] == 4
    assert finding.metadata["expected_row_count"] == 2


def test_peer_pivot_markdown_parity_catches_metric_cell_drift() -> None:
    """A changed metric cell in the wide peer table fails instead of skipping."""
    result = _peer_bundle_result()
    body = _render_peer_body(result).replace("| 10 | Yes |", "| 99 | Yes |", 1)

    report = validate_result(_peer_plan(), result, rendered_body=body)

    findings_by_code = {f.code: f for f in report.findings}
    assert "markdown_cell_drift" in findings_by_code
    finding = findings_by_code["markdown_cell_drift"]
    assert finding.metadata["column"] == "Sick leave days"
    assert finding.metadata["rendered_cell"] == "99"
    assert finding.metadata["expected_cell"] == "10"


# ── csv_cell_drift ───────────────────────────────────────────────────────────


def test_csv_row_parity_passes_on_aligned_csv() -> None:
    """The auto-derived csv_export must match its source rows by construction."""
    result = _ranking_result()
    report = validate_result(_ranking_plan(), result)

    codes = {f.code for f in report.findings}
    assert "csv_cell_drift" not in codes


def test_csv_row_parity_catches_tampered_csv() -> None:
    """A manually-constructed csv with a wrong district_name triggers the check.

    Mimics the bug class where someone passes a pre-built csv_export to
    `MetricRankingResult(...)` that doesn't match the rows.
    """
    base = _ranking_result()
    # Build a csv that says District A but the source rows say Alpha ISD.
    from compass_backend.artifacts.results import CsvExportArtifact

    tampered_csv = CsvExportArtifact(
        columns=list(base.csv_export.columns),
        rows=[
            {**base.csv_export.rows[0], "district_name": "TAMPERED ISD"},
            base.csv_export.rows[1],
        ],
    )
    result = MetricRankingResult(
        rows=base.rows,
        citations=base.citations,
        csv_export=tampered_csv,
        total_considered=base.total_considered,
        excluded_count=base.excluded_count,
        order_statement=base.order_statement,
    )

    report = validate_result(_ranking_plan(), result)

    findings_by_code = {f.code: f for f in report.findings}
    assert "csv_cell_drift" in findings_by_code
    finding = findings_by_code["csv_cell_drift"]
    assert finding.metadata["column"] == "district_name"
    assert finding.metadata["csv_value"] == "TAMPERED ISD"
    assert finding.metadata["source_value"] == "Alpha ISD"


def test_csv_row_parity_catches_source_metadata_drift() -> None:
    """CSV source-document metadata must stay derived from row citation markers."""
    base = _lookup_result()
    tampered_csv = CsvExportArtifact(
        columns=list(base.csv_export.columns),
        rows=[
            {**base.csv_export.rows[0], "source_document": "Wrong source"},
            *base.csv_export.rows[1:],
        ],
    )
    result = base.model_copy(update={"csv_export": tampered_csv})

    report = validate_result(_lookup_plan(), result)

    findings_by_code = {finding.code: finding for finding in report.findings}
    assert "csv_cell_drift" in findings_by_code
    finding = findings_by_code["csv_cell_drift"]
    assert finding.metadata["column"] == "source_document"
    assert finding.metadata["csv_value"] == "Wrong source"
    assert finding.metadata["source_value"] == "Alpha Metric A policy"


# ── count CSV parity (#1514 Fix B blocker B1) ────────────────────────────────


def _count_plan() -> QueryPlan:
    return QueryPlan(
        operation="count",
        question=(
            "What is the union status and board approval distribution for "
            "Alpha ISD, Beta USD, and Gamma USD?"
        ),
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Alpha ISD", "Beta USD", "Gamma USD"],
        ),
        metrics=[MetricSpec(name="Union status"), MetricSpec(name="Board approval")],
        output=OutputSpec(format="table"),
        temporal=TemporalSpec(intent="current", academic_year="2024 - 2025"),
    )


def _categorical_bucket(
    *,
    metric_id: int,
    metric_name: str,
    category: str,
    count: int,
    qualifying_district_ids: list[int],
    coverage_state: str = "covered",
    coverage_reason: str = "categorical_value_count",
) -> CategoricalCountRow:
    display_value = f"{count} of 3 covered districts"
    return CategoricalCountRow(
        metric_id=metric_id,
        metric_name=metric_name,
        value=category,
        category=category,
        display_value=display_value,
        academic_year="2024 - 2025",
        count=count,
        denominator=3,
        percent=round(count / 3 * 100, 1),
        filter_statement="grouped current categorical values",
        qualifying_district_ids=qualifying_district_ids,
        source="policy_answer",
        citation_markers=[],
        coverage_state=coverage_state,
        coverage_display=display_value,
        coverage_reason=coverage_reason,
    )


def _count_result_with_not_reviewed_bucket() -> MetricCountResult:
    """The B1 reviewer repro: multi-metric categorical count whose MIDDLE row
    is metric 1's not_reviewed bucket. Pre-fix, the count CSV builder emitted
    all three rows while ``_validate_csv_row_parity`` zipped against the
    answers-only subset — the zip shifted at row 1 and ``csv_cell_drift``
    (error severity) replaced the healthy answer with the apology body."""
    return MetricCountResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha ISD", state="CA"),
                SelectedDistrict(district_id=2, district_name="Beta USD", state="CA"),
                SelectedDistrict(district_id=3, district_name="Gamma USD", state="CA"),
            ],
        ),
        rows=[
            _categorical_bucket(
                metric_id=100,
                metric_name="Union status",
                category="Yes",
                count=2,
                qualifying_district_ids=[1, 2],
            ),
            _categorical_bucket(
                metric_id=100,
                metric_name="Union status",
                category="Not reviewed",
                count=1,
                qualifying_district_ids=[3],
                coverage_state="not_reviewed",
                coverage_reason="metric_not_reviewed",
            ),
            _categorical_bucket(
                metric_id=200,
                metric_name="Board approval",
                category="No",
                count=3,
                qualifying_district_ids=[1, 2, 3],
            ),
        ],
        total_considered=6,
        excluded_count=0,
        order_statement="Counted categorical value distribution.",
    )


def test_count_csv_parity_end_to_end_with_categorical_buckets() -> None:
    """#1514 Fix B blocker B1, end to end on the live-path shape: render the
    count result through the real writer, then re-validate with the rendered
    body. The count CSV builder now filters to answer buckets with the SAME
    predicate the parity validator zips against, so the multi-metric
    categorical count with an interior not_reviewed bucket produces zero
    surface_consistency findings."""
    plan = _count_plan()
    result = _count_result_with_not_reviewed_bucket()
    # The builder mirrors table + chart: answer buckets only.
    assert [row["metric_id"] for row in result.csv_export.rows] == [100, 200]
    manifest = render_response(
        plan,
        result,
        ValidationReport(valid=True, dimensions_checked=[], findings=[]),
    )

    report = validate_result(
        plan,
        result,
        rendered_body=manifest.body,
        manifest_metadata=manifest.metadata,
    )

    surface_findings = [
        finding
        for finding in report.findings
        if finding.dimension == "surface_consistency"
    ]
    assert surface_findings == []


def test_markdown_table_parity_exempts_unmapped_result_types() -> None:
    """Count results have no per-cell mapper — a table-less body stays exempt
    (the #1514 Fix B loud table-absent check applies to mapped types only)."""
    report = validate_result(
        _count_plan(),
        _count_result_with_not_reviewed_bucket(),
        rendered_body="Narrative count answer with no table.",
    )

    codes = {f.code for f in report.findings}
    assert "markdown_row_count_mismatch" not in codes


# ── artifact_id_drift ────────────────────────────────────────────────────────


def test_artifact_id_parity_passes_when_stamp_matches_result() -> None:
    """The stamp computed by `compute_artifact_id` matches itself."""
    result = _ranking_result()
    manifest_metadata = {"artifact_id": compute_artifact_id(result)}
    report = validate_result(
        _ranking_plan(),
        result,
        rendered_body=_markdown_body(),
        manifest_metadata=manifest_metadata,
    )

    codes = {f.code for f in report.findings}
    assert "artifact_id_drift" not in codes


def test_artifact_id_parity_catches_stale_stamp() -> None:
    """If the manifest stamp came from a different ResultSet, drift fires."""
    other_result = _ranking_result(
        rows=[
            RankingRow(
                district_id=99,
                district_name="Different District",
                state="TX",
                metric_id=100,
                metric_name="Starting salary",
                value=99999,
                display_value="$99,999",
                academic_year="2024 - 2025",
                rank=1,
            )
        ]
    )
    fresh_result = _ranking_result()
    manifest_metadata = {"artifact_id": compute_artifact_id(other_result)}

    report = validate_result(
        _ranking_plan(),
        fresh_result,
        rendered_body=_markdown_body(),
        manifest_metadata=manifest_metadata,
    )

    findings_by_code = {f.code: f for f in report.findings}
    assert "artifact_id_drift" in findings_by_code
    finding = findings_by_code["artifact_id_drift"]
    assert (
        finding.metadata["stamped_artifact_id"]
        != finding.metadata["fresh_artifact_id"]
    )


def test_artifact_id_parity_skips_when_no_stamp_present() -> None:
    """Missing or non-string `artifact_id` key is a no-op (not an error)."""
    result = _ranking_result()
    # No artifact_id key at all.
    report = validate_result(
        _ranking_plan(),
        result,
        rendered_body=_markdown_body(),
        manifest_metadata={"question": "x"},
    )

    codes = {f.code for f in report.findings}
    assert "artifact_id_drift" not in codes


def test_artifact_id_parity_skips_when_manifest_metadata_none() -> None:
    """Pre-render (no manifest yet) skips silently."""
    result = _ranking_result()
    report = validate_result(_ranking_plan(), result, manifest_metadata=None)

    codes = {f.code for f in report.findings}
    assert "artifact_id_drift" not in codes


# ── unbounded_ranking_truncated: kind="all" keeps validator active ───────────


def _plan_with_limit(limit: LimitSpec | None) -> QueryPlan:
    """Build a state-scope ranking plan with an optional limit spec."""
    return QueryPlan(
        operation="rank",
        question="rank California districts by starting salary",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="Starting salary")],
        output=OutputSpec(format="table"),
        temporal=TemporalSpec(intent="current", academic_year="2024 - 2025"),
        sort=SortSpec(field="Starting salary", direction="desc"),
        limit=limit,
    )


def _truncated_ranking_result() -> MetricRankingResult:
    """A MetricRankingResult whose row count is below total_considered - excluded_count.

    total_considered=10, excluded_count=0 → 10 eligible; only 2 rows returned.
    This is the silent-truncation pattern the validator should catch.
    """
    return MetricRankingResult(
        rows=_ranking_result().rows,  # 2 rows
        citations=_ranking_result().citations,
        total_considered=10,
        excluded_count=0,
        order_statement="Sorted by Starting salary descending.",
    )


def test_surface_consistency_validator_fires_for_kind_all_when_truncated() -> None:
    """kind='all' must NOT disable the unbounded_ranking_truncated validator.

    Regression for Fix 2: the guard `if plan.limit is not None: return []`
    previously skipped validation for ANY plan with a limit, including kind='all'.
    kind='all' is a no-cap sentinel — it should be treated as unbounded and the
    truncation check must remain active to catch downstream cap-point leaks.
    """
    plan = _plan_with_limit(LimitSpec(kind="all"))
    result = _truncated_ranking_result()

    report = validate_result(plan, result)

    codes = {f.code for f in report.findings}
    assert "unbounded_ranking_truncated" in codes, (
        "Validator must fire when kind='all' and row_count < eligible_row_count. "
        "Fix 2: guard must be `plan.limit is not None and plan.limit.kind != 'all'`."
    )


def test_surface_consistency_validator_silent_for_numeric_limit() -> None:
    """A numeric limit (kind='top', count=5) correctly disables the validator.

    When the user asked for a capped result, truncation is expected and must
    not raise unbounded_ranking_truncated.
    """
    plan = _plan_with_limit(LimitSpec(kind="top", count=5))
    result = _truncated_ranking_result()

    report = validate_result(plan, result)

    codes = {f.code for f in report.findings}
    assert "unbounded_ranking_truncated" not in codes, (
        "Validator must NOT fire when the user asked for a numeric cap (kind='top')."
    )


def test_surface_consistency_validator_silent_for_no_limit() -> None:
    """No limit (plan.limit is None) with exact row count does not fire.

    This is the happy-path: result.rows matches all eligible rows.
    """
    plan = _plan_with_limit(None)
    result = _ranking_result()  # total_considered=2, rows=2: no truncation

    report = validate_result(plan, result)

    codes = {f.code for f in report.findings}
    assert "unbounded_ranking_truncated" not in codes
