"""Deterministic selected-district metric lookup operation path."""

from __future__ import annotations

from compass_backend.artifacts import (
    CitationRef,
    MethodologyRef,
    MetricLookupResult,
    MetricValueRow,
    ResultCriterion,
    ResultSelection,
    SelectedDistrict,
    coverage_frame_from_labels,
    coverage_label_for_answer,
    coverage_label_for_missing_answer,
    coverage_label_for_out_of_universe,
    coverage_label_for_stale_answer,
)
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts.planning import QueryPlan

from ._text_utils import metric_phrase_key, parse_compass_numeric_value
from .evidence import citation_markers_for_row
from .selection import (
    direction,
    direction_phrase,
    primary_sort_step,
    selection_filters_are_supported,
    sort_kind as _plan_sort_kind,
    split_unresolved_district_state,
)
from .types import MetricAnswerRow

_AFFIRMATIVE_TEXT_PREFIXES = (
    "yes",
    "y",
    "true",
    "offered",
    "available",
)
_NON_QUALIFYING_TEXT = {
    "",
    "0",
    "$0",
    "$0.00",
    "0%",
    "no",
    "n",
    "false",
    "none",
    "n/a",
    "na",
    "ina",
    "unavailable",
    "not available",
    "issue not addressed",
    "not addressed",
}
def lookup_shape_is_supported(plan: QueryPlan) -> bool:
    """Return whether the lookup path supports this query plan.

    `limit` rules (matches the planner instructions at planner.py:93-100):
    - `LimitSpec(kind="all")` is valid for `all_covered_districts`, `state`,
      and `named_districts` scopes — the executor returns every matching row
      (chart/CSV-export use case, plus the multi-turn inheritance path where
      a follow-up materializes named_districts from prior_result_rows and
      preserves the planner's `kind="all"`). Closes #763, #1012.
    - Numeric `LimitSpec(count=N)` is only valid for `largest_districts`
      scope (`select_largest_districts` honors the count).
    - `limit=None` is always valid.
    """

    limit_is_supported = (
        plan.limit is None
        or plan.selection.scope == "largest_districts"
        or (
            plan.limit.kind == "all"
            and plan.selection.scope
            in {"all_covered_districts", "state", "named_districts"}
        )
    )
    return (
        plan.operation == "lookup"
        and plan.selection.scope
        in {
            "all_covered_districts",
            "state",
            "named_districts",
            "largest_districts",
        }
        and _lookup_metrics_are_supported(plan)
        and limit_is_supported
        and selection_filters_are_supported(plan)
        and _lookup_sort_is_supported(plan)
    )


