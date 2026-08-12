"""Tests for MVP 6 deterministic result validation."""

from collections import Counter

from compass_backend.artifacts import (
    CitationRef,
    CountRow,
    CoverageBreakdown,
    CoverageDisclosure,
    CoverageFrame,
    MethodologyRef,
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricValueRow,
    PeerComparisonResult,
    PeerComparisonRow,
    ProfileLookupResult,
    ProfileValueRow,
    RankingRow,
    ResultSelection,
    ResultSet,
    SelectedDistrict,
    ThresholdCountRow,
)
from compass_backend.contracts import (
    LimitSpec,
    MetricSpec,
    QueryPlan,
    SelectionSpec,
    SimilarityQuerySpec,
    SortSpec,
    SortStepSpec,
)
from compass_backend.contracts.validation import (
    ResolvedMetricAuthority,
    ResolvedProfileFieldAuthority,
    ResolvedSelectionAuthority,
    ValidationAuthority,
)
from compass_backend.quality import validate_result
from compass_backend.quality.validators.metrics import _ranking_result_metric_specs
from compass_backend.rendering import render_response


def _plan(
    *,
    limit: LimitSpec | None = None,
    scope: str = "all_covered_districts",
) -> QueryPlan:
    return QueryPlan(
        question="Rank covered districts by starting salary.",
        selection=SelectionSpec(scope=scope, states=["CA"] if scope == "state" else []),
        metrics=[MetricSpec(name="starting salary")],
        limit=limit,
    )


def _state_count_plan(*, states: list[str] | None = None) -> QueryPlan:
    return QueryPlan(
        operation="count",
        question="Starting salary comparison for the selected state.",
        selection=SelectionSpec(
            scope="state",
            states=states or ["TX"],
        ),
        metrics=[MetricSpec(name="starting salary")],
    )


def test_validation_report_includes_only_executed_first_pass_dimensions() -> None:
    report = validate_result(_plan(), _result([_row(1, "Alpha", 50000, rank=1)]))

    assert report.dimensions_checked == [
        "selection",
        "metric",
        "sort_order",
        "surface_consistency",
        "coverage_state",
        "citation_coverage",
    ]


def test_validation_report_omits_surface_when_no_surface_validator_runs() -> None:
    plan = QueryPlan(
        question="Rank selected districts by starting salary.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="starting salary")],
    )
    result = _result([_row(1, "Alpha", 50000, rank=1)]).model_copy(
        update={"csv_export": None}
    )

    report = validate_result(plan, result)

    assert "surface_consistency" not in report.dimensions_checked


def test_lookup_validation_reports_surface_when_csv_parity_runs() -> None:
    result = MetricLookupResult(
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=89,
                metric_name="Starting salary",
                value=50000,
                display_value="$50,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up selected districts.",
    )

    report = validate_result(
        QueryPlan(
            operation="lookup",
            question="What is Alpha's starting salary?",
            selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
            metrics=[MetricSpec(name="starting salary")],
        ),
        result,
    )

    assert "surface_consistency" in report.dimensions_checked


def test_post_render_validation_adds_numeric_token_provenance_dimension() -> None:
    result = _result([_row(1, "Alpha", 50000, rank=1)])

    # The body carries the answer-row table the real writer emits — under
    # #1514 (Fix B) a table-less body with answer rows expected is a loud
    # markdown_row_count_mismatch, so a prose-only fixture would no longer
    # be valid=True.
    rendered_body = (
        "Alpha: $50,000 [1].\n"
        "\n"
        "| Rank | District | State | Average teacher starting salary | Sources |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 1 | Alpha | CA | $50,000 | [1] |\n"
    )
    report = validate_result(_plan(), result, rendered_body=rendered_body)

    assert report.valid is True
    assert report.dimensions_checked == [
        "selection",
        "metric",
        "sort_order",
        "surface_consistency",
        "coverage_state",
        "citation_coverage",
        "numeric_token_provenance",
        "fact_coverage",
        "coverage_wording",
    ]


def _row(
    district_id: int,
    district_name: str,
    value: float,
    *,
    rank: int,
    citation_markers: list[int] | None = None,
    metric_id: int = 1234,
    metric_name: str = "Average teacher starting salary",
) -> RankingRow:
    display_value = f"${value:,.0f}"
    return RankingRow(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=metric_id,
        metric_name=metric_name,
        value=value,
        display_value=display_value,
        academic_year="2024 - 2025",
        rank=rank,
        citation_markers=citation_markers or [rank],
        coverage_state="covered",
        coverage_display=display_value,
        coverage_reason="answer_value",
    )


def _citation(marker: int, *, district_id: int | None = None) -> CitationRef:
    return CitationRef(
        marker=marker,
        title=f"District {district_id or marker} Contract, 2024-2025",
        url=f"https://example.org/{marker}.pdf",
        page_number=marker,
        page_ref=f"p. {marker}",
        academic_year="2024 - 2025",
        document_type="Contract",
        district_id=district_id or marker,
    )


def _result(
    rows: list[RankingRow],
    *,
    citations: list[CitationRef] | None = None,
    selection: ResultSelection | None = None,
) -> ResultSet:
    return MetricRankingResult(
        rows=rows,
        citations=citations
        if citations is not None
        else [_citation(row.rank, district_id=row.district_id) for row in rows],
        total_considered=len(rows),
        excluded_count=0,
        coverage_frame=_coverage_frame_for_rows(rows),
        order_statement="Ranked by starting salary, highest to lowest.",
        source_notes=[],
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
        selection=selection,
    )


def _named_plan() -> QueryPlan:
    return QueryPlan(
        question="Rank named districts by starting salary.",
        selection=SelectionSpec(scope="named_districts", districts=["Bravo"]),
        metrics=[MetricSpec(name="starting salary")],
    )


def _named_selection(*, district_ids: list[int] | None = None) -> ResultSelection:
    return ResultSelection(
        scope="named_districts",
        districts=[
            SelectedDistrict(
                district_id=district_id,
                district_name=f"District {district_id}",
                state="CA",
            )
            for district_id in (district_ids or [2])
        ],
    )


