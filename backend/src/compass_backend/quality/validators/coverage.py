"""Validator group split from quality/validation.py.

Each function returns a list of ValidationFinding. The module is private
in spirit (consumed only by quality/validation.py); names retain their
underscore prefix from the original monolith.
"""

from __future__ import annotations

from collections import Counter

from compass_backend.artifacts import (
    COUNT_ROW_TYPES,
    STALE_PRIOR_VALUE_MARKER,
    CountRow,
    ResultSet,
    coverage_frame_from_state_counts,
)
from compass_backend.contracts.planning import QueryPlan
from compass_backend.contracts.validation import (
    ValidationAuthority,
    ValidationFinding,
)

from compass_backend.execution._text_utils import parse_compass_numeric_value
from compass_backend.execution.selection import (
    direction as _direction,
    primary_sort_step as _primary_sort_step,
    requested_enrollment_bounds as _requested_enrollment_bounds,
    sort_field as _sort_field,
    sort_kind as _sort_kind,
)
from compass_backend.rendering.formatting import (
    MATCHING_DISTRICTS_DISPLAY_LIMIT,
    MIN_DENOMINATOR_FOR_PERCENT,
    format_percent,
)

from .._validation_helpers import (
    _NUMERIC_TOKEN_RE,
    _FORBIDDEN_COVERAGE_WORDING,
    _REASON_BREAKDOWN_FIELDS,
    _finding,
    _normalize_numeric_token,
)


def _lookup_direction(
    plan: QueryPlan,
    result: ResultSet | None = None,
    *,
    sort_kind: str | None = None,
) -> str:
    """Sort direction for lookup rendering — lives here because it depends on
    `_lookup_sort_kind` (defined below in this module)."""

    # Canonical read: mirror execution.lookup._lookup_direction_for_kind so the
    # validator's expected order tracks the executor's. Post-finalize plan.sort
    # is always None and the requested direction lives on the folded
    # presentation-phase SortStepSpec.
    if plan.sort is None and _primary_sort_step(plan) is None:
        return "asc"
    active_sort_kind = sort_kind
    if active_sort_kind is None and result is not None:
        active_sort_kind = _lookup_sort_kind(plan, result)
    field = _sort_field(plan)
    normalized = field.casefold().strip() if field is not None else ""
    if active_sort_kind == "district" and normalized not in {
        "district",
        "district_name",
    }:
        return "asc"
    return _direction(plan)


def _validate_denominator(result: ResultSet) -> list[ValidationFinding]:
    if result.result_type != "metric_count":
        return []
    count_rows = [row for row in result.rows if isinstance(row, COUNT_ROW_TYPES)]
    if not count_rows:
        return []
    findings: list[ValidationFinding] = []
    categorical_rows = [
        row for row in count_rows if row.count_kind == "categorical_value_count"
    ]
    non_categorical_rows = [
        row for row in count_rows if row.count_kind != "categorical_value_count"
    ]
    for row in count_rows:
        unique_qualifying_ids = set(row.qualifying_district_ids)
        if (
            len(unique_qualifying_ids) != len(row.qualifying_district_ids)
            or row.count != len(unique_qualifying_ids)
        ):
            findings.append(
                _finding(
                    code="count_qualifying_ids_mismatch",
                    dimension="denominator",
                    message=(
                        "Count rows must agree with their qualifying district IDs."
                    ),
                    metadata={
                        "metric_id": row.metric_id,
                        "count": row.count,
                        "qualifying_district_ids": row.qualifying_district_ids,
                    },
                )
            )
        if row.count > row.denominator:
            findings.append(
                _finding(
                    code="denominator_mismatch",
                    dimension="denominator",
                    message="Count cannot exceed its denominator.",
                    metadata={
                        "metric_id": row.metric_id,
                        "count": row.count,
                        "denominator": row.denominator,
                    },
                )
            )
    findings.extend(_validate_categorical_count_denominators(categorical_rows, result))
    selection_district_count = (
        len(result.selection.districts) if result.selection is not None else None
    )
    if selection_district_count is not None:
        for row in count_rows:
            if row.denominator > selection_district_count:
                findings.append(
                    _finding(
                        code="denominator_mismatch",
                        dimension="denominator",
                        message=(
                            "Count denominator cannot exceed resolved selection size."
                        ),
                        metadata={
                            "metric_id": row.metric_id,
                            "denominator": row.denominator,
                            "selection_district_count": selection_district_count,
                        },
                    )
                )
    if categorical_rows:
        return findings
    # A topic-coverage count's denominator is the covered-district UNIVERSE
    # (e.g. "121 of 133 have evaluation data"), not the reviewed-data count, so
    # it is validated against universe_count below and excluded from the
    # reviewed-data denominator checks that apply to threshold counts.
    topic_coverage_rows = [
        row
        for row in non_categorical_rows
        if getattr(row, "coverage_reason", None) == "topic_coverage_count"
    ]
    non_categorical_rows = [
        row
        for row in non_categorical_rows
        if getattr(row, "coverage_reason", None) != "topic_coverage_count"
    ]
    if topic_coverage_rows and result.coverage_frame is not None:
        universe_count = result.coverage_frame.universe_count
        for row in topic_coverage_rows:
            if row.denominator != universe_count:
                findings.append(
                    _finding(
                        code="denominator_mismatch",
                        dimension="denominator",
                        message=(
                            "Topic-coverage count denominator must be the covered "
                            "universe."
                        ),
                        metadata={
                            "metric_id": row.metric_id,
                            "denominator": row.denominator,
                            "universe_count": universe_count,
                        },
                    )
                )
    if len(non_categorical_rows) == 1 and result.coverage_frame is not None:
        real_data_count = result.coverage_frame.real_data_count
        if non_categorical_rows[0].denominator != real_data_count:
            findings.append(
                _finding(
                    code="denominator_mismatch",
                    dimension="denominator",
                    message="Single-metric count denominator must match real-data coverage.",
                    metadata={
                        "metric_id": non_categorical_rows[0].metric_id,
                        "denominator": non_categorical_rows[0].denominator,
                        "real_data_count": real_data_count,
                    },
                )
            )
    if len(non_categorical_rows) > 1 and result.coverage_frame is not None:
        total_denominator = sum(row.denominator for row in non_categorical_rows)
        if total_denominator != result.coverage_frame.real_data_count:
            findings.append(
                _finding(
                    code="denominator_mismatch",
                    dimension="denominator",
                    message=(
                        "Count denominators must sum to real-data coverage across "
                        "multi-metric count artifacts."
                    ),
                    metadata={
                        "denominator_sum": total_denominator,
                        "real_data_count": result.coverage_frame.real_data_count,
                    },
                )
            )
    return findings


