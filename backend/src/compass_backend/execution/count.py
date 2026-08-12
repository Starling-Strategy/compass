"""Deterministic count and denominator operation path."""

from __future__ import annotations

from collections.abc import Iterable

from compass_backend.artifacts import (
    CategoricalCountRow,
    CitationRef,
    CoveredUniverseCountRow,
    CoverageLabel,
    DistrictCoverageSummary,
    FilterPrevalenceSummary,
    MethodologyRef,
    MetricCountResult,
    ResultSelection,
    ThresholdCountRow,
    coverage_frame_from_state_counts,
    coverage_frame_from_labels,
    coverage_label_for_answer,
    coverage_label_for_missing_answer,
    is_answer_state,
)
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts.planning import FilterSpec, QueryPlan
from compass_backend.reference import normalize_whitespace_casefold

from ._text_utils import parse_compass_numeric_value
from .evidence import citation_markers_for_row
from .filters import ResolvedMetricFilter, filter_kind, filter_statement
from .selection import select_rows
from .types import MetricAnswerRow

_COVERED_UNIVERSE_METRIC_ID = 0
_COVERED_UNIVERSE_METRIC_NAME = "Covered district universe"
_CATEGORICAL_ANSWER_TYPES = {
    "bool",
    "boolean",
    "categorical",
    "category",
    "enum",
    "text",
}
_MAX_CATEGORICAL_DISTINCT_VALUES = 12
_MAX_CATEGORY_LABEL_LENGTH = 80
_CATEGORICAL_FILTER_STATEMENT = "group by reviewed value"


def count_shape_is_supported(plan: QueryPlan) -> bool:
    """Return whether the count path supports this query plan."""

    if plan.operation != "count":
        return False
    if plan.selection.scope not in {
        "all_covered_districts",
        "state",
        "named_districts",
        "largest_districts",
    }:
        return False
    if not all(_count_filter_is_supported(filter_spec) for filter_spec in plan.filters):
        return False
    # covered_universe_count counts the universe itself and carries no metric.
    # threshold_count and categorical_value_count both require a metric.
    if plan.count_kind == "covered_universe_count":
        return not plan.metrics
    return bool(plan.metrics)


def count_plan_is_covered_universe(plan: QueryPlan) -> bool:
    """Return whether a count plan asks for the covered-district universe size."""

    return plan.operation == "count" and plan.count_kind == "covered_universe_count"


def count_plan_is_categorical_value(plan: QueryPlan) -> bool:
    """Return whether a count plan asks for a value/category distribution."""

    return plan.operation == "count" and plan.count_kind == "categorical_value_count"


def count_plan_is_topic_coverage(plan: QueryPlan) -> bool:
    """Return whether a count plan asks how many districts have data in a topic.

    Shape: "How many districts have data addressing Evaluation policies?" — a
    topic-scoped availability count, distinct from threshold_count (which counts
    districts whose answer satisfies a specific metric/threshold).
    """

    return plan.operation == "count" and plan.count_kind == "topic_coverage_count"


def categorical_count_metric_queries(plan: QueryPlan) -> list[str]:
    """Return approved-catalog query candidates for a categorical count field.

    For count_kind='categorical_value_count' plans the planner emits the
    breakdown axis as a single MetricSpec with role='grouping'. The
    catalog adjudicator then resolves it via the normal alias-resolution
    path; this helper simply returns the planner-emitted names (dedup'd).
    """

    grouping_metrics = [metric for metric in plan.metrics if metric.role == "grouping"]
    source_metrics = grouping_metrics or list(plan.metrics)
    return _dedupe_text(metric.name for metric in source_metrics)


def covered_universe_metric() -> MetricCandidate:
    """Return the internal metric authority for synthetic universe count rows."""

    return MetricCandidate(
        metric_id=_COVERED_UNIVERSE_METRIC_ID,
        name=_COVERED_UNIVERSE_METRIC_NAME,
        answer_type="coverage_state",
    )