def _state_count_selection(
    *,
    states: list[str] | None = None,
    district_states: list[tuple[int, str]] | None = None,
) -> ResultSelection:
    return ResultSelection(
        scope="all_covered_districts",
        states=states or ["TX"],
        districts=[
            SelectedDistrict(
                district_id=district_id,
                district_name=f"District {district_id}",
                state=state,
            )
            for district_id, state in (district_states or [(101, "TX"), (102, "TX")])
        ],
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


def _lookup_value_row(
    district_id: int,
    district_name: str,
    metric_id: int,
    metric_name: str,
    display_value: str,
    *,
    citation_marker: int,
) -> MetricValueRow:
    return MetricValueRow(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=metric_id,
        metric_name=metric_name,
        value=display_value,
        display_value=display_value,
        academic_year="2024 - 2025",
        citation_markers=[citation_marker],
        coverage_state="covered",
        coverage_display=display_value,
        coverage_reason="answer_value",
    )


def _lookup_comparison_result(
    rows: list[MetricValueRow] | None = None,
    *,
    selection: ResultSelection | None = None,
) -> ResultSet:
    result_rows = rows or [
        _lookup_value_row(
            1,
            "Alpha",
            9876,
            "Average teacher starting salary",
            "$50,000",
            citation_marker=1,
        ),
        _lookup_value_row(
            1,
            "Alpha",
            4321,
            "Collective bargaining status",
            "Yes",
            citation_marker=2,
        ),
        _lookup_value_row(
            2,
            "Bravo",
            9876,
            "Average teacher starting salary",
            "$60,000",
            citation_marker=3,
        ),
        _lookup_value_row(
            2,
            "Bravo",
            4321,
            "Collective bargaining status",
            "No",
            citation_marker=4,
        ),
    ]
    return MetricLookupResult(        selection=selection
        if selection is not None
        else _named_selection(district_ids=[1, 2]),
        rows=result_rows,
        citations=[
            _citation(marker, district_id=(1 if marker < 3 else 2))
            for marker in range(1, len(result_rows) + 1)
        ],
        total_considered=len(result_rows),
        excluded_count=0,
        coverage_frame=_coverage_frame_for_rows(result_rows),
        order_statement=(
            "Looked up selected metrics for selected districts, "
            "alphabetical by district name."
        ),
        source_notes=[],
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
    )


def _coverage_frame_for_rows(rows: list[RankingRow | MetricValueRow]) -> CoverageFrame:
    state_counts = Counter(row.coverage_state for row in rows)
    in_scope_count = (
        state_counts["covered"]
        + state_counts["ina"]
        + state_counts["na"]
        + state_counts["not_reviewed"]
    )
    real_data_count = state_counts["covered"]
    coverage_ratio = real_data_count / in_scope_count if in_scope_count else 0.0
    return CoverageFrame(
        universe_count=len(rows),
        in_scope_count=in_scope_count,
        addressed_count=state_counts["covered"] + state_counts["ina"] + state_counts["na"],
        real_data_count=real_data_count,
        not_reviewed_count=state_counts["not_reviewed"],
        out_of_universe_count=state_counts["out_of_universe"],
        coverage_ratio=coverage_ratio,
        sparse=in_scope_count >= 3 and coverage_ratio < 0.5,
        sparse_disclosure=(
            f"Sparse coverage: {real_data_count} of {in_scope_count} in-scope "
            "cells have current reviewed data."
            if in_scope_count >= 3 and coverage_ratio < 0.5
            else None
        ),
    )


def _state_count_result(
    *,
    selection: ResultSelection | None = None,
    qualifying_district_ids: list[int] | None = None,
    count_rows: list[CountRow] | None = None,
    coverage_frame: CoverageFrame | None = None,
) -> ResultSet:
    count = len(qualifying_district_ids or [101, 102])
    display_value = f"{count} of 2 covered districts"
    return MetricCountResult(        selection=selection or _state_count_selection(),
        rows=count_rows
        if count_rows is not None
        else [
            ThresholdCountRow(
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=count,
                display_value=display_value,
                academic_year="2024 - 2025",
                count=count,
                denominator=2,
                filter_statement="reviewed current value",
                qualifying_district_ids=qualifying_district_ids or [101, 102],
                coverage_state="covered",
                coverage_display=display_value,
                coverage_reason="count_summary",
            )
        ],
        total_considered=2,
        excluded_count=0,
        coverage_frame=coverage_frame
        or CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=2,
            real_data_count=2,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement="Counted qualifying districts for selected metrics.",
        source_notes=[],
        methodology_codes=[
            MethodologyRef(code="count_denominator_current_reviewed_rows"),
        ],
    )


def _authority(
    *,
    metric_ids: list[int] | None = None,
    selected_district_ids: list[int] | None = None,
    profile_field_keys: list[str] | None = None,
) -> ValidationAuthority:
    return ValidationAuthority(
        metrics=[
            ResolvedMetricAuthority(
                metric_id=metric_id,
                metric_name=f"Metric {metric_id}",
                role="primary" if index == 0 else "comparison",
            )
            for index, metric_id in enumerate(metric_ids or [1234])
        ],
        selection=ResolvedSelectionAuthority(
            scope="named_districts",
            district_ids=selected_district_ids or [2],
        ),
        profile_fields=[
            ResolvedProfileFieldAuthority(
                field_key=field_key,
                label=field_key,
                input_phrase=field_key,
                metadata={"resolution_method": "alias"},
            )
            for field_key in (profile_field_keys or [])
        ],
    )


def _profile_lookup_result(*, field_key: str = "enrollment") -> ProfileLookupResult:
    return ProfileLookupResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=2, district_name="Bravo", state="CA")
            ],
        ),
        rows=[
            ProfileValueRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                field_key=field_key,
                label=field_key,
                value=10000,
                display_value="10,000",
                academic_year="2024 - 2025",
                source_label="NCES district profile",
                provenance="NCES directory year: 2024",
                metric_name=field_key,
                coverage_state="covered",
                coverage_display="10,000",
                coverage_reason="profile_field_value",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Profile field lookup.",
    )


def test_profile_field_validation_requires_resolved_authority() -> None:
    plan = QueryPlan(
        operation="profile_lookup",
        question="What is Bravo's enrollment?",
        selection=SelectionSpec(scope="named_districts", districts=["Bravo"]),
        profile_fields=[{"name": "student enrollment"}],
    )

    report = validate_result(plan, _profile_lookup_result(), authority=_authority())

    assert report.valid is False
    assert any(
        finding.code == "missing_profile_field_authority"
        for finding in report.findings
    )


def test_profile_field_validation_rejects_field_key_outside_authority() -> None:
    plan = QueryPlan(
        operation="profile_lookup",
        question="What is Bravo's enrollment?",
        selection=SelectionSpec(scope="named_districts", districts=["Bravo"]),
        profile_fields=[{"name": "student enrollment"}],
    )

    report = validate_result(
        plan,
        _profile_lookup_result(field_key="frpl_pct"),
        authority=_authority(profile_field_keys=["enrollment"]),
    )

    assert report.valid is False
    assert any(
        finding.code == "profile_field_authority_mismatch"
        for finding in report.findings
    )


def test_profile_field_validation_is_silent_without_authority() -> None:
    """Bridge-validator path (no authority threaded) must not flag every profile
    lookup as missing_profile_field_authority.

    Context: the L2 deterministic-bridge dispatcher calls validate_result()
    without an authority object (chat-time validation does pass one). Previously,
    `_validate_profile_fields` emitted `missing_profile_field_authority` whenever
    `authority is None`, which turned every profile-lookup verdict into a
    `dim_metric_check=fail` row. The fresh-sweep evidence at
    `.context/fresh-sweep-2026-05-18-postPR648/cross-sweep-analysis.md`
    showed 6 such fails on sort-accuracy. After the fix, no
    missing_profile_field_authority finding is emitted on the no-authority path.
    """
    plan = QueryPlan(
        operation="profile_lookup",
        question="What is Bravo's enrollment?",
        selection=SelectionSpec(scope="named_districts", districts=["Bravo"]),
        profile_fields=[{"name": "student enrollment"}],
    )

    report = validate_result(plan, _profile_lookup_result(), authority=None)

    assert not any(
        finding.code == "missing_profile_field_authority"
        for finding in report.findings
    ), "no missing_profile_field_authority when authority is not supplied"


def test_validation_report_is_valid_for_consistent_ranking_artifact() -> None:
    result = _result(
        [
            _row(2, "Bravo", 70000.0, rank=1),
            _row(3, "Charlie", 60000.0, rank=2),
        ]
    )

    report = validate_result(_plan(), result)

    assert report.valid is True
    assert report.findings == []
    assert report.dimensions_checked == [
        "selection",
        "metric",
        "sort_order",
        "surface_consistency",
        "coverage_state",
        "citation_coverage",
    ]


def test_validation_rejects_unbounded_ranking_truncated_before_rendering() -> None:
    result = _result(
        [
            _row(index, f"District {index}", 100_000 - index, rank=index)
            for index in range(1, 11)
        ]
    ).model_copy(
        update={
            "total_considered": 12,
            "coverage_frame": CoverageFrame(
                universe_count=12,
                in_scope_count=12,
                addressed_count=12,
                real_data_count=12,
                not_reviewed_count=0,
                out_of_universe_count=0,
                coverage_ratio=1.0,
            ),
        }
    )

    report = validate_result(_plan(), result)

    assert report.valid is False
    finding = next(
        finding
        for finding in report.findings
        if finding.code == "unbounded_ranking_truncated"
    )
    assert finding.dimension == "surface_consistency"
    assert finding.metadata == {
        "row_count": 10,
        "eligible_row_count": 12,
        "total_considered": 12,
        "excluded_count": 0,
        "selection_scope": "all_covered_districts",
    }