def build_metric_lookup_result(
    plan: QueryPlan,
    metric_rows: list[
        tuple[MetricCandidate, list[MetricAnswerRow], list[MetricAnswerRow]]
    ],
    *,
    selection: ResultSelection,
    reviewed_district_ids: set[int],
    academic_year: str,
    preserve_selection_order: bool = False,
    source_notes: list[str] | None = None,
) -> MetricLookupResult:
    """Build an exact metric lookup result from resolved catalog IDs."""

    selected_metric_rows = _selected_lookup_cells(
        metric_rows,
        selection=selection,
    )
    if preserve_selection_order:
        sort_kind = "selection"
        _sort_lookup_metric_rows_by_selection(
            selected_metric_rows,
            selection=selection,
            metric_rows=metric_rows,
        )
    else:
        sort_kind = _lookup_sort_kind_for_cells(plan, selected_metric_rows)
        _sort_lookup_metric_rows(plan, selected_metric_rows, sort_kind=sort_kind)

    citation_refs: list[CitationRef] = []
    citation_marker_by_identity: dict[tuple[object, ...], int] = {}
    result_rows: list[MetricValueRow] = []
    coverage_labels = []
    for metric, district, unresolved_name, row, recent_row in selected_metric_rows:
        if district is None:
            requested_name = unresolved_name or ""
            display_name, derived_state = split_unresolved_district_state(
                requested_name,
                selection=selection,
            )
            coverage = coverage_label_for_out_of_universe(
                display_name,
                state=derived_state,
            )
            citation_markers = []
            value = None
            row_year = academic_year
            source = "coverage_state"
            state = derived_state
            district_id = None
            district_name = requested_name
        elif row is None and recent_row is not None:
            coverage = coverage_label_for_stale_answer(
                recent_row.value,
                district_name=district.district_name,
                metric_name=metric.name,
                current_academic_year=academic_year,
                prior_academic_year=recent_row.academic_year,
                metric_topic=_metric_topic(metric),
            )
            citation_markers = []
            value = None
            row_year = academic_year
            source = "coverage_state"
            state = district.state
            district_id = district.district_id
            district_name = district.district_name
        elif row is None:
            coverage = coverage_label_for_missing_answer(
                district_name=district.district_name,
                metric_name=metric.name,
                academic_year=academic_year,
                district_has_current_year_rows=(
                    district.district_id in reviewed_district_ids
                ),
                metric_topic=_metric_topic(metric),
            )
            citation_markers: list[int] = []
            value = None
            row_year = academic_year
            source = "coverage_state"
            state = district.state
            district_id = district.district_id
            district_name = district.district_name
        else:
            coverage = coverage_label_for_answer(
                row.value,
                district_name=row.district_name,
                metric_name=metric.name,
                academic_year=row.academic_year,
            )
            citation_markers = citation_markers_for_row(
                row,
                citation_refs,
                citation_marker_by_identity,
            )
            value = row.value
            row_year = row.academic_year
            source = "policy_answer"
            state = district.state
            district_id = district.district_id
            district_name = district.district_name
        coverage_labels.append(coverage)
        result_rows.append(
            MetricValueRow(
                district_id=district_id,
                district_name=district_name,
                state=state,
                metric_id=metric.metric_id,
                metric_name=metric.name,
                value=value,
                display_value=coverage.display,
                academic_year=row_year,
                source=source,
                citation_markers=citation_markers,
                coverage_state=coverage.state,
                coverage_display=coverage.display,
                coverage_reason=coverage.reason,
                coverage_qualifier=coverage.qualifier,
                coverage_prior_academic_year=coverage.prior_academic_year,
                coverage_prior_display_value=coverage.prior_display_value,
            )
        )
    order = _lookup_order_statement(plan, sort_kind=sort_kind)
    metric_label = (
        "selected metrics"
        if len(metric_rows) > 1
        else metric_rows[0][0].name
        if metric_rows
        else "the selected metric"
    )
    ordering_code = (
        "ranked_lookup_selection_order"
        if preserve_selection_order
        else "lookup_default_district_order"
    )
    return MetricLookupResult(
        selection=selection,
        rows=result_rows,
        citations=citation_refs,
        coverage_frame=coverage_frame_from_labels(coverage_labels),
        total_considered=len(selected_metric_rows),
        excluded_count=0,
        order_statement=f"Looked up {metric_label} for selected districts, {order}.",
        source_notes=source_notes or [],
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
            MethodologyRef(code=ordering_code),
        ],
    )


