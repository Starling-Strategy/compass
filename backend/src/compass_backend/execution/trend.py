"""Deterministic longitudinal metric trend operation path."""

from __future__ import annotations

from collections import defaultdict

from compass_backend.artifacts import (
    ChartArtifact,
    ChartPoint,
    ChartSeries,
    CitationRef,
    MethodologyRef,
    MetricTrendResult,
    MetricValueRow,
    ResultSelection,
    coverage_frame_from_labels,
    coverage_label_for_answer,
    coverage_label_for_missing_answer,
    coverage_label_for_out_of_universe,
)
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts.planning import QueryPlan
from compass_backend.planning.temporal import (
    academic_year_start,
    academic_year_window_ending_at,
)

from .evidence import citation_markers_for_row
from .selection import (
    selection_filters_are_supported,
    split_unresolved_district_state,
)
from .types import MetricAnswerRow

MAX_TREND_CELLS = 30


def trend_shape_is_supported(plan: QueryPlan) -> bool:
    """Return whether the trend path supports this query plan."""

    return (
        plan.operation == "trend"
        and plan.selection.scope
        in {
            "all_covered_districts",
            "state",
            "named_districts",
            "largest_districts",
        }
        and len(_trend_result_metrics(plan)) == 1
        and _trend_result_metrics(plan)[0].role == "primary"
        and plan.sort is None
        and not plan.sort_steps
        and (plan.limit is None or plan.selection.scope == "largest_districts")
        and selection_filters_are_supported(plan)
    )


def trend_academic_years(plan: QueryPlan, *, current_academic_year: str) -> list[str]:
    """Return the canonical year list a trend plan should fetch."""

    if plan.temporal.academic_years:
        return list(plan.temporal.academic_years)
    if plan.temporal.intent == "specific_year" and plan.temporal.academic_year:
        return [plan.temporal.academic_year]
    if plan.temporal.window_years:
        return academic_year_window_ending_at(
            current_academic_year,
            plan.temporal.window_years,
        )
    return [current_academic_year]


def trend_cell_count(
    *,
    district_count: int,
    metric_count: int,
    year_count: int,
) -> int:
    """Return the total cells needed for a trend artifact."""

    return district_count * metric_count * year_count


def build_metric_trend_result(
    plan: QueryPlan,
    metric: MetricCandidate,
    rows: list[MetricAnswerRow],
    *,
    selection: ResultSelection,
    academic_years: list[str],
    reviewed_district_ids_by_year: dict[str, set[int]],
) -> MetricTrendResult:
    """Build a chronological metric-trend result from approved rows."""

    citation_refs: list[CitationRef] = []
    citation_marker_by_identity: dict[tuple[object, ...], int] = {}
    result_rows: list[MetricValueRow] = []
    coverage_labels = []
    rows_by_identity = {
        (row.district_id, row.academic_year): row
        for row in rows
    }

    selected_districts = list(selection.districts)
    for district in selected_districts:
        for academic_year in academic_years:
            row = rows_by_identity.get((district.district_id, academic_year))
            if row is None:
                coverage = coverage_label_for_missing_answer(
                    district_name=district.district_name,
                    metric_name=metric.name,
                    academic_year=academic_year,
                    district_has_current_year_rows=(
                        district.district_id
                        in reviewed_district_ids_by_year.get(academic_year, set())
                    ),
                    metric_topic=_metric_topic(metric),
                )
                result_row = MetricValueRow(
                    district_id=district.district_id,
                    district_name=district.district_name,
                    state=district.state,
                    metric_id=metric.metric_id,
                    metric_name=metric.name,
                    value=None,
                    display_value=coverage.display,
                    academic_year=academic_year,
                    source="coverage_state",
                    citation_markers=[],
                    coverage_state=coverage.state,
                    coverage_display=coverage.display,
                    coverage_reason=coverage.reason,
                    coverage_qualifier=coverage.qualifier,
                )
            else:
                coverage = coverage_label_for_answer(
                    row.value,
                    district_name=row.district_name,
                    metric_name=metric.name,
                    academic_year=row.academic_year,
                )
                markers = citation_markers_for_row(
                    row,
                    citation_refs,
                    citation_marker_by_identity,
                )
                result_row = MetricValueRow(
                    district_id=district.district_id,
                    district_name=district.district_name,
                    state=district.state,
                    metric_id=metric.metric_id,
                    metric_name=metric.name,
                    value=row.value,
                    display_value=coverage.display,
                    academic_year=row.academic_year,
                    source="policy_answer",
                    citation_markers=markers,
                    coverage_state=coverage.state,
                    coverage_display=coverage.display,
                    coverage_reason=coverage.reason,
                    coverage_qualifier=coverage.qualifier,
                    # Parse-once: the source MetricAnswerRow already parsed its
                    # value at the row boundary; carry that typed numeric onto
                    # the MetricValueRow instead of re-parsing row.value here.
                    numeric_value=row.numeric_value,
                )
            coverage_labels.append(coverage)
            result_rows.append(result_row)

    for unresolved_name in selection.unresolved_districts:
        display_name, derived_state = split_unresolved_district_state(
            unresolved_name,
            selection=selection,
        )
        coverage = coverage_label_for_out_of_universe(
            display_name,
            state=derived_state,
        )
        for academic_year in academic_years:
            coverage_labels.append(coverage)
            result_rows.append(
                MetricValueRow(
                    district_id=None,
                    district_name=unresolved_name,
                    state=derived_state,
                    metric_id=metric.metric_id,
                    metric_name=metric.name,
                    value=None,
                    display_value=coverage.display,
                    academic_year=academic_year,
                    source="coverage_state",
                    citation_markers=[],
                    coverage_state=coverage.state,
                    coverage_display=coverage.display,
                    coverage_reason=coverage.reason,
                    coverage_qualifier=coverage.qualifier,
                )
            )

    _attach_deltas(result_rows)
    chart = _trend_chart(plan, result_rows, metric=metric)
    return MetricTrendResult(
        selection=selection,
        rows=result_rows,
        citations=citation_refs,
        coverage_frame=coverage_frame_from_labels(coverage_labels),
        chart=chart,
        total_considered=len(result_rows),
        excluded_count=0,
        order_statement=(
            f"Built a chronological trend for {metric.name} across "
            f"{len(academic_years)} academic years."
        ),
        methodology_codes=[
            MethodologyRef(code="trend_chronological_coverage_gaps"),
            MethodologyRef(code="trend_deltas_from_artifact_values"),
        ],
    )


