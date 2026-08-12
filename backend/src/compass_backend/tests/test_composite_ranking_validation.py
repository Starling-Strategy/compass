"""W2-M3-01 phase 4 (#861): ``validate_result`` accepts ``CompositeRankingResult``.

Before this PR, ``validate_result`` ran ``_validate_selection`` and
``_validate_metric`` over the composite envelope, which:
- ``_validate_selection`` (quality/validators/selection.py:48) emitted an
  ``unsupported_result_type`` error because ``"composite_ranking"`` was not
  in the allowed set.
- ``_validate_metric`` (quality/validators/metrics.py:62) fell through to
  the ranking branch, saw N metric specs on the plan, and emitted an
  ``unsupported_metric_plan`` error because the ranking validator
  requires exactly one primary metric.

Either error flipped ``ValidationReport.valid=False``, which made
``writer.render_response`` short-circuit to ``"validation_failed"`` and
never call the new ``_render_composite_ranking`` path. The renderer was
dead code in production.

These tests pin the new behavior: composite results validate by
delegating to per-child ``validate_result`` calls and aggregating, so
each child is validated as a standalone ``MetricRankingResult``.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    CitationRef,
    CompositeRankingResult,
    CoverageFrame,
    MethodologyRef,
    MetricRankingResult,
    RankingRow,
    ResultSelection,
)
from compass_backend.contracts import (
    MetricSpec,
    QueryPlan,
    ResolvedMetricAuthority,
    ResolvedSelectionAuthority,
    SelectionSpec,
    ValidationAuthority,
)
from compass_backend.quality.validation import validate_result


def _ranking_child(metric_id: int, metric_name: str) -> MetricRankingResult:
    return MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=metric_id,
                metric_name=metric_name,
                value=80000.0 + metric_id,
                display_value=f"${80_000 + metric_id:,}",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display=f"${80_000 + metric_id:,}",
                coverage_reason="answer_value",
            ),
            RankingRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=metric_id,
                metric_name=metric_name,
                value=70000.0 + metric_id,
                display_value=f"${70_000 + metric_id:,}",
                academic_year="2024 - 2025",
                rank=2,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display=f"${70_000 + metric_id:,}",
                coverage_reason="answer_value",
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title=f"{metric_name} contract",
                url=f"https://example.org/{metric_id}.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            ),
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
        order_statement=f"Ranked by {metric_name}, highest to lowest.",
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def _composite_plan() -> QueryPlan:
    return QueryPlan(
        operation="rank",
        question="Rank covered districts on three lanes, separately.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="BA starting salary"),
            MetricSpec(name="MA starting salary"),
            MetricSpec(name="PhD starting salary"),
        ],
        requires_composite_ranking=True,
    )


def _composite_authority() -> ValidationAuthority:
    return ValidationAuthority(
        metrics=[
            ResolvedMetricAuthority(
                metric_id=101, metric_name="BA starting salary", role="primary"
            ),
            ResolvedMetricAuthority(
                metric_id=102, metric_name="MA starting salary", role="comparison"
            ),
            ResolvedMetricAuthority(
                metric_id=103, metric_name="PhD starting salary", role="comparison"
            ),
        ],
        selection=ResolvedSelectionAuthority(scope="all_covered_districts"),
    )


def _composite_result() -> CompositeRankingResult:
    return CompositeRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        children=[
            _ranking_child(101, "BA starting salary"),
            _ranking_child(102, "MA starting salary"),
            _ranking_child(103, "PhD starting salary"),
        ],
        total_considered=0,
        excluded_count=0,
        order_statement="3 metrics, ranked highest to lowest by each.",
    )


def test_validate_result_accepts_composite_ranking() -> None:
    """The composite path must NOT emit ``unsupported_result_type`` or
    ``unsupported_metric_plan`` — both would gate the renderer."""

    plan = _composite_plan()
    result = _composite_result()
    authority = _composite_authority()

    report = validate_result(plan, result, authority=authority)

    finding_codes = {finding.code for finding in report.findings}
    assert "unsupported_result_type" not in finding_codes, (
        "selection validator must not reject composite_ranking"
    )
    assert "unsupported_metric_plan" not in finding_codes, (
        "metric validator must not see N>1 metric specs when delegating "
        "to per-child validation (each child is validated against a "
        "single-metric child_plan)"
    )
    assert report.valid is True, (
        "composite validation must pass when every child is a well-formed "
        f"MetricRankingResult; got findings: {finding_codes}"
    )


def test_validate_result_prunes_shared_child_citations_for_composite_ranking() -> None:
    """Composite children may carry the shared envelope citation list.

    Each child table only references the markers used by that child. Validation
    should not reject child A because the shared citation list also contains a
    marker that only child B uses.
    """

    plan = QueryPlan(
        operation="rank",
        question="Rank covered districts on two lanes, separately.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="BA starting salary"),
            MetricSpec(name="MA starting salary"),
        ],
        requires_composite_ranking=True,
    )
    ba_child = _ranking_child(101, "BA starting salary")
    ma_child = _ranking_child(102, "MA starting salary")
    shared_citations = [
        ba_child.citations[0],
        ma_child.citations[0].model_copy(update={"marker": 2}),
    ]
    ba_rows = [
        row.model_copy(update={"citation_markers": [1]})
        for row in ba_child.rows
    ]
    ma_rows = [
        row.model_copy(update={"citation_markers": [2]})
        for row in ma_child.rows
    ]
    result = CompositeRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        children=[
            ba_child.model_copy(update={"rows": ba_rows, "citations": shared_citations}),
            ma_child.model_copy(update={"rows": ma_rows, "citations": shared_citations}),
        ],
        citations=shared_citations,
        total_considered=0,
        excluded_count=0,
        order_statement="2 metrics, ranked highest to lowest by each.",
    )

    report = validate_result(plan, result)

    finding_codes = {finding.code for finding in report.findings}
    assert "citation_not_referenced" not in finding_codes
    assert report.valid is True


def test_validate_result_aggregates_dimensions_across_composite_children() -> None:
    """The aggregated ``dimensions_checked`` should reflect that every
    canonical dimension was checked at least once via a child."""

    plan = _composite_plan()
    result = _composite_result()

    report = validate_result(plan, result)

    # Every dimension a single MetricRankingResult would have checked
    # must appear in the composite's aggregated dimension list.
    for dim in ("selection", "metric", "coverage_state", "citation_coverage"):
        assert dim in report.dimensions_checked, (
            f"composite dimensions_checked must include {dim!r} from at least "
            f"one child; got {report.dimensions_checked}"
        )


def test_validate_result_composite_propagates_child_failure() -> None:
    """If a single child fails validation, the composite report must be
    invalid — phase 1's contract that every child is fully validated."""

    plan = _composite_plan()
    # Build a result where one child has mismatched metric_id rows
    # (well-formed envelope, broken child).
    bad_child = _ranking_child(101, "BA starting salary")
    # Mutate via model_copy to swap one row's metric_id (the validator's
    # metric_row_mismatch finding will fire).
    bad_rows = list(bad_child.rows)
    bad_rows[1] = bad_rows[1].model_copy(update={"metric_id": 999})
    broken_child = bad_child.model_copy(update={"rows": bad_rows})
    result = CompositeRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        children=[
            broken_child,
            _ranking_child(102, "MA starting salary"),
        ],
        total_considered=0,
        excluded_count=0,
        order_statement="2 metrics, ranked highest to lowest by each.",
    )

    report = validate_result(plan, result)

    assert report.valid is False
    finding_codes = {finding.code for finding in report.findings}
    assert "metric_row_mismatch" in finding_codes, (
        "composite must propagate per-child validation failures upward"
    )
