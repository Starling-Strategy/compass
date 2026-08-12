"""Tests for MVP 7 artifact rendering."""

import pytest
from pydantic import ValidationError

from compass_backend.artifacts import (
    CategoricalCountRow,
    CitationRef,
    CoverageBreakdown,
    CoverageDisclosure,
    CoverageFrame,
    MethodologyRef,
    MetricCountResult,
    FilterPrevalenceSummary,
    MetricLookupResult,
    MetricRankingResult,
    MetricValueRow,
    PeerComparisonResult,
    PeerComparisonRow,
    RankingRow,
    ResultCriterion,
    ResultSelection,
    ResultSet,
    SelectedDistrict,
    ThresholdCountRow,
    district_coverage_summary_for_rows,
)
from compass_backend.contracts import (
    FilterSpec,
    LimitSpec,
    MetricSpec,
    OutputSpec,
    QueryPlan,
    ResolvedMetricAuthority,
    ResolvedSelectionAuthority,
    SelectionSpec,
    SortStepSpec,
    ValidationAuthority,
    ValidationFinding,
    ValidationReport,
)
from compass_backend.quality.validation import validate_result
from compass_backend.rendering import render_response


def _plan() -> QueryPlan:
    return QueryPlan(
        question="Rank covered districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
    )


def _state_plan() -> QueryPlan:
    return QueryPlan(
        question="Rank California districts by starting salary.",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="starting salary")],
    )


def _named_plan() -> QueryPlan:
    return QueryPlan(
        question="Rank Bravo and Charlie by starting salary.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Bravo", "Charlie"],
        ),
        metrics=[MetricSpec(name="starting salary")],
    )


def _lookup_named_plan() -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="Compare starting salary for Alpha and Bravo.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Alpha", "Bravo"],
        ),
        metrics=[MetricSpec(name="starting salary")],
    )


def _lookup_comparison_plan() -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="Compare collective bargaining and starting salary for Alpha and Bravo.",
        selection=SelectionSpec(scope="named_districts", districts=["Bravo", "Alpha"]),
        metrics=[
            MetricSpec(name="collective bargaining"),
            MetricSpec(name="starting salary", role="comparison"),
        ],
    )


def _result() -> ResultSet:
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=70000.0,
                display_value="$70,000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
            ),
            RankingRow(
                district_id=3,
                district_name="Charlie",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=60000.0,
                display_value="$60,000",
                academic_year="2024 - 2025",
                rank=2,
                citation_markers=[2],
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Bravo District Contract, 2024-2025",
                url="https://example.org/bravo.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=2,
            ),
            CitationRef(
                marker=2,
                title="Charlie District Contract, 2024-2025",
                url="https://example.org/charlie.pdf",
                page_number=3,
                page_ref="p. 3",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=3,
            ),
        ],
        total_considered=2,
        excluded_count=0,
        order_statement="Ranked by starting salary, highest to lowest.",
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def _many_row_ranking_result(row_count: int = 12) -> ResultSet:
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=index,
                district_name=f"District {index:02d}",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=100_000 - index,
                display_value=f"${100_000 - index:,}",
                academic_year="2024 - 2025",
                rank=index,
                citation_markers=[],
                coverage_state="covered",
                coverage_display=f"${100_000 - index:,}",
                coverage_reason="answer_value",
            )
            for index in range(1, row_count + 1)
        ],
        total_considered=row_count,
        excluded_count=0,
        order_statement="Ranked by starting salary, highest to lowest.",
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def _mixed_ranking_result() -> ResultSet:
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value="$50,000",
                display_value="$50,000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
                sort_metric_id=-1002,
                sort_metric_name="FRPL %",
                sort_value=90.0,
                sort_display_value="90%",
                sort_academic_year="2024 - 2025",
            ),
            RankingRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value="$70,000",
                display_value="$70,000",
                academic_year="2024 - 2025",
                rank=2,
                citation_markers=[2],
                coverage_state="covered",
                coverage_display="$70,000",
                coverage_reason="answer_value",
                sort_metric_id=-1002,
                sort_metric_name="FRPL %",
                sort_value=80.0,
                sort_display_value="80%",
                sort_academic_year="2024 - 2025",
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha District Contract, 2024-2025",
                url="https://example.org/alpha.pdf",
                academic_year="2024 - 2025",
                district_id=1,
            ),
            CitationRef(
                marker=2,
                title="Bravo District Contract, 2024-2025",
                url="https://example.org/bravo.pdf",
                academic_year="2024 - 2025",
                district_id=2,
            ),
        ],
        total_considered=2,
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=2,
            real_data_count=2,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement=(
            "Ranked by FRPL %, highest to lowest; displayed "
            "Average teacher starting salary."
        ),
    )


def _profile_ordered_result_without_policy_citations() -> ResultSet:
    result = _mixed_ranking_result()
    return result.model_copy(
        update={
            "rows": [
                row.model_copy(update={"citation_markers": []})
                for row in result.rows
            ],
            "citations": [],
            "methodology_codes": [
                MethodologyRef(
                    code="profile_rank_uses_profile_field",
                    metadata={"profile_field": "FRPL % (NCES directory year: 2024)"},
                )
            ],
        }
    )


def _mixed_ranking_result_with_one_uncited_row() -> ResultSet:
    result = _mixed_ranking_result()
    return result.model_copy(
        update={
            "rows": [
                result.rows[0],
                result.rows[1].model_copy(update={"citation_markers": []}),
            ],
        }
    )


def _broad_partial_ranking_result() -> ResultSet:
    result = _partial_ranking_result()
    disclosures = [
        CoverageDisclosure(
            district_id=100 + index,
            district_name=f"District {index}",
            state="CA",
            metric_id=1234,
            metric_name="Average teacher starting salary",
            academic_year="2024 - 2025",
            coverage_state="not_reviewed",
            display=f"NCTQ hasn't reviewed District {index} for 2024 - 2025 yet.",
            reason="district_not_reviewed",
        )
        for index in range(1, 8)
    ]
    return result.model_copy(
        update={
            "selection": ResultSelection(scope="state", states=["CA"]),
            "coverage_disclosures": disclosures,
            "coverage_frame": result.coverage_frame.model_copy(
                update={
                    "universe_count": 8,
                    "in_scope_count": 8,
                    "addressed_count": 1,
                    "real_data_count": 1,
                    "not_reviewed_count": 7,
                    "coverage_ratio": 1 / 8,
                    "sparse_disclosure": (
                        "Sparse coverage: 1 of 8 in-scope cells have current reviewed data."
                    ),
                    # #1702: all 7 gaps are district_not_reviewed, so the counted
                    # clause's "7" must be backed by this single serialized field
                    # (model_copy does not re-run populate_legacy_breakdown).
                    "breakdown": CoverageBreakdown(
                        answer_value_count=1,
                        district_not_reviewed_count=7,
                    ),
                }
            ),
            "total_considered": 8,
            "excluded_count": 7,
        }
    )


def _texas_observation_preview_result() -> ResultSet:
    rows = [
        RankingRow(
            district_id=index,
            district_name=f"Texas District {index:02d}",
            state="TX",
            metric_id=39,
            metric_name=(
                "Minimum number of formal observations per evaluation cycle "
                "for non-tenured teachers"
            ),
            value=1,
            display_value="1",
            academic_year="2024 - 2025",
            rank=index,
            citation_markers=[],
            coverage_state="covered",
            coverage_display="1",
            coverage_reason="answer_value",
        )
        for index in range(1, 14)
    ]
    disclosures = [
        CoverageDisclosure(
            district_id=200 + index,
            district_name=f"Older Year District {index}",
            state="TX",
            metric_id=39,
            metric_name=rows[0].metric_name,
            academic_year="2024 - 2025",
            coverage_state="not_reviewed",
            display=(
                f"NCTQ last reviewed Older Year District {index} for "
                f"{rows[0].metric_name} in 2023 - 2024; the value then was 1."
            ),
            reason="stale_recent_answer",
            prior_academic_year="2023 - 2024",
            prior_display_value="1",
        )
        for index in range(1, 7)
    ]
    disclosures.extend(
        [
            CoverageDisclosure(
                district_id=300,
                district_name="Issue Not Addressed District",
                state="TX",
                metric_id=39,
                metric_name=rows[0].metric_name,
                academic_year="2024 - 2025",
                coverage_state="ina",
                display="Issue not addressed in the documents reviewed.",
                reason="issue_not_addressed",
            ),
            CoverageDisclosure(
                district_id=301,
                district_name="Narrative District",
                state="TX",
                metric_id=39,
                metric_name=rows[0].metric_name,
                academic_year="2024 - 2025",
                coverage_state="covered",
                display="Observation frequency varies by teacher status.",
                reason="non_numeric_rank_exclusion",
            ),
        ]
    )
    return MetricRankingResult(
        selection=ResultSelection(
            scope="state",
            states=["TX"],
            districts=[
                SelectedDistrict(
                    district_id=row.district_id,
                    district_name=row.district_name,
                    state=row.state,
                )
                for row in rows
            ],
        ),
        rows=rows,
        total_considered=21,
        excluded_count=8,
        coverage_frame=CoverageFrame(
            universe_count=21,
            in_scope_count=21,
            addressed_count=15,
            real_data_count=14,
            not_reviewed_count=6,
            out_of_universe_count=0,
            coverage_ratio=14 / 21,
            breakdown=CoverageBreakdown(
                answer_value_count=13,
                stale_recent_answer_count=6,
                issue_not_addressed_count=1,
                non_numeric_rank_exclusion_count=1,
            ),
        ),
        coverage_disclosures=disclosures,
        order_statement="Ranked by observation count, lowest to highest.",
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def _named_result() -> ResultSet:
    return _result().model_copy(
        update={
            "selection": ResultSelection(
                scope="named_districts",
                districts=[
                    SelectedDistrict(
                        district_id=2,
                        district_name="Bravo",
                        state="CA",
                    ),
                    SelectedDistrict(
                        district_id=3,
                        district_name="Charlie",
                        state="CA",
                    ),
                ],
            )
        }
    )


def _partial_ranking_result() -> ResultSet:
    return MetricRankingResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=2, district_name="Bravo", state="CA"),
                SelectedDistrict(district_id=3, district_name="Charlie", state="CA"),
                SelectedDistrict(district_id=4, district_name="Delta", state="CA"),
            ],
        ),
        rows=[
            RankingRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=70000.0,
                display_value="$70,000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$70,000",
                coverage_reason="answer_value",
            )
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Bravo District Contract, 2024-2025",
                url="https://example.org/bravo.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=2,
            )
        ],
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=1,
            real_data_count=1,
            not_reviewed_count=2,
            out_of_universe_count=0,
            coverage_ratio=1 / 3,
            sparse=True,
            sparse_disclosure=(
                "Sparse coverage: 1 of 3 in-scope cells have current reviewed data."
            ),
            # #1702: honest per-reason breakdown so the availability narrator's
            # counted-gap clause (Delta) sources its number from a serialized
            # field that equals the gap-disclosure subset (1 district_not_reviewed).
            breakdown=CoverageBreakdown(
                answer_value_count=1,
                stale_recent_answer_count=1,
                district_not_reviewed_count=1,
            ),
        ),
        coverage_disclosures=[
            CoverageDisclosure(
                district_id=3,
                district_name="Charlie",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                academic_year="2024 - 2025",
                coverage_state="not_reviewed",
                display=(
                    "NCTQ last reviewed Charlie for Average teacher starting "
                    "salary in 2023 - 2024; the value then was $65,000."
                ),
                reason="stale_recent_answer",
                prior_academic_year="2023 - 2024",
                prior_display_value="$65,000",
            ),
            CoverageDisclosure(
                district_id=4,
                district_name="Delta",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                academic_year="2024 - 2025",
                coverage_state="not_reviewed",
                display="NCTQ hasn't reviewed Delta for 2024 - 2025 yet.",
                reason="district_not_reviewed",
            ),
        ],
        total_considered=3,
        excluded_count=2,
        order_statement="Ranked selected districts by starting salary, highest to lowest.",
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
        ],
    )


def _lookup_comparison_result() -> ResultSet:
    return MetricLookupResult(        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                SelectedDistrict(district_id=2, district_name="Bravo", state="CA"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=9876,
                metric_name="Average teacher starting salary",
                value="$50,000",
                display_value="$50,000",
                academic_year="2024 - 2025",
                citation_markers=[1],
            ),
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=4321,
                metric_name="Collective bargaining status",
                value="Yes",
                display_value="Yes",
                academic_year="2024 - 2025",
                citation_markers=[2],
            ),
            MetricValueRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=9876,
                metric_name="Average teacher starting salary",
                value="$60,000",
                display_value="$60,000",
                academic_year="2024 - 2025",
                citation_markers=[3],
            ),
            MetricValueRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=4321,
                metric_name="Collective bargaining status",
                value="No",
                display_value="No",
                academic_year="2024 - 2025",
                citation_markers=[4],
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha Salary Schedule, 2024-2025",
                url="https://example.org/alpha-salary.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            ),
            CitationRef(
                marker=2,
                title="Alpha Contract, 2024-2025",
                url="https://example.org/alpha-contract.pdf",
                page_number=4,
                page_ref="p. 4",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            ),
            CitationRef(
                marker=3,
                title="Bravo Salary Schedule, 2024-2025",
                url="https://example.org/bravo-salary.pdf",
                page_number=3,
                page_ref="p. 3",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=2,
            ),
            CitationRef(
                marker=4,
                title="Bravo Contract, 2024-2025",
                url="https://example.org/bravo-contract.pdf",
                page_number=5,
                page_ref="p. 5",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=2,
            ),
        ],
        total_considered=4,
        excluded_count=0,
        order_statement=(
            "Looked up selected metrics for selected districts, "
            "alphabetical by district name."
        ),
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
    )


def _lookup_with_not_reviewed_result() -> ResultSet:
    return MetricLookupResult(        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                SelectedDistrict(district_id=2, district_name="Bravo", state="CA"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=9876,
                metric_name="Average teacher starting salary",
                value="$50,000",
                display_value="$50,000",
                academic_year="2024 - 2025",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=9876,
                metric_name="Average teacher starting salary",
                value=None,
                display_value=(
                    "NCTQ last reviewed Bravo for Average teacher starting "
                    "salary in 2023 - 2024; the value then was $60,000."
                ),
                academic_year="2024 - 2025",
                source="coverage_state",
                citation_markers=[],
                coverage_state="not_reviewed",
                coverage_display=(
                    "NCTQ last reviewed Bravo for Average teacher starting "
                    "salary in 2023 - 2024; the value then was $60,000."
                ),
                coverage_reason="stale_recent_answer",
                coverage_prior_academic_year="2023 - 2024",
                coverage_prior_display_value="$60,000",
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha Salary Schedule, 2024-2025",
                url="https://example.org/alpha-salary.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            )
        ],
        total_considered=2,
        excluded_count=1,
        coverage_frame=CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=1,
            real_data_count=1,
            not_reviewed_count=1,
            out_of_universe_count=0,
            coverage_ratio=0.5,
        ),
        order_statement="Looked up selected metrics for selected districts.",
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
    )


def _strike_state_grouped_plan() -> QueryPlan:
    metric = "Legality of teacher strikes"
    return QueryPlan(
        operation="lookup",
        question="Which states allow strikes?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=metric)],
        filters=[
            FilterSpec(
                field=metric,
                operator="equals",
                value="Striking is permissible",
            )
        ],
        output=OutputSpec(format="table", row_display="all", group_by="state"),
    )


def _strike_state_lookup_result() -> ResultSet:
    return MetricLookupResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                SelectedDistrict(district_id=2, district_name="Bravo", state="CA"),
                SelectedDistrict(district_id=3, district_name="Maple", state="VT"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=262,
                metric_name="Legality of teacher strikes",
                value="Striking is permissible",
                display_value="Striking is permissible",
                academic_year="2024 - 2025",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="Striking is permissible",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=262,
                metric_name="Legality of teacher strikes",
                value="Striking is permissible",
                display_value="Striking is permissible",
                academic_year="2024 - 2025",
                citation_markers=[2],
                coverage_state="covered",
                coverage_display="Striking is permissible",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=3,
                district_name="Maple",
                state="VT",
                metric_id=262,
                metric_name="Legality of teacher strikes",
                value="Striking is permissible",
                display_value="Striking is permissible",
                academic_year="2024 - 2025",
                citation_markers=[3],
                coverage_state="covered",
                coverage_display="Striking is permissible",
                coverage_reason="answer_value",
            ),
        ],
        citations=[
            CitationRef(marker=1, title="Alpha Contract", district_id=1),
            CitationRef(marker=2, title="Bravo Contract", district_id=2),
            CitationRef(marker=3, title="Maple Contract", district_id=3),
        ],
        total_considered=3,
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=3,
            real_data_count=3,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement="Looked up selected metrics for all covered districts.",
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
    )