def test_validation_allows_explicit_top_limit_to_return_less_than_eligible_rows() -> None:
    result = _result(
        [
            _row(index, f"District {index}", 100_000 - index, rank=index)
            for index in range(1, 11)
        ]
    ).model_copy(
        update={
            "total_considered": 12,
            "coverage_frame": CoverageFrame(
                universe_count=12,
                in_scope_count=12,
                addressed_count=12,
                real_data_count=12,
                not_reviewed_count=0,
                out_of_universe_count=0,
                coverage_ratio=1.0,
            ),
        }
    )

    report = validate_result(_plan(limit=LimitSpec(count=10, kind="top")), result)

    assert "unbounded_ranking_truncated" not in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_descending_sort_order_mismatch() -> None:
    result = _result(
        [
            _row(2, "Bravo", 70000.0, rank=1),
            _row(1, "Alpha", 80000.0, rank=2),
        ]
    )

    report = validate_result(_plan(), result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == ["sort_order_mismatch"]


def test_validation_accepts_ascending_bottom_ranking_order() -> None:
    result = _result(
        [
            _row(1, "Alpha", 50000.0, rank=1),
            _row(3, "Charlie", 60000.0, rank=2),
        ]
    )

    report = validate_result(_plan(limit=LimitSpec(count=2, kind="bottom")), result)

    assert report.valid is True
    assert report.findings == []


def _coverage_state_row(
    district_id: int,
    district_name: str,
    *,
    rank: int,
    metric_id: int = 1234,
    metric_name: str = "Average teacher starting salary",
) -> RankingRow:
    """Build a coverage-state placeholder row (appended after the ranked block)."""
    return RankingRow(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=metric_id,
        metric_name=metric_name,
        value=None,
        display_value="Issue not addressed in the documents reviewed.",
        academic_year="2024 - 2025",
        rank=rank,
        source="coverage_state",
        citation_markers=[],
        coverage_state="not_reviewed",
        coverage_display="Issue not addressed in the documents reviewed.",
        coverage_reason="metric_not_reviewed",
    )


def test_validation_accepts_ranking_with_coverage_state_appendage() -> None:
    """Coverage-state placeholders (value=None) after the ranked block must
    not trigger sort_value_missing or sort_order_mismatch.

    Closes M3a-2: when an explicit-scope ranking includes districts with no
    current data, the executor appends placeholder rows with value=None after
    the ranked block. The ranking validator's sort-order branch must treat
    these as appendages, not as part of the sort comparison.
    """
    # Build with citations covering only the ranked rows; placeholders have
    # citation_markers=[] by design.
    ranked_rows = [
        _row(2, "Bravo", 80000.0, rank=1),
        _row(1, "Alpha", 50000.0, rank=2),
    ]
    placeholder = _coverage_state_row(3, "Charlie", rank=3)
    result = _result(
        [*ranked_rows, placeholder],
        citations=[_citation(row.rank, district_id=row.district_id) for row in ranked_rows],
    )

    report = validate_result(_plan(), result)

    finding_codes = [f.code for f in report.findings]
    # Sort-order branch must accept the result.
    assert "sort_value_missing" not in finding_codes
    assert "sort_order_mismatch" not in finding_codes
    assert "placeholder_row_misplaced" not in finding_codes


def test_validation_rejects_coverage_state_row_interleaved_with_ranked_block() -> None:
    """Coverage-state placeholders must follow all ranked rows, not be mixed in."""
    ranked_rows = [
        _row(2, "Bravo", 80000.0, rank=1),
        _row(1, "Alpha", 50000.0, rank=3),
    ]
    placeholder = _coverage_state_row(3, "Charlie", rank=2)  # mis-placed
    result = _result(
        [ranked_rows[0], placeholder, ranked_rows[1]],
        citations=[_citation(row.rank, district_id=row.district_id) for row in ranked_rows],
    )

    report = validate_result(_plan(), result)

    finding_codes = [f.code for f in report.findings]
    assert "placeholder_row_misplaced" in finding_codes


def test_validation_accepts_ranking_with_only_coverage_state_rows() -> None:
    """All-placeholder result (no ranked rows) does not trigger sort_value_missing."""
    result = _result(
        [
            _coverage_state_row(1, "Alpha", rank=1),
            _coverage_state_row(2, "Bravo", rank=2),
        ],
        citations=[],
    )

    report = validate_result(_plan(), result)

    finding_codes = [f.code for f in report.findings]
    # No ranked rows means no sort to validate; sort-branch should not fire.
    assert "sort_value_missing" not in finding_codes


def test_validation_accepts_profile_ordered_policy_metric_ranking() -> None:
    plan = QueryPlan(
        question="Show salaries for districts with the highest FRPL share.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary"),
            MetricSpec(name="free-and-reduced lunch share", role="grouping"),
        ],
        sort=SortSpec(field="free-and-reduced lunch share", direction="desc"),
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="free-and-reduced lunch share",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 50000.0, rank=1).model_copy(
                update={
                    "sort_metric_name": "FRPL %",
                    "sort_value": 90.0,
                    "sort_display_value": "90%",
                }
            ),
            _row(2, "Bravo", 70000.0, rank=2).model_copy(
                update={
                    "sort_metric_name": "FRPL %",
                    "sort_value": 80.0,
                    "sort_display_value": "80%",
                }
            ),
        ]
    )
    authority = ValidationAuthority(
        profile_fields=[
            ResolvedProfileFieldAuthority(
                field_key="frpl_pct",
                label="FRPL %",
                input_phrase="free-and-reduced lunch share",
            )
        ]
    )

    report = validate_result(plan, result, authority=authority)

    assert report.valid is True
    assert report.findings == []


def test_validation_accepts_profile_sort_comparison_metric_authority() -> None:
    plan = QueryPlan(
        question=(
            "Show me starting teacher salaries for districts with the highest "
            "free-and-reduced lunch share"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary", role="primary"),
            MetricSpec(name="free and reduced lunch percentage", role="comparison"),
        ],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="free-and-reduced lunch share",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 50000.0, rank=1).model_copy(
                update={
                    "sort_metric_name": "FRPL %",
                    "sort_value": 90.0,
                    "sort_display_value": "90%",
                }
            ),
            _row(2, "Bravo", 70000.0, rank=2).model_copy(
                update={
                    "sort_metric_name": "FRPL %",
                    "sort_value": 80.0,
                    "sort_display_value": "80%",
                }
            ),
        ]
    )
    authority = ValidationAuthority(
        profile_fields=[
            ResolvedProfileFieldAuthority(
                field_key="frpl_pct",
                label="FRPL %",
                input_phrase="free-and-reduced lunch share",
            )
        ]
    )

    report = validate_result(plan, result, authority=authority)

    assert report.valid is True
    assert report.findings == []


def test_validation_accepts_frpl_profile_sort_label_variants() -> None:
    plan = QueryPlan(
        question="Show salaries for districts with the lowest FRPL share.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary", role="primary")],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="free and reduced lunch percentage",
                direction="asc",
                key_type="profile_field",
            )
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 50000.0, rank=1).model_copy(
                update={
                    "sort_metric_name": "FRPL %",
                    "sort_value": 20.0,
                    "sort_display_value": "20%",
                }
            ),
            _row(2, "Bravo", 70000.0, rank=2).model_copy(
                update={
                    "sort_metric_name": "FRPL %",
                    "sort_value": 30.0,
                    "sort_display_value": "30%",
                }
            ),
        ]
    )
    authority = ValidationAuthority(
        profile_fields=[
            ResolvedProfileFieldAuthority(
                field_key="frpl_pct",
                label="frpl pct",
                input_phrase="free and reduced lunch percentage",
            )
        ]
    )

    report = validate_result(plan, result, authority=authority)

    assert report.valid is True
    assert report.findings == []