def build_covered_universe_count_result(
    plan: QueryPlan,
    *,
    selection: ResultSelection,
    academic_year: str,
) -> MetricCountResult:
    """Build a count artifact for resolved covered-district universe questions."""

    metric = covered_universe_metric()
    district_ids = sorted(district.district_id for district in selection.districts)
    count = len(district_ids)
    display_value = f"{count} covered districts"
    return MetricCountResult(
        selection=selection,
        rows=[
            CoveredUniverseCountRow(
                metric_id=metric.metric_id,
                metric_name=metric.name,
                value=count,
                display_value=display_value,
                academic_year=academic_year,
                count=count,
                denominator=count,
                percent=100.0 if count else None,
                filter_statement="covered district universe",
                qualifying_district_ids=district_ids,
                source="coverage_state",
                coverage_state="covered",
                coverage_display=display_value,
                coverage_reason="covered_universe_count",
            )
        ],
        coverage_frame=coverage_frame_from_state_counts(
            {"covered": count},
            universe_count=count,
        ),
        total_considered=count,
        excluded_count=0,
        order_statement="Counted covered districts in the resolved selection.",
        methodology_codes=[
            MethodologyRef(code="covered_universe_selection_count"),
        ],
    )


def build_topic_coverage_count_result(
    plan: QueryPlan,
    metric_rows: list[tuple[MetricCandidate, list[MetricAnswerRow]]],
    *,
    selection: ResultSelection,
    academic_year: str,
    topic_label: str,
) -> MetricCountResult:
    """Build a single "X of Y covered districts have <topic> data" count.

    Aggregates across every metric in a topic: a district counts as having
    topic data when it has >= 1 answer-state cell in ANY of the topic's metrics
    (the same per-district reduction ``build_metric_count_result`` does for its
    DistrictCoverageSummary, collapsed to ONE topic-level row instead of one row
    per metric). The denominator is the covered-district universe in the
    resolved selection — the same sizing ``build_covered_universe_count_result``
    uses — so the pair reads "answered / covered".
    """

    answered_district_ids: set[int] = set()

    for metric, raw_rows in metric_rows:
        selected_rows = select_rows(plan, raw_rows, selection)
        labels = [
            coverage_label_for_answer(
                row.value,
                district_name=row.district_name,
                metric_name=metric.name,
                academic_year=row.academic_year,
            )
            for row in selected_rows
        ]
        for row, label in zip(selected_rows, labels, strict=True):
            if is_answer_state(label.state):
                answered_district_ids.add(row.district_id)

    denominator = len(selection.districts)
    count = len(answered_district_ids)
    out_of_universe = len(selection.unresolved_districts)
    gap_district_ids = {
        district.district_id for district in selection.districts
    } - answered_district_ids
    display_value = (
        f"{count} of {denominator} covered districts have {topic_label} data"
    )
    return MetricCountResult(
        selection=selection,
        rows=[
            ThresholdCountRow(
                metric_id=_COVERED_UNIVERSE_METRIC_ID,
                metric_name=topic_label,
                value=count,
                display_value=display_value,
                academic_year=academic_year,
                count=count,
                denominator=denominator,
                percent=_count_percent(count, denominator),
                # No filters on a topic-coverage count — use the same default
                # statement the validator expects for an unfiltered count so the
                # row stays internally consistent.
                filter_statement=filter_statement(plan.filters),
                qualifying_district_ids=sorted(answered_district_ids),
                citation_markers=[],
                coverage_state="covered",
                coverage_display=display_value,
                coverage_reason="topic_coverage_count",
            )
        ],
        citations=[],
        # District-level frame (not per-metric labels): the count is per
        # district, so the frame must tally districts — answered vs the gap —
        # against the covered universe so denominator / coverage_frame checks
        # agree with the row.
        coverage_frame=coverage_frame_from_state_counts(
            {"covered": count, "not_reviewed": len(gap_district_ids)},
            universe_count=denominator,
        ),
        district_coverage=DistrictCoverageSummary(
            districts_asked=(
                len(answered_district_ids | gap_district_ids) + out_of_universe
            ),
            districts_with_current_data=count,
            districts_not_reviewed=len(gap_district_ids),
            districts_out_of_universe=out_of_universe,
            requested_academic_year=academic_year,
        ),
        total_considered=denominator,
        excluded_count=0,
        order_statement=f"Counted covered districts with reviewed {topic_label} data.",
        methodology_codes=[MethodologyRef(code="topic_coverage_count")],
    )


def metric_supports_categorical_value_count(
    plan: QueryPlan,
    metric: MetricCandidate,
    raw_rows: list[MetricAnswerRow],
    *,
    selection: ResultSelection,
) -> bool:
    """Return whether resolved metric rows are safe to group as categories."""

    if (metric.answer_type or "").casefold().strip() not in _CATEGORICAL_ANSWER_TYPES:
        return False
    selected_rows = select_rows(plan, raw_rows, selection)
    categories = []
    for row in selected_rows:
        label = _categorical_label_for_answer(row)
        if label.state != "covered":
            continue
        category = _category_for_label(label)
        if not _category_label_is_safe(category):
            return False
        categories.append(category)
    distinct_categories = set(categories)
    return 0 < len(distinct_categories) <= _MAX_CATEGORICAL_DISTINCT_VALUES