def _metric_count_result() -> ResultSet:
    return MetricCountResult(        rows=[
            ThresholdCountRow(
                metric_id=39,
                metric_name=(
                    "Minimum number of formal observations per evaluation cycle "
                    "for non-tenured teachers"
                ),
                value=2,
                display_value="2 of 3 covered districts",
                academic_year="2024 - 2025",
                count=2,
                denominator=3,
                filter_statement="value >= 3",
                qualifying_district_ids=[1, 2],
                coverage_state="covered",
                coverage_display="2 of 3 covered districts",
                coverage_reason="count_summary",
            )
        ],
        citations=[],
        total_considered=4,
        excluded_count=1,
        coverage_frame=CoverageFrame(
            universe_count=4,
            in_scope_count=4,
            addressed_count=3,
            real_data_count=3,
            not_reviewed_count=1,
            out_of_universe_count=0,
            coverage_ratio=0.75,
        ),
        order_statement="Counted qualifying districts for selected metrics.",
        methodology_codes=[
            MethodologyRef(code="count_denominator_current_reviewed_rows"),
        ],
    )


def _covered_universe_count_result() -> ResultSet:
    return MetricCountResult(        rows=[
            ThresholdCountRow(
                metric_id=0,
                metric_name="Covered district universe",
                value=2,
                display_value="2 covered districts",
                academic_year="2024 - 2025",
                count=2,
                denominator=2,
                filter_statement="covered district universe",
                qualifying_district_ids=[10, 11],
                source="coverage_state",
                coverage_state="covered",
                coverage_display="2 covered districts",
                coverage_reason="covered_universe_count",
            )
        ],
        citations=[],
        total_considered=2,
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=2,
            real_data_count=2,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement="Counted covered districts in the resolved selection.",
        methodology_codes=[MethodologyRef(code="covered_universe_selection_count")],
    )


def _valid_report() -> ValidationReport:
    return ValidationReport(
        valid=True,
        dimensions_checked=[
            "selection",
            "metric",
            "sort_order",
            "coverage_state",
            "citation_coverage",
            "numeric_token_provenance",
        ],
        findings=[],
    )


def _peer_comparison_plan() -> QueryPlan:
    return QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's peers and how do sick leave policies compare?",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="Sick leave days")],
    )


def _coverage_screened_peer_result() -> PeerComparisonResult:
    return PeerComparisonResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(
                    district_id=26,
                    district_name="Denver Public Schools",
                    state="CO",
                ),
                SelectedDistrict(
                    district_id=24,
                    district_name="Aurora Public Schools",
                    state="CO",
                ),
                SelectedDistrict(
                    district_id=96,
                    district_name="Albuquerque Public Schools",
                    state="NM",
                ),
            ],
        ),
        rows=[
            PeerComparisonRow(
                district_id=26,
                district_name="Denver Public Schools",
                state="CO",
                metric_id=198,
                metric_name="Sick leave days",
                value="10",
                display_value="10",
                academic_year="2024 - 2025",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="10",
                coverage_reason="answer_value",
                peer_role="anchor",
                peer_rank=None,
                peer_score=None,
                peer_reason="Anchor district selected by the user.",
                peer_enrollment=87883,
                peer_urbanicity="City: Large",
            ),
            PeerComparisonRow(
                district_id=24,
                district_name="Aurora Public Schools",
                state="CO",
                metric_id=198,
                metric_name="Sick leave days",
                value="12",
                display_value="12",
                academic_year="2024 - 2025",
                citation_markers=[2],
                coverage_state="covered",
                coverage_display="12",
                coverage_reason="answer_value",
                peer_role="peer",
                peer_rank=1,
                peer_score=0.86,
                peer_reason=(
                    "Similar enrollment (87,883 vs 38,135 students); "
                    "similar urbanicity (City: Large vs City: Large); "
                    "similar FRPL share (62% vs 58% FRPL)"
                ),
                peer_enrollment=38135,
                peer_urbanicity="City: Large",
            ),
            PeerComparisonRow(
                district_id=96,
                district_name="Albuquerque Public Schools",
                state="NM",
                metric_id=198,
                metric_name="Sick leave days",
                value="10",
                display_value="10",
                academic_year="2024 - 2025",
                citation_markers=[3],
                coverage_state="covered",
                coverage_display="10",
                coverage_reason="answer_value",
                peer_role="peer",
                peer_rank=2,
                peer_score=0.73,
                peer_reason=(
                    "Similar enrollment (87,883 vs 79,805 students); "
                    "similar urbanicity (City: Large vs City: Large); "
                    "similar FRPL share (62% vs 60% FRPL)"
                ),
                peer_enrollment=79805,
                peer_urbanicity="City: Large",
            ),
        ],
        citations=[
            CitationRef(marker=1, title="Denver Agreement", district_id=26),
            CitationRef(marker=2, title="Aurora Agreement", district_id=24),
            CitationRef(marker=3, title="Albuquerque Agreement", district_id=96),
        ],
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=3,
            real_data_count=3,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        total_considered=6,
        excluded_count=3,
        order_statement=(
            "Compared selected policy metrics for the anchor district and "
            "2 deterministic NCES-similar peer districts."
        ),
        methodology_codes=[
            MethodologyRef(code="peer_selection_nces_profiles"),
            MethodologyRef(code="peer_score_method"),
            MethodologyRef(
                code="peer_metric_coverage_screen_applied",
                metadata={
                    "candidate_count": "5",
                    "excluded_unavailable_count": "3",
                    "final_peer_count": "2",
                },
            ),
            MethodologyRef(code="peer_policy_cells_with_citations"),
        ],
    )


def _coverage_screened_peer_result_with_missing_anchor() -> PeerComparisonResult:
    result = _coverage_screened_peer_result()
    rows = [
        row.model_copy(
            update={
                "value": None,
                "display_value": "NCTQ hasn't reviewed Denver Public Schools for 2024 - 2025 yet.",
                "coverage_state": "not_reviewed",
                "coverage_display": "NCTQ hasn't reviewed Denver Public Schools for 2024 - 2025 yet.",
                "coverage_reason": "district_not_reviewed",
                "citation_markers": [],
            }
        )
        if row.peer_role == "anchor"
        else row
        for row in result.rows
    ]
    return result.model_copy(update={"rows": rows})


def test_peer_comparison_rendering_explains_coverage_screen_before_table() -> None:
    manifest = render_response(
        _peer_comparison_plan(),
        _coverage_screened_peer_result(),
        _valid_report(),
    )

    table_position = manifest.body.index("| Role |")
    lead_position = manifest.body.index("current covered data")
    skipped_position = manifest.body.index("I skipped 3 peer candidates")
    fewer_position = manifest.body.index(
        "rather than padding the table with unavailable comparisons"
    )
    assert lead_position < table_position
    assert skipped_position < table_position
    assert fewer_position < table_position
    assert "not_reviewed" not in manifest.body
    assert "NCTQ hasn't reviewed" not in manifest.body


def _with_extra_in_state_peer(
    result: PeerComparisonResult,
) -> PeerComparisonResult:
    """Return a copy of ``result`` with a second same-state peer appended,
    exercising the 2-in-state-peer prose branch (cap = 2)."""
    anchor = next(row for row in result.rows if row.peer_role == "anchor")
    extra = result.rows[1].model_copy(
        update={
            "district_id": 999,
            "district_name": "Cherry Creek Schools",
            "state": anchor.state,
            "peer_rank": 3,
        }
    )
    return result.model_copy(update={"rows": [*result.rows, extra]})


def _without_in_state_peer(
    result: PeerComparisonResult,
) -> PeerComparisonResult:
    """Return a copy of ``result`` whose peer rows are all out-of-state
    relative to the anchor, exercising the fully-national prose branch."""
    rows = [
        row if row.peer_role != "peer" or (row.state or "").upper() != "CO"
        else row.model_copy(update={"state": "NM"})
        for row in result.rows
    ]
    return result.model_copy(update={"rows": rows})


def test_peer_comparison_rendering_states_cap_rule_in_every_response() -> None:
    """Every peer-comparison response that runs the coverage screen
    states the cap rule up front: up to 2 in-state peers, the rest from
    outside the anchor's state. The rule is the stated default, not a
    silent guard (Refs docs/plans/2026-05-26-peer-comparison-cap-rule.md)."""

    manifest = render_response(
        _peer_comparison_plan(),
        _coverage_screened_peer_result(),
        _valid_report(),
    )
    assert (
        "When you ask for peer districts, I include up to 2 of the most "
        "similar districts from your state, with the rest from outside your "
        "state."
    ) in manifest.body
    rule_position = manifest.body.index("up to 2 of the most similar")
    table_position = manifest.body.index("| Role |")
    assert rule_position < table_position


def test_peer_comparison_rendering_names_single_in_state_peer() -> None:
    """Default fixture has one in-state CO peer (Aurora). Disclosure names
    it so the user sees exactly which district the rule selected, without
    needing to scan the table."""

    manifest = render_response(
        _peer_comparison_plan(),
        _coverage_screened_peer_result(),
        _valid_report(),
    )
    assert "Your in-state CO peer: Aurora Public Schools." in manifest.body


def test_peer_comparison_rendering_names_two_in_state_peers_when_cap_full() -> None:
    """When the rule selects both allowed in-state peers (cap = 2),
    name them with natural conjunction."""

    manifest = render_response(
        _peer_comparison_plan(),
        _with_extra_in_state_peer(_coverage_screened_peer_result()),
        _valid_report(),
    )
    assert (
        "Your in-state CO peers: Aurora Public Schools and "
        "Cherry Creek Schools."
    ) in manifest.body


def test_peer_comparison_rendering_discloses_fully_national_when_no_in_state_peer() -> None:
    """When no in-state district matched on similarity, the disclosure
    says the comparison is fully national so the user isn't left
    wondering why their state isn't represented."""

    manifest = render_response(
        _peer_comparison_plan(),
        _without_in_state_peer(_coverage_screened_peer_result()),
        _valid_report(),
    )
    assert (
        "No other CO districts matched on similarity, so this comparison "
        "is fully national."
    ) in manifest.body


def test_peer_comparison_rendering_shows_demographic_peer_evidence_columns() -> None:
    manifest = render_response(
        _peer_comparison_plan(),
        _coverage_screened_peer_result(),
        _valid_report(),
    )

    assert (
        "| Role | Peer Rank | District | State | Enrollment | Urbanicity | "
        "Metric | Value | Peer Rationale | Sources |"
    ) in manifest.body
    assert "| anchor |  | Denver Public Schools | CO | 87,883 | City: Large |" in manifest.body
    assert "| peer | 1 | Aurora Public Schools | CO | 38,135 | City: Large |" in manifest.body


def test_peer_comparison_rendering_leads_with_demography_first_policy() -> None:
    manifest = render_response(
        _peer_comparison_plan(),
        _coverage_screened_peer_result(),
        _valid_report(),
    )

    first_line = manifest.body.splitlines()[0]
    assert "enrollment, urbanicity, and FRPL" in first_line
    assert "distance/state used as secondary signals" in first_line
    assert "Peer score is demography-first" in manifest.body
    assert "NCES-style geography, enrollment" not in manifest.body


def test_peer_comparison_rendering_confirms_anchor_value_in_prose_before_table() -> None:
    """The anchor district's own value is voiced in prose before the peer table.

    VOICE-R1 (NCTQ feedback 2026-06-15): a peer comparison must answer "where do
    I stand?" before listing comparables, not leave the anchor's value only in a
    table row. The line sits after the demography-first methodology lead and
    before the table header.
    """
    manifest = render_response(
        _peer_comparison_plan(),
        _coverage_screened_peer_result(),
        _valid_report(),
    )

    body = manifest.body
    assert "For Denver Public Schools, NCTQ's reviewed figures are:" in body
    assert "sick leave days — 10" in body
    anchor_prose_idx = body.index("For Denver Public Schools, NCTQ's reviewed figures are:")
    table_header_idx = body.index("| Role | Peer Rank | District |")
    assert anchor_prose_idx < table_header_idx


def test_peer_comparison_rendering_discloses_policy_bundle_fields_before_table() -> None:
    result = _coverage_screened_peer_result()
    bundle_rows = [
        *result.rows,
        *[
            row.model_copy(
                update={
                    "metric_id": 201,
                    "metric_name": "Whether sick leave increases with service",
                    "value": "Yes",
                    "display_value": "Yes",
                    "coverage_display": "Yes",
                }
            )
            for row in result.rows
        ],
        *[
            row.model_copy(
                update={
                    "metric_id": 202,
                    "metric_name": "Whether unused sick leave can carry over",
                    "value": "Yes",
                    "display_value": "Yes",
                    "coverage_display": "Yes",
                }
            )
            for row in result.rows
        ],
    ]
    manifest = render_response(
        _peer_comparison_plan(),
        result.model_copy(update={"rows": bundle_rows}),
        _valid_report(),
    )

    table_position = manifest.body.index("| Role |")
    bundle_position = manifest.body.index(
        "For this policy bundle, I compared these reviewed fields:"
    )
    assert bundle_position < table_position
    assert "Sick leave days" in manifest.body
    assert "Whether sick leave increases with service" in manifest.body
    assert "Whether unused sick leave can carry over" in manifest.body
    # #1645: a bundle renders WIDE — the metric names are column headers, so
    # the long layout's shared "Metric | Value" columns are gone.
    assert "| Metric | Value |" not in manifest.body


def test_peer_comparison_rendering_pivots_multi_metric_bundle_to_one_row_per_district() -> None:
    # #1645: a metric bundle pivots to ONE row per district with each metric as
    # its own column, instead of repeating the district once per metric.
    base = _coverage_screened_peer_result()
    bundle_rows = [
        *base.rows,  # metric 198 "Sick leave days": Denver 10 / Aurora 12 / Albuquerque 10
        *[
            row.model_copy(
                update={
                    "metric_id": 201,
                    "metric_name": "Increases with service",
                    "value": value,
                    "display_value": value,
                    "coverage_display": value,
                }
            )
            for row, value in zip(base.rows, ["No", "Yes", "No"], strict=True)
        ],
        *[
            row.model_copy(
                update={
                    "metric_id": 202,
                    "metric_name": "Carries over",
                    "value": value,
                    "display_value": value,
                    "coverage_display": value,
                }
            )
            for row, value in zip(
                base.rows, ["Yes - unlimited", "No", "Yes - capped"], strict=True
            )
        ],
    ]
    manifest = render_response(
        _peer_comparison_plan(),
        base.model_copy(update={"rows": bundle_rows}),
        _valid_report(),
    )
    body = manifest.body

    # Wide header: each metric is its own column, in first-appearance order,
    # between Urbanicity and Peer Rationale. The long Metric/Value pair is gone.
    assert (
        "| Role | Peer Rank | District | State | Enrollment | Urbanicity | "
        "Sick leave days | Increases with service | Carries over | "
        "Peer Rationale | Sources |"
    ) in body
    assert "| Metric | Value |" not in body

    # One row per district — each appears exactly once inside the table block.
    table_block = body[body.index("| Role | Peer Rank |") :]
    assert table_block.count("Denver Public Schools") == 1
    assert table_block.count("Aurora Public Schools") == 1
    assert table_block.count("Albuquerque Public Schools") == 1

    # Per-metric cells land on the correct district row (column mapping).
    assert (
        "| anchor |  | Denver Public Schools | CO | 87,883 | City: Large | "
        "10 | No | Yes - unlimited |"
    ) in body
    assert (
        "| peer | 1 | Aurora Public Schools | CO | 38,135 | City: Large | "
        "12 | Yes | No |"
    ) in body

    # The in-state cap-rule disclosure names each peer once, not once per
    # metric (the dedupe fix that rides with #1645).
    assert "Your in-state CO peer: Aurora Public Schools." in body
    assert "Aurora Public Schools, Aurora Public Schools" not in body