def test_validation_rejects_profile_sort_comparison_without_sort_artifact() -> None:
    plan = QueryPlan(
        question=(
            "Show me starting teacher salaries for districts with the highest "
            "free-and-reduced lunch share"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary", role="primary"),
            MetricSpec(name="free and reduced lunch percentage", role="comparison"),
        ],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="frpl_pct",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 70000.0, rank=1),
            _row(2, "Bravo", 50000.0, rank=2),
        ]
    )

    report = validate_result(plan, result)

    assert report.valid is False
    assert "profile_sort_artifact_missing" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_profile_sort_artifact_plan_mismatch() -> None:
    plan = QueryPlan(
        question=(
            "Show me starting teacher salaries for districts with the highest "
            "free-and-reduced lunch share"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary", role="primary"),
            MetricSpec(name="free and reduced lunch percentage", role="comparison"),
        ],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="frpl_pct",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 50000.0, rank=1).model_copy(
                update={
                    "sort_metric_name": "Enrollment",
                    "sort_value": 90000.0,
                    "sort_display_value": "90,000",
                }
            ),
            _row(2, "Bravo", 70000.0, rank=2).model_copy(
                update={
                    "sort_metric_name": "Enrollment",
                    "sort_value": 80000.0,
                    "sort_display_value": "80,000",
                }
            ),
        ]
    )

    report = validate_result(plan, result)

    assert report.valid is False
    assert "profile_sort_artifact_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_profile_sort_artifact_authority_mismatch() -> None:
    plan = QueryPlan(
        question=(
            "Show me starting teacher salaries for districts with the highest "
            "free-and-reduced lunch share"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary", role="primary"),
            MetricSpec(name="free and reduced lunch percentage", role="comparison"),
        ],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="frpl_pct",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 50000.0, rank=1).model_copy(
                update={
                    "sort_metric_name": "Enrollment",
                    "sort_value": 90000.0,
                    "sort_display_value": "90,000",
                }
            ),
            _row(2, "Bravo", 70000.0, rank=2).model_copy(
                update={
                    "sort_metric_name": "Enrollment",
                    "sort_value": 80000.0,
                    "sort_display_value": "80,000",
                }
            ),
        ]
    )
    authority = ValidationAuthority(
        profile_fields=[
            ResolvedProfileFieldAuthority(
                field_key="frpl_pct",
                label="FRPL %",
                input_phrase="free-and-reduced lunch share",
            )
        ]
    )

    report = validate_result(plan, result, authority=authority)

    assert report.valid is False
    assert "profile_sort_artifact_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_accepts_duplicate_comparison_metric_for_profile_sort() -> None:
    plan = QueryPlan(
        question=(
            "Show me starting teacher salaries for teachers with a masters for "
            "districts with the lowest free-and-reduced lunch share"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(
                name="starting salary for teachers with a master's degree",
                role="primary",
                degree_lane="ma",
            ),
            MetricSpec(
                name="starting salary for teachers with a master's degree",
                role="comparison",
                degree_lane="ma",
            ),
        ],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="frpl_pct",
                direction="asc",
                key_type="profile_field",
            )
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 50000.0, rank=1).model_copy(
                update={
                    "sort_metric_name": "FRPL %",
                    "sort_value": 0.0,
                    "sort_display_value": "0%",
                }
            )
        ]
    )
    authority = ValidationAuthority(
        profile_fields=[
            ResolvedProfileFieldAuthority(
                field_key="frpl_pct",
                label="FRPL %",
                input_phrase="frpl_pct",
            )
        ]
    )

    report = validate_result(plan, result, authority=authority)

    assert report.valid is True
    assert report.findings == []