def build_metric_intersection_lookup_result(
    plan: QueryPlan,
    criteria_metric_rows: list[
        tuple[
            str,
            str,
            list[
                tuple[MetricCandidate, list[MetricAnswerRow], list[MetricAnswerRow]]
            ],
        ]
    ],
    *,
    selection: ResultSelection,
    reviewed_district_ids: set[int],
    academic_year: str,
) -> MetricLookupResult:
    """Build a lookup grid for districts satisfying every positive criterion."""

    selected_district_ids = {district.district_id for district in selection.districts}
    positive_rows_by_criterion: dict[
        str, dict[int, list[tuple[MetricCandidate, MetricAnswerRow]]]
    ] = {}
    result_criteria: list[ResultCriterion] = []

    for criterion_id, criterion_label, metric_rows in criteria_metric_rows:
        rows_by_district: dict[int, list[tuple[MetricCandidate, MetricAnswerRow]]] = {}
        metric_ids: list[int] = []
        for metric, rows, _recent_rows in metric_rows:
            if metric.metric_id not in metric_ids:
                metric_ids.append(metric.metric_id)
            for row in rows:
                if row.district_id not in selected_district_ids:
                    continue
                if not _lookup_value_is_strict_positive(row.value):
                    continue
                rows_by_district.setdefault(row.district_id, []).append((metric, row))

        qualifying_district_ids = sorted(rows_by_district)
        positive_rows_by_criterion[criterion_id] = rows_by_district
        result_criteria.append(
            ResultCriterion(
                criterion_id=criterion_id,
                label=criterion_label,
                metric_ids=metric_ids,
                qualifying_district_ids=qualifying_district_ids,
            )
        )

    if result_criteria:
        qualifying_ids = set(result_criteria[0].qualifying_district_ids)
        for criterion in result_criteria[1:]:
            qualifying_ids &= set(criterion.qualifying_district_ids)
    else:
        qualifying_ids = set()

    selected_by_id = {
        district.district_id: district for district in selection.districts
    }
    qualifying_districts = [
        district
        for district in selection.districts
        if district.district_id in qualifying_ids
    ]
    filtered_selection = selection.model_copy(
        update={"districts": qualifying_districts, "unresolved_districts": []}
    )

    citation_refs: list[CitationRef] = []
    citation_marker_by_identity: dict[tuple[object, ...], int] = {}
    result_rows: list[MetricValueRow] = []
    coverage_labels = []

    for criterion in result_criteria:
        rows_by_district = positive_rows_by_criterion.get(criterion.criterion_id, {})
        for district_id in sorted(
            qualifying_ids,
            key=lambda candidate_id: (
                selected_by_id[candidate_id].district_name.casefold()
                if candidate_id in selected_by_id
                else ""
            ),
        ):
            district = selected_by_id.get(district_id)
            if district is None:
                continue
            for metric, row in sorted(
                rows_by_district.get(district_id, []),
                key=lambda item: item[0].name.casefold(),
            ):
                coverage = coverage_label_for_answer(
                    row.value,
                    district_name=row.district_name,
                    metric_name=metric.name,
                    academic_year=row.academic_year,
                )
                citation_markers = citation_markers_for_row(
                    row,
                    citation_refs,
                    citation_marker_by_identity,
                )
                coverage_labels.append(coverage)
                result_rows.append(
                    MetricValueRow(
                        district_id=district.district_id,
                        district_name=district.district_name,
                        state=district.state,
                        metric_id=metric.metric_id,
                        metric_name=metric.name,
                        value=row.value,
                        display_value=coverage.display,
                        academic_year=row.academic_year,
                        source="policy_answer",
                        citation_markers=citation_markers,
                        coverage_state=coverage.state,
                        coverage_display=coverage.display,
                        coverage_reason=coverage.reason,
                        coverage_qualifier=coverage.qualifier,
                        criterion_id=criterion.criterion_id,
                        criterion_label=criterion.label,
                        criterion_satisfied=True,
                    )
                )

    result_rows.sort(
        key=lambda row: (row.district_name.casefold(), row.metric_name.casefold())
    )
    total_considered = len(result_rows)
    return MetricLookupResult(
        selection=filtered_selection,
        rows=result_rows,
        citations=citation_refs,
        coverage_frame=coverage_frame_from_labels(coverage_labels),
        total_considered=total_considered,
        excluded_count=max(0, len(selection.districts) - len(qualifying_districts)),
        order_statement=(
            "Selected districts satisfying every requested compensation criterion "
            "using current reviewed positive values, ordered alphabetically by "
            "district name."
        ),
        methodology_codes=[
            MethodologyRef(code="intersection_requires_all_criteria"),
            MethodologyRef(code="intersection_accepts_any_current_positive_value"),
            MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
        ],
        criteria=result_criteria,
    )