def build_categorical_value_count_result(
    plan: QueryPlan,
    metric_rows: list[tuple[MetricCandidate, list[MetricAnswerRow]]],
    *,
    selection: ResultSelection,
    academic_year: str,
) -> MetricCountResult:
    """Build deterministic value-count rows for approved categorical metrics."""

    result_rows: list[CategoricalCountRow] = []
    all_labels: list[CoverageLabel] = []
    total_considered = 0

    for metric, raw_rows in metric_rows:
        selected_rows = select_rows(plan, raw_rows, selection)
        selected_by_id = {row.district_id: row for row in selected_rows}
        scope_districts = selection.districts
        if scope_districts:
            denominator = len(scope_districts)
            missing_district_ids = [
                district.district_id
                for district in scope_districts
                if district.district_id not in selected_by_id
            ]
        else:
            denominator = len(selected_rows)
            missing_district_ids = []

        total_considered += denominator
        buckets: dict[tuple[str, str], set[int]] = {}
        bucket_labels: dict[tuple[str, str], CoverageLabel] = {}
        for row in selected_rows:
            label = _categorical_label_for_answer(row)
            all_labels.append(label)
            category = _category_for_label(label)
            key = (label.state, category)
            buckets.setdefault(key, set()).add(row.district_id)
            bucket_labels[key] = label

        if missing_district_ids:
            for district_id in missing_district_ids:
                district = next(
                    (
                        candidate
                        for candidate in scope_districts
                        if candidate.district_id == district_id
                    ),
                    None,
                )
                label = coverage_label_for_missing_answer(
                    district_name=district.district_name if district else "District",
                    metric_name=metric.name,
                    academic_year=academic_year,
                    district_has_current_year_rows=True,
                    metric_topic=_metric_topic(metric),
                )
                all_labels.append(label)
            key = ("not_reviewed", "Not reviewed")
            buckets.setdefault(key, set()).update(missing_district_ids)
            bucket_labels[key] = CoverageLabel(
                state="not_reviewed",
                display="Not reviewed",
                reason="metric_not_reviewed",
            )

        for key, district_ids in _ordered_category_buckets(buckets):
            label = bucket_labels[key]
            count = len(district_ids)
            display_value = f"{count} of {denominator} covered districts"
            category = key[1]
            result_rows.append(
                CategoricalCountRow(
                    metric_id=metric.metric_id,
                    metric_name=metric.name,
                    value=category,
                    category=category,
                    display_value=display_value,
                    academic_year=academic_year,
                    count=count,
                    denominator=denominator,
                    percent=_count_percent(count, denominator),
                    filter_statement=_CATEGORICAL_FILTER_STATEMENT,
                    qualifying_district_ids=sorted(district_ids),
                    coverage_state=label.state,
                    coverage_display=display_value,
                    coverage_reason=(
                        "categorical_value_count"
                        if label.state == "covered"
                        else label.reason
                    ),
                )
            )

    return MetricCountResult(
        selection=selection,
        rows=list(result_rows),
        coverage_frame=coverage_frame_from_labels(all_labels),
        total_considered=total_considered,
        excluded_count=0,
        order_statement="Counted categorical value distribution.",
        methodology_codes=[
            MethodologyRef(code="categorical_count_grouped_current_values"),
            MethodologyRef(code="categorical_count_missing_unavailable_separate"),
        ],
    )