def test_peer_comparison_rendering_wide_table_handles_mixed_coverage_anchor() -> None:
    # #1645 + #1514 D3: in a multi-metric bundle, an anchor covered for one
    # metric but not reviewed for another keeps its single wide row (the
    # answerless metric renders an empty cell), voices the not-reviewed sentence
    # after the table (#1228 / VOICE-R1), and stays surface-consistent — the
    # writer groups answer rows and the validator must agree on the same
    # one-row-per-district shape.
    base = _coverage_screened_peer_result()
    metric_b = [
        row.model_copy(
            update={
                "metric_id": 201,
                "metric_name": "Carries over",
                "value": "Yes",
                "display_value": "Yes",
                "coverage_display": "Yes",
            }
        )
        for row in base.rows
    ]
    # Make the anchor's "Carries over" cell answerless (not reviewed).
    metric_b = [
        row.model_copy(
            update={
                "value": None,
                "display_value": "NCTQ hasn't reviewed Denver Public Schools for carryover yet.",
                "coverage_state": "not_reviewed",
                "coverage_display": "NCTQ hasn't reviewed Denver Public Schools for carryover yet.",
                "coverage_reason": "district_not_reviewed",
                "citation_markers": [],
            }
        )
        if row.peer_role == "anchor"
        else row
        for row in metric_b
    ]
    result = base.model_copy(update={"rows": [*base.rows, *metric_b]})

    manifest = render_response(_peer_comparison_plan(), result, _valid_report())
    body = manifest.body

    # The anchor keeps ONE wide row; its answerless "Carries over" cell is empty.
    assert (
        "| anchor |  | Denver Public Schools | CO | 87,883 | City: Large | 10 |  |"
    ) in body
    # The not-reviewed sentence is voiced after the table (#1228 / VOICE-R1),
    # not in a cell.
    assert body.index(
        "NCTQ hasn't reviewed Denver Public Schools for carryover yet."
    ) > body.index("| Role | Peer Rank |")
    # A peer covered for both metrics shows both values on its single row.
    assert (
        "| peer | 1 | Aurora Public Schools | CO | 38,135 | City: Large | 12 | Yes |"
    ) in body

    # Writer and validator agree on the mixed shape — no spurious surface drift.
    report = validate_result(_peer_comparison_plan(), result, rendered_body=body)
    codes = {finding.code for finding in report.findings}
    assert "markdown_cell_drift" not in codes
    assert "markdown_row_count_mismatch" not in codes
    assert "markdown_column_missing" not in codes


def test_peer_comparison_rendering_voices_answerless_anchor_outside_table() -> None:
    # #1514 D13: an answerless anchor leaves the table and is voiced with its
    # canonical coverage sentence. #1228 / VOICE-R1: that sentence now appears
    # AFTER the answer table under a collapsible #### heading; peers with
    # answers lead.
    manifest = render_response(
        _peer_comparison_plan(),
        _coverage_screened_peer_result_with_missing_anchor(),
        _valid_report(),
    )

    table_position = manifest.body.index("| Role |")
    warning_position = manifest.body.index(
        "NCTQ hasn't reviewed Denver Public Schools for 2024 - 2025 yet."
    )
    assert warning_position > table_position
    assert "| anchor |" not in manifest.body
    assert "kept it in the table" not in manifest.body
    assert "| peer | 1 | Aurora Public Schools |" in manifest.body
    assert "Albuquerque Public Schools" in manifest.body
    # The cap-rule disclosure still reads the anchor's state from the
    # unfiltered rows.
    assert "Your in-state CO peer: Aurora Public Schools." in manifest.body
    # Coverage is under the labeled collapsible heading.
    assert "#### Districts without a current reviewed value" in manifest.body


def test_peer_comparison_rendering_coverage_follows_answer_table() -> None:
    """#1228 / VOICE-R1: the answer table leads; answerless anchor/peer
    coverage sentences appear after it under a collapsible #### heading."""
    manifest = render_response(
        _peer_comparison_plan(),
        _coverage_screened_peer_result_with_missing_anchor(),
        _valid_report(),
    )

    table_position = manifest.body.index("| Role |")
    coverage_heading_position = manifest.body.index(
        "#### Districts without a current reviewed value"
    )
    # The coverage heading and sentences appear after the answer table.
    assert coverage_heading_position > table_position