def test_rendered_body_validation_accepts_profile_sort_column() -> None:
    plan = QueryPlan(
        question="Show salaries for districts with the highest FRPL share.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="free-and-reduced lunch share",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 50000.0, rank=1).model_copy(
                update={
                    "sort_metric_name": "FRPL %",
                    "sort_value": 90.0,
                    "sort_display_value": "90%",
                }
            ),
            _row(2, "Bravo", 70000.0, rank=2).model_copy(
                update={
                    "sort_metric_name": "FRPL %",
                    "sort_value": 80.0,
                    "sort_display_value": "80%",
                }
            ),
        ]
    )
    pre_render = validate_result(plan, result)
    manifest = render_response(plan, result, pre_render)

    report = validate_result(plan, result, rendered_body=manifest.body)

    assert "markdown_cell_drift" not in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_inconsistent_metric_rows() -> None:
    result = _result(
        [
            _row(2, "Bravo", 70000.0, rank=1),
            _row(3, "Charlie", 60000.0, rank=2, metric_id=4321),
        ]
    )

    report = validate_result(_plan(), result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == ["metric_row_mismatch"]


def test_validation_rejects_ranking_metric_outside_resolved_authority() -> None:
    result = _result([_row(2, "Bravo", 70000.0, rank=1, metric_id=9999)])

    report = validate_result(_plan(), result, authority=_authority(metric_ids=[1234]))

    assert report.valid is False
    assert [finding.code for finding in report.findings] == [
        "metric_authority_mismatch"
    ]


def test_validation_accepts_lookup_with_multiple_metric_rows_when_ordered() -> None:
    report = validate_result(
        _lookup_comparison_plan(),
        _lookup_comparison_result(),
        authority=_authority(metric_ids=[4321, 9876], selected_district_ids=[1, 2]),
    )

    assert report.valid is True
    assert report.findings == []


def test_validation_rejects_lookup_metric_outside_resolved_authority() -> None:
    result = _lookup_comparison_result()

    report = validate_result(
        _lookup_comparison_plan(),
        result,
        authority=_authority(metric_ids=[4321], selected_district_ids=[1, 2]),
    )

    assert report.valid is False
    assert "metric_authority_mismatch" in [finding.code for finding in report.findings]


def test_validation_rejects_lookup_comparison_order_mismatch() -> None:
    ordered_rows = _lookup_comparison_result().rows
    result = _lookup_comparison_result(
        rows=[
            ordered_rows[1],
            ordered_rows[0],
            ordered_rows[2],
            ordered_rows[3],
        ]
    )

    report = validate_result(_lookup_comparison_plan(), result)

    assert report.valid is False
    assert "sort_order_mismatch" in [finding.code for finding in report.findings]


def test_validation_rejects_lookup_duplicate_district_metric_rows() -> None:
    ordered_rows = _lookup_comparison_result().rows
    result = _lookup_comparison_result(
        rows=[ordered_rows[0], ordered_rows[0], *ordered_rows[1:]]
    )

    report = validate_result(_lookup_comparison_plan(), result)

    assert report.valid is False
    assert "metric_lookup_duplicate" in [finding.code for finding in report.findings]


def test_validation_accepts_named_district_scope_when_rows_match_selection() -> None:
    result = _result(
        [_row(2, "Bravo", 70000.0, rank=1)],
        selection=_named_selection(district_ids=[2]),
    )

    report = validate_result(_named_plan(), result, authority=_authority())

    assert report.valid is True
    assert report.findings == []


def test_validation_rejects_named_district_row_outside_resolved_authority() -> None:
    result = _result(
        [_row(99, "Outside", 70000.0, rank=1)],
        selection=_named_selection(district_ids=[99]),
    )

    report = validate_result(
        _named_plan(),
        result,
        authority=_authority(selected_district_ids=[2]),
    )

    assert report.valid is False
    assert "selection_authority_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_named_district_scope_without_selection_metadata() -> None:
    result = _result([_row(2, "Bravo", 70000.0, rank=1)])

    report = validate_result(_named_plan(), result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == [
        "selection_metadata_missing"
    ]


def test_validation_rejects_named_district_row_outside_selection() -> None:
    result = _result(
        [
            _row(2, "Bravo", 70000.0, rank=1),
            _row(3, "Charlie", 60000.0, rank=2),
        ],
        selection=_named_selection(district_ids=[2]),
    )

    report = validate_result(_named_plan(), result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == [
        "selection_district_mismatch"
    ]


def test_validation_accepts_state_scope_when_rows_match_requested_state() -> None:
    result = _result([_row(2, "Bravo", 70000.0, rank=1)])

    report = validate_result(_plan(scope="state"), result)

    assert report.valid is True
    assert report.findings == []


def test_validation_rejects_state_scope_row_outside_requested_state() -> None:
    result = _result(
        [
            _row(2, "Bravo", 70000.0, rank=1),
            _row(3, "Charlie", 60000.0, rank=2).model_copy(update={"state": "NY"}),
        ]
    )

    report = validate_result(_plan(scope="state"), result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == ["selection_state_mismatch"]


def test_validation_accepts_state_scoped_metric_count_summary_row() -> None:
    result = _state_count_result()

    report = validate_result(_state_count_plan(), result)

    assert report.valid is True
    assert "selection_state_mismatch" not in [
        finding.code for finding in report.findings
    ]


def test_validation_reports_only_executed_metric_count_dimensions() -> None:
    result = _state_count_result()

    report = validate_result(_state_count_plan(), result)

    assert report.dimensions_checked == [
        "selection",
        "metric",
        "filter",
        "denominator",
        "surface_consistency",
        "coverage_state",
        "citation_coverage",
    ]


def test_validation_accepts_state_scoped_metric_count_with_postal_abbreviation() -> None:
    result = _state_count_result(
        selection=_state_count_selection(
            states=["OH"],
            district_states=[(101, "OH"), (102, "OH")],
        )
    )

    report = validate_result(_state_count_plan(states=["OH"]), result)

    assert report.valid is True
    assert report.findings == []


def test_validation_accepts_state_scoped_metric_count_with_full_state_name() -> None:
    result = _state_count_result(
        selection=_state_count_selection(
            states=["OH"],
            district_states=[(101, "OH"), (102, "OH")],
        )
    )

    report = validate_result(_state_count_plan(states=["Ohio"]), result)

    assert report.valid is True
    assert report.findings == []


def test_validation_rejects_state_scoped_metric_count_selection_outside_state() -> None:
    result = _state_count_result(
        selection=_state_count_selection(
            district_states=[(101, "TX"), (102, "CA")],
        )
    )

    report = validate_result(_state_count_plan(), result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == [
        "selection_state_mismatch"
    ]


def test_validation_rejects_state_scoped_metric_count_qualifying_id_outside_selection() -> None:
    result = _state_count_result(qualifying_district_ids=[101, 999])

    report = validate_result(_state_count_plan(), result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == [
        "selection_district_mismatch"
    ]


def test_validation_rejects_metric_count_qualifying_id_outside_selection_without_state_scope() -> None:
    result = _state_count_result(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(
                    district_id=101,
                    district_name="District 101",
                    state="TX",
                ),
                SelectedDistrict(
                    district_id=102,
                    district_name="District 102",
                    state="TX",
                ),
            ],
        ),
        qualifying_district_ids=[101, 999],
    )
    plan = QueryPlan(
        operation="count",
        question="How many covered districts have reviewed data?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
    )

    report = validate_result(plan, result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == [
        "selection_district_mismatch"
    ]


def test_validation_rejects_metric_count_when_count_disagrees_with_qualifying_ids() -> None:
    result = _state_count_result(
        count_rows=[
            ThresholdCountRow(
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=2,
                display_value="2 of 2 covered districts",
                academic_year="2024 - 2025",
                count=2,
                denominator=2,
                filter_statement="reviewed current value",
                qualifying_district_ids=[101],
                coverage_state="covered",
                coverage_display="2 of 2 covered districts",
                coverage_reason="count_summary",
            )
        ]
    )

    report = validate_result(_state_count_plan(), result)

    assert report.valid is False
    assert "count_qualifying_ids_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_metric_count_coverage_frame_real_data_mismatch() -> None:
    result = _state_count_result(
        count_rows=[
            ThresholdCountRow(
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=1,
                display_value="1 of 2 covered districts",
                academic_year="2024 - 2025",
                count=1,
                denominator=2,
                filter_statement="reviewed current value",
                qualifying_district_ids=[101],
                coverage_state="covered",
                coverage_display="1 of 2 covered districts",
                coverage_reason="count_summary",
            ),
            ThresholdCountRow(
                metric_id=5678,
                metric_name="Collective bargaining status",
                value=1,
                display_value="1 of 1 covered districts",
                academic_year="2024 - 2025",
                count=1,
                denominator=1,
                filter_statement="reviewed current value",
                qualifying_district_ids=[101],
                coverage_state="covered",
                coverage_display="1 of 1 covered districts",
                coverage_reason="count_summary",
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
    )

    report = validate_result(_state_count_plan(), result)

    assert report.valid is False
    assert "coverage_frame_count_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_metric_count_filter_statement_mismatch() -> None:
    plan = QueryPlan(
        operation="count",
        question="How many covered districts have salaries of at least 60000?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        filters=[
            {
                "field": "value",
                "operator": "greater_than_or_equal",
                "value": 60000,
            }
        ],
    )
    result = _state_count_result()

    report = validate_result(plan, result)

    assert report.valid is False
    assert "filter_statement_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_missing_citation_marker_reference() -> None:
    result = _result(
        [_row(2, "Bravo", 70000.0, rank=1, citation_markers=[7])],
        citations=[_citation(1, district_id=2)],
    )

    report = validate_result(_plan(), result)

    assert report.valid is False
    assert "citation_marker_missing" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_unreferenced_citation() -> None:
    result = _result(
        [_row(2, "Bravo", 70000.0, rank=1, citation_markers=[1])],
        citations=[_citation(1, district_id=2), _citation(2, district_id=3)],
    )

    report = validate_result(_plan(), result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == ["citation_not_referenced"]


def test_validation_rejects_coverage_display_mismatch() -> None:
    result = _lookup_comparison_result(
        rows=[
            _lookup_value_row(
                1,
                "Alpha",
                4321,
                "Collective bargaining status",
                "Not available",
                citation_marker=1,
            ).model_copy(
                update={
                    "coverage_state": "ina",
                    "coverage_display": (
                        "Issue not addressed in the documents reviewed."
                    ),
                }
            )
        ]
    )

    report = validate_result(_lookup_comparison_plan(), result)

    assert report.valid is False
    assert "coverage_display_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_forbidden_missing_data_wording_after_render() -> None:
    result = _result([_row(1, "Alpha", 50000, rank=1)])

    report = validate_result(
        _plan(),
        result,
        rendered_body="Alpha has no data available for this metric.",
    )

    assert report.valid is False
    finding = next(
        item for item in report.findings if item.code == "forbidden_coverage_wording"
    )
    assert finding.dimension == "coverage_wording"
    assert finding.metadata == {"phrases": ["no data available"]}


def test_validation_rejects_retired_coverage_short_labels_after_render() -> None:
    """#1514 D11: the retired "Older year only" / "Out of Pathfinder" short
    labels are forbidden wording — the renderer voices those coverage states
    as canonical narrative sentences instead."""
    result = _result([_row(1, "Alpha", 50000, rank=1)])

    report = validate_result(
        _plan(),
        result,
        rendered_body="Alpha: Older year only. Bravo: Out of Pathfinder.",
    )

    finding = next(
        item for item in report.findings if item.code == "forbidden_coverage_wording"
    )
    assert finding.metadata == {"phrases": ["Older year only", "Out of Pathfinder"]}


def test_validation_allows_canonical_out_of_universe_sentence() -> None:
    """#1514 D11: the canonical sentence "... is not in the District Policy
    Pathfinder." must NOT trip the "Out of Pathfinder" ban."""
    result = _result([_row(1, "Alpha", 50000, rank=1)])

    report = validate_result(
        _plan(),
        result,
        rendered_body=(
            "Alpha: $50,000 [1].\n\n"
            "Springfield is not in the District Policy Pathfinder."
        ),
    )

    assert "forbidden_coverage_wording" not in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_not_reviewed_table_cell_after_render() -> None:
    """#1514: tables hold answer rows only (value, INA, N/A) — a bare
    "Not reviewed" markdown table CELL is forbidden wording. Prose may still
    say a district wasn't reviewed; the ban is cell-scoped."""
    result = _result([_row(1, "Alpha", 50000, rank=1)])
    body = (
        "Alpha: $50,000 [1].\n\n"
        "| Rank | District | State | Average teacher starting salary |\n"
        "| --- | --- | --- | --- |\n"
        "| 1 | Alpha | CA | $50,000 |\n"
        "| 2 | Bravo | CA | Not reviewed |\n"
    )

    report = validate_result(_plan(), result, rendered_body=body)

    finding = next(
        item for item in report.findings if item.code == "forbidden_coverage_wording"
    )
    assert finding.metadata == {"phrases": ["Not reviewed (table cell)"]}


def test_validation_checks_ranking_disclosure_reason_breakdown() -> None:
    result = _result([_row(1, "Alpha", 50000, rank=1)]).model_copy(
        update={
            "total_considered": 2,
            "excluded_count": 1,
            "coverage_frame": CoverageFrame(
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
            "coverage_disclosures": [
                CoverageDisclosure(
                    district_id=2,
                    district_name="Bravo",
                    state="CA",
                    metric_id=1234,
                    metric_name="Average teacher starting salary",
                    academic_year="2024 - 2025",
                    coverage_state="not_reviewed",
                    display=(
                        "NCTQ last reviewed Bravo for Average teacher starting "
                        "salary in 2023 - 2024; the value then was $49,000."
                    ),
                    reason="stale_recent_answer",
                    prior_academic_year="2023 - 2024",
                    prior_display_value="$49,000",
                )
            ],
        }
    )

    report = validate_result(_plan(), result)

    assert report.valid is False
    assert "coverage_breakdown_count_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_accepts_unavailable_disclosure_in_ranking_breakdown() -> None:
    """#1698 fix-forward: a current-year "Unavailable" district narrated as a
    not_reviewed/reason="unavailable" disclosure, with a breakdown that counts it
    correctly (unavailable_count=1), must NOT trip coverage_breakdown_count_mismatch.
    The metric_ranking branch copied unavailable_count from the frame AND
    re-incremented it from the disclosure (it wasn't in the zero-then-rebuild
    set), double-counting it to 2 and swallowing the whole answer (live case 14).
    """
    result = _result([_row(1, "Alpha", 50000, rank=1)]).model_copy(
        update={
            "total_considered": 2,
            "excluded_count": 1,
            "coverage_frame": CoverageFrame(
                universe_count=2,
                in_scope_count=2,
                addressed_count=1,
                real_data_count=1,
                not_reviewed_count=1,
                out_of_universe_count=0,
                coverage_ratio=0.5,
                breakdown=CoverageBreakdown(
                    answer_value_count=1,
                    unavailable_count=1,
                ),
            ),
            "coverage_disclosures": [
                CoverageDisclosure(
                    district_id=2,
                    district_name="Houston ISD",
                    state="TX",
                    metric_id=1234,
                    metric_name="Average teacher starting salary",
                    academic_year="2024 - 2025",
                    coverage_state="not_reviewed",
                    display="Unavailable",
                    reason="unavailable",
                )
            ],
        }
    )

    report = validate_result(_plan(), result)

    assert "coverage_breakdown_count_mismatch" not in [
        finding.code for finding in report.findings
    ]


def test_validation_allows_noncovered_rows_without_citation_markers() -> None:
    result = _lookup_comparison_result(
        rows=[
            _lookup_value_row(
                1,
                "Alpha",
                4321,
                "Collective bargaining status",
                "Issue not addressed in the documents reviewed.",
                citation_marker=1,
            ).model_copy(
                update={
                    "citation_markers": [],
                    "coverage_state": "ina",
                    "coverage_display": (
                        "Issue not addressed in the documents reviewed."
                    ),
                }
            )
        ],
        selection=_named_selection(district_ids=[1]),
    ).model_copy(update={"citations": []})

    report = validate_result(_lookup_comparison_plan(), result)

    assert "row_missing_citation_marker" not in [
        finding.code for finding in report.findings
    ]


def test_validation_accepts_similarity_peer_discovery_result() -> None:
    """WS-3 (#1242): a similarity (peer-set DISCOVERY) result carries no policy
    metric and a zeroed coverage frame by design — every row uses the
    ``metric_id=0`` sentinel and the renderer dispatches it to
    ``_render_similarity``. The metric and coverage-state validators assume a
    metric-bearing result and must NOT over-fire on it.

    Reproduces the S101/S94 ``manifest_validation_failed`` bug: a valid 11-row
    NCES peer set tripped ``unsupported_metric_plan`` + ``coverage_display_mismatch``
    + ``coverage_frame_count_mismatch`` + ``coverage_breakdown_count_mismatch``
    and never rendered. The peer set is fully grounded (real covered districts,
    similarity scores); these invariants simply do not apply to the shape, and
    peer order / selection grounding are still validated elsewhere.
    """

    plan = QueryPlan(
        operation="similarity",
        question="What peer districts should I compare against?",
        selection=SelectionSpec(
            scope="named_districts", districts=["Atlanta Public Schools"]
        ),
        similarity=SimilarityQuerySpec(anchor_name="Atlanta Public Schools"),
    )
    result = PeerComparisonResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(
                    district_id=51,
                    district_name="Atlanta Public Schools",
                    state="GA",
                ),
                SelectedDistrict(
                    district_id=138,
                    district_name="Charleston County School District",
                    state="SC",
                ),
            ],
        ),
        rows=[
            PeerComparisonRow(
                district_id=51,
                district_name="Atlanta Public Schools",
                state="GA",
                metric_id=0,
                metric_name="similarity",
                value=None,
                display_value="anchor",
                academic_year="2024 - 2025",
                source="coverage_state",
                citation_markers=[],
                coverage_state="covered",
                coverage_display="Anchor district",
                coverage_reason="Anchor district selected by the user.",
                peer_role="anchor",
                peer_rank=None,
                peer_score=None,
                peer_reason="Anchor district selected by the user.",
            ),
            PeerComparisonRow(
                district_id=138,
                district_name="Charleston County School District",
                state="SC",
                metric_id=0,
                metric_name="similarity",
                value=0.9,
                display_value="0.90",
                academic_year="2024 - 2025",
                source="coverage_state",
                citation_markers=[],
                coverage_state="covered",
                coverage_display="Similarity score: 0.90",
                coverage_reason="answer_value",
                peer_role="peer",
                peer_rank=1,
                peer_score=0.9,
                peer_reason="Similar enrollment and urbanicity.",
            ),
        ],
        coverage_frame=CoverageFrame(
            universe_count=0,
            in_scope_count=0,
            addressed_count=0,
            real_data_count=0,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=0.0,
            breakdown=CoverageBreakdown(),
        ),
        total_considered=2,
        excluded_count=0,
        order_statement=(
            "Found 1 NCES-similar peer district for Atlanta Public Schools."
        ),
        methodology_codes=[],
    )

    report = validate_result(plan, result)

    over_fire = {
        "unsupported_metric_plan",
        "coverage_display_mismatch",
        "coverage_frame_count_mismatch",
        "coverage_breakdown_count_mismatch",
    }
    fired = {finding.code for finding in report.findings}
    assert not (over_fire & fired), (
        f"similarity result over-fired metric/coverage validators: {over_fire & fired}"
    )
    assert report.valid is True, [finding.code for finding in report.findings]


def test_validation_rejects_noncovered_peer_comparison_rows() -> None:
    plan = QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's peers and how do sick leave policies compare?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="sick leave policy")],
    )
    result = PeerComparisonResult(
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
            ],
        ),
        rows=[
            PeerComparisonRow(
                district_id=26,
                district_name="Denver Public Schools",
                state="CO",
                metric_id=198,
                metric_name="Maximum number of annual paid sick days",
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
            ),
            PeerComparisonRow(
                district_id=24,
                district_name="Aurora Public Schools",
                state="CO",
                metric_id=198,
                metric_name="Maximum number of annual paid sick days",
                value=None,
                display_value=(
                    "NCTQ hasn't reviewed Aurora Public Schools for Maximum "
                    "number of annual paid sick days in 2024 - 2025 yet."
                ),
                academic_year="2024 - 2025",
                citation_markers=[],
                coverage_state="not_reviewed",
                coverage_display=(
                    "NCTQ hasn't reviewed Aurora Public Schools for Maximum "
                    "number of annual paid sick days in 2024 - 2025 yet."
                ),
                coverage_reason="metric_not_reviewed",
                peer_role="peer",
                peer_rank=1,
                peer_score=0.82,
                peer_reason="Both in CO.",
            ),
        ],
        citations=[CitationRef(marker=1, title="Denver Agreement", district_id=26)],
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
        total_considered=2,
        excluded_count=0,
        order_statement="Compared selected policy metrics.",
        methodology_codes=[],
    )

    report = validate_result(plan, result)

    assert report.valid is False
    assert "peer_comparison_peer_unreviewed_policy_cell" in [
        finding.code for finding in report.findings
    ]

    for coverage_state, display_value in [
        ("na", "Not applicable."),
        ("ina", "Issue not addressed in the documents reviewed."),
    ]:
        variant = result.model_copy(
            update={
                "rows": [
                    result.rows[0],
                    result.rows[1].model_copy(
                        update={
                            "coverage_state": coverage_state,
                            "coverage_display": display_value,
                            "coverage_reason": coverage_state,
                            "display_value": display_value,
                        }
                    ),
                ]
            }
        )
        variant_report = validate_result(plan, variant)
        assert variant_report.valid is False
        assert "peer_comparison_peer_unreviewed_policy_cell" in [
            finding.code for finding in variant_report.findings
        ]


def test_validation_rejects_bad_coverage_frame_counts() -> None:
    result = _lookup_comparison_result().model_copy(
        update={
            "coverage_frame": {
                "universe_count": 4,
                "in_scope_count": 4,
                "addressed_count": 3,
                "real_data_count": 99,
                "not_reviewed_count": 0,
                "out_of_universe_count": 0,
            }
        }
    )

    report = validate_result(_lookup_comparison_plan(), result)

    assert report.valid is False
    assert "coverage_frame_count_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_bad_sparse_coverage_frame_metadata() -> None:
    result = _lookup_comparison_result().model_copy(
        update={
            "coverage_frame": {
                "universe_count": 4,
                "in_scope_count": 4,
                "addressed_count": 4,
                "real_data_count": 4,
                "not_reviewed_count": 0,
                "out_of_universe_count": 0,
                "coverage_ratio": 0.25,
                "sparse": True,
                "sparse_disclosure": (
                    "Sparse coverage: 1 of 4 in-scope cells have current reviewed data."
                ),
            }
        }
    )

    report = validate_result(_lookup_comparison_plan(), result)

    assert report.valid is False
    assert "coverage_frame_count_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_stale_row_displayed_as_current_data() -> None:
    result = _lookup_comparison_result(
        rows=[
            _lookup_value_row(
                1,
                "Alpha",
                4321,
                "Collective bargaining status",
                "Yes",
                citation_marker=1,
            ).model_copy(
                update={
                    "citation_markers": [],
                    "coverage_state": "not_reviewed",
                    "coverage_display": "Yes",
                    "coverage_reason": "stale_recent_answer",
                    "coverage_prior_academic_year": "2023 - 2024",
                    "coverage_prior_display_value": "Yes",
                }
            )
        ],
        selection=_named_selection(district_ids=[1]),
    ).model_copy(update={"citations": []})

    report = validate_result(_lookup_comparison_plan(), result)

    assert report.valid is False
    assert "stale_coverage_display_invalid" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_out_of_universe_row_with_citation_markers() -> None:
    result = _lookup_comparison_result(
        rows=[
            _lookup_value_row(
                1,
                "Unknown District",
                4321,
                "Collective bargaining status",
                "Unknown District is not in the District Policy Pathfinder.",
                citation_marker=1,
            ).model_copy(
                update={
                    "district_id": None,
                    "coverage_state": "out_of_universe",
                    "coverage_display": (
                        "Unknown District is not in the District Policy Pathfinder."
                    ),
                    "coverage_reason": "out_of_universe",
                }
            )
        ],
        selection=ResultSelection(
            scope="named_districts",
            unresolved_districts=["Unknown District"],
        ),
    )

    report = validate_result(_lookup_comparison_plan(), result)

    assert report.valid is False
    assert "out_of_universe_row_has_citation" in [
        finding.code for finding in report.findings
    ]


def test_validation_rejects_rendered_numeric_token_without_artifact_provenance() -> None:
    result = _lookup_comparison_result()

    report = validate_result(
        _lookup_comparison_plan(),
        result,
        rendered_body="Alpha had 999 teachers.",
    )

    assert report.valid is False
    assert "numeric_token_not_in_artifact" in [
        finding.code for finding in report.findings
    ]


def test_validation_dedups_repeated_comparison_of_same_metric() -> None:
    """#933: a metric surfaced as several variant clarification candidates can
    land in the plan as duplicate ``comparison`` specs that all resolve to the
    same metric (same normalized name, same degree_lane). The ranking validator
    must collapse those redundant comparisons against each other — not only
    against the primary — so the one-primary-metric check sees a single primary
    plus one comparison, instead of tripping ``unsupported_metric_plan`` on the
    duplicate."""

    plan = QueryPlan(
        operation="rank",
        question="Rank covered districts by starting salary, with class size.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary", role="primary", degree_lane="ba"),
            MetricSpec(name="class size", role="comparison"),
            MetricSpec(name="class size", role="comparison"),
        ],
    )

    specs = _ranking_result_metric_specs(
        plan, _result([_row(1, "Alpha", 90000.0, rank=1)]), None
    )

    # The duplicate "class size" comparison collapses; one primary + one
    # comparison survive (the comparison itself is still emitted once).
    assert [(spec.name, spec.role) for spec in specs].count(
        ("class size", "comparison")
    ) == 1