def _validate_categorical_count_denominators(
    rows: list[CountRow],
    result: ResultSet,
) -> list[ValidationFinding]:
    if not rows:
        return []
    findings: list[ValidationFinding] = []
    rows_by_metric: dict[int, list[CountRow]] = {}
    for row in rows:
        rows_by_metric.setdefault(row.metric_id, []).append(row)

    selection_district_count = (
        len(result.selection.districts) if result.selection is not None else None
    )
    for metric_id, metric_rows in rows_by_metric.items():
        denominators = {row.denominator for row in metric_rows}
        if len(denominators) != 1:
            findings.append(
                _finding(
                    code="denominator_mismatch",
                    dimension="denominator",
                    message="Categorical count rows must share one denominator per metric.",
                    metadata={
                        "metric_id": metric_id,
                        "denominators": sorted(denominators),
                    },
                )
            )
            continue
        denominator = next(iter(denominators))
        if (
            selection_district_count is not None
            and denominator != selection_district_count
        ):
            findings.append(
                _finding(
                    code="denominator_mismatch",
                    dimension="denominator",
                    message=(
                        "Categorical count denominator must match the resolved "
                        "covered-district selection size."
                    ),
                    metadata={
                        "metric_id": metric_id,
                        "denominator": denominator,
                        "selection_district_count": selection_district_count,
                    },
                )
            )
        total_count = sum(row.count for row in metric_rows)
        if total_count != denominator:
            findings.append(
                _finding(
                    code="denominator_mismatch",
                    dimension="denominator",
                    message=(
                        "Categorical count rows must sum to their covered "
                        "selection denominator."
                    ),
                    metadata={
                        "metric_id": metric_id,
                        "count_sum": total_count,
                        "denominator": denominator,
                    },
                )
            )
    return findings