def _trend_result_metrics(plan: QueryPlan):
    return [
        metric
        for metric in plan.metrics
        if metric.role not in {"filter", "grouping"}
    ]


def _attach_deltas(rows: list[MetricValueRow]) -> None:
    previous_by_identity: dict[tuple[int | None, int], MetricValueRow] = {}
    for index, row in enumerate(rows):
        identity = (row.district_id, row.metric_id)
        previous = previous_by_identity.get(identity)
        if row.numeric_value is not None and previous is not None:
            delta = row.numeric_value - (previous.numeric_value or 0.0)
            percent = (
                delta / previous.numeric_value
                if previous.numeric_value not in {None, 0.0}
                else None
            )
            updated = row.model_copy(
                update={
                    "delta_value": delta,
                    "delta_percent": percent,
                    "delta_from_academic_year": previous.academic_year,
                }
            )
            rows[index] = updated
            row = updated
        if row.numeric_value is not None:
            previous_by_identity[identity] = row


def _trend_chart(
    plan: QueryPlan,
    rows: list[MetricValueRow],
    *,
    metric: MetricCandidate,
) -> ChartArtifact | None:
    # #1240: trend already gates chart-building on intent here, so the central
    # rendering/chart_visibility.resolve_chart_visibility helper (now the single
    # visibility authority for the four auto-building variants) is a harmless
    # no-op for trend — not-requested means this returned None already, and
    # requested means there is nothing to strip. Left as-is on purpose.
    if plan.output.format != "chart":
        return None

    rows_by_district: dict[str, list[MetricValueRow]] = defaultdict(list)
    for row in rows:
        if row.coverage_state != "covered" or row.numeric_value is None:
            continue
        rows_by_district[row.district_name].append(row)

    series = [
        ChartSeries(
            label=district_name,
            metric_id=metric.metric_id,
            points=[
                ChartPoint(
                    label=row.academic_year,
                    value=row.numeric_value or 0.0,
                    district_id=row.district_id,
                    academic_year=row.academic_year,
                    citation_markers=row.citation_markers,
                )
                for row in sorted(
                    district_rows,
                    key=lambda item: academic_year_start(item.academic_year),
                )
            ],
        )
        for district_name, district_rows in sorted(
            rows_by_district.items(),
            key=lambda item: item[0].casefold(),
        )
    ]
    if not series:
        return None
    return ChartArtifact(
        artifact_type="line_chart",
        title=f"{metric.name} trend",
        x_axis_label="Academic year",
        y_axis_label=metric.name,
        points=series[0].points if len(series) == 1 else [],
        series=series,
        show_legend=len(series) > 1,
    )


def _metric_topic(metric: MetricCandidate) -> str | None:
    topic = (metric.topic or "").strip()
    if topic:
        return topic
    metadata_topic = metric.metadata.get("topic")
    return str(metadata_topic).strip() if metadata_topic else None