def test_render_response_uses_typed_methodology_codes_without_source_notes() -> None:
    result = _result().model_copy(
        update={
            "source_notes": [],
            "methodology_codes": [
                MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
                MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
            ],
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.body.count("Sources cite the specific document for each row when available.") == 1
    assert "Methodology" in manifest.body
    assert "Notes" not in manifest.body


def test_render_response_includes_source_notes_in_methodology() -> None:
    result = _result().model_copy(
        update={
            "source_notes": [
                "I used first-year BA starting salary because no degree lane was specified."
            ],
            "methodology_codes": [
                MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
            ],
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    assert (
        "- I used first-year BA starting salary because no degree lane was specified."
        in manifest.body
    )
    assert "Methodology" in manifest.body


def test_render_response_uses_methodology_metadata_for_profile_field() -> None:
    result = _profile_ordered_result_without_policy_citations().model_copy(
        update={
            "source_notes": [],
            "methodology_codes": [
                MethodologyRef(
                    code="profile_rank_uses_profile_field",
                    metadata={"profile_field": "FRPL % (NCES directory year: 2024)"},
                )
            ],
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    assert (
        "District order uses district profile data: FRPL % "
        "(NCES directory year: 2024)."
    ) in manifest.body
    assert "Notes" not in manifest.body


def test_methodology_ref_rejects_unknown_codes() -> None:
    with pytest.raises(ValidationError):
        MethodologyRef(code="made_up_methodology_code")


def test_render_response_formats_valid_ranking_artifact() -> None:
    manifest = render_response(_plan(), _result(), _valid_report())

    assert manifest.status == "rendered"
    assert manifest.body.startswith(
        "I ranked covered districts by Average teacher starting salary, highest to lowest."
    )
    assert "| Rank | District | State | Average teacher starting salary | Sources |" in manifest.body
    assert "| 1 | Bravo | CA | $70,000 | [1] |" in manifest.body
    assert "[1] Bravo District Contract, 2024-2025, p. 2" not in manifest.body
    assert "\nSources\n" not in manifest.body
    assert "Methodology" in manifest.body
    assert "Sources cite the specific document for each row when available." in manifest.body
    assert "Notes" not in manifest.body
    assert manifest.result_type == "metric_ranking"
    assert manifest.validation_valid is True


def test_render_response_stamps_artifact_presence_in_metadata() -> None:
    # WS-2 (#1242): the answer-layer brief reads these flags so the stylist
    # never claims Compass "can't generate a chart" when one was attached.
    # _result() has too few rows for a chart but always carries a CSV export.
    no_chart = render_response(_plan(), _result(), _valid_report())
    assert no_chart.metadata["has_chart"] is False
    assert no_chart.metadata["has_csv_export"] is True

    # _many_row_ranking_result has enough distinct values to render a chart.
    charted = render_response(
        _plan(), _many_row_ranking_result(12), _valid_report()
    )
    assert charted.metadata["has_chart"] is True
    assert charted.metadata["has_csv_export"] is True


def test_render_response_previews_long_unbounded_rankings() -> None:
    result = _many_row_ranking_result()

    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.status == "rendered"
    assert "Showing 10 of 12 ranked rows here." in manifest.body
    assert "Export includes all 12 ranked districts." in manifest.body
    assert "Ask to show all to expand the table." in manifest.body
    assert "| 10 | District 10 | CA | $99,990 |" in manifest.body
    assert "| 11 | District 11 | CA | $99,989 |  |" not in manifest.body
    assert result.csv_export is not None
    assert len(result.csv_export.rows) == 12
    assert manifest.metadata["row_count"] == 12
    assert manifest.metadata["displayed_row_count"] == 10
    assert manifest.metadata["display_limit"] == 10
    assert manifest.metadata["row_display"] == "preview"
    assert manifest.metadata["data_limit_count"] is None
    assert manifest.metadata["data_limit_kind"] is None
    assert manifest.metadata["data_limit_source"] == "unbounded"
    assert manifest.metadata["display_limit_source"] == "renderer_preview"


def test_render_response_ranking_preview_honors_settings_override(monkeypatch) -> None:
    """`settings.ranking_display_limit` controls the preview cap, not a constant."""

    from compass_backend.config import settings

    monkeypatch.setattr(settings, "ranking_display_limit", 5)
    result = _many_row_ranking_result()

    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.status == "rendered"
    assert "Showing 5 of 12 ranked rows here." in manifest.body
    assert "Showing 10 of 12 ranked rows here." not in manifest.body
    assert "| 5 | District 05 | CA | $99,995 |" in manifest.body
    assert "| 6 | District 06 | CA | $99,994 |" not in manifest.body
    assert manifest.metadata["displayed_row_count"] == 5
    assert manifest.metadata["display_limit"] == 5


def test_render_response_can_show_all_rows_for_long_unbounded_rankings() -> None:
    result = _many_row_ranking_result()
    plan = _plan().model_copy(
        update={"output": OutputSpec(format="table", row_display="all")}
    )

    manifest = render_response(plan, result, _valid_report())

    assert manifest.status == "rendered"
    assert "Showing 10 of 12 ranked rows here." not in manifest.body
    assert "| 12 | District 12 | CA | $99,988 |" in manifest.body
    assert manifest.metadata["row_count"] == 12
    assert manifest.metadata["displayed_row_count"] == 12
    assert manifest.metadata["display_limit"] is None
    assert manifest.metadata["row_display"] == "all"
    assert manifest.metadata["data_limit_source"] == "unbounded"
    assert manifest.metadata["display_limit_source"] == "none"


def test_render_response_records_explicit_data_limit_provenance() -> None:
    result = _many_row_ranking_result(row_count=10)
    plan = _plan().model_copy(update={"limit": LimitSpec(count=10, kind="top")})

    manifest = render_response(plan, result, _valid_report())

    assert manifest.status == "rendered"
    assert manifest.metadata["row_count"] == 10
    assert manifest.metadata["displayed_row_count"] == 10
    assert manifest.metadata["display_limit"] is None
    assert manifest.metadata["data_limit_count"] == 10
    assert manifest.metadata["data_limit_kind"] == "top"
    assert manifest.metadata["data_limit_source"] == "limit_spec"
    assert manifest.metadata["display_limit_source"] == "none"


def test_render_response_summarizes_partial_ranking_coverage() -> None:
    manifest = render_response(_named_plan(), _partial_ranking_result(), _valid_report())

    assert manifest.status == "rendered"
    assert manifest.body.startswith(
        "I found current reviewed numeric data for 1 of 3 requested districts."
    )
    assert (
        "The ranking below includes only districts with current numeric values."
        in manifest.body
    )
    assert "| 1 | Bravo | CA | $70,000 | [1] |" in manifest.body
    assert "Not included in ranking" in manifest.body
    # #1514 D8: Charlie (stale_recent_answer) re-enters the narrative as the
    # canonical prior-year sentence — name, prior value, AND prior year are
    # required reader-facing facts now (reversing the #1435 suppression).
    assert (
        "- NCTQ last reviewed Charlie for Average teacher starting salary "
        "in 2023 - 2024; the value then was $65,000."
    ) in manifest.body
    # …but never as a table row, and never via the retired short label.
    assert "| Charlie" not in manifest.body
    assert "Older year only" not in manifest.body
    assert "Most recent available" not in manifest.body
    # Delta (district_not_reviewed) still surfaces in "Not included in ranking",
    # now COUNTED (#1702, criterion 26) rather than named per-district. The one
    # not-reviewed gap is voiced as a counted sentence; the prior per-district
    # "NCTQ hasn't reviewed Delta …" bullet is retired for not-reviewed gaps.
    assert (
        "- NCTQ hasn't reviewed 1 district for the requested year yet." in manifest.body
    )
    assert "NCTQ hasn't reviewed Delta" not in manifest.body
    assert "Delta: Not reviewed" not in manifest.body
    assert "| Rank | District | State | Average teacher starting salary | Sources |" in manifest.body
    assert "\nSources\n" not in manifest.body
    assert "Methodology" in manifest.body
    # ranking_excludes_unrankable_rows dropped (Track 3.3); availability block conveys it
    assert "Rows without current numeric values are not included in the rank order." not in manifest.body
    assert "Sources cite the specific document for each row when available." in manifest.body
    assert "Notes" not in manifest.body


def test_render_response_summarizes_broad_ranking_coverage() -> None:
    manifest = render_response(
        _state_plan(),
        _broad_partial_ranking_result(),
        _valid_report(),
    )

    assert manifest.status == "rendered"
    assert "Not included in ranking" in manifest.body
    # #1702: all 7 disclosures are district_not_reviewed gaps, so they are
    # COUNTED in one sentence (number from breakdown.district_not_reviewed_count),
    # not listed per-district and not folded into the retired mixed grand-total
    # intro ("NCTQ tracks N … not included").
    assert (
        "- NCTQ hasn't reviewed 7 districts for the requested year yet."
        in manifest.body
    )
    assert "NCTQ tracks 7 additional" not in manifest.body
    assert "District 1:" not in manifest.body
    assert "District 7:" not in manifest.body
    assert "Coverage:" not in manifest.body
    assert "in-scope cells" not in manifest.body
    assert "real-data cells" not in manifest.body


def test_render_response_uses_plain_language_for_texas_observation_preview() -> None:
    manifest = render_response(
        _state_plan().model_copy(
            update={
                "question": "Show me observation counts for Texas districts, lowest first.",
                "selection": SelectionSpec(scope="state", states=["TX"]),
            }
        ),
        _texas_observation_preview_result(),
        _valid_report(),
    )

    assert manifest.status == "rendered"
    assert manifest.body.startswith(
        "I found current reviewed numeric data for 13 of 21 TX districts NCTQ tracks."
    )
    assert (
        "Showing 10 of 13 ranked rows here. Export includes all 13 ranked districts."
        in manifest.body
    )
    # #1702: the 1 INA disclosure is partitioned OUT of the availability block
    # and voiced as a reviewed finding in the lead (sin 2 cleared) — never under
    # the "Not included in ranking" heading, never counted in it.
    assert (
        "NCTQ reviewed 1 more district where the issue is not addressed."
        in manifest.body
    )
    # #1702 sin-2, heading-PRESENT regime: this is the load-bearing case the
    # reverted #1703 regressed 1/3 -> 0/8 — 7 real gaps DO render the "Not
    # included in ranking" heading, yet the INA finding must sit in the lead
    # ABOVE that heading, not be folded under it or counted in its block.
    assert "Not included in ranking" in manifest.body
    assert manifest.body.index(
        "where the issue is not addressed"
    ) < manifest.body.index("Not included in ranking")
    # #1702: the remaining 7 gaps (6 stale + 1 non-numeric) are NAMED reasons but
    # > 5 with a state scope, so they fall back to COUNTED clauses — each number
    # from its single serialized breakdown field. No mixed grand-total intro.
    assert (
        "- NCTQ last reviewed 6 districts in an earlier year." in manifest.body
    )
    assert (
        "- 1 district has a current reviewed non-numeric value." in manifest.body
    ), manifest.body
    assert "NCTQ tracks 8 additional" not in manifest.body
    # No supplementary table and no retired vocabulary.
    assert "Most recent available" not in manifest.body
    assert "Older year only" not in manifest.body
    assert "older-year" not in manifest.body
    assert "in-scope cells" not in manifest.body
    assert "Coverage:" not in manifest.body
    assert "Excluded rows:" not in manifest.body


def test_render_response_places_named_district_availability_after_ranking_table() -> None:
    manifest = render_response(_named_plan(), _partial_ranking_result(), _valid_report())

    assert manifest.status == "rendered"
    assert manifest.body.startswith(
        "I found current reviewed numeric data for 1 of 3 requested districts."
    )
    # #1702: Charlie (stale) keeps its named sentence; Delta (district_not_reviewed)
    # is counted. The mixed grand-total intro is gone — no "NCTQ tracks N …" line.
    assert "Not included in ranking" in manifest.body
    assert "NCTQ tracks 2 requested districts" not in manifest.body
    # #1228 / VOICE-R1: the answer table leads; the availability disclosure now
    # FOLLOWS it (under its own collapsible #### heading), reversing the prior
    # #1514 coverage-first ordering so the answer is no longer buried beneath the
    # caveats. The labeled heading resolves #1514's footnote-ambiguity concern.
    assert manifest.body.index(
        "NCTQ hasn't reviewed 1 district for the requested year yet."
    ) > manifest.body.index("| Rank | District |")
    assert "in-scope cells" not in manifest.body


def test_render_response_partial_coverage_lead_passes_numeric_token_validation() -> None:
    result = _partial_ranking_result()
    manifest = render_response(_named_plan(), result, _valid_report())
    report = validate_result(
        _named_plan(),
        result,
        authority=ValidationAuthority(
            metrics=[
                ResolvedMetricAuthority(
                    metric_id=1234,
                    metric_name="Average teacher starting salary",
                )
            ],
            selection=ResolvedSelectionAuthority(
                scope="named_districts",
                district_ids=[2, 3, 4],
            ),
        ),
        rendered_body=manifest.body,
    )

    assert not [
        finding
        for finding in report.findings
        if finding.code == "numeric_token_not_in_artifact"
    ]


# ---------------------------------------------------------------------------
# #1702 — INA / N-A reviewed findings off the "Not included in ranking" block
# ---------------------------------------------------------------------------


def _ranking_with_findings(
    *,
    ina_count: int = 0,
    na_count: int = 0,
    not_reviewed_count: int = 0,
) -> ResultSet:
    """A small ranking (1 ranked row) plus INA / N-A / not-reviewed exclusions.

    INA and N-A carry coverage_state "ina"/"na" (reviewed findings); not-reviewed
    is a real gap. The CoverageBreakdown counts match the disclosure subset, as
    the production builder guarantees (coverage_breakdown_from_labels).
    """

    ranked = RankingRow(
        district_id=1,
        district_name="Alpha",
        state="CA",
        metric_id=1234,
        metric_name="Average teacher starting salary",
        value=70000.0,
        display_value="$70,000",
        academic_year="2024 - 2025",
        rank=1,
        citation_markers=[1],
        coverage_state="covered",
        coverage_display="$70,000",
        coverage_reason="answer_value",
    )
    disclosures: list[CoverageDisclosure] = []
    next_id = 100
    for _ in range(ina_count):
        disclosures.append(
            CoverageDisclosure(
                district_id=next_id,
                district_name=f"INA District {next_id}",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                academic_year="2024 - 2025",
                coverage_state="ina",
                display="Issue not addressed in the documents reviewed.",
                reason="issue_not_addressed",
            )
        )
        next_id += 1
    for _ in range(na_count):
        disclosures.append(
            CoverageDisclosure(
                district_id=next_id,
                district_name=f"N-A District {next_id}",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                academic_year="2024 - 2025",
                coverage_state="na",
                display="Not applicable.",
                reason="not_applicable",
            )
        )
        next_id += 1
    for _ in range(not_reviewed_count):
        disclosures.append(
            CoverageDisclosure(
                district_id=next_id,
                district_name=f"Gap District {next_id}",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                academic_year="2024 - 2025",
                coverage_state="not_reviewed",
                display=f"NCTQ hasn't reviewed Gap District {next_id} for 2024 - 2025 yet.",
                reason="district_not_reviewed",
            )
        )
        next_id += 1

    in_scope = 1 + ina_count + na_count + not_reviewed_count
    return MetricRankingResult(
        selection=ResultSelection(scope="state", states=["CA"]),
        rows=[ranked],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha District Contract, 2024-2025",
                url="https://example.org/alpha.pdf",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
        coverage_frame=CoverageFrame(
            universe_count=in_scope,
            in_scope_count=in_scope,
            addressed_count=1 + ina_count + na_count,
            real_data_count=1,
            not_reviewed_count=not_reviewed_count,
            out_of_universe_count=0,
            coverage_ratio=1 / in_scope,
            breakdown=CoverageBreakdown(
                answer_value_count=1,
                issue_not_addressed_count=ina_count,
                not_applicable_count=na_count,
                district_not_reviewed_count=not_reviewed_count,
            ),
        ),
        coverage_disclosures=disclosures,
        total_considered=in_scope,
        excluded_count=ina_count + na_count + not_reviewed_count,
        order_statement="Ranked CA districts by starting salary, highest to lowest.",
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
        ],
    )


def test_ina_findings_narrated_off_the_not_included_block() -> None:
    """#1702 sin 2: INA districts are narrated as a reviewed finding, NOT under
    the "Not included in ranking" heading and NOT counted in that block."""

    result = _ranking_with_findings(ina_count=2)
    manifest = render_response(_state_plan(), result, _valid_report())

    # The reviewed-finding sentence is present and frames INA as a FINDING.
    assert (
        "NCTQ reviewed 2 more districts where the issue is not addressed."
        in manifest.body
    )

    # With no real gap, there is no availability block at all — INA is never
    # voiced under the heading and never as a per-district "X: Not addressed".
    assert "Not included in ranking" not in manifest.body
    assert ": Not addressed" not in manifest.body
    # The finding line precedes the table (it lives in the lead, not the block).
    assert manifest.body.index(
        "where the issue is not addressed"
    ) < manifest.body.index("| Rank | District |")


def test_ina_and_na_findings_are_separate_field_backed_clauses() -> None:
    """#1702 R4: 1 INA + 1 N-A are two separate clauses, each number from its
    own serialized breakdown field — neither token is a sum of two fields."""

    result = _ranking_with_findings(ina_count=1, na_count=1)
    manifest = render_response(_state_plan(), result, _valid_report())

    assert "where the issue is not addressed" in manifest.body
    assert "where the policy is not applicable" in manifest.body
    # The combined "2" (1 INA + 1 N-A) must NOT appear as a single count token —
    # each clause carries its own "1".
    assert "2 more districts where the issue" not in manifest.body
    assert (
        "NCTQ reviewed 1 more district where the issue is not addressed and "
        "found 1 district where the policy is not applicable." in manifest.body
    )


def test_ina_finding_sentence_passes_numeric_token_provenance() -> None:
    """#1702 R4: every numeric token in the INA finding sentence is traceable to
    a single serialized artifact field (mirrors the partial-coverage provenance
    test)."""

    result = _ranking_with_findings(ina_count=2, na_count=1, not_reviewed_count=1)
    manifest = render_response(_state_plan(), result, _valid_report())
    report = validate_result(
        _state_plan(),
        result,
        authority=ValidationAuthority(
            metrics=[
                ResolvedMetricAuthority(
                    metric_id=1234,
                    metric_name="Average teacher starting salary",
                )
            ],
            selection=ResolvedSelectionAuthority(scope="state", states=["CA"]),
        ),
        rendered_body=manifest.body,
    )

    assert not [
        finding
        for finding in report.findings
        if finding.code == "numeric_token_not_in_artifact"
    ]


def test_ranking_without_findings_emits_no_finding_sentence() -> None:
    """#1702: a ranking with no INA/N-A emits no finding sentence and leaves the
    availability block unchanged (no empty-sentence regression)."""

    result = _ranking_with_findings(not_reviewed_count=2)
    manifest = render_response(_state_plan(), result, _valid_report())

    assert "where the issue is not addressed" not in manifest.body
    assert "where the policy is not applicable" not in manifest.body
    # The not-reviewed gap is still counted under the heading.
    assert "Not included in ranking" in manifest.body
    assert (
        "- NCTQ hasn't reviewed 2 districts for the requested year yet."
        in manifest.body
    )


def test_unmapped_not_reviewed_disclosure_is_counted_never_labelled() -> None:
    """#1702 hardening: a not_reviewed disclosure with an UNMAPPED reason
    (reason=None — reachable via the promoted-row path, where
    coverage_reason may be absent) must be COUNTED as a gap, never routed to the
    forbidden "{district}: Not reviewed" per-district label that is criterion
    26's own failure mode. In production such a label counts as
    metric_not_reviewed_count (``_default_reason_for_state``), so it folds into
    the metric-not-reviewed clause and is never named per-district. The
    availability partition is an allowlist of NAMED reasons, so any unmapped
    not_reviewed reason falls to the counted path, not the label path.
    """

    ranked = RankingRow(
        district_id=1,
        district_name="Alpha",
        state="CA",
        metric_id=1234,
        metric_name="Average teacher starting salary",
        value=70000.0,
        display_value="$70,000",
        academic_year="2024 - 2025",
        rank=1,
        citation_markers=[1],
        coverage_state="covered",
        coverage_display="$70,000",
        coverage_reason="answer_value",
    )
    mystery = CoverageDisclosure(
        district_id=200,
        district_name="Mystery District",
        state="CA",
        metric_id=1234,
        metric_name="Average teacher starting salary",
        academic_year="2024 - 2025",
        coverage_state="not_reviewed",
        display="Not reviewed",
        reason=None,
    )
    result = MetricRankingResult(
        selection=ResultSelection(scope="state", states=["CA"]),
        rows=[ranked],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha District Contract, 2024-2025",
                url="https://example.org/alpha.pdf",
                academic_year="2024 - 2025",
                district_id=1,
            )
        ],
        coverage_frame=CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=1,
            real_data_count=1,
            not_reviewed_count=1,
            out_of_universe_count=0,
            coverage_ratio=0.5,
            breakdown=CoverageBreakdown(
                answer_value_count=1,
                metric_not_reviewed_count=1,
            ),
        ),
        coverage_disclosures=[mystery],
        total_considered=2,
        excluded_count=1,
        order_statement="Ranked CA districts by starting salary, highest to lowest.",
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
        ],
    )
    manifest = render_response(_state_plan(), result, _valid_report())

    # Counted as a gap (the metric-not-reviewed clause), backed by
    # breakdown.metric_not_reviewed_count — never named per-district and never
    # the forbidden "{district}: Not reviewed" label.
    assert (
        "NCTQ hasn't reviewed 1 district for the requested year yet."
        in manifest.body
    )
    assert "Mystery District:" not in manifest.body
    assert "Mystery District" not in manifest.body


def test_profile_ordered_ina_row_not_double_narrated() -> None:
    """#1702: a profile-ordered ranking keeps INA/N-A as TABLE ROWS (no
    disclosure), so the reviewed-finding sentence is suppressed — the in-table
    cell is the only mention. The finding sentence is gated on EXCLUDED findings
    (disclosures), not on the breakdown count, so an in-table INA row is never
    double-narrated."""

    base = _mixed_ranking_result()
    rows = [
        base.rows[0],
        base.rows[1].model_copy(
            update={
                "value": None,
                "display_value": "Issue not addressed in the documents reviewed.",
                "coverage_state": "ina",
                "coverage_display": "Issue not addressed in the documents reviewed.",
                "coverage_reason": "issue_not_addressed",
                "citation_markers": [],
            }
        ),
    ]
    result = base.model_copy(
        update={
            "rows": rows,
            "coverage_frame": base.coverage_frame.model_copy(
                update={
                    "addressed_count": 2,
                    "real_data_count": 1,
                    "coverage_ratio": 0.5,
                    "breakdown": CoverageBreakdown(
                        answer_value_count=1,
                        issue_not_addressed_count=1,
                    ),
                }
            ),
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    # The INA district stays a table cell …
    assert "| 2 | Bravo | CA | 80% | Not addressed |" in manifest.body
    # … and is NOT re-narrated as an excluded reviewed finding.
    assert "where the issue is not addressed" not in manifest.body


def test_render_response_formats_profile_ordered_policy_metric_ranking() -> None:
    result = _mixed_ranking_result()
    manifest = render_response(
        _plan(),
        result,
        ValidationReport(valid=True),
    )

    assert manifest.status == "rendered"
    assert (
        "I ranked covered districts by FRPL %, highest to lowest, and displayed "
        "Average teacher starting salary."
    ) in manifest.body
    assert (
        "| Rank | District | State | FRPL % | "
        "Average teacher starting salary | Sources |"
    ) in manifest.body
    assert "| 1 | Alpha | CA | 90% | $50,000 | [1] |" in manifest.body
    assert result.csv_export is not None
    assert "sort_metric_name" in result.csv_export.columns
    assert result.csv_export.rows[0]["sort_display_value"] == "90%"
    assert "sort_source_document" in result.csv_export.columns
    assert result.csv_export.rows[0]["sort_source_document"] == "NCES district profile"
    assert result.csv_export.rows[0]["sort_source_document_type"] == "district_profile"
    assert result.csv_export.rows[0]["sort_source_valid_from"] == "2024"
    assert result.csv_export.rows[0]["source_document"] == (
        "Alpha District Contract, 2024-2025"
    )


def test_render_response_omits_empty_sources_column_for_profile_ranking() -> None:
    manifest = render_response(
        _plan(),
        _profile_ordered_result_without_policy_citations(),
        _valid_report(),
    )

    assert (
        "| Rank | District | State | FRPL % | Average teacher starting salary |"
    ) in manifest.body
    assert (
        "| Rank | District | State | FRPL % | Average teacher starting salary | Sources |"
    ) not in manifest.body
    assert "| 1 | Alpha | CA | 90% | $50,000 |" in manifest.body
    assert (
        "District order uses district profile data: FRPL % "
        "(NCES directory year: 2024)."
    ) in manifest.body
    assert "Sources" not in manifest.body


def test_render_response_keeps_sources_column_for_mixed_cited_rows() -> None:
    manifest = render_response(
        _plan(),
        _mixed_ranking_result_with_one_uncited_row(),
        _valid_report(),
    )

    assert (
        "| Rank | District | State | FRPL % | "
        "Average teacher starting salary | Sources |"
    ) in manifest.body
    assert "| 1 | Alpha | CA | 90% | $50,000 | [1] |" in manifest.body
    assert "| 2 | Bravo | CA | 80% | $70,000 |  |" in manifest.body
    assert "\nSources\n" not in manifest.body


def test_render_response_blocks_table_when_validation_failed() -> None:
    report = ValidationReport(
        valid=False,
        dimensions_checked=["sort_order"],
        findings=[
            ValidationFinding(
                code="sort_order_mismatch",
                message="Ranking row order does not match the requested sort direction.",
                dimension="sort_order",
            )
        ],
    )

    manifest = render_response(_plan(), _result(), report)

    assert manifest.status == "validation_failed"
    assert "validation failed" in manifest.body
    assert "sort_order_mismatch" in manifest.body
    assert "| Rank | District |" not in manifest.body
    assert manifest.validation_valid is False


def test_render_response_names_state_scope() -> None:
    manifest = render_response(_state_plan(), _result(), _valid_report())

    assert manifest.body.startswith(
        "I ranked covered districts in CA by Average teacher starting salary, highest to lowest."
    )


def test_render_response_names_selected_district_scope() -> None:
    manifest = render_response(_named_plan(), _named_result(), _valid_report())

    assert manifest.body.startswith(
        "I ranked selected districts by Average teacher starting salary, highest to lowest."
    )


def test_render_response_formats_multi_metric_lookup_as_comparison_table() -> None:
    manifest = render_response(
        _lookup_comparison_plan(),
        _lookup_comparison_result(),
        _valid_report(),
    )

    assert manifest.status == "rendered"
    assert manifest.result_type == "metric_lookup"
    assert manifest.body.startswith("I looked up selected metrics for selected districts.")
    assert "| Rank |" not in manifest.body
    assert (
        "| District | State | Average teacher starting salary | "
        "Collective bargaining status | Sources |"
    ) in manifest.body
    assert "| Alpha | CA | $50,000 | Yes | [1] [2] |" in manifest.body
    assert "| Bravo | CA | $60,000 | No | [3] [4] |" in manifest.body


def test_render_response_comparison_lookup_voices_coverage_after_table() -> None:
    """#1228 / VOICE-R1: in a multi-metric comparison lookup, fully-absent
    districts are disclosed after the answer table under a collapsible heading."""
    base = _lookup_comparison_result()
    # Make all Alpha rows not-reviewed so Alpha drops from the table.
    not_reviewed_rows = [
        _as_not_reviewed_lookup_row(row)
        for row in base.rows
        if row.district_name == "Alpha"
    ]
    other_rows = [r for r in base.rows if r.district_name != "Alpha"]
    result = base.model_copy(update={"rows": other_rows + not_reviewed_rows})

    manifest = render_response(
        _lookup_comparison_plan(),
        result,
        _valid_report(),
    )

    # Answer table still leads.
    assert "| District | State |" in manifest.body
    # Alpha is dropped from the table.
    assert "| Alpha |" not in manifest.body
    # Coverage sentence exists.
    assert "NCTQ hasn't reviewed Alpha" in manifest.body
    # Coverage follows the answer table.
    assert manifest.body.index("NCTQ hasn't reviewed Alpha") > manifest.body.index(
        "| District | State |"
    )
    # Coverage is under the labeled heading.
    assert "#### Districts without a current reviewed value" in manifest.body


def test_render_response_comparison_lookup_all_not_reviewed_keeps_coverage_inline() -> None:
    """R4: when all districts are not-reviewed (zero answer rows), coverage
    stays inline — it cannot be collapsed when it is the entire answer."""
    base = _lookup_comparison_result()
    not_reviewed_rows = [_as_not_reviewed_lookup_row(row) for row in base.rows]
    result = base.model_copy(update={"rows": not_reviewed_rows})

    manifest = render_response(
        _lookup_comparison_plan(),
        result,
        _valid_report(),
    )

    # Coverage is present.
    assert "NCTQ hasn't reviewed" in manifest.body
    # No bare table header (empty-table guard fired, no answer rows).
    assert "| District | State |" not in manifest.body
    # No collapsible heading — coverage is inline, not collapsed.
    assert "#### Districts without a current reviewed value" not in manifest.body


def test_render_response_groups_lookup_rows_by_state_when_requested() -> None:
    manifest = render_response(
        _strike_state_grouped_plan(),
        _strike_state_lookup_result(),
        _valid_report(),
    )

    assert manifest.status == "rendered"
    assert manifest.result_type == "metric_lookup"
    assert manifest.body.startswith("Based on NCTQ's covered district data")
    assert (
        "| State | District rows represented | Academic year | "
        "Legality of teacher strikes | Sources |"
    ) in manifest.body
    assert "| CA | 2 | 2024 - 2025 | Striking is permissible | [1] [2] |" in manifest.body
    assert "| VT | 1 | 2024 - 2025 | Striking is permissible | [3] |" in manifest.body
    assert "Alpha | CA" not in manifest.body
    assert "Bravo | CA" not in manifest.body
    assert "State rows aggregate covered district rows" in manifest.body


def test_state_grouped_lookup_voices_non_answer_districts() -> None:
    """#1514 R5 (reviewer repro): a group_by='state' lookup with an
    out-of-Pathfinder name ("Fakeville USD") and a not-reviewed district must
    voice the same canonical sentences as the single-metric lookup — never
    silently drop them from the aggregated table."""

    base = _strike_state_lookup_result()
    hudson_not_reviewed = _as_not_reviewed_lookup_row(
        base.rows[0].model_copy(
            update={"district_id": 4, "district_name": "Hudson", "state": "NY"}
        )
    )
    result = _with_rederived_district_coverage(
        base.model_copy(
            update={
                "selection": base.selection.model_copy(
                    update={
                        "districts": [
                            *base.selection.districts,
                            SelectedDistrict(
                                district_id=4, district_name="Hudson", state="NY"
                            ),
                        ],
                        "unresolved_districts": ["Fakeville USD"],
                    }
                ),
                "rows": [
                    *base.rows,
                    hudson_not_reviewed,
                    _out_of_universe_lookup_row(
                        "Fakeville USD", 262, "Legality of teacher strikes"
                    ),
                ],
                "coverage_frame": CoverageFrame(
                    universe_count=5,
                    in_scope_count=4,
                    addressed_count=3,
                    real_data_count=3,
                    not_reviewed_count=1,
                    out_of_universe_count=1,
                    coverage_ratio=0.75,
                ),
            }
        )
    )

    manifest = render_response(_strike_state_grouped_plan(), result, _valid_report())

    # The district-count line reads the serialized summary; the named-district
    # sentences are never threshold-suppressed (coverage here is 75% >= 70%).
    assert (
        "Of the 5 districts you asked about, 3 have current reviewed data; "
        "1 hasn't been reviewed for 2024 - 2025 yet."
    ) in manifest.body
    assert "Fakeville USD is not in the District Policy Pathfinder." in manifest.body
    assert (
        "NCTQ hasn't reviewed Hudson for Legality of teacher strikes "
        "in 2024 - 2025 yet."
    ) in manifest.body
    # The aggregated table still renders, without the non-answer rows.
    assert "| CA | 2 | 2024 - 2025 | Striking is permissible | [1] [2] |" in manifest.body
    assert "Fakeville" not in manifest.body.split("| State |")[1].split("\n\n")[0]


def test_state_grouped_lookup_with_no_answers_renders_no_table() -> None:
    """#1514 R5 empty-table guard: all-non-answer state-grouped lookups get a
    narrative-only body, never a header-only table."""

    base = _strike_state_lookup_result()
    result = _with_rederived_district_coverage(
        base.model_copy(
            update={
                "selection": base.selection.model_copy(
                    update={
                        "districts": [],
                        "unresolved_districts": ["Fakeville USD"],
                    }
                ),
                "rows": [
                    _out_of_universe_lookup_row(
                        "Fakeville USD", 262, "Legality of teacher strikes"
                    ),
                ],
                "citations": [],
                "coverage_frame": CoverageFrame(
                    universe_count=1,
                    in_scope_count=0,
                    addressed_count=0,
                    real_data_count=0,
                    not_reviewed_count=0,
                    out_of_universe_count=1,
                    coverage_ratio=0.0,
                ),
            }
        )
    )

    manifest = render_response(_strike_state_grouped_plan(), result, _valid_report())

    assert "Fakeville USD is not in the District Policy Pathfinder." in manifest.body
    assert "|" not in manifest.body


def test_render_response_describes_metric_lookup_intersection_criteria() -> None:
    result = _lookup_comparison_result().model_copy(
        update={
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(district_id=1, district_name="Alpha", state="CA")
                ],
            ),
            "rows": [
                _lookup_comparison_result().rows[0].model_copy(
                    update={
                        "criterion_id": "criterion_1",
                        "criterion_label": "performance pay bonuses",
                        "criterion_satisfied": True,
                    }
                ),
                _lookup_comparison_result().rows[1].model_copy(
                    update={
                        "criterion_id": "criterion_2",
                        "criterion_label": "hard-to-staff school pay",
                        "criterion_satisfied": True,
                    }
                ),
            ],
            "criteria": [
                ResultCriterion(
                    criterion_id="criterion_1",
                    label="performance pay bonuses",
                    metric_ids=[9876],
                    qualifying_district_ids=[1],
                ),
                ResultCriterion(
                    criterion_id="criterion_2",
                    label="hard-to-staff school pay",
                    metric_ids=[4321],
                    qualifying_district_ids=[1],
                ),
            ],
            "excluded_count": 3,
            "methodology_codes": [
                MethodologyRef(code="intersection_requires_all_criteria"),
                MethodologyRef(code="intersection_accepts_any_current_positive_value"),
            ],
        }
    )
    plan = QueryPlan(
        operation="lookup",
        question="Which districts offer both performance pay and hard-to-staff pay?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="performance pay bonuses"),
            MetricSpec(name="hard-to-staff school pay", role="comparison"),
        ],
    )

    manifest = render_response(plan, result, _valid_report())

    assert manifest.body.startswith(
        "I found 1 covered districts that satisfy all requested criteria: "
        "performance pay bonuses; hard-to-staff school pay."
    )
    assert "Districts are included only when they satisfy every requested criterion." in manifest.body
    assert "Excluded rows:" not in manifest.body
    assert (
        "Other rows were filtered out because they did not satisfy every "
        "requested criterion: 3."
    ) in manifest.body