def build_metric_count_result(
    plan: QueryPlan,
    metric_rows: list[tuple[MetricCandidate, list[MetricAnswerRow]]],
    *,
    selection: ResultSelection,
    academic_year: str,
    resolved_metric_filters: list[ResolvedMetricFilter] | None = None,
) -> MetricCountResult:
    """Build deterministic count rows with denominator metadata.

    ``resolved_metric_filters`` carries the metric_id each metric-value filter
    resolved to (#1373 Stage 2). It scopes a per-metric threshold to its own
    metric's rows so a filter for metric A does not narrow metric B's count —
    the scoping the removed ``"metric:<id>"`` field token used to provide.
    """

    scoping_metric_id_by_filter = _scoping_metric_id_by_filter(
        plan.filters, resolved_metric_filters or []
    )

    citation_refs: list[CitationRef] = []
    citation_marker_by_identity: dict[tuple[object, ...], int] = {}
    result_rows: list[ThresholdCountRow] = []
    all_labels = []
    total_considered = 0
    excluded_count = 0
    # #1514 D9 — district tallies for the narrated coverage sentences.
    # Aggregate count rows do not serialize per-district coverage, so this
    # builder (the only place that sees per-district label states across
    # metrics) computes the DistrictCoverageSummary and passes it explicitly;
    # a district with >= 1 answer cell across metrics counts as reviewed.
    answered_district_ids: set[int] = set()
    gap_district_ids: set[int] = set()

    for metric, raw_rows in metric_rows:
        selected_rows = select_rows(plan, raw_rows, selection)
        labels = [
            coverage_label_for_answer(
                row.value,
                district_name=row.district_name,
                metric_name=metric.name,
                academic_year=row.academic_year,
            )
            for row in selected_rows
        ]
        all_labels.extend(labels)
        for row, label in zip(selected_rows, labels, strict=True):
            if is_answer_state(label.state):
                answered_district_ids.add(row.district_id)
            else:
                gap_district_ids.add(row.district_id)
        real_rows = [
            row
            for row, label in zip(selected_rows, labels, strict=True)
            if label.state == "covered"
        ]
        qualifying_rows = [
            row
            for row in real_rows
            if _row_matches_count_filters(
                row, plan.filters, scoping_metric_id_by_filter
            )
        ]
        for row in qualifying_rows:
            citation_markers_for_row(
                row,
                citation_refs,
                citation_marker_by_identity,
            )

        denominator = len(real_rows)
        count = len(qualifying_rows)
        excluded_count += len(selected_rows) - denominator
        total_considered += len(selected_rows)
        rendered_filter_statement = filter_statement(plan.filters)
        display_value = f"{count} of {denominator} covered districts"
        result_rows.append(
            ThresholdCountRow(
                metric_id=metric.metric_id,
                metric_name=metric.name,
                value=count,
                display_value=display_value,
                academic_year=academic_year,
                count=count,
                denominator=denominator,
                percent=_count_percent(count, denominator),
                filter_statement=rendered_filter_statement,
                qualifying_district_ids=sorted(
                    row.district_id for row in qualifying_rows
                ),
                denominator_district_ids=sorted(
                    row.district_id for row in real_rows
                ),
                citation_markers=sorted(
                    {
                        marker
                        for row in qualifying_rows
                        for marker in citation_markers_for_row(
                            row,
                            citation_refs,
                            citation_marker_by_identity,
                        )
                    }
                ),
                coverage_state="covered",
                coverage_display=display_value,
                coverage_reason="count_summary",
            )
        )

    # Named districts with no stored row for any requested metric were never
    # labeled above; they are not-reviewed districts, the same treatment the
    # categorical builder gives its missing_district_ids.
    gap_district_ids |= {
        district.district_id for district in selection.districts
    } - answered_district_ids
    gap_district_ids -= answered_district_ids
    out_of_universe = len(selection.unresolved_districts)
    return MetricCountResult(
        selection=selection,
        rows=list(result_rows),
        citations=citation_refs,
        coverage_frame=coverage_frame_from_labels(all_labels),
        district_coverage=DistrictCoverageSummary(
            districts_asked=(
                len(answered_district_ids | gap_district_ids) + out_of_universe
            ),
            districts_with_current_data=len(answered_district_ids),
            districts_not_reviewed=len(gap_district_ids),
            districts_out_of_universe=out_of_universe,
            requested_academic_year=academic_year,
        ),
        total_considered=total_considered,
        excluded_count=excluded_count,
        order_statement="Counted qualifying districts for selected metrics.",
        methodology_codes=[
            MethodologyRef(code="count_denominator_current_reviewed_rows"),
        ],
    )


def _count_filter_is_supported(filter_spec: FilterSpec) -> bool:
    if _filter_is_state_scope(filter_spec):
        return True
    return filter_spec.operator in {
        "equals",
        "not_equals",
        "greater_than",
        "greater_than_or_equal",
        "less_than",
        "less_than_or_equal",
    }