def _validate_temporal_data_fidelity(
    plan: QueryPlan,
    result: ResultSet,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    temporal = plan.temporal
    if temporal.intent == "specific_year" and temporal.academic_year:
        mismatched_rows = [
            {
                "district_id": row.district_id,
                "metric_id": row.metric_id,
                "academic_year": row.academic_year,
            }
            for row in result.rows
            if row.academic_year != temporal.academic_year
        ]
        if mismatched_rows:
            findings.append(
                _finding(
                    code="requested_year_mismatch",
                    dimension="data_fidelity",
                    message="Historical lookup rows must match the requested year.",
                    metadata={
                        "requested_academic_year": temporal.academic_year,
                        "rows": mismatched_rows,
                    },
                )
            )

    if result.result_type != "metric_trend":
        return findings

    row_count = len(result.rows)
    if row_count > 30:
        findings.append(
            _finding(
                code="trend_row_budget_exceeded",
                dimension="data_fidelity",
                message="Trend artifacts must stay within the deterministic row budget.",
                metadata={"row_count": row_count, "max_rows": 30},
            )
        )

    distinct_years = _distinct_academic_years(result)
    if len(distinct_years) < 2:
        findings.append(
            _finding(
                code="trend_requires_multiple_years",
                dimension="data_fidelity",
                message="Trend results must contain multiple distinct academic years.",
                metadata={"academic_years": distinct_years},
            )
        )

    expected_years = temporal.academic_years
    if expected_years:
        unexpected_years = sorted(set(distinct_years) - set(expected_years))
        missing_years = sorted(set(expected_years) - set(distinct_years))
        if unexpected_years or missing_years:
            findings.append(
                _finding(
                    code="trend_years_mismatch",
                    dimension="data_fidelity",
                    message="Trend rows must match the normalized temporal year list.",
                    metadata={
                        "expected_academic_years": expected_years,
                        "unexpected_academic_years": unexpected_years,
                        "missing_academic_years": missing_years,
                    },
                )
            )

    for row in result.rows:
        if row.coverage_state == "covered" and not row.citation_markers:
            findings.append(
                _finding(
                    code="trend_row_missing_citation_marker",
                    dimension="data_fidelity",
                    message="Covered trend rows must carry year-specific citations.",
                    metadata={
                        "district_id": row.district_id,
                        "metric_id": row.metric_id,
                        "academic_year": row.academic_year,
                    },
                )
            )
            break
    return findings


def _distinct_academic_years(result: ResultSet) -> list[str]:
    years: list[str] = []
    for row in result.rows:
        if row.academic_year not in years:
            years.append(row.academic_year)
    return years


def _validate_metric_authority(
    result: ResultSet,
    authority: ValidationAuthority | None,
) -> list[ValidationFinding]:
    if authority is None or not authority.metrics:
        return []

    resolved_metric_ids = {metric.metric_id for metric in authority.metrics}
    result_metric_ids = {row.metric_id for row in result.rows}
    unauthorized_metric_ids = sorted(result_metric_ids - resolved_metric_ids)
    if not unauthorized_metric_ids:
        return []
    return [
        _finding(
            code="metric_authority_mismatch",
            dimension="metric",
            message="Result rows include metrics outside resolved catalog authority.",
            metadata={
                "resolved_metric_ids": sorted(resolved_metric_ids),
                "result_metric_ids": sorted(result_metric_ids),
                "unauthorized_metric_ids": unauthorized_metric_ids,
            },
        )
    ]


def _validate_lookup_criteria(result: ResultSet) -> list[ValidationFinding]:
    if result.result_type != "metric_lookup" or not result.criteria:
        return []

    findings: list[ValidationFinding] = []
    criteria_by_id = {criterion.criterion_id: criterion for criterion in result.criteria}
    criterion_ids = set(criteria_by_id)
    qualifying_sets = [
        set(criterion.qualifying_district_ids) for criterion in result.criteria
    ]
    qualifying_intersection = (
        set.intersection(*qualifying_sets) if qualifying_sets else set()
    )

    row_district_ids = {
        row.district_id for row in result.rows if row.district_id is not None
    }
    non_qualifying_rows = sorted(row_district_ids - qualifying_intersection)
    if non_qualifying_rows:
        findings.append(
            _finding(
                code="criteria_non_qualifying_district_returned",
                dimension="selection",
                message=(
                    "Intersection lookup rows include districts that do not "
                    "satisfy every criterion."
                ),
                metadata={"district_ids": non_qualifying_rows},
            )
        )

    missing_criteria_rows: list[dict[str, object]] = []
    rows_by_district: dict[int, set[str]] = {}
    for row in result.rows:
        if row.district_id is None:
            continue
        criterion_id = getattr(row, "criterion_id", None)
        if criterion_id:
            rows_by_district.setdefault(row.district_id, set()).add(criterion_id)
    for district_id in sorted(qualifying_intersection):
        missing = sorted(criterion_ids - rows_by_district.get(district_id, set()))
        if missing:
            missing_criteria_rows.append(
                {"district_id": district_id, "missing_criteria": missing}
            )
    if missing_criteria_rows:
        findings.append(
            _finding(
                code="criteria_evidence_rows_missing",
                dimension="surface_consistency",
                message=(
                    "Every qualifying district must include evidence rows for "
                    "each satisfied criterion."
                ),
                metadata={"rows": missing_criteria_rows},
            )
        )

    invalid_rows: list[dict[str, object]] = []
    for row in result.rows:
        criterion_id = getattr(row, "criterion_id", None)
        criterion = criteria_by_id.get(criterion_id or "")
        if criterion is None:
            invalid_rows.append(
                {
                    "district_id": row.district_id,
                    "metric_id": row.metric_id,
                    "criterion_id": criterion_id,
                }
            )
            continue
        if row.metric_id not in criterion.metric_ids or (
            getattr(row, "criterion_satisfied", None) is not True
        ):
            invalid_rows.append(
                {
                    "district_id": row.district_id,
                    "metric_id": row.metric_id,
                    "criterion_id": criterion_id,
                    "criterion_satisfied": getattr(row, "criterion_satisfied", None),
                }
            )
    if invalid_rows:
        findings.append(
            _finding(
                code="criteria_row_metadata_invalid",
                dimension="filter",
                message=(
                    "Intersection lookup rows must be tied to a satisfied "
                    "criterion and one of that criterion's resolved metrics."
                ),
                metadata={"rows": invalid_rows},
            )
        )

    return findings


def _validate_sort_order(
    plan: QueryPlan,
    result: ResultSet,
) -> list[ValidationFinding]:
    if not result.rows:
        return []
    if result.result_type == "metric_count":
        return []
    if result.result_type == "metric_trend":
        return _validate_trend_order(result)
    if result.result_type == "profile_lookup":
        return []
    if result.result_type == "peer_comparison":
        return _validate_peer_order(result)
    if result.result_type == "metric_lookup":
        return _validate_lookup_order(plan, result)

    expected_ranks = list(range(1, len(result.rows) + 1))
    actual_ranks = [row.rank for row in result.rows]
    if actual_ranks != expected_ranks:
        return [
            _finding(
                code="rank_sequence_mismatch",
                dimension="sort_order",
                message="Ranking rows are not numbered sequentially from 1.",
                metadata={"actual_ranks": actual_ranks},
            )
        ]

    reverse = _direction(plan) == "desc"
    # Split rows into the ranked block (rows that participate in ordering)
    # and coverage-state placeholders that the executor appends after the
    # ranked block when an explicit-scope selection includes districts with
    # no current data. A placeholder is a row whose sort key cannot be
    # determined — _ranking_sort_value falls back through sort_value and
    # row.value and returns None when both are missing or non-numeric.
    # In profile-ordered rankings every row carries sort_value (from the
    # profile field) so this discriminator correctly excludes them from the
    # placeholder bucket.
    sort_value_by_row = {id(row): _ranking_sort_value(row) for row in result.rows}
    ranked_rows = [
        row for row in result.rows if sort_value_by_row[id(row)] is not None
    ]
    placeholder_rows = [
        row for row in result.rows if sort_value_by_row[id(row)] is None
    ]
    if placeholder_rows and ranked_rows:
        last_ranked_rank = max(row.rank for row in ranked_rows)
        misplaced = [
            row.district_id
            for row in placeholder_rows
            if row.rank <= last_ranked_rank
        ]
        if misplaced:
            return [
                _finding(
                    code="placeholder_row_misplaced",
                    dimension="sort_order",
                    message=(
                        "Coverage-state placeholder rows must follow all ranked rows."
                    ),
                    metadata={"district_ids": misplaced},
                )
            ]
    expected_rows = sorted(
        ranked_rows,
        key=lambda row: (
            -(_ranking_sort_value(row) or 0.0)
            if reverse
            else (_ranking_sort_value(row) or 0.0),
            row.district_name.casefold(),
        ),
    )
    actual_identity = [
        (row.district_id, _ranking_sort_value(row), row.district_name)
        for row in ranked_rows
    ]
    expected_identity = [
        (row.district_id, _ranking_sort_value(row), row.district_name)
        for row in expected_rows
    ]
    if actual_identity != expected_identity:
        return [
            _finding(
                code="sort_order_mismatch",
                dimension="sort_order",
                message="Ranking row order does not match the requested sort direction.",
                metadata={"direction": _direction(plan)},
            )
        ]
    return []


def _ranking_sort_value(row) -> float | None:
    sort_value = getattr(row, "sort_value", None)
    if sort_value is not None:
        return float(sort_value)
    return parse_compass_numeric_value(getattr(row, "value", None))


def _validate_peer_order(result: ResultSet) -> list[ValidationFinding]:
    rows_by_metric: dict[int, list[object]] = {}
    for row in result.rows:
        rows_by_metric.setdefault(row.metric_id, []).append(row)
    for rows in rows_by_metric.values():
        expected = sorted(
            rows,
            key=lambda row: (
                0 if getattr(row, "peer_role", "") == "anchor" else 1,
                getattr(row, "peer_rank", None) or 0,
                row.district_name.casefold(),
            ),
        )
        if [
            (row.district_id, row.metric_id)
            for row in rows
        ] != [
            (row.district_id, row.metric_id)
            for row in expected
        ]:
            return [
                _finding(
                    code="peer_order_mismatch",
                    dimension="sort_order",
                    message="Peer rows must be ordered anchor first, then peer rank.",
                    metadata={},
                )
            ]
    return []


def _validate_lookup_order(
    plan: QueryPlan,
    result: ResultSet,
) -> list[ValidationFinding]:
    ranking_lookup_findings = _validate_ranked_lookup_selection_order(plan, result)
    if ranking_lookup_findings is not None:
        return ranking_lookup_findings

    sort_kind = _lookup_sort_kind(plan, result)
    reverse = _lookup_direction(plan, result, sort_kind=sort_kind) == "desc"
    expected_rows = list(result.rows)
    expected_rows.sort(key=lambda row: row.metric_name.casefold())
    if sort_kind == "value":
        expected_rows.sort(key=lambda row: _lookup_value_sort_key(row, reverse=reverse))
    else:
        expected_rows.sort(
            key=lambda row: row.district_name.casefold(),
            reverse=reverse,
        )
    actual_identity = [
        (row.district_id, row.district_name, row.metric_id, row.metric_name)
        for row in result.rows
    ]
    expected_identity = [
        (row.district_id, row.district_name, row.metric_id, row.metric_name)
        for row in expected_rows
    ]
    if actual_identity == expected_identity:
        return []
    return [
        _finding(
            code="sort_order_mismatch",
            dimension="sort_order",
            message="Lookup rows are not ordered by district name as requested.",
            metadata={"direction": _lookup_direction(plan)},
        )
    ]


def _validate_ranked_lookup_selection_order(
    plan: QueryPlan,
    result: ResultSet,
) -> list[ValidationFinding] | None:
    """Validate rank-then-lookup tables against the selected ranking order."""

    if (
        plan.operation != "rank"
        or plan.limit is None
        or len({row.metric_id for row in result.rows}) <= 1
    ):
        return None
    if result.selection is None or not result.selection.districts:
        return None

    district_order = {
        district.district_id: index
        for index, district in enumerate(result.selection.districts)
    }
    metric_order: dict[int, int] = {}
    for row in result.rows:
        metric_order.setdefault(row.metric_id, len(metric_order))

    expected_rows = sorted(
        result.rows,
        key=lambda row: (
            district_order.get(row.district_id, len(district_order)),
            metric_order.get(row.metric_id, len(metric_order)),
            row.metric_name.casefold(),
        ),
    )
    actual_identity = [
        (row.district_id, row.district_name, row.metric_id, row.metric_name)
        for row in result.rows
    ]
    expected_identity = [
        (row.district_id, row.district_name, row.metric_id, row.metric_name)
        for row in expected_rows
    ]
    if actual_identity == expected_identity:
        return []
    return [
        _finding(
            code="sort_order_mismatch",
            dimension="sort_order",
            message="Lookup rows do not preserve the selected ranking order.",
            metadata={"direction": _lookup_direction(plan)},
        )
    ]


def _validate_trend_order(result: ResultSet) -> list[ValidationFinding]:
    actual_identity = [
        (
            row.district_name.casefold(),
            row.metric_id,
            _academic_year_start(row.academic_year),
        )
        for row in result.rows
    ]
    expected_identity = sorted(actual_identity)
    if actual_identity == expected_identity:
        return []
    return [
        _finding(
            code="trend_chronological_order_mismatch",
            dimension="sort_order",
            message="Trend rows must be ordered by district, metric, then academic year.",
            metadata={"row_count": len(result.rows)},
        )
    ]


def _validate_surface_consistency(
    plan: QueryPlan,
    result: ResultSet,
) -> list[ValidationFinding]:
    if result.result_type != "metric_ranking":
        return []
    if plan.limit is not None and plan.limit.kind != "all":
        return []
    if plan.selection.scope not in {"all_covered_districts", "state"}:
        return []

    eligible_row_count = max(0, result.total_considered - result.excluded_count)
    if len(result.rows) >= eligible_row_count:
        return []
    return [
        _finding(
            code="unbounded_ranking_truncated",
            dimension="surface_consistency",
            message=(
                "Unbounded ranking artifacts must retain all eligible rows; "
                "chat previews belong in rendering metadata, not ResultSet.rows."
            ),
            metadata={
                "row_count": len(result.rows),
                "eligible_row_count": eligible_row_count,
                "total_considered": result.total_considered,
                "excluded_count": result.excluded_count,
                "selection_scope": plan.selection.scope,
            },
        )
    ]


def _lookup_sort_kind(plan: QueryPlan, result: ResultSet) -> str:
    # Canonical read: mirror execution.lookup._lookup_sort_kind. Post-finalize
    # plan.sort is None and the value/district intent lives on the folded
    # presentation-phase SortStepSpec. A multi-metric non-rank lookup cannot be
    # value-sorted by the row-sorter, so it collapses to district order.
    if (
        len({row.metric_id for row in result.rows}) > 1
        and plan.operation != "rank"
    ):
        return "district"
    result_metric_names = frozenset(
        row.metric_name.casefold().strip() for row in result.rows
    )
    return _sort_kind(plan, metric_names=result_metric_names)


def _lookup_value_sort_key(row, *, reverse: bool) -> tuple[object, ...]:
    numeric_value = parse_compass_numeric_value(row.value)
    district_name = row.district_name.casefold()
    if numeric_value is not None:
        return (0, -numeric_value if reverse else numeric_value, district_name)
    if row.value is not None:
        return (1, str(row.value).casefold(), district_name)
    return (2, district_name)


def _academic_year_start(value: str) -> int:
    parts = value.split(" - ", 1)
    try:
        return int(parts[0])
    except (ValueError, IndexError):
        return 0


def _validate_citation_coverage(result: ResultSet) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    citation_markers = [citation.marker for citation in result.citations]
    citation_marker_set = set(citation_markers)
    referenced_markers = {
        marker for row in result.rows for marker in row.citation_markers
    }

    duplicate_markers = [
        marker for marker, count in Counter(citation_markers).items() if count > 1
    ]
    if duplicate_markers:
        findings.append(
            _finding(
                code="citation_marker_duplicate",
                dimension="citation_coverage",
                message="Result citations contain duplicate markers.",
                metadata={"markers": duplicate_markers},
            )
        )

    for row in result.rows:
        if row.coverage_state == "out_of_universe" and row.citation_markers:
            findings.append(
                _finding(
                    code="out_of_universe_row_has_citation",
                    dimension="citation_coverage",
                    message="Out-of-universe rows must not carry citation markers.",
                    metadata={"district_name": row.district_name},
                )
            )
            break
        if (
            result.result_type != "metric_count"
            and row.coverage_state == "covered"
            and getattr(row, "source", None) == "policy_answer"
            and not row.citation_markers
        ):
            findings.append(
                _finding(
                    code="row_missing_citation_marker",
                    dimension="citation_coverage",
                    message="A retained covered row has no citation marker.",
                    metadata={"district_id": row.district_id},
                )
            )
            break

    missing_markers = sorted(referenced_markers - citation_marker_set)
    if missing_markers:
        findings.append(
            _finding(
                code="citation_marker_missing",
                dimension="citation_coverage",
                message="A ranking row references a citation marker not in the result.",
                metadata={"markers": missing_markers},
            )
        )

    unreferenced_markers = sorted(citation_marker_set - referenced_markers)
    if unreferenced_markers:
        findings.append(
            _finding(
                code="citation_not_referenced",
                dimension="citation_coverage",
                message="Result citations include markers not referenced by any row.",
                metadata={"markers": unreferenced_markers},
            )
        )
    return findings


def _validate_coverage_state(result: ResultSet) -> list[ValidationFinding]:
    # #1242 WS-3: a similarity (peer-set DISCOVERY) result has no policy-metric
    # coverage — its rows are NCES-derived peer scores and its coverage frame is
    # zeroed by design. Row display values ("anchor", "0.90") differ from the
    # canonical coverage display ("Anchor district", "Similarity score: 0.90"),
    # so the metric-coverage invariants here (display / frame-count / breakdown
    # mismatch) would false-fire on a valid peer set (S101/S94
    # manifest_validation_failed). The peer set's grounding and order are
    # validated elsewhere.
    if getattr(result, "is_similarity_result", False):
        return []
    findings: list[ValidationFinding] = []
    missing_rows = [
        row.district_id
        for row in result.rows
        if row.coverage_state is None or row.coverage_display is None
    ]
    if missing_rows:
        findings.append(
            _finding(
                code="coverage_metadata_missing",
                dimension="coverage_state",
                message="Result rows must carry deterministic coverage metadata.",
                metadata={"district_ids": missing_rows},
            )
        )

    mismatched_rows = [
        {
            "district_id": row.district_id,
            "metric_id": row.metric_id,
            "display_value": row.display_value,
            "coverage_display": row.coverage_display,
        }
        for row in result.rows
        if row.coverage_display is not None and row.display_value != row.coverage_display
    ]
    if mismatched_rows:
        findings.append(
            _finding(
                code="coverage_display_mismatch",
                dimension="coverage_state",
                message="Row display values must match the canonical coverage display.",
                metadata={"rows": mismatched_rows},
            )
        )

    stale_current_rows = [
        {
            "district_id": row.district_id,
            "metric_id": row.metric_id,
            "coverage_display": row.coverage_display,
        }
        for row in result.rows
        if row.coverage_reason == "stale_recent_answer"
        and (
            row.coverage_display is None
            # #1514 D7: the canonical stale sentence is built around this
            # marker (artifacts/coverage.py) — import it, never restate the
            # display literal here.
            or STALE_PRIOR_VALUE_MARKER not in row.coverage_display
        )
    ]
    if stale_current_rows:
        findings.append(
            _finding(
                code="stale_coverage_display_invalid",
                dimension="coverage_state",
                message=(
                    "Stale coverage rows must disclose the prior reviewed value "
                    "instead of displaying it as current data."
                ),
                metadata={"rows": stale_current_rows},
            )
        )

    frame = result.coverage_frame
    if frame is None:
        findings.append(
            _finding(
                code="coverage_frame_missing",
                dimension="coverage_state",
                message="Result sets must include aggregate coverage counts.",
                metadata={},
            )
        )
        return findings

    if isinstance(frame, dict):
        frame_values = frame
    else:
        frame_values = frame.model_dump()

    if frame_values["universe_count"] != result.total_considered:
        findings.append(
            _finding(
                code="coverage_frame_count_mismatch",
                dimension="coverage_state",
                message="Coverage frame universe count must match total considered rows.",
                metadata={
                    "universe_count": frame_values["universe_count"],
                    "total_considered": result.total_considered,
                },
            )
        )
    frame_mismatches = _coverage_frame_invariant_mismatches(frame_values)
    if frame_mismatches:
        findings.append(
            _finding(
                code="coverage_frame_count_mismatch",
                dimension="coverage_state",
                message="Coverage frame aggregate counts are internally inconsistent.",
                metadata={"mismatches": frame_mismatches},
            )
        )

    if result.result_type in {"metric_lookup", "metric_trend"}:
        state_counts = Counter(row.coverage_state for row in result.rows)
        expected_frame = coverage_frame_from_state_counts(
            state_counts,
            universe_count=len(result.rows),
        )
        expected = expected_frame.model_dump()
        expected.pop("breakdown", None)
        mismatches = {
            key: {"expected": value, "actual": frame_values.get(key)}
            for key, value in expected.items()
            if frame_values.get(key) != value
        }
        if mismatches:
            findings.append(
                _finding(
                    code="coverage_frame_count_mismatch",
                    dimension="coverage_state",
                    message="Coverage frame counts must match lookup result rows.",
                    metadata={"mismatches": mismatches},
                )
            )
    if result.result_type in {
        "metric_ranking",
        "metric_lookup",
        "metric_trend",
        "profile_lookup",
        "peer_comparison",
    }:
        breakdown_mismatches = _coverage_breakdown_mismatches(result, frame_values)
        if breakdown_mismatches:
            findings.append(
                _finding(
                    code="coverage_breakdown_count_mismatch",
                    dimension="coverage_state",
                    message=(
                        "Coverage frame reason counts must match row-level and "
                        "disclosure-level coverage reasons."
                    ),
                    metadata={"mismatches": breakdown_mismatches},
                )
            )
    if result.result_type == "peer_comparison":
        invalid_peer_rows = [
            {
                "district_id": row.district_id,
                "district_name": row.district_name,
                "metric_id": row.metric_id,
                "metric_name": row.metric_name,
                "coverage_state": row.coverage_state,
                "coverage_reason": row.coverage_reason,
            }
            for row in result.rows
            if getattr(row, "peer_role", None) == "peer"
            and row.coverage_state != "covered"
        ]
        if invalid_peer_rows:
            findings.append(
                _finding(
                    code="peer_comparison_peer_unreviewed_policy_cell",
                    dimension="coverage_state",
                    message=(
                        "Peer comparison rows must only include peers with "
                        "current covered data for displayed policy fields."
                    ),
                    metadata={"rows": invalid_peer_rows},
                )
            )
    if result.result_type == "metric_count":
        count_rows = [row for row in result.rows if isinstance(row, COUNT_ROW_TYPES)]
        if any(row.count_kind == "categorical_value_count" for row in count_rows):
            expected_real_data_count = sum(
                row.count
                for row in count_rows
                if row.count_kind == "categorical_value_count"
                and row.coverage_state == "covered"
            )
        elif all(
            getattr(row, "coverage_reason", None) == "topic_coverage_count"
            for row in count_rows
        ):
            # Topic-coverage count: the frame's real-data count is the number of
            # districts WITH topic data (the row count), not the denominator
            # (which is the covered universe); the rest are the not-reviewed gap.
            expected_real_data_count = sum(row.count for row in count_rows)
        else:
            expected_real_data_count = sum(row.denominator for row in count_rows)
        if count_rows and frame_values["real_data_count"] != expected_real_data_count:
            findings.append(
                _finding(
                    code="coverage_frame_count_mismatch",
                    dimension="coverage_state",
                    message=(
                        "Count coverage frame real-data count must match row "
                        "denominators."
                    ),
                    metadata={
                        "expected_real_data_count": expected_real_data_count,
                        "actual_real_data_count": frame_values["real_data_count"],
                    },
                )
            )
    return findings


def _coverage_breakdown_mismatches(
    result: ResultSet,
    frame_values: dict[str, object],
) -> dict[str, dict[str, object]]:
    breakdown = frame_values.get("breakdown")
    if not isinstance(breakdown, dict):
        return {"breakdown": {"expected": "object", "actual": breakdown}}

    expected = {field: 0 for field in _REASON_BREAKDOWN_FIELDS.values()}
    if result.result_type == "metric_ranking":
        expected.update(
            {
                field: int(breakdown.get(field, 0) or 0)
                for field in _REASON_BREAKDOWN_FIELDS.values()
            }
        )
        for field in (
            "issue_not_addressed_count",
            "not_applicable_count",
            "metric_not_reviewed_count",
            "district_not_reviewed_count",
            "stale_recent_answer_count",
            "non_numeric_rank_exclusion_count",
            "out_of_universe_count",
            # #1698: "unavailable" can now appear as a not_reviewed disclosure on
            # the ranking surface, so it must be zero-then-rebuilt from the rows/
            # disclosures like the other disclosure reasons — otherwise it is
            # copied from the breakdown AND re-incremented here (double-counted),
            # falsely tripping coverage_breakdown_count_mismatch (case 14 swallow).
            "unavailable_count",
        ):
            expected[field] = 0
        for row in result.rows:
            reason = _coverage_reason_for_row(row.coverage_state, row.coverage_reason)
            if reason in {"answer_value", "profile_field_value"}:
                continue
            field = _REASON_BREAKDOWN_FIELDS.get(reason)
            if field:
                expected[field] += 1
        for disclosure in result.coverage_disclosures:
            reason = _coverage_reason_for_row(
                disclosure.coverage_state,
                disclosure.reason,
            )
            field = _REASON_BREAKDOWN_FIELDS.get(reason)
            if field:
                expected[field] += 1
        return {
            field: {"expected": count, "actual": breakdown.get(field, 0)}
            for field, count in expected.items()
            if count != breakdown.get(field, 0)
        }

    for row in result.rows:
        reason = _coverage_reason_for_row(row.coverage_state, row.coverage_reason)
        field = _REASON_BREAKDOWN_FIELDS.get(reason)
        if field:
            expected[field] += 1
    for disclosure in result.coverage_disclosures:
        reason = _coverage_reason_for_row(disclosure.coverage_state, disclosure.reason)
        field = _REASON_BREAKDOWN_FIELDS.get(reason)
        if field:
            expected[field] += 1

    return {
        field: {"expected": count, "actual": breakdown.get(field, 0)}
        for field, count in expected.items()
        if count != breakdown.get(field, 0)
    }


def _coverage_reason_for_row(
    state: object,
    reason: object,
) -> str:
    if isinstance(reason, str) and reason in _REASON_BREAKDOWN_FIELDS:
        return reason
    if state == "covered":
        return "answer_value"
    if state == "ina":
        return "issue_not_addressed"
    if state == "na":
        return "not_applicable"
    if state == "out_of_universe":
        return "out_of_universe"
    return "metric_not_reviewed"


def _coverage_frame_invariant_mismatches(
    frame_values: dict[str, object],
) -> dict[str, dict[str, object]]:
    universe_count = _int_frame_value(frame_values, "universe_count")
    in_scope_count = _int_frame_value(frame_values, "in_scope_count")
    addressed_count = _int_frame_value(frame_values, "addressed_count")
    real_data_count = _int_frame_value(frame_values, "real_data_count")
    not_reviewed_count = _int_frame_value(frame_values, "not_reviewed_count")
    out_of_universe_count = _int_frame_value(frame_values, "out_of_universe_count")
    coverage_ratio = _float_frame_value(frame_values, "coverage_ratio")

    mismatches: dict[str, dict[str, object]] = {}
    expected_universe_count = in_scope_count + out_of_universe_count
    if universe_count != expected_universe_count:
        mismatches["universe_count"] = {
            "expected": expected_universe_count,
            "actual": universe_count,
        }
    expected_in_scope_count = addressed_count + not_reviewed_count
    if in_scope_count != expected_in_scope_count:
        mismatches["in_scope_count"] = {
            "expected": expected_in_scope_count,
            "actual": in_scope_count,
        }
    if real_data_count > addressed_count:
        mismatches["real_data_count"] = {
            "expected_max": addressed_count,
            "actual": real_data_count,
        }
    expected_coverage_ratio = (
        real_data_count / in_scope_count if in_scope_count else 0.0
    )
    if abs(coverage_ratio - expected_coverage_ratio) > 1e-9:
        mismatches["coverage_ratio"] = {
            "expected": expected_coverage_ratio,
            "actual": coverage_ratio,
        }
    return mismatches


def _int_frame_value(frame_values: dict[str, object], key: str) -> int:
    return int(frame_values[key])


def _float_frame_value(frame_values: dict[str, object], key: str) -> float:
    return float(frame_values.get(key, 0.0))


def _threshold_hint_filter_tokens(plan: QueryPlan | None) -> set[str]:
    """Vague-quantifier cutoff numbers the renderer is licensed to surface (#1651).

    The transparency note (composer._threshold_hint_line) states the concrete
    cutoff Compass applied for a vague qualifier ("$5,000 or more"). That number
    is the planner-chosen ``FilterSpec.value`` — it lives in the plan, not in the
    result artifact — so without this it would trip the provenance guard as an
    ungrounded token. The allowance is deliberately one-to-one: only filters that
    carry a ``threshold_hint`` (the ones whose value the note prints) and only a
    scalar numeric ``value`` (never a list/bool, never a None on an
    ``is_missing``/anchor filter). "We surface this number, so we allow exactly
    this number."
    """

    if plan is None:
        return set()
    tokens: set[str] = set()
    for filter_spec in plan.filters:
        if filter_spec.threshold_hint is None:
            continue
        value = filter_spec.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        tokens.add(_normalize_numeric_token(str(value)))
    return tokens


def _validate_numeric_token_provenance(
    result: ResultSet,
    rendered_body: str | None,
    plan: QueryPlan | None = None,
) -> list[ValidationFinding]:
    if rendered_body is None:
        return []

    artifact_tokens = {
        _normalize_numeric_token(token)
        for token in _NUMERIC_TOKEN_RE.findall(result.model_dump_json())
    } | _threshold_hint_filter_tokens(plan)
    rendered_tokens = {
        _normalize_numeric_token(token)
        for token in _NUMERIC_TOKEN_RE.findall(rendered_body)
    }
    missing_tokens = sorted(rendered_tokens - artifact_tokens)
    if not missing_tokens:
        return []
    return [
        _finding(
            code="numeric_token_not_in_artifact",
            dimension="numeric_token_provenance",
            message="Rendered numeric tokens must be traceable to the result artifact.",
            metadata={"tokens": missing_tokens},
        )
    ]


def _token_present(value: object, body_tokens: set[str]) -> bool:
    return _normalize_numeric_token(str(value)) in body_tokens


def _name_present(name: str, rendered_body: str) -> bool:
    # Markdown table cells escape pipes; accept either form.
    return name in rendered_body or name.replace("|", "\\|") in rendered_body


def _fact_coverage_count_findings(
    result: ResultSet,
    rendered_body: str,
    body_tokens: set[str],
) -> list[ValidationFinding]:
    rows = [row for row in result.rows if isinstance(row, COUNT_ROW_TYPES)]
    if not rows or any(row.count_kind == "categorical_value_count" for row in rows):
        # Categorical distributions are pinned cell-by-cell by table parity;
        # their lead is intentionally token-free.
        return []
    findings: list[ValidationFinding] = []

    missing_counts = [row.count for row in rows if not _token_present(row.count, body_tokens)]
    if missing_counts:
        findings.append(
            _finding(
                code="count_token_not_surfaced",
                dimension="fact_coverage",
                message="A count answer must state the computed count.",
                metadata={"missing_counts": missing_counts},
                severity="warning",
            )
        )

    single_filter = len(rows) == 1 and rows[0].count_kind == "threshold_count"
    if not single_filter:
        return findings
    row = rows[0]

    if not _token_present(row.denominator, body_tokens):
        findings.append(
            _finding(
                code="count_denominator_not_surfaced",
                dimension="fact_coverage",
                message="A single-filter count must state its denominator.",
                metadata={"denominator": row.denominator},
                severity="warning",
            )
        )

    if (
        row.percent is not None
        and row.denominator >= MIN_DENOMINATOR_FOR_PERCENT
        and not _token_present(format_percent(row.percent), body_tokens)
    ):
        findings.append(
            _finding(
                code="count_percent_not_surfaced",
                dimension="fact_coverage",
                message="A grounded match share must reach the reader.",
                metadata={"percent": row.percent},
                severity="warning",
            )
        )

    if result.selection is not None:
        qualifying: list[int] = []
        seen: set[int] = set()
        for district_id in row.qualifying_district_ids:
            if district_id not in seen:
                seen.add(district_id)
                qualifying.append(district_id)
        name_by_id = {
            district.district_id: district.district_name
            for district in result.selection.districts
        }
        matched = [name_by_id[i] for i in qualifying if i in name_by_id]
        if 0 < len(matched) <= MATCHING_DISTRICTS_DISPLAY_LIMIT:
            missing_names = sorted(
                (name for name in matched if not _name_present(name, rendered_body)),
                key=str.casefold,
            )
            if missing_names:
                findings.append(
                    _finding(
                        code="count_names_not_surfaced",
                        dimension="fact_coverage",
                        message=(
                            "A count answer holding the matching district names "
                            "must surface them."
                        ),
                        metadata={"missing_names": missing_names},
                        severity="warning",
                    )
                )
        elif len(matched) > MATCHING_DISTRICTS_DISPLAY_LIMIT and not _token_present(
            len(matched), body_tokens
        ):
            findings.append(
                _finding(
                    code="count_match_total_not_surfaced",
                    dimension="fact_coverage",
                    message=(
                        "An over-limit count must state how many districts match "
                        "(the offer to list them carries the total)."
                    ),
                    metadata={"match_total": len(matched)},
                    severity="warning",
                )
            )
    return findings


def _fact_coverage_lookup_findings(
    plan: QueryPlan,
    result: ResultSet,
    body_tokens: set[str],
) -> list[ValidationFinding]:
    if result.criteria:
        return []
    bounds = _requested_enrollment_bounds(plan)
    if bounds is None:
        return []
    min_enrollment, max_enrollment = bounds
    if (min_enrollment is None) == (max_enrollment is None):
        return []  # two-sided range or no bound: the #942 lead does not fire
    list_coverage = getattr(result, "list_coverage", None)
    if list_coverage is None:
        return []
    findings: list[ValidationFinding] = []
    missing = [
        value
        for value in (list_coverage.district_total, list_coverage.district_with_data)
        if not _token_present(value, body_tokens)
    ]
    if missing:
        findings.append(
            _finding(
                code="lookup_list_coverage_not_surfaced",
                dimension="fact_coverage",
                message=(
                    "A size-filtered lookup must state the district denominator "
                    "(of N districts, X have data)."
                ),
                metadata={"missing_values": missing},
                severity="warning",
            )
        )
    if (
        list_coverage.percent is not None
        and list_coverage.district_total >= MIN_DENOMINATOR_FOR_PERCENT
        and not _token_present(format_percent(list_coverage.percent), body_tokens)
    ):
        findings.append(
            _finding(
                code="lookup_percent_not_surfaced",
                dimension="fact_coverage",
                message="A grounded data-coverage share must reach the reader.",
                metadata={"percent": list_coverage.percent},
                severity="warning",
            )
        )
    return findings


def _fact_coverage_ranking_findings(
    result: ResultSet,
    body_tokens: set[str],
) -> list[ValidationFinding]:
    frame = result.coverage_frame
    if not result.coverage_disclosures or frame is None:
        return []
    # Single-sourced on the artifact (CoverageFrame.current_numeric_count),
    # the same property the composer's coverage lead narrates.
    rankable = frame.current_numeric_count
    missing = [
        value
        for value in (rankable, frame.in_scope_count)
        if not _token_present(value, body_tokens)
    ]
    if not missing:
        return []
    return [
        _finding(
            code="ranking_coverage_lead_not_surfaced",
            dimension="fact_coverage",
            message=(
                "A coverage-disclosing ranking must state its data coverage "
                "(rankable of in-scope)."
            ),
            metadata={"missing_values": missing},
            severity="warning",
        )
    ]


def _validate_fact_coverage(
    plan: QueryPlan,
    result: ResultSet,
    rendered_body: str | None,
) -> list[ValidationFinding]:
    """C4 (#1416): no fact the result computed may be silently dropped.

    The inverse of numeric-token provenance: provenance checks body ⊆ artifact
    (no invented values); fact-coverage checks required artifact facts ⊆ body
    (no silently dropped ones). Required facts derive from the ARTIFACT and the
    typed plan only — never from renderer wording — so a renderer change that
    drops a fact fails this guard instead of redefining it. Warning severity:
    an incomplete answer still ships (incompleteness is not falsehood); the
    finding feeds the dim_fact_coverage eval criterion for visibility first,
    promotion to blocking per shape comes after calibration.
    """

    if rendered_body is None:
        return []
    body_tokens = {
        _normalize_numeric_token(token)
        for token in _NUMERIC_TOKEN_RE.findall(rendered_body)
    }
    if result.result_type == "metric_count":
        return _fact_coverage_count_findings(result, rendered_body, body_tokens)
    if result.result_type == "metric_lookup":
        return _fact_coverage_lookup_findings(plan, result, body_tokens)
    if result.result_type == "metric_ranking":
        return _fact_coverage_ranking_findings(result, body_tokens)
    return []


def _validate_forbidden_coverage_wording(
    rendered_body: str | None,
) -> list[ValidationFinding]:
    if rendered_body is None:
        return []

    matches = [
        phrase
        for phrase, pattern in _FORBIDDEN_COVERAGE_WORDING
        if pattern.search(rendered_body)
    ]
    if not matches:
        return []
    return [
        _finding(
            # Own dimension (not coverage_state): at L2 the bridge feeds the
            # reject-severity dim_coverage_state criterion, and this is the one
            # rendered-body check the STYLIST can trip (its guards don't ban
            # these phrases) — keeping it separate means arming the bridges
            # doesn't open an uncalibrated reject channel on stylist wording.
            # No dim bridge reads coverage_wording yet, deliberately.
            code="forbidden_coverage_wording",
            dimension="coverage_wording",
            message=(
                "Rendered coverage language must use canonical coverage-state "
                "phrases instead of vague missing-data wording."
            ),
            metadata={"phrases": matches},
        )
    ]