def test_validation_keeps_explicit_ba_ma_variants_distinct() -> None:
    """#874 guard: when the user EXPLICITLY names two degree lanes (BA primary,
    MA comparison) of the same metric, the dedup must NOT collapse them — they
    are genuinely distinct asks the renderer surfaces separately."""

    plan = QueryPlan(
        operation="rank",
        question="Rank by BA starting salary and show MA starting salary too.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary", role="primary", degree_lane="ba"),
            MetricSpec(name="starting salary", role="comparison", degree_lane="ma"),
        ],
    )

    specs = _ranking_result_metric_specs(plan, _result([_row(1, "Alpha", 90000.0, rank=1)]), None)

    assert {(spec.role, spec.degree_lane) for spec in specs} == {
        ("primary", "ba"),
        ("comparison", "ma"),
    }


def test_validation_accepts_cross_topic_ranking_with_comparison_metric() -> None:
    """#1212: a cross-topic ranking that carries one ``primary`` policy metric
    plus a ``comparison`` policy column (e.g. rank by starting salary, show years
    of experience side by side) must validate. The ranking branch previously
    required EXACTLY one primary metric and rejected this with
    ``unsupported_metric_plan`` — even though every row consistently references
    the primary metric. The fix mirrors the lookup branch: allow one primary
    plus comparisons; row-level consistency is still enforced by
    ``metric_row_mismatch``."""

    plan = QueryPlan(
        operation="rank",
        question=(
            "show me 10 districts with the highest starting teacher salary and "
            "their average years of experience"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary", role="primary"),
            MetricSpec(name="average years of experience", role="comparison"),
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 90000.0, rank=1),
            _row(2, "Bravo", 70000.0, rank=2),
        ]
    )

    report = validate_result(plan, result)

    assert "unsupported_metric_plan" not in [
        finding.code for finding in report.findings
    ], report.findings