def _scoping_metric_id_by_filter(
    filters: Iterable[FilterSpec],
    resolved_metric_filters: list[ResolvedMetricFilter],
) -> dict[int, int]:
    """Map each metric-value FilterSpec (by identity) to its resolved metric_id.

    ``resolved_metric_filters`` is built by ``_resolve_metric_value_filters`` in
    the same order metric-value filters appear in ``plan.filters``, so they pair
    positionally. This replaces the per-filter scoping the removed
    ``"metric:<id>"`` field token used to carry (#1373 Stage 2).
    """

    metric_value_filters = [f for f in filters if filter_kind(f) == "metric_value"]
    return {
        id(filter_spec): resolved.metric_id
        for filter_spec, resolved in zip(
            metric_value_filters, resolved_metric_filters, strict=False
        )
    }


def _row_matches_count_filters(
    row: MetricAnswerRow,
    filters: Iterable[FilterSpec],
    scoping_metric_id_by_filter: dict[int, int],
) -> bool:
    for filter_spec in filters:
        if filter_kind(filter_spec) == "state":
            continue
        # A metric-value threshold applies only to rows for the metric it
        # resolved to — scoping carried by ResolvedMetricFilter.metric_id, not a
        # field-string token. This keeps a metric-A filter from narrowing
        # metric B's count in multi-metric count plans.
        scoping_metric_id = scoping_metric_id_by_filter.get(id(filter_spec))
        if scoping_metric_id is not None and row.metric_id != scoping_metric_id:
            continue
        if not _value_matches_filter(row, filter_spec):
            return False
    return True


def _value_matches_filter(row: MetricAnswerRow, filter_spec: FilterSpec) -> bool:
    value = row.value
    comparison_value = filter_spec.value
    # Parse-once on the ROW side: read the typed numeric the row carries from its
    # boundary (MetricAnswerRow.numeric_value), not a fresh parse of value. The
    # FILTER side is a comparison threshold (filter_spec.value), parsed here.
    row_number = row.numeric_value
    filter_number = _parse_count_numeric(comparison_value)
    if row_number is not None and filter_number is not None:
        if filter_spec.operator == "equals":
            return row_number == filter_number
        if filter_spec.operator == "not_equals":
            return row_number != filter_number
        if filter_spec.operator == "greater_than":
            return row_number > filter_number
        if filter_spec.operator == "greater_than_or_equal":
            return row_number >= filter_number
        if filter_spec.operator == "less_than":
            return row_number < filter_number
        if filter_spec.operator == "less_than_or_equal":
            return row_number <= filter_number

    if filter_spec.operator == "equals":
        return str(value).casefold() == str(comparison_value).casefold()
    if filter_spec.operator == "not_equals":
        return str(value).casefold() != str(comparison_value).casefold()
    return False


def _parse_count_numeric(value: object) -> float | None:
    # Delegates to the single numeric-value authority (execution/_text_utils.py).
    # The canonical regex now accepts the optional trailing "+" this parser used
    # to carry on its own, so the two can never drift again.
    return parse_compass_numeric_value(value)


def _filter_is_state_scope(filter_spec: FilterSpec) -> bool:
    return filter_kind(filter_spec) == "state"


def _normalized_text(value: str) -> str:
    return normalize_whitespace_casefold(value)


def _dedupe_text(values: Iterable[str]) -> list[str]:
    seen = set()
    deduped = []
    for value in values:
        key = _normalized_text(value)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _categorical_label_for_answer(row: MetricAnswerRow) -> CoverageLabel:
    # #1698: the "Unavailable" -> not_reviewed/reason="unavailable" remap now
    # lives in the shared coverage_label_for_answer, so the count and ranking
    # surfaces narrate it identically (single source).
    return coverage_label_for_answer(
        row.value,
        district_name=row.district_name,
        metric_name=row.metric_name,
        academic_year=row.academic_year,
    )


def _category_for_label(label: CoverageLabel) -> str:
    if label.state == "covered":
        return str(label.display).strip()
    if label.state == "ina":
        return "Issue not addressed"
    if label.state == "na":
        return "Not applicable"
    if label.reason == "unavailable":
        return "Unavailable"
    if label.state == "not_reviewed":
        return "Not reviewed"
    return "Out of universe"


def _category_label_is_safe(category: str) -> bool:
    return (
        bool(category.strip())
        and "\n" not in category
        and len(category) <= _MAX_CATEGORY_LABEL_LENGTH
    )