def test_render_response_orders_intersection_columns_by_criteria() -> None:
    result = MetricLookupResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[SelectedDistrict(district_id=1, district_name="Alpha", state="CA")],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=175,
                metric_name=(
                    "District offers additional pay for teaching in schools "
                    "classified as hard-to-staff"
                ),
                value="Yes",
                display_value="Yes",
                academic_year="2024 - 2025",
            ),
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=171,
                metric_name="Maximum annual performance pay bonus",
                value="$5,000",
                display_value="$5,000",
                academic_year="2024 - 2025",
            ),
        ],
        total_considered=2,
        excluded_count=5,
        order_statement="Looked up selected metrics for selected districts.",
        criteria=[
            ResultCriterion(
                criterion_id="criterion_1",
                label="performance pay bonuses",
                metric_ids=[171],
                qualifying_district_ids=[1],
            ),
            ResultCriterion(
                criterion_id="criterion_2",
                label="hard-to-staff school pay",
                metric_ids=[175],
                qualifying_district_ids=[1],
            ),
        ],
        methodology_codes=[
            MethodologyRef(code="intersection_requires_all_criteria"),
        ],
    )
    plan = QueryPlan(
        operation="lookup",
        question="Which districts offer both performance pay and hard-to-staff pay?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="performance pay bonuses"),
            MetricSpec(name="hard-to-staff school pay", role="comparison"),
        ],
    )

    manifest = render_response(plan, result, _valid_report())

    assert (
        "| District | State | Maximum annual performance pay bonus | "
        "District offers additional pay for teaching in schools classified as "
        "hard-to-staff |"
    ) in manifest.body
    assert (
        manifest.body.index("Maximum annual performance pay bonus")
        < manifest.body.index("District offers additional pay")
    )
    assert "Excluded rows:" not in manifest.body
    assert (
        "Other rows were filtered out because they did not satisfy every "
        "requested criterion: 5."
    ) in manifest.body


def test_render_response_formats_metric_count_artifact_without_replanning() -> None:
    manifest = render_response(_plan(), _metric_count_result(), _valid_report())

    assert manifest.status == "rendered"
    assert manifest.result_type == "metric_count"
    assert manifest.body.startswith(
        "Of 3 covered districts with data, 2 match your criteria."
    )
    assert "| Metric | Count | Denominator | Filter |" in manifest.body
    assert (
        "| Minimum number of formal observations per evaluation cycle "
        "for non-tenured teachers | 2 | 3 | value >= 3 |"
    ) in manifest.body
    # count_resolved_catalog_ids dropped (operator-flavored); denominator code reworded (Track 3.3)
    assert "Denominator counts only districts with a current reviewed answer for this metric." in manifest.body
    # coverage_ratio=0.75 >= _COVERAGE_VERBOSE_THRESHOLD (0.7), so Coverage line suppressed
    assert "Coverage: 3 of 4 in-scope cells had current reviewed data." not in manifest.body
    assert "Average teacher starting salary" not in manifest.body
    assert "Notes" not in manifest.body
    # No selection on this fixture, so no matching-districts table is emitted.
    assert "Matching districts" not in manifest.body


def test_render_response_count_lead_shows_grounded_percent() -> None:
    """C3 (#1415): with percent grounded on the row, the count lead shows X (Y%).

    The percent is a real artifact value (count.py grounds it via _count_percent),
    so it renders in the centralized sentence AND survives the numeric-token-
    provenance validator (the reason C1 had to omit it).
    """
    row = _metric_count_result().rows[0].model_copy(
        update={"count": 20, "denominator": 100, "percent": 20.0}
    )
    result = _metric_count_result().model_copy(update={"rows": [row]})

    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.body.startswith(
        "Of 100 covered districts with data, 20 (20.0%) match your criteria."
    )
    report = validate_result(_plan(), result, rendered_body=manifest.body)
    offending = [
        finding
        for finding in report.findings
        if finding.code == "numeric_token_not_in_artifact"
    ]
    assert not offending, f"Grounded percent must pass provenance: {offending}"


def test_render_response_count_lead_omits_percent_for_small_sample() -> None:
    """C3 / #744: a tiny pool (<5) states counts only — no misleading percentage."""
    row = _metric_count_result().rows[0].model_copy(
        update={"count": 3, "denominator": 4, "percent": 75.0}
    )
    result = _metric_count_result().model_copy(update={"rows": [row]})

    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.body.startswith(
        "Of 4 covered districts with data, 3 match your criteria."
    )
    assert "(75.0%)" not in manifest.body


def test_render_response_single_district_count_pluralizes() -> None:
    """C3 / #744: a single-district count reads grammatically, no '1 (100%)'."""
    row = _metric_count_result().rows[0].model_copy(
        update={"count": 1, "denominator": 1, "percent": 100.0}
    )
    result = _metric_count_result().model_copy(update={"rows": [row]})

    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.body.startswith(
        "Of 1 covered district with data, 1 matches your criteria."
    )
    assert "(100.0%)" not in manifest.body


def test_render_response_keeps_covered_universe_count_free_of_policy_citations() -> None:
    manifest = render_response(
        _plan(),
        _covered_universe_count_result(),
        _valid_report(),
    )

    assert manifest.status == "rendered"
    assert "| Covered district universe | 2 | 2 | covered district universe |" in manifest.body
    assert "I counted the resolved covered-district selection." in manifest.body
    # covered_universe_selection_count reworded in Track 3.3
    assert "Count reflects all covered districts in the Compass policy universe." in manifest.body
    # coverage_ratio=1.0 >= _COVERAGE_VERBOSE_THRESHOLD (0.7), so Coverage line suppressed
    assert "Coverage: 2 of 2 in-scope cells had current reviewed data." not in manifest.body
    assert "Sources" not in manifest.body
    assert "policy-answer" not in manifest.body
    assert "[1]" not in manifest.body


def test_render_response_lists_matching_district_names() -> None:
    """C1 (#1413): a count answer lists the matching district names it holds.

    The result already carries qualifying_district_ids and a selection that maps
    those ids to display names. The renderer must surface them — as a markdown
    table, so the downstream voice pass (which can drop prose lists but not an
    immutable table block) cannot lose them — instead of discarding the names.
    """
    result = _metric_count_result().model_copy(
        update={
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                    SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
                ],
            )
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    # Plain match-denominator lead, not operator jargon.
    assert manifest.body.startswith("Of 3 covered districts with data, 2 match")
    # The two qualifying district names appear, in a markdown table.
    assert "Matching districts (2):" in manifest.body
    assert "| Alpha | CA |" in manifest.body
    assert "| Bravo | TX |" in manifest.body


def test_render_response_count_body_passes_numeric_token_provenance() -> None:
    """The rendered count body introduces no numeric token absent from the artifact.

    Regression guard for the live-pipeline reject (numeric_token_not_in_artifact)
    that render_response's pre-valid report otherwise masks: a derived value in
    the lead (e.g. a match percentage) would fail validate_result before the
    answer ever renders. Runs the REAL validator over the rendered body.
    """
    result = _metric_count_result().model_copy(
        update={
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                    SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
                ],
            )
        }
    )

    body = render_response(_plan(), result, _valid_report()).body
    report = validate_result(_plan(), result, rendered_body=body)

    offending = [
        finding
        for finding in report.findings
        if finding.code == "numeric_token_not_in_artifact"
    ]
    assert not offending, f"Untraceable numeric tokens in count body: {offending}"


def test_render_response_count_names_never_invent_unselected_id() -> None:
    """Grounding guard: a qualifying id absent from the selection is dropped.

    The renderer resolves names ONLY through result.selection.districts, so an
    id in qualifying_district_ids that the selection cannot name must never
    surface a (fabricated) row — the core "no invented names" invariant.
    """
    row = _metric_count_result().rows[0].model_copy(
        update={"qualifying_district_ids": [1, 999], "count": 1, "denominator": 5}
    )
    result = _metric_count_result().model_copy(
        update={
            "rows": [row],
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                ],
            ),
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    assert "Matching districts (1):" in manifest.body  # only the grounded id
    assert "| Alpha | CA |" in manifest.body
    assert "999" not in manifest.body  # the unselectable id is never rendered


def test_render_response_count_names_escape_pipe_and_blank_state() -> None:
    """District names are markdown-escaped and a null state renders an empty cell."""
    result = _metric_count_result().model_copy(
        update={
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(
                        district_id=1, district_name="North | South USD", state=None
                    ),
                    SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
                ],
            )
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    # The pipe in the name is escaped so the table column count stays intact.
    assert "| North \\| South USD |  |" in manifest.body
    assert "| Bravo | TX |" in manifest.body


def test_render_response_count_offers_to_list_when_over_limit() -> None:
    """Past the display cap the count offers to list rather than dumping names."""
    ids = list(range(1, 42))  # 41 > _MATCHING_DISTRICTS_DISPLAY_LIMIT
    row = _metric_count_result().rows[0].model_copy(
        update={"qualifying_district_ids": ids, "count": 41, "denominator": 41}
    )
    result = _metric_count_result().model_copy(
        update={
            "rows": [row],
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(
                        district_id=i, district_name=f"District {i:02d}", state="TX"
                    )
                    for i in ids
                ],
            ),
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    assert "41 districts match — ask me to name them" in manifest.body
    assert "| District | State |" not in manifest.body  # no names table dumped


def test_render_response_covered_universe_count_omits_names_table() -> None:
    """A covered-universe count has no filter, so it must not list 'matching' names.

    Even with a real selection and qualifying ids on the row, the no-filter
    universe count keeps its own lead and never renders the match/filter names
    block (gate: composer.is_single_filter_count).
    """
    result = _covered_universe_count_result().model_copy(
        update={
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(district_id=10, district_name="Ten ISD", state="TX"),
                    SelectedDistrict(
                        district_id=11, district_name="Eleven ISD", state="TX"
                    ),
                ],
            )
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    assert "I counted the resolved covered-district selection." in manifest.body
    assert "Matching districts" not in manifest.body
    assert "Ten ISD" not in manifest.body


def test_render_response_multi_metric_count_omits_names_table() -> None:
    """A multi-metric count has no single qualifying set, so it lists no names.

    The lead falls back to the generic sentence and the renderer suppresses the
    names table rather than conflating per-metric matches into one list.
    """
    row_a = _metric_count_result().rows[0]  # qualifying [1, 2]
    row_b = row_a.model_copy(
        update={
            "metric_id": 41,
            "metric_name": "Minimum number of informal observations",
            "qualifying_district_ids": [2, 3],
        }
    )
    result = _metric_count_result().model_copy(
        update={
            "rows": [row_a, row_b],
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                    SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
                    SelectedDistrict(district_id=3, district_name="Charlie", state="NY"),
                ],
            ),
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    assert manifest.body.startswith("I counted qualifying covered districts.")
    assert "Matching districts" not in manifest.body