def test_validation_still_rejects_cross_topic_ranking_with_mismatched_rows() -> None:
    """#1212 guard: relaxing the one-primary limit must NOT let a genuinely
    broken multi-metric ranking through. When rows reference different metrics,
    ``metric_row_mismatch`` must still fail the report."""

    plan = QueryPlan(
        operation="rank",
        question="rank by starting salary, show years of experience",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary", role="primary"),
            MetricSpec(name="average years of experience", role="comparison"),
        ],
    )
    result = _result(
        [
            _row(1, "Alpha", 90000.0, rank=1),
            _row(2, "Bravo", 70000.0, rank=2, metric_id=9999),
        ]
    )

    report = validate_result(plan, result)

    assert report.valid is False
    assert "metric_row_mismatch" in [finding.code for finding in report.findings]


def _finalized_value_lookup_plan(direction: str = "desc") -> QueryPlan:
    """Build a single-metric lookup plan sorted by value, then finalize it.

    The finalizer folds ``plan.sort`` into a presentation-phase SortStepSpec and
    clears ``plan.sort`` — exactly the shape the executor and validator twins
    must read post-finalize.
    """
    from compass_backend.contracts.catalog_resolution import (
        CatalogReconciliationReport,
    )
    from compass_backend.planning.finalizer import finalize_plan

    draft = QueryPlan(
        operation="lookup",
        question="Average teacher starting salary for these districts, highest first.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha", "Bravo"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        sort=SortSpec(field="Average teacher starting salary", direction=direction),
    )
    finalized = finalize_plan(draft, CatalogReconciliationReport()).plan
    assert finalized.sort is None
    return finalized