def _lookup_value_is_strict_positive(value: str | int | float | bool | None) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value > 0
    normalized = metric_phrase_key(str(value))
    if normalized in _NON_QUALIFYING_TEXT:
        return False
    numeric_value = parse_compass_numeric_value(value)
    if numeric_value is not None:
        return numeric_value > 0
    return any(
        normalized == prefix or normalized.startswith(f"{prefix} ")
        for prefix in _AFFIRMATIVE_TEXT_PREFIXES
    )


def _lookup_metrics_are_supported(plan: QueryPlan) -> bool:
    result_metrics = _lookup_result_metrics(plan)
    if not result_metrics or result_metrics[0].role != "primary":
        return False
    return all(metric.role in {"primary", "comparison"} for metric in result_metrics[1:])


def _lookup_result_metrics(plan: QueryPlan):
    return [
        metric
        for metric in plan.metrics
        if metric.role not in {"filter", "grouping"}
    ]


def _lookup_sort_is_supported(plan: QueryPlan) -> bool:
    # Canonical read: a finalized plan always has plan.sort is None, with the
    # requested sort folded into a presentation-phase SortStepSpec. Mirror the
    # original three-branch intent off the canonical sort: no sort -> supported;
    # a district-keyed sort -> supported; a value sort -> supported only for a
    # single metric (the row-sorter cannot honor a multi-metric value sort); any
    # other (unrecognized) sort field -> refused.
    if plan.sort is None and primary_sort_step(plan) is None:
        return True
    if _canonical_sort_field(plan) in {"district", "district_name"}:
        return True
    return len(plan.metrics) == 1 and _lookup_sort_kind(plan) == "value"


def _lookup_order_statement(plan: QueryPlan, *, sort_kind: str | None = None) -> str:
    active_sort_kind = sort_kind or _lookup_sort_kind(plan)
    if active_sort_kind == "selection":
        return "the selected ranking order"
    direction = _lookup_direction_for_kind(plan, active_sort_kind)
    if active_sort_kind == "value":
        return f"value, {direction_phrase(direction)}"
    return (
        "reverse alphabetical by district name"
        if direction == "desc"
        else "alphabetical by district name"
    )


def _lookup_sort_kind(plan: QueryPlan) -> str:
    # Canonical read: post-finalize plan.sort is always None and the requested
    # value/district intent lives on the folded presentation-phase SortStepSpec.
    return _plan_sort_kind(plan)


def _lookup_sort_kind_for_cells(
    plan: QueryPlan,
    metric_rows: list[
        tuple[
            MetricCandidate,
            SelectedDistrict | None,
            str | None,
            MetricAnswerRow | None,
            MetricAnswerRow | None,
        ]
    ],
) -> str:
    if (
        len({metric.metric_id for metric, *_ in metric_rows}) > 1
        and plan.operation != "rank"
    ):
        return "district"
    return _lookup_sort_kind(plan)


def _lookup_direction_for_kind(plan: QueryPlan, sort_kind: str) -> str:
    # Canonical read: resolve direction from the folded sort step (via
    # selection.direction) rather than the always-None post-finalize plan.sort.
    if plan.sort is None and primary_sort_step(plan) is None:
        return "asc"
    field = _canonical_sort_field(plan)
    if sort_kind == "district" and field not in {"district", "district_name"}:
        return "asc"
    return direction(plan)


def _canonical_sort_field(plan: QueryPlan) -> str:
    """Return the casefolded canonical sort field (legacy sort or folded step)."""

    if plan.sort is not None:
        return plan.sort.field.casefold().strip()
    step = primary_sort_step(plan)
    return step.field.casefold().strip() if step is not None else ""