def test_render_response_formats_categorical_count_table() -> None:
    result = MetricCountResult(        rows=[
            CategoricalCountRow(
                metric_id=232,
                metric_name="Does the district cover 100% of employees' health insurance premium?",
                value="Yes",
                category="Yes",
                display_value="2 of 3 covered districts",
                academic_year="2024 - 2025",
                count=2,
                denominator=3,
                percent=66.666667,                filter_statement="group by reviewed value",
                qualifying_district_ids=[1, 3],
                coverage_state="covered",
                coverage_display="2 of 3 covered districts",
                coverage_reason="categorical_value_count",
            ),
            CategoricalCountRow(
                metric_id=232,
                metric_name="Does the district cover 100% of employees' health insurance premium?",
                value="No",
                category="No",
                display_value="1 of 3 covered districts",
                academic_year="2024 - 2025",
                count=1,
                denominator=3,
                percent=33.333333,                filter_statement="group by reviewed value",
                qualifying_district_ids=[2],
                coverage_state="covered",
                coverage_display="1 of 3 covered districts",
                coverage_reason="categorical_value_count",
            ),
        ],
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=3,
            real_data_count=3,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        total_considered=3,
        excluded_count=0,
        order_statement="Counted health benefits type distribution.",
        methodology_codes=[
            MethodologyRef(code="categorical_count_grouped_current_values"),
        ],
    )

    manifest = render_response(
        QueryPlan(
            operation="count",
            question="Tell me how many districts offer each type of health benefits",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="health benefits type", role="grouping")],
        ),
        result,
        _valid_report(),
    )

    assert manifest.status == "rendered"
    assert manifest.body.startswith("Counted health benefits type distribution.")
    assert (
        "| Health benefits type | District count | Share of covered districts |"
        in manifest.body
    )
    assert "| Yes | 2 | 66.7% |" in manifest.body
    assert "| No | 1 | 33.3% |" in manifest.body
    assert "| Metric | Count | Denominator | Filter |" not in manifest.body


def test_categorical_count_header_falls_back_to_result_axis_after_rebalance() -> None:
    """Post-finalize regression: when ``_rebalance_metric_drawers`` moves a
    profile grouping axis out of ``plan.metrics`` (dropping ``role="grouping"``),
    the header must fall back to the executed result's resolved axis name
    instead of the generic ``"Category"`` placeholder.

    Simulated by a plan whose ``metrics`` carries no ``role="grouping"`` member
    (the rebalanced shape) while the result rows carry the resolved axis name.
    """
    result = MetricCountResult(
        rows=[
            CategoricalCountRow(
                metric_id=232,
                metric_name="Locale",
                value="Rural",
                category="Rural",
                display_value="2 of 3 covered districts",
                academic_year="2024 - 2025",
                count=2,
                denominator=3,
                percent=66.666667,
                filter_statement="group by reviewed value",
                qualifying_district_ids=[1, 3],
                coverage_state="covered",
                coverage_display="2 of 3 covered districts",
                coverage_reason="categorical_value_count",
            ),
            CategoricalCountRow(
                metric_id=232,
                metric_name="Locale",
                value="Urban",
                category="Urban",
                display_value="1 of 3 covered districts",
                academic_year="2024 - 2025",
                count=1,
                denominator=3,
                percent=33.333333,
                filter_statement="group by reviewed value",
                qualifying_district_ids=[2],
                coverage_state="covered",
                coverage_display="1 of 3 covered districts",
                coverage_reason="categorical_value_count",
            ),
        ],
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=3,
            real_data_count=3,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        total_considered=3,
        excluded_count=0,
        order_statement="Counted locale distribution.",
        methodology_codes=[
            MethodologyRef(code="categorical_count_grouped_current_values"),
        ],
    )

    manifest = render_response(
        QueryPlan(
            operation="count",
            question="Break districts down by locale",
            selection=SelectionSpec(scope="all_covered_districts"),
            # Rebalanced shape: the grouping axis was moved out of metrics, so
            # no metric carries role="grouping".
            metrics=[MetricSpec(name="Locale")],
        ),
        result,
        _valid_report(),
    )

    assert manifest.status == "rendered"
    assert manifest.body.startswith("Counted locale distribution.")
    assert (
        "| Locale | District count | Share of covered districts |"
        in manifest.body
    )
    assert "| Category |" not in manifest.body


def test_render_response_uses_canonical_coverage_phrase_in_lookup_table() -> None:
    result = _lookup_comparison_result()
    updated_rows = [
        *result.rows[:1],
        result.rows[1].model_copy(
            update={
                "value": "INA",
                "display_value": "Issue not addressed in the documents reviewed.",
                "coverage_state": "ina",
                "coverage_display": (
                    "Issue not addressed in the documents reviewed."
                ),
                "citation_markers": [],
            }
        ),
        *result.rows[2:],
    ]
    result = result.model_copy(update={"rows": updated_rows})

    manifest = render_response(
        _lookup_comparison_plan(),
        result,
        _valid_report(),
    )

    assert "Issue not addressed in the documents reviewed." in manifest.body
    assert "Not available" not in manifest.body


def test_render_response_summarizes_lookup_availability_without_current_claim() -> None:
    manifest = render_response(
        _lookup_named_plan(),
        _lookup_with_not_reviewed_result(),
        _valid_report(),
    )

    # #1514 D9: the lead counts districts, never "data points". The coverage
    # LABELING is unchanged by #1826 — Bravo is still counted as not reviewed for
    # the current year, even though (below) its prior value now renders in-table.
    assert manifest.body.startswith(
        "Of the 2 districts you asked about, 1 has current reviewed data; "
        "1 hasn't been reviewed for 2024 - 2025 yet."
    )
    assert "data points" not in manifest.body
    assert "Cells without current data stay visible in the table." not in manifest.body
    assert "| Alpha | CA | 2024 - 2025 | $50,000 | [1] |" in manifest.body
    # #1826: Bravo (stale) now renders IN the table — its prior-year value
    # ($60,000) under the year it was last reviewed (2023 - 2024), instead of
    # being demoted to a "NCTQ last reviewed …" prose sentence. The
    # Academic-year column annotates the staleness honestly beside Alpha's
    # current-year row.
    assert "| Bravo | CA | 2023 - 2024 | $60,000 |" in manifest.body
    assert "Older year only" not in manifest.body
    # The prior-year value moved in-table, so the narrative sentence that used
    # to carry it is no longer emitted (no double-voicing).
    assert "NCTQ last reviewed Bravo" not in manifest.body
    # With no non-answer rows left to narrate, the coverage-disclosure section
    # is gone; the answers table carries every requested district.
    assert "#### Districts without a current reviewed value" not in manifest.body
    # lookup_resolved_catalog_ids dropped (Track 3.3); lookup_default_district_order kept
    assert "Rows are ordered by district name unless a supported sort is requested." in manifest.body
    # #1228 #8: assert the H4 PREFIX, not a bare "Methodology" substring — a
    # dropped "#### " prefix would break the frontend fold and a bare check
    # would not catch it.
    assert "#### Methodology" in manifest.body
    assert "Notes" not in manifest.body


def test_render_response_leads_with_filter_prevalence_numeric() -> None:
    """#1337 / FILT-88: a metric-value-filtered list leads with the matched count
    + the policy-honest covered denominator, then discloses not-reviewed
    separately. Numbers come from the pre-narrow FilterPrevalenceSummary, never
    the post-narrow survivor count."""

    result = _lookup_with_not_reviewed_result().model_copy(
        update={
            "filter_prevalence": FilterPrevalenceSummary(
                matched=42,
                denominator=100,
                percent=42.0,
                not_reviewed_count=26,
                na_count=0,
            )
        }
    )

    manifest = render_response(_lookup_named_plan(), result, _valid_report())

    # Matched count + covered denominator + share, ending in the sealed phrase.
    assert (
        "Of 100 districts with a current value for Average teacher starting "
        "salary, 42 (42.0%) match your criteria." in manifest.body
    )
    # Not-reviewed disclosed as a separate count, never folded into 100.
    assert (
        "NCTQ hasn't reviewed 26 districts in this selection for "
        "2024 - 2025 yet." in manifest.body
    )
    # The prevalence lead precedes the answers table.
    assert manifest.body.index("match your criteria") < manifest.body.index(
        "| District | State |"
    )
    # The denominator is the covered count, not the full universe.
    assert "of 133" not in manifest.body.lower()


def test_render_response_filter_prevalence_categorical_discloses_na() -> None:
    """Categorical filters carry a 'not applicable' bucket excluded from the
    denominator and disclosed on its own line (e.g. parental-leave eligibility:
    38 of 40, with 42 NA)."""

    result = _lookup_with_not_reviewed_result().model_copy(
        update={
            "filter_prevalence": FilterPrevalenceSummary(
                matched=38,
                denominator=40,
                percent=95.0,
                not_reviewed_count=51,
                na_count=42,
            )
        }
    )

    manifest = render_response(_lookup_named_plan(), result, _valid_report())

    assert "38 (95.0%) match your criteria." in manifest.body
    assert "Of 40 districts with a current value for" in manifest.body
    assert "42 districts are marked not applicable for" in manifest.body
    assert (
        "NCTQ hasn't reviewed 51 districts in this selection for "
        "2024 - 2025 yet." in manifest.body
    )


def test_render_response_unfiltered_lookup_has_no_prevalence_lead() -> None:
    """No metric-value filter → filter_prevalence is None → the existing lookup
    lead is unchanged (no 'match your criteria' prevalence sentence)."""

    manifest = render_response(
        _lookup_named_plan(),
        _lookup_with_not_reviewed_result(),
        _valid_report(),
    )

    assert "match your criteria" not in manifest.body


def test_render_response_shows_coverage_line_for_sparse_result_below_threshold() -> None:
    """sparse_disclosure text is no longer appended by the composer (Track 3.3);
    the data-availability line carries the low-coverage signal when rate < 70%.
    #1514 D9: the line counts districts, never "data points".
    """
    base = _lookup_comparison_result()
    result = base.model_copy(
        update={
            "rows": [
                base.rows[0],
                _as_not_reviewed_lookup_row(base.rows[1]),
                _as_not_reviewed_lookup_row(base.rows[2]),
                _as_not_reviewed_lookup_row(base.rows[3]),
            ],
            "coverage_frame": CoverageFrame(
                universe_count=4,
                in_scope_count=4,
                addressed_count=1,
                real_data_count=1,
                not_reviewed_count=3,
                out_of_universe_count=0,
                coverage_ratio=0.25,
                sparse=True,
                sparse_disclosure=(
                    "Sparse coverage: 1 of 4 in-scope cells have current reviewed data."
                ),
            ),
        }
    )

    result = _with_rederived_district_coverage(result)

    manifest = render_response(_lookup_comparison_plan(), result, _valid_report())

    # Coverage rate 25% < 70% threshold, so the data-availability line is emitted.
    assert (
        "Data availability: Of the 2 districts you asked about, 1 has current "
        "reviewed data; 1 hasn't been reviewed for 2024 - 2025 yet."
        in manifest.body
    )
    assert "data points" not in manifest.body
    # sparse_disclosure is an artifact-layer field; composer no longer appends it separately
    assert "Sparse coverage:" not in manifest.body
    assert "Coverage:" not in manifest.body


def test_render_response_distinguishes_universe_from_out_of_pathfinder_names() -> None:
    """#1514 D9: the availability line counts districts and voices requested
    names outside the Pathfinder with the canonical aggregate phrasing."""

    base = _lookup_comparison_result()
    result = base.model_copy(
        update={
            "rows": [
                base.rows[0],
                base.rows[1],
                _as_not_reviewed_lookup_row(base.rows[2]),
                _as_not_reviewed_lookup_row(base.rows[3]),
                _out_of_universe_lookup_row(
                    "Ghost District",
                    9876,
                    "Average teacher starting salary",
                ),
            ],
            "coverage_frame": CoverageFrame(
                universe_count=5,
                in_scope_count=4,
                addressed_count=2,
                real_data_count=2,
                not_reviewed_count=2,
                out_of_universe_count=1,
                coverage_ratio=0.5,
                sparse=False,
            ),
        }
    )

    result = _with_rederived_district_coverage(result)

    manifest = render_response(_lookup_comparison_plan(), result, _valid_report())

    assert (
        "Data availability: Of the 3 districts you asked about, 1 has current "
        "reviewed data; 1 hasn't been reviewed for 2024 - 2025 yet. 1 requested "
        "name was outside the District Policy Pathfinder."
        in manifest.body
    )
    assert "data point" not in manifest.body
    assert "in-scope cells" not in manifest.body


def test_render_response_keeps_multiple_out_of_universe_districts_separate() -> None:
    rows = [
        _out_of_universe_lookup_row(
            "Unknown Alpha",
            9876,
            "Average teacher starting salary",
            state="CA",
        ),
        _out_of_universe_lookup_row(
            "Unknown Alpha",
            4321,
            "Collective bargaining status",
            state="CA",
        ),
        _out_of_universe_lookup_row(
            "Unknown Bravo",
            9876,
            "Average teacher starting salary",
            state="CA",
        ),
        _out_of_universe_lookup_row(
            "Unknown Bravo",
            4321,
            "Collective bargaining status",
            state="CA",
        ),
    ]
    result = _lookup_comparison_result().model_copy(
        update={
            "selection": ResultSelection(
                scope="named_districts",
                districts=[],
                unresolved_districts=["Unknown Alpha", "Unknown Bravo"],
            ),
            "rows": rows,
            "citations": [],
            "coverage_frame": CoverageFrame(
                universe_count=4,
                in_scope_count=0,
                addressed_count=0,
                real_data_count=0,
                not_reviewed_count=0,
                out_of_universe_count=4,
                coverage_ratio=0,
                sparse=False,
            ),
        }
    )

    manifest = render_response(_lookup_comparison_plan(), result, _valid_report())

    # #1514: out-of-Pathfinder names never become table rows — the body is
    # narrative-only (no header-only table) with ONE canonical with-state
    # sentence per requested district (D6), deduped across its metric rows.
    assert "|" not in manifest.body.split("Methodology")[0]
    assert (
        manifest.body.count(
            "Unknown Alpha, CA is not in the District Policy Pathfinder."
        )
        == 1
    )
    assert (
        manifest.body.count(
            "Unknown Bravo, CA is not in the District Policy Pathfinder."
        )
        == 1
    )
    # The retired short label is gone for good.
    assert "Out of Pathfinder" not in manifest.body


def test_coverage_summary_omitted_when_coverage_rate_above_70_percent() -> None:
    """When coverage_ratio >= 0.7, the data-availability line is not emitted.

    The lead paragraph already says "I found current reviewed data for N of M
    requested data points"; repeating it at >= 70% coverage is boilerplate.
    """
    result = _lookup_comparison_result().model_copy(
        update={
            "coverage_frame": CoverageFrame(
                universe_count=10,
                in_scope_count=10,
                addressed_count=8,
                real_data_count=8,
                not_reviewed_count=2,
                out_of_universe_count=0,
                coverage_ratio=0.8,
                sparse=False,
            ),
            "methodology_codes": [MethodologyRef(code="lookup_default_district_order")],
        }
    )

    manifest = render_response(_lookup_comparison_plan(), result, _valid_report())

    assert "Coverage: " not in manifest.body
    assert "Data availability:" not in manifest.body


def test_coverage_summary_emitted_when_coverage_rate_below_70_percent() -> None:
    """When coverage_ratio < 0.7, the availability line is emitted for context.

    #1514 D9: the line counts districts you asked about, never data points.
    """
    base = _lookup_comparison_result()
    result = base.model_copy(
        update={
            "rows": [
                base.rows[0],
                base.rows[1],
                _as_not_reviewed_lookup_row(base.rows[2]),
                _as_not_reviewed_lookup_row(base.rows[3]),
            ],
            "coverage_frame": CoverageFrame(
                universe_count=10,
                in_scope_count=10,
                addressed_count=6,
                real_data_count=6,
                not_reviewed_count=4,
                out_of_universe_count=0,
                coverage_ratio=0.6,
                sparse=False,
            ),
            "methodology_codes": [MethodologyRef(code="lookup_default_district_order")],
        }
    )

    result = _with_rederived_district_coverage(result)

    manifest = render_response(_lookup_comparison_plan(), result, _valid_report())

    assert (
        "Data availability: Of the 2 districts you asked about, 1 has current "
        "reviewed data; 1 hasn't been reviewed for 2024 - 2025 yet."
        in manifest.body
    )
    assert "data points" not in manifest.body