def _value_lookup_result(rows: list[MetricValueRow]) -> ResultSet:
    return MetricLookupResult(
        selection=_named_selection(district_ids=[1, 2]),
        rows=rows,
        citations=[
            _citation(row.citation_markers[0], district_id=row.district_id)
            for row in rows
        ],
        total_considered=len(rows),
        excluded_count=0,
        coverage_frame=_coverage_frame_for_rows(rows),
        order_statement="Looked up the selected metric for selected districts.",
        source_notes=[],
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
    )


def test_lookup_validator_twins_resolve_value_desc_after_finalize() -> None:
    """The validator twins must read the folded sort step, not the always-None
    ``plan.sort``, so they classify a finalized value-desc lookup as value/desc."""
    from compass_backend.quality.validators.coverage import (
        _lookup_direction,
        _lookup_sort_kind,
    )

    finalized = _finalized_value_lookup_plan(direction="desc")
    result = _value_lookup_result(
        [
            _lookup_value_row(2, "Bravo", 9876, "Average teacher starting salary",
                              "$80,000", citation_marker=1),
            _lookup_value_row(1, "Alpha", 9876, "Average teacher starting salary",
                              "$50,000", citation_marker=2),
        ]
    )
    assert _lookup_sort_kind(finalized, result) == "value"
    assert _lookup_direction(finalized, result) == "desc"


def test_lookup_validator_flags_value_desc_rows_left_district_ascending() -> None:
    """Consistency-validator blindness regression: a finalized value-desc lookup
    whose rows are left in district-name-ascending order (the executor's
    pre-fix wrong order) must now be flagged ``sort_order_mismatch``.

    Pre-fix both validator twins defaulted to district/asc on the always-None
    ``plan.sort``, so the validator's expected order matched the executor's wrong
    order and no finding fired.
    """
    from compass_backend.quality.validators.coverage import _validate_lookup_order

    finalized = _finalized_value_lookup_plan(direction="desc")
    # Rows physically in district-name-ascending order ($50k Alpha, $80k Bravo),
    # which contradicts the requested value-descending intent.
    rows = [
        _lookup_value_row(1, "Alpha", 9876, "Average teacher starting salary",
                          "$50,000", citation_marker=1),
        _lookup_value_row(2, "Bravo", 9876, "Average teacher starting salary",
                          "$80,000", citation_marker=2),
    ]
    result = _value_lookup_result(rows)
    findings = _validate_lookup_order(finalized, result)
    assert [f.code for f in findings] == ["sort_order_mismatch"]
    assert findings[0].metadata["direction"] == "desc"


def test_lookup_validator_passes_value_desc_rows_correctly_ordered() -> None:
    """Same finalized plan, but rows in the requested value-descending order:
    the validator must NOT flag a mismatch (no false positive)."""
    from compass_backend.quality.validators.coverage import _validate_lookup_order

    finalized = _finalized_value_lookup_plan(direction="desc")
    rows = [
        _lookup_value_row(2, "Bravo", 9876, "Average teacher starting salary",
                          "$80,000", citation_marker=1),
        _lookup_value_row(1, "Alpha", 9876, "Average teacher starting salary",
                          "$50,000", citation_marker=2),
    ]
    result = _value_lookup_result(rows)
    assert _validate_lookup_order(finalized, result) == []


# ---------------------------------------------------------------------------
# C4 (#1416) — fact-coverage guard: no computed fact silently dropped
# ---------------------------------------------------------------------------

# The literal pre-C1 count shape (jargon lead + table, NO district names) for
# the _state_count_result fixture — the issue's exit criterion is that the
# guard fires red on exactly this body while the artifact holds the names.
_PRE_C1_COUNT_BODY = (
    "I counted qualifying covered districts.\n"
    "\n"
    "| Metric | Count | Denominator | Filter |\n"
    "| --- | ---: | ---: | --- |\n"
    "| Average teacher starting salary | 2 | 2 | reviewed current value |\n"
    "\n"
    "Methodology\n"
    "\n"
    "- Denominator counts only districts with a current reviewed answer for this metric."
)


def test_validation_flags_count_body_that_drops_qualifying_names() -> None:
    """C4 red proof: the pre-C1 count body fails fact-coverage.

    The artifact holds qualifying_district_ids joined to named selection
    districts, but the body never surfaces the names — the lossy shape that
    sat live for weeks. The guard fires (warning severity: incompleteness is
    not falsehood, so the answer still ships while we calibrate).
    """
    plan = _state_count_plan()
    result = _state_count_result()

    report = validate_result(plan, result, rendered_body=_PRE_C1_COUNT_BODY)

    fact_findings = [f for f in report.findings if f.dimension == "fact_coverage"]
    assert [f.code for f in fact_findings] == ["count_names_not_surfaced"]
    assert fact_findings[0].severity == "warning"
    assert fact_findings[0].metadata["missing_names"] == [
        "District 101",
        "District 102",
    ]
    # Warning-first: the report stays valid; the finding is observability.
    assert report.valid is True


def test_validation_flags_count_body_that_drops_count_token() -> None:
    """C4: a count body missing the count itself fails fact-coverage."""
    plan = _state_count_plan()
    result = _state_count_result()
    body = (
        "Some districts match your criteria.\n\n"
        "Matching districts (District 101, District 102) are shown above."
    )

    report = validate_result(plan, result, rendered_body=body)

    codes = [f.code for f in report.findings if f.dimension == "fact_coverage"]
    assert "count_token_not_surfaced" in codes


def test_validation_accepts_count_body_with_names_and_tokens() -> None:
    """C4 green at the validator level: names + count/denominator present."""
    plan = _state_count_plan()
    result = _state_count_result()
    body = (
        "Of 2 covered districts with data, 2 match your criteria.\n\n"
        "| Metric | Count | Denominator | Filter |\n"
        "| --- | ---: | ---: | --- |\n"
        "| Average teacher starting salary | 2 | 2 | reviewed current value |\n\n"
        "Matching districts (2):\n\n"
        "| District | State |\n| --- | --- |\n"
        "| District 101 | TX |\n| District 102 | TX |"
    )

    report = validate_result(plan, result, rendered_body=body)

    assert [f for f in report.findings if f.dimension == "fact_coverage"] == []


def test_post_render_validation_adds_fact_coverage_dimension() -> None:
    """fact_coverage joins dimensions_checked only when a body is provided."""
    plan = _state_count_plan()
    result = _state_count_result()

    pre = validate_result(plan, result)
    post = validate_result(plan, result, rendered_body=_PRE_C1_COUNT_BODY)

    assert "fact_coverage" not in pre.dimensions_checked
    assert "fact_coverage" in post.dimensions_checked