def _sort_lookup_metric_rows(
    plan: QueryPlan,
    metric_rows: list[
        tuple[
            MetricCandidate,
            SelectedDistrict | None,
            str | None,
            MetricAnswerRow | None,
            MetricAnswerRow | None,
        ]
    ],
    *,
    sort_kind: str | None = None,
) -> None:
    sort_kind = sort_kind or _lookup_sort_kind_for_cells(plan, metric_rows)
    reverse = _lookup_direction_for_kind(plan, sort_kind) == "desc"
    metric_rows.sort(key=lambda item: item[0].name.casefold())
    if sort_kind == "value":
        metric_rows.sort(key=lambda item: _lookup_value_sort_key(item, reverse=reverse))
        return
    metric_rows.sort(
        key=lambda item: (
            item[1].district_name if item[1] is not None else item[2] or ""
        ).casefold(),
        reverse=reverse,
    )


def _sort_lookup_metric_rows_by_selection(
    selected_metric_rows: list[
        tuple[
            MetricCandidate,
            SelectedDistrict | None,
            str | None,
            MetricAnswerRow | None,
            MetricAnswerRow | None,
        ]
    ],
    *,
    selection: ResultSelection,
    metric_rows: list[
        tuple[MetricCandidate, list[MetricAnswerRow], list[MetricAnswerRow]]
    ],
) -> None:
    district_order = {
        district.district_id: index
        for index, district in enumerate(selection.districts)
    }
    metric_order = {
        metric.metric_id: index for index, (metric, _rows, _recent) in enumerate(metric_rows)
    }
    selected_metric_rows.sort(
        key=lambda item: (
            district_order.get(
                item[1].district_id if item[1] is not None else -1,
                len(district_order),
            ),
            metric_order.get(item[0].metric_id, len(metric_order)),
            item[0].name.casefold(),
        )
    )


def _lookup_value_sort_key(
    item: tuple[
        MetricCandidate,
        SelectedDistrict | None,
        str | None,
        MetricAnswerRow | None,
        MetricAnswerRow | None,
    ],
    *,
    reverse: bool,
) -> tuple[object, ...]:
    _, district, unresolved_name, row, _ = item
    district_name = (
        district.district_name if district is not None else unresolved_name or ""
    ).casefold()
    # Parse-once: read the typed numeric the row carries from its boundary
    # (MetricAnswerRow.numeric_value), not a fresh parse of row.value.
    numeric_value = row.numeric_value if row is not None else None
    if numeric_value is not None:
        return (0, -numeric_value if reverse else numeric_value, district_name)
    if row is not None and row.value is not None:
        return (1, str(row.value).casefold(), district_name)
    return (2, district_name)


def _metric_topic(metric: MetricCandidate) -> str | None:
    topic = (metric.topic or "").strip()
    if topic:
        return topic
    metadata_topic = metric.metadata.get("topic")
    return str(metadata_topic).strip() if metadata_topic else None


def _selected_lookup_cells(
    metric_rows: list[
        tuple[MetricCandidate, list[MetricAnswerRow], list[MetricAnswerRow]]
    ],
    *,
    selection: ResultSelection,
) -> list[
    tuple[
        MetricCandidate,
        SelectedDistrict | None,
        str | None,
        MetricAnswerRow | None,
        MetricAnswerRow | None,
    ]
]:
    cells: list[
        tuple[
            MetricCandidate,
            SelectedDistrict | None,
            str | None,
            MetricAnswerRow | None,
            MetricAnswerRow | None,
        ]
    ] = []
    selected_district_ids = {district.district_id for district in selection.districts}
    for metric, rows, recent_rows in metric_rows:
        rows_by_district = {
            row.district_id: row
            for row in rows
            if row.district_id in selected_district_ids
        }
        recent_by_district = {
            row.district_id: row
            for row in recent_rows
            if row.district_id in selected_district_ids
        }
        for district in selection.districts:
            current_row = rows_by_district.get(district.district_id)
            cells.append(
                (
                    metric,
                    district,
                    None,
                    current_row,
                    recent_by_district.get(district.district_id),
                )
            )
        for unresolved_name in selection.unresolved_districts:
            cells.append((metric, None, unresolved_name, None, None))
    return cells