def test_methodology_lines_does_not_emit_excluded_rows_for_ranking() -> None:
    """Ranking results suppress 'Excluded rows: N' — the availability
    block already conveys why rows were excluded from the ranking.
    """
    result = _partial_ranking_result()
    assert result.excluded_count == 2, "fixture sanity: excluded_count must be > 0"

    manifest = render_response(_named_plan(), result, _valid_report())

    assert "Excluded rows:" not in manifest.body


def test_methodology_lines_emits_plain_exclusion_summary_for_lookup() -> None:
    """Lookup/intersection results explain filtered rows without backend wording."""
    result = _lookup_comparison_result().model_copy(
        update={
            "excluded_count": 5,
            "methodology_codes": [MethodologyRef(code="lookup_default_district_order")],
        }
    )

    manifest = render_response(_lookup_comparison_plan(), result, _valid_report())

    assert "Other rows were filtered out before rendering: 5." in manifest.body
    assert "Excluded rows:" not in manifest.body


def _out_of_universe_lookup_row(
    district_name: str,
    metric_id: int,
    metric_name: str,
    state: str | None = None,
) -> MetricValueRow:
    # #1514 D6: execution derives the state where it can, producing the
    # "[District, State] is not in the District Policy Pathfinder." sentence.
    display_name = f"{district_name}, {state}" if state else district_name
    display_value = f"{display_name} is not in the District Policy Pathfinder."
    return MetricValueRow(
        district_id=None,
        district_name=district_name,
        state=state,
        metric_id=metric_id,
        metric_name=metric_name,
        value=None,
        display_value=display_value,
        academic_year="2024 - 2025",
        source="coverage_state",
        citation_markers=[],
        coverage_state="out_of_universe",
        coverage_display=display_value,
        coverage_reason="out_of_universe",
    )


def _with_rederived_district_coverage(result: ResultSet) -> ResultSet:
    """Re-derive the serialized district summary after a model_copy row swap.

    ``model_copy(update={"rows": ...})`` does not re-run the populate
    validator, so ``district_coverage`` would stay stale at the base
    fixture's tallies. The composer reads ONLY the serialized summary
    (#1514 — numeric-token provenance), so tests that rewrite rows pass
    through here, re-deriving with the same one artifact-layer function.
    """

    return result.model_copy(
        update={
            "district_coverage": district_coverage_summary_for_rows(
                result.rows, result.selection
            )
        }
    )


def _as_not_reviewed_lookup_row(row: MetricValueRow) -> MetricValueRow:
    """Turn a covered fixture row into its canonical not-reviewed shape."""

    display = (
        f"NCTQ hasn't reviewed {row.district_name} for {row.metric_name} "
        f"in {row.academic_year} yet."
    )
    return row.model_copy(
        update={
            "value": None,
            "display_value": display,
            "source": "coverage_state",
            "citation_markers": [],
            "coverage_state": "not_reviewed",
            "coverage_display": display,
            "coverage_reason": "metric_not_reviewed",
        }
    )


def test_renderer_requested_states_includes_named_districts_state_constraint() -> None:
    """Regression: renderer must not silently drop ``selection.states`` for
    ``scope='named_districts'`` plans.

    The renderer previously kept its own copy of ``_requested_states`` that
    gated state extraction on ``scope in {"state", "largest_districts"}``,
    which dropped the state constraint on plans like
    "Compare Bravo and Charlie in California". The canonical helper in
    ``compass_backend.execution.selection`` always emits ``selection.states``
    when present; the renderer must use the canonical version.

    Since #1419 the writer's state-extraction site moved into the shared
    ``rendering.shared.selection_label`` helper (writer's duplicate
    ``_selection_label`` was deleted), so this guard now pins the canonical
    alias in ``rendering.composer`` and ``rendering.shared`` — the two renderer
    modules that still extract requested states — instead of in ``writer``.
    """

    from compass_backend.execution.selection import requested_states
    from compass_backend.rendering.composer import (
        _requested_states as composer_requested_states,
    )
    from compass_backend.rendering.shared import (
        _requested_states as shared_requested_states,
    )

    plan = QueryPlan(
        question="Rank Bravo and Charlie in California by starting salary.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Bravo", "Charlie"],
            states=["CA"],
        ),
        metrics=[MetricSpec(name="starting salary")],
    )

    # Canonical helper emits selection.states regardless of scope.
    assert requested_states(plan) == {"CA"}

    # Renderer aliases resolve to the canonical helper — no scope gating.
    assert composer_requested_states is requested_states
    assert shared_requested_states is requested_states


def test_direction_uses_selection_sort_step_when_plan_sort_missing() -> None:
    """Regression: ``direction(plan)`` must consult ``sort_steps`` when
    ``plan.sort`` is absent (audit finding #13).

    Three of the four pre-canonicalization copies skipped ``sort_steps`` and
    fell through to the conventional ``"desc"`` default, silently disagreeing
    with the validator helper. The canonical version reads the first
    ``selection``-phase ``SortStepSpec`` direction in that situation.
    """

    from compass_backend.execution.selection import direction

    plan = QueryPlan(
        question="List the smallest districts in California.",
        selection=SelectionSpec(scope="largest_districts", states=["CA"]),
        metrics=[MetricSpec(name="enrollment")],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="enrollment",
                direction="asc",
                key_type="profile_field",
            )
        ],
    )

    # plan.sort is None but a selection-phase sort_step says "asc" — the
    # canonical helper honors it instead of falling back to "desc".
    assert plan.sort is None
    assert direction(plan) == "asc"


def test_parse_compass_numeric_value_rejects_trailing_garbage() -> None:
    """Regression: ``parse_compass_numeric_value("12.5%abc")`` returns ``None``
    (audit finding #14).

    The previous ``operations.py._parse_numeric`` used ``.rstrip("%")`` and
    silently accepted trailing garbage as ``12.5``, while the regex-based
    parsers in ranking/lookup/validation rejected the same input — a
    validator-vs-executor disagreement on what counts as numeric. The
    canonical helper enforces the strict anchored pattern.
    """

    from compass_backend.execution._text_utils import parse_compass_numeric_value

    # The bug-fix invariant: malformed inputs return None.
    assert parse_compass_numeric_value("12.5%abc") is None
    assert parse_compass_numeric_value("$100xyz") is None
    assert parse_compass_numeric_value("not a number") is None

    # Well-formed inputs still parse.
    assert parse_compass_numeric_value("12.5%") == 12.5
    assert parse_compass_numeric_value("$1,234") == 1234.0
    assert parse_compass_numeric_value("-5.75") == -5.75
    assert parse_compass_numeric_value(42) == 42.0
    assert parse_compass_numeric_value(None) is None
    assert parse_compass_numeric_value(True) is None


# ---------------------------------------------------------------------------
# #942 — filtered-lookup district denominator sentence
# ---------------------------------------------------------------------------


def _small_districts_lookup(total: int, with_data: int) -> MetricLookupResult:
    """A FILT-88-shaped lookup: `total` small districts, `with_data` covered."""
    districts = [
        SelectedDistrict(district_id=100 + i, district_name=f"Small ISD {i}", state="TX")
        for i in range(total)
    ]
    rows = [
        MetricValueRow(
            district_id=d.district_id,
            district_name=d.district_name,
            state="TX",
            metric_id=39,
            metric_name="Minimum number of formal observations",
            value="3" if i < with_data else None,
            display_value="3" if i < with_data else "Not reviewed",
            academic_year="2024 - 2025",
            source="policy_answer" if i < with_data else "coverage_state",
            coverage_state="covered" if i < with_data else "not_reviewed",
            coverage_display="3" if i < with_data else "Not reviewed",
            coverage_reason="answer_value" if i < with_data else "metric_not_reviewed",
        )
        for i, d in enumerate(districts)
    ]
    return MetricLookupResult(
        selection=ResultSelection(scope="all_covered_districts", districts=districts),
        rows=rows,
        citations=[],
        total_considered=len(rows),
        excluded_count=0,
        order_statement="Looked up.",
    )


def _small_districts_plan() -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="What do districts with fewer than 10,000 students do for evaluation?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations")],
        filters=[
            FilterSpec(kind="enrollment", field="enrollment", operator="less_than", value=10000)
        ],
    )


def test_lookup_result_autocomputes_list_coverage() -> None:
    """#942: the lookup result grounds district-level N / X / % on list_coverage."""
    result = _small_districts_lookup(total=6, with_data=4)

    assert result.list_coverage is not None
    assert result.list_coverage.district_total == 6
    assert result.list_coverage.district_with_data == 4
    assert result.list_coverage.percent == 66.7


def test_filtered_lookup_leads_with_district_denominator() -> None:
    """#942: a small-districts lookup leads with the district denominator pattern."""
    manifest = render_response(
        _small_districts_plan(), _small_districts_lookup(total=6, with_data=4), _valid_report()
    )

    assert manifest.body.startswith(
        "Of the 6 small districts, 4 (66.7%) have current evaluation data."
    )


def test_filtered_lookup_denominator_passes_numeric_token_provenance() -> None:
    """#942: every number in the denominator sentence is grounded on the artifact.

    The district-level N / X / % live nowhere on the cell-level coverage_frame,
    so they are stored on list_coverage — without it the share (and counts)
    would fail validation the way C1's percent did.
    """
    result = _small_districts_lookup(total=6, with_data=4)
    body = render_response(_small_districts_plan(), result, _valid_report()).body

    report = validate_result(_small_districts_plan(), result, rendered_body=body)
    offending = [
        finding for finding in report.findings
        if finding.code == "numeric_token_not_in_artifact"
    ]
    assert not offending, f"Untraceable numeric tokens: {offending}"


def test_filtered_lookup_omits_percent_for_small_sample() -> None:
    """#942 / #744: a <5-district pool states counts only and notes the small sample."""
    manifest = render_response(
        _small_districts_plan(), _small_districts_lookup(total=4, with_data=3), _valid_report()
    )

    assert manifest.body.startswith(
        "Of the 4 small districts, 3 have current evaluation data."
    )
    assert "%" not in manifest.body.splitlines()[0]
    assert "Small sample (fewer than five districts)" in manifest.body


def test_filtered_lookup_single_district_pluralizes() -> None:
    """#942 / #744: a single-district pool reads grammatically and drops the %."""
    manifest = render_response(
        _small_districts_plan(), _small_districts_lookup(total=1, with_data=1), _valid_report()
    )

    assert manifest.body.startswith(
        "Of the 1 small district, 1 has current evaluation data."
    )


def test_unfiltered_lookup_keeps_existing_lead() -> None:
    """#942 guard: a lookup with no size filter does NOT get the scope-noun
    denominator lead ("Of the 6 small districts, …"). With two districts
    unreviewed it gets the #1514 D9 district-counting lead instead.

    #1827: the fixture selection is ``all_covered_districts`` (a system-derived
    universe, not a user-named list), so the D9 preamble now reads "Of the 6
    districts Compass covers, …" rather than mis-crediting the user with "you
    asked about"."""
    plan = QueryPlan(
        operation="lookup",
        question="Look up formal observations for these districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations")],
    )
    manifest = render_response(plan, _small_districts_lookup(total=6, with_data=4), _valid_report())

    assert "small districts" not in manifest.body
    assert manifest.body.startswith(
        "Of the 6 districts Compass covers, 4 have current reviewed data; "
        "2 haven't been reviewed for 2024 - 2025 yet."
    )


def test_filtered_lookup_small_sample_passes_provenance() -> None:
    """#942: the <5 small-sample note carries no ungrounded numeric token.

    Regression guard for the literal-"5" blocker — the note spells the
    threshold, so a small-district lookup (the FILT-88 case) still renders
    instead of hitting the numeric_token_not_in_artifact fallback.
    """
    for total, with_data in ((4, 3), (1, 1)):
        result = _small_districts_lookup(total=total, with_data=with_data)
        body = render_response(_small_districts_plan(), result, _valid_report()).body
        report = validate_result(_small_districts_plan(), result, rendered_body=body)
        offending = [
            f for f in report.findings if f.code == "numeric_token_not_in_artifact"
        ]
        assert not offending, f"total={total}: untraceable tokens {offending}"


def test_filtered_lookup_denominator_is_voice_immutable() -> None:
    """#942: the denominator sentence is sealed so the voice pass can't drop it."""
    from compass_backend.answer_layer.briefs import canonical_caveat_fragments

    body = render_response(
        _small_districts_plan(), _small_districts_lookup(total=6, with_data=4), _valid_report()
    ).body

    fragments = canonical_caveat_fragments(body)
    assert any("current evaluation data" in f for f in fragments), (
        f"denominator sentence must be a preserved caveat fragment: {fragments}"
    )


def test_criteria_lookup_with_enrollment_filter_keeps_criteria_lead() -> None:
    """#942 guard: an intersection lookup keeps its criteria lead even when a
    size filter is present — it is not reframed as a coverage-rate sentence."""
    result = _small_districts_lookup(total=6, with_data=4).model_copy(
        update={
            "criteria": [
                ResultCriterion(
                    criterion_id="c1",
                    label="a mentoring policy",
                    metric_ids=[171],
                    qualifying_district_ids=[100],
                )
            ]
        }
    )
    manifest = render_response(_small_districts_plan(), result, _valid_report())

    assert "satisfy all requested criteria" in manifest.body
    assert not manifest.body.startswith("Of the 6 small districts")


# ---------------------------------------------------------------------------
# C4 (#1416) — fact-coverage green proofs: today's renderer output passes
# ---------------------------------------------------------------------------


def _fact_coverage_findings(plan, result, body):
    report = validate_result(plan, result, rendered_body=body)
    return [f for f in report.findings if f.dimension == "fact_coverage"]


def test_render_response_count_body_passes_fact_coverage() -> None:
    """C4 green: the post-C1/C3 count body carries count, denominator, names."""
    result = _metric_count_result().model_copy(
        update={
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                    SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
                ],
            )
        }
    )

    body = render_response(_plan(), result, _valid_report()).body

    assert _fact_coverage_findings(_plan(), result, body) == []


def test_render_response_over_limit_offer_passes_fact_coverage() -> None:
    """C4 green: past the display cap, the offer line satisfies the guard."""
    ids = list(range(1, 42))
    row = _metric_count_result().rows[0].model_copy(
        update={"qualifying_district_ids": ids, "count": 41, "denominator": 41}
    )
    result = _metric_count_result().model_copy(
        update={
            "rows": [row],
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(
                        district_id=i, district_name=f"District {i:02d}", state="TX"
                    )
                    for i in ids
                ],
            ),
        }
    )

    body = render_response(_plan(), result, _valid_report()).body

    assert _fact_coverage_findings(_plan(), result, body) == []


def test_render_response_filtered_lookup_passes_fact_coverage() -> None:
    """C4 green: the #942 filtered-lookup denominator lead satisfies the guard."""
    result = _small_districts_lookup(total=6, with_data=4)
    plan = _small_districts_plan()

    body = render_response(plan, result, _valid_report()).body

    assert _fact_coverage_findings(plan, result, body) == []


def test_lossy_count_body_fails_fact_coverage_round_trip() -> None:
    """C4 red sentinel: strip the names table from today's body -> guard fires.

    This is the forward-looking proof the issue asks for: the day a renderer
    change drops the names again, this fails — without depending on any
    pre-C1 code.
    """
    result = _metric_count_result().model_copy(
        update={
            "selection": ResultSelection(
                scope="all_covered_districts",
                districts=[
                    SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                    SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
                ],
            )
        }
    )
    body = render_response(_plan(), result, _valid_report()).body
    lossy = "\n".join(
        line
        for line in body.splitlines()
        if "Alpha" not in line and "Bravo" not in line and "Matching districts" not in line
    )

    codes = [f.code for f in _fact_coverage_findings(_plan(), result, lossy)]
    assert codes == ["count_names_not_surfaced"]


def test_render_response_partial_ranking_passes_fact_coverage() -> None:
    """C4 green: a coverage-disclosing ranking's lead carries rankable-of-in-scope.

    Pins the single-sourced rankable formula (artifacts/coverage.py) — if the
    composer and the guard ever computed it differently, this round trip fails.
    """
    result = _partial_ranking_result()
    plan = _named_plan()

    body = render_response(plan, result, _valid_report()).body

    assert _fact_coverage_findings(plan, result, body) == []

# ─── #1436: numeric-data sentences use current_numeric_count ─────────────────