def _ordered_category_buckets(
    buckets: dict[tuple[str, str], set[int]],
) -> list[tuple[tuple[str, str], set[int]]]:
    state_order = {
        "covered": 0,
        "ina": 1,
        "na": 2,
        "not_reviewed": 3,
        "out_of_universe": 4,
    }
    category_order = {
        "Unavailable": 0,
        "Not reviewed": 1,
    }
    return sorted(
        buckets.items(),
        key=lambda item: (
            state_order.get(item[0][0], 99),
            -len(item[1]) if item[0][0] == "covered" else category_order.get(item[0][1], 99),
            item[0][1].casefold(),
        ),
    )


def _count_percent(count: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((count / denominator) * 100, 1)


def _metric_topic(metric: MetricCandidate) -> str | None:
    topic = (metric.topic or "").strip()
    if topic:
        return topic
    metadata_topic = metric.metadata.get("topic")
    return str(metadata_topic).strip() if metadata_topic else None


def build_filter_prevalence_summary(
    filter_rows: list[MetricAnswerRow],
    *,
    pre_narrow_selection: ResultSelection,
    post_narrow_district_count: int,
) -> FilterPrevalenceSummary | None:
    """Compute a pre-narrow prevalence tally for a filtered list answer (#1337).

    Returns ``None`` (suppress the summary) when the matched count exceeds the
    covered denominator — an incoherent "Of N covered districts, X match" tally
    that the anchor-equality shape can produce. See the guard below.

    Call this BEFORE ``_apply_metric_value_filters_to_selection`` narrows the
    selection. ``filter_rows`` must be the rows already fetched for the filter
    metric (the same rows the narrow consumed), scoped to the pre-narrow
    ``resolved_district_ids``.

    Mirrors the per-district label reduction in ``build_metric_count_result``
    and ``build_topic_coverage_count_result``: classifies each district in the
    pre-narrow selection by the filter metric's coverage_state, then returns
    the policy-honest denominator (``covered`` districts), the matched count
    (post-narrow survivors), and the not-reviewed / NA counts separately.

    For numeric filters the denominator equals ``len(real_rows)`` in
    ``build_metric_count_result`` at the same prompt, providing parity with
    the count path.  For categorical filters there is no count-path parity
    target; the NA bucket (``coverage_state == "na"``) is expected to be
    non-zero for metrics like parental-leave eligibility.
    """
    pre_narrow_ids = {d.district_id for d in pre_narrow_selection.districts}
    selected = [r for r in filter_rows if r.district_id in pre_narrow_ids]

    # Per-district reduction: classify each district by its label state.
    # A district with multiple rows for the same metric takes the first row
    # (rows are fetched for one academic year so duplicates are rare; taking
    # the first is the same strategy the narrow uses).
    state_by_district: dict[int, str] = {}
    for row in selected:
        if row.district_id not in state_by_district:
            label = coverage_label_for_answer(
                row.value,
                district_name=row.district_name,
                metric_name=row.metric_name,
                academic_year=row.academic_year,
            )
            state_by_district[row.district_id] = label.state

    # Districts in the pre-narrow selection that have NO row for the filter
    # metric are not-reviewed.
    not_reviewed_count = 0
    for d in pre_narrow_selection.districts:
        if d.district_id not in state_by_district:
            not_reviewed_count += 1

    covered_count = sum(1 for s in state_by_district.values() if s == "covered")
    na_count = sum(1 for s in state_by_district.values() if s == "na")
    # INA districts (s == "ina") are answered but not policy-covered; they are
    # excluded from both the denominator and na_count (not the same as NA).
    # The partition (covered + ina + na + not_reviewed) should equal the
    # pre-narrow universe; any extra not_reviewed slots are already counted above.

    matched = post_narrow_district_count
    # Invariant: the matched subset is drawn from the covered denominator, so
    # matched <= covered_count. The anchor-equality shape ("same <metric> as X")
    # violates it when the anchor value is itself NA / "Not addressed": every
    # NA district "matches", but NA districts are not in covered_count
    # (coverage_state == "covered"), so matched can exceed the denominator
    # (observed 40 matched vs 17 covered -> 234.8%). The "Of N covered districts,
    # X match" sentence is then incoherent AND percent would breach the
    # FilterPrevalenceSummary.percent <= 100 bound and 500 the turn. Suppress the
    # whole summary — the answer still lists the matching districts — rather than
    # fabricate a clamped percent (a grounding-sensitive product never invents a
    # number; #1772 "a value computation must never 500 the turn").
    if matched > covered_count:
        return None
    percent = _count_percent(matched, covered_count)

    return FilterPrevalenceSummary(
        matched=matched,
        denominator=covered_count,
        percent=percent,
        not_reviewed_count=not_reviewed_count,
        na_count=na_count,
    )