def test_ranking_lead_uses_current_numeric_count_not_real_data_count() -> None:
    """Ranking lead reports the genuine numeric count, not all covered rows.

    When a ranking has both numeric covered rows (answer_value) and non-numeric
    covered rows (non_numeric_rank_exclusion), the lead sentence "I found current
    reviewed numeric data for N of M" must use N = current_numeric_count
    (answer_value rows only), not real_data_count (all covered rows).

    Before #1436 a case like 464 could say "74 of 133" when only ~10 districts
    held a rankable numeric value — the non-rankable text answers were inflating
    the headline count.
    """

    result = _texas_observation_preview_result()
    # Sanity: fixture has real_data_count=14 with 1 non_numeric_rank_exclusion row.
    assert result.coverage_frame is not None
    assert result.coverage_frame.real_data_count == 14
    assert result.coverage_frame.breakdown.non_numeric_rank_exclusion_count == 1
    assert result.coverage_frame.current_numeric_count == 13

    plan = _state_plan().model_copy(
        update={
            "question": "Show me observation counts for Texas districts, lowest first.",
            "selection": SelectionSpec(scope="state", states=["TX"]),
        }
    )
    manifest = render_response(plan, result, _valid_report())

    # current_numeric_count (13), not real_data_count (14), appears in the lead.
    assert "I found current reviewed numeric data for 13 of 21" in manifest.body
    assert "I found current reviewed numeric data for 14 of 21" not in manifest.body


def test_data_availability_summary_counts_districts() -> None:
    """#1514 D9: the availability line counts DISTRICTS, never cells or
    "data points". A district with at least one answer cell — numeric or not
    — counts as having current reviewed data; a district whose only state is
    not_reviewed counts in the "haven't been reviewed" clause.
    """

    base = _lookup_comparison_result()
    charlie_not_reviewed = _as_not_reviewed_lookup_row(
        base.rows[0].model_copy(
            update={"district_id": 3, "district_name": "Charlie"}
        )
    )
    result = base.model_copy(
        update={
            "rows": [
                base.rows[0],
                base.rows[1],
                _as_not_reviewed_lookup_row(base.rows[2]),
                base.rows[3],
                charlie_not_reviewed,
            ],
            "coverage_frame": CoverageFrame(
                universe_count=5,
                in_scope_count=5,
                addressed_count=3,
                real_data_count=3,
                not_reviewed_count=2,
                out_of_universe_count=0,
                coverage_ratio=3 / 5,
                breakdown=CoverageBreakdown(
                    answer_value_count=3,
                    metric_not_reviewed_count=2,
                ),
            ),
            "methodology_codes": [MethodologyRef(code="lookup_default_district_order")],
        }
    )

    result = _with_rederived_district_coverage(result)

    manifest = render_response(_lookup_comparison_plan(), result, _valid_report())

    # Bravo keeps one answer cell, so 2 of 3 districts have data; only
    # Charlie (zero answer cells) lands in the not-reviewed clause.
    assert (
        "Data availability: Of the 3 districts you asked about, 2 have current "
        "reviewed data; 1 hasn't been reviewed for 2024 - 2025 yet."
        in manifest.body
    )
    assert "data points" not in manifest.body


def test_multi_metric_lookup_district_tallies_pass_numeric_token_provenance() -> None:
    """#1514 review blocker A1 (reviewer repro): a multi-metric lookup with six
    resolved districts (3 covered / 3 not reviewed) plus one out-of-Pathfinder
    name narrates district tallies (7 asked / 3 with data / 3 not reviewed)
    that exist nowhere as cell counts. They must be serialized
    (``district_coverage``) or the error-severity
    ``numeric_token_not_in_artifact`` finding invalidates the whole answer and
    the user gets the "validation failed before rendering" apology."""

    rows: list[MetricValueRow] = []
    districts: list[SelectedDistrict] = []
    citations: list[CitationRef] = []
    for district_id, name in enumerate(
        ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"], start=1
    ):
        districts.append(
            SelectedDistrict(district_id=district_id, district_name=name, state="CA")
        )
        for metric_id, metric_name, value in (
            (9876, "Average teacher starting salary", "$50,000"),
            (4321, "Collective bargaining status", "Yes"),
        ):
            covered = district_id <= 3
            markers: list[int] = []
            if covered:
                marker = len(citations) + 1
                citations.append(
                    CitationRef(
                        marker=marker,
                        title=f"{name} Contract, 2024-2025",
                        district_id=district_id,
                    )
                )
                markers = [marker]
            row = MetricValueRow(
                district_id=district_id,
                district_name=name,
                state="CA",
                metric_id=metric_id,
                metric_name=metric_name,
                value=value,
                display_value=value,
                academic_year="2024 - 2025",
                citation_markers=markers,
                coverage_state="covered",
                coverage_display=value,
                coverage_reason="answer_value",
            )
            rows.append(row if covered else _as_not_reviewed_lookup_row(row))
        if name == "Echo":
            # Keep the rendered rows in district-name order (the lookup
            # sort contract): "Fakeville USD" sorts between Echo and Foxtrot.
            rows.append(
                _out_of_universe_lookup_row(
                    "Fakeville USD", 9876, "Average teacher starting salary"
                )
            )
    result = MetricLookupResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=districts,
            unresolved_districts=["Fakeville USD"],
        ),
        rows=rows,
        citations=citations,
        total_considered=len(rows),
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=13,
            in_scope_count=12,
            addressed_count=6,
            real_data_count=6,
            not_reviewed_count=6,
            out_of_universe_count=1,
            coverage_ratio=0.5,
            breakdown=CoverageBreakdown(
                answer_value_count=6,
                metric_not_reviewed_count=6,
                out_of_universe_count=1,
            ),
        ),
        order_statement="Looked up selected metrics for selected districts.",
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
    )

    assert result.district_coverage is not None
    assert result.district_coverage.districts_asked == 7
    assert result.district_coverage.districts_with_current_data == 3
    assert result.district_coverage.districts_not_reviewed == 3
    assert result.district_coverage.districts_out_of_universe == 1

    plan = QueryPlan(
        operation="lookup",
        question=(
            "Compare starting salary and collective bargaining for these "
            "seven districts."
        ),
        selection=SelectionSpec(
            scope="named_districts",
            districts=[*[d.district_name for d in districts], "Fakeville USD"],
        ),
        metrics=[
            MetricSpec(name="starting salary"),
            MetricSpec(name="collective bargaining", role="comparison"),
        ],
    )
    body = render_response(plan, result, _valid_report()).body

    assert (
        "Of the 7 districts you asked about, 3 have current reviewed data; "
        "3 haven't been reviewed for 2024 - 2025 yet."
    ) in body
    assert "Fakeville USD is not in the District Policy Pathfinder." in body

    report = validate_result(plan, result, rendered_body=body)
    offending = [
        finding
        for finding in report.findings
        if finding.code == "numeric_token_not_in_artifact"
    ]
    assert not offending, f"Untraceable numeric tokens: {offending}"
    assert report.valid, [
        (finding.code, finding.message) for finding in report.findings
    ]



# #1702 retired test_additional_scope_label_is_a_neutral_noun: the
# "NCTQ tracks N additional … not included in this ranking" mixed grand-total
# intro (and its _additional_scope_label helper) were dropped (KTD3) — once
# INA/N-A findings are partitioned out, that count would need a renderer-side
# subtraction with no backing field. Each gap reason now leads with its own
# field-backed counted clause instead.


# ---------------------------------------------------------------------------
# #1514 — coverage-state presentation policy: tables hold answers only
# ---------------------------------------------------------------------------


def test_trend_not_reviewed_years_narrated_out_of_universe_deduped() -> None:
    """#1514 D5: covered year-rows stay in the trend table; a not_reviewed
    year-row is narrated per year; an out-of-universe district collapses to
    ONE canonical sentence even though its rows repeat per requested year."""

    from compass_backend.artifacts import MetricTrendResult

    metric = "Average teacher starting salary"

    def _year_row(year: str, value: str | None, **overrides) -> MetricValueRow:
        defaults = dict(
            district_id=2,
            district_name="Bravo",
            state="CA",
            metric_id=9876,
            metric_name=metric,
            value=value,
            display_value=value or "missing",
            academic_year=year,
            coverage_state="covered",
            coverage_display=value or "missing",
            coverage_reason="answer_value",
        )
        defaults.update(overrides)
        return MetricValueRow(**defaults)

    not_reviewed_display = (
        f"NCTQ hasn't reviewed Bravo for {metric} in 2023 - 2024 yet."
    )
    ghost_display = "Ghost District, CA is not in the District Policy Pathfinder."
    rows = [
        _year_row("2022 - 2023", "$58,000"),
        _year_row(
            "2023 - 2024",
            None,
            display_value=not_reviewed_display,
            source="coverage_state",
            coverage_state="not_reviewed",
            coverage_display=not_reviewed_display,
            coverage_reason="metric_not_reviewed",
        ),
        _year_row("2024 - 2025", "$60,000"),
        *[
            _year_row(
                year,
                None,
                district_id=None,
                district_name="Ghost District",
                display_value=ghost_display,
                source="coverage_state",
                coverage_state="out_of_universe",
                coverage_display=ghost_display,
                coverage_reason="out_of_universe",
            )
            for year in ("2022 - 2023", "2023 - 2024", "2024 - 2025")
        ],
    ]
    result = MetricTrendResult(
        rows=rows,
        total_considered=6,
        excluded_count=0,
        order_statement="Built a chronological trend.",
    )

    manifest = render_response(
        QueryPlan(
            operation="trend",
            question="How has Bravo's starting salary changed since 2022?",
            selection=SelectionSpec(
                scope="named_districts", districts=["Bravo", "Ghost District"]
            ),
            metrics=[MetricSpec(name="starting salary")],
        ),
        result,
        _valid_report(),
    )

    # Covered year-rows stay.
    assert "| 2022 - 2023 | Bravo | CA | $58,000 |" in manifest.body
    assert "| 2024 - 2025 | Bravo | CA | $60,000 |" in manifest.body
    # The not_reviewed year leaves the table and is narrated for that year.
    assert "| 2023 - 2024 | Bravo" not in manifest.body
    assert not_reviewed_display in manifest.body
    # The out-of-universe district gets ONE sentence, not one per year, and
    # never a table row.
    assert manifest.body.count(ghost_display) == 1
    assert "| Ghost District" not in manifest.body
    assert "Out of Pathfinder" not in manifest.body


def test_mixed_ranking_keeps_honest_rank_gaps_and_narrates_dropped_row() -> None:
    """#1514 D4: a mixed (profile-ordered) ranking row without an answer for
    the displayed metric leaves the table; surviving ranks are never
    renumbered (CSV and follow-ups reference artifact ranks)."""

    base = _mixed_ranking_result()
    not_reviewed_display = (
        "NCTQ hasn't reviewed Bravo for Average teacher starting salary "
        "in 2024 - 2025 yet."
    )
    third_row = base.rows[1].model_copy(
        update={
            "district_id": 3,
            "district_name": "Charlie",
            "rank": 3,
            "sort_value": 70.0,
            "sort_display_value": "70%",
            "citation_markers": [],
        }
    )
    rows = [
        base.rows[0],
        base.rows[1].model_copy(
            update={
                "value": None,
                "display_value": not_reviewed_display,
                "source": "coverage_state",
                "citation_markers": [],
                "coverage_state": "not_reviewed",
                "coverage_display": not_reviewed_display,
                "coverage_reason": "metric_not_reviewed",
            }
        ),
        third_row,
    ]
    result = base.model_copy(
        update={
            "rows": rows,
            "total_considered": 3,
            # #1702: honest breakdown — the one promoted metric_not_reviewed gap
            # (Bravo) backs the counted clause's "1" with a serialized field.
            "coverage_frame": base.coverage_frame.model_copy(
                update={
                    "in_scope_count": 3,
                    "universe_count": 3,
                    "addressed_count": 2,
                    "real_data_count": 2,
                    "not_reviewed_count": 1,
                    "coverage_ratio": 2 / 3,
                    "breakdown": CoverageBreakdown(
                        answer_value_count=2,
                        metric_not_reviewed_count=1,
                    ),
                }
            ),
        }
    )

    manifest = render_response(_plan(), result, _valid_report())

    # Rank 2 (Bravo) left the table; ranks 1 and 3 keep their honest gap.
    assert "| 1 | Alpha | CA | 90% | $50,000 |" in manifest.body
    assert "| 3 | Charlie | CA | 70% | $70,000 |" in manifest.body
    assert "| 2 | Bravo" not in manifest.body
    # #1702: the dropped not-reviewed district is COUNTED, not named per-district.
    assert "Not included in ranking" in manifest.body
    assert (
        "- NCTQ hasn't reviewed 1 district for the requested year yet."
        in manifest.body
    )
    assert "NCTQ hasn't reviewed Bravo" not in manifest.body
    assert "Bravo: Not reviewed" not in manifest.body


def test_render_response_categorical_count_routes_not_reviewed_to_narrative() -> None:
    """#1514 D12: the Not reviewed / Unavailable buckets leave the table and
    their district counts are voiced as narrative sentences."""

    covered_row = CategoricalCountRow(
        metric_id=232,
        metric_name="Does the district cover 100% of employees' health insurance premium?",
        value="Yes",
        category="Yes",
        display_value="3 of 6 covered districts",
        academic_year="2024 - 2025",
        count=3,
        denominator=6,
        percent=50.0,
        filter_statement="group by reviewed value",
        qualifying_district_ids=[1, 2, 3],
        coverage_state="covered",
        coverage_display="3 of 6 covered districts",
        coverage_reason="categorical_value_count",
    )
    unavailable_row = covered_row.model_copy(
        update={
            "value": "Unavailable",
            "category": "Unavailable",
            "display_value": "1 of 6 covered districts",
            "count": 1,
            "percent": 16.666667,
            "qualifying_district_ids": [5],
            "coverage_state": "not_reviewed",
            "coverage_reason": "unavailable",
        }
    )
    not_reviewed_row = covered_row.model_copy(
        update={
            "value": "Not reviewed",
            "category": "Not reviewed",
            "display_value": "2 of 6 covered districts",
            "count": 2,
            "percent": 33.333333,
            "qualifying_district_ids": [4, 6],
            "coverage_state": "not_reviewed",
            "coverage_reason": "metric_not_reviewed",
        }
    )
    result = MetricCountResult(
        rows=[covered_row, unavailable_row, not_reviewed_row],
        coverage_frame=CoverageFrame(
            universe_count=6,
            in_scope_count=6,
            addressed_count=3,
            real_data_count=3,
            not_reviewed_count=3,
            out_of_universe_count=0,
            coverage_ratio=0.5,
        ),
        total_considered=6,
        excluded_count=0,
        order_statement="Counted health benefits type distribution.",
        methodology_codes=[
            MethodologyRef(code="categorical_count_grouped_current_values"),
        ],
    )

    manifest = render_response(
        QueryPlan(
            operation="count",
            question="How many districts cover the full premium?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="health benefits type", role="grouping")],
        ),
        result,
        _valid_report(),
    )

    # Answer buckets stay tabular; non-answer buckets leave the table.
    assert "| Yes | 3 | 50.0% |" in manifest.body
    assert "| Not reviewed |" not in manifest.body
    assert "| Unavailable |" not in manifest.body
    # Their district counts are narrated (row.count IS a district count).
    assert (
        "NCTQ hasn't reviewed 2 of the 6 covered districts for this metric "
        "in 2024 - 2025 yet."
    ) in manifest.body
    assert (
        "The reviewed value is unavailable for 1 of the 6 covered districts."
    ) in manifest.body


def test_pivot_lookup_drops_zero_answer_district_with_one_sentence() -> None:
    """#1514 D3: in a multi-metric pivot, a district with >= 1 answer cell
    keeps its row (answerless cells render ""); a district with zero answer
    cells drops from the table with exactly ONE prose sentence."""

    base = _lookup_comparison_result()
    rows = [
        base.rows[0],
        _as_not_reviewed_lookup_row(base.rows[1]),
        _as_not_reviewed_lookup_row(base.rows[2]),
        _as_not_reviewed_lookup_row(base.rows[3]),
    ]
    result = base.model_copy(update={"rows": rows})

    manifest = render_response(_lookup_comparison_plan(), result, _valid_report())

    # Alpha keeps its row; the answerless bargaining cell renders "".
    assert "| Alpha | CA | $50,000 |  | [1] |" in manifest.body
    # Bravo (zero answer cells) drops from the table with ONE sentence.
    assert "| Bravo" not in manifest.body
    bravo_sentences = [
        line
        for line in manifest.body.splitlines()
        if "NCTQ hasn't reviewed Bravo" in line
    ]
    assert len(bravo_sentences) == 1
