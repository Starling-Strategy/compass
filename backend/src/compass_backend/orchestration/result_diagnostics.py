"""Result diagnostics for chat-turn orchestration.

Pure helpers extracted verbatim from `orchestration/chat.py` (#1130
decomposition): query-context construction from a validated ``ResultSet``,
manifest-metadata attachment, and span trace-attribute setters. Behaviour-
preserving move — no logic changes.
"""

from compass_backend.artifacts import ResultSet
from compass_backend.catalog import CatalogResolutionReport, RecallReport
from compass_backend.contracts import (
    QueryContext,
    QueryContextDistrictRef,
    QueryContextMetricRef,
)
from compass_backend.contracts.planning import PlannerTurn
from compass_backend.rendering import display_metadata_for_result
from compass_backend.session.memory import _MAX_CONTEXT_RESULT_REFS

def _query_context_for_result(
    *,
    query_plan,
    authority,
    result: ResultSet,
) -> QueryContext:
    """Build compact session memory from a validated deterministic result."""

    display_metadata = display_metadata_for_result(query_plan, result)
    return QueryContext(
        query_plan=query_plan,
        authority=authority,
        result_type=result.result_type,
        order_statement=result.order_statement,
        row_count=len(_result_rows(result)),
        displayed_row_count=display_metadata["displayed_row_count"],
        display_limit=display_metadata["display_limit"],
        row_display=str(display_metadata["row_display"]),
        data_limit_count=display_metadata["data_limit_count"],
        data_limit_kind=display_metadata["data_limit_kind"],
        data_limit_source=str(display_metadata["data_limit_source"]),
        display_limit_source=str(display_metadata["display_limit_source"]),
        result_districts=_result_district_refs(result),
        result_population_districts=_result_population_district_refs(result),
        count_denominator=_result_count_denominator(result),
        result_metrics=_result_metric_refs(result),
    )


def _result_population_district_refs(
    result: ResultSet,
) -> list[QueryContextDistrictRef]:
    """Build prior-result POPULATION refs from count denominator members (#393).

    A threshold count carries the covered-with-a-value population it counted over
    in ``ThresholdCountRow.denominator_district_ids`` (the denominator's members),
    distinct from ``qualifying_district_ids`` (the matching subset). This extractor
    returns the population so a "who are the <denominator>?" follow-up can present
    it. Empty for non-count results (no ``denominator_district_ids``).
    """
    refs: list[QueryContextDistrictRef] = []
    seen: set[int] = set()
    population_ids: list[int] = []
    for row in _result_rows(result):
        for district_id in getattr(row, "denominator_district_ids", []):
            if district_id not in population_ids:
                population_ids.append(district_id)
    if not population_ids or result.selection is None:
        return refs
    population = set(population_ids)
    for district in result.selection.districts:
        if district.district_id not in population or district.district_id in seen:
            continue
        seen.add(district.district_id)
        refs.append(
            QueryContextDistrictRef(
                district_id=district.district_id,
                district_name=district.district_name,
                state=district.state,
            )
        )
        if len(refs) >= _MAX_CONTEXT_RESULT_REFS:
            break
    return refs


def _result_count_denominator(result: ResultSet) -> int | None:
    """Return the prior count's denominator (the population SIZE) as an integer.

    #1658: the population MEMBER list (``denominator_district_ids``) is dropped in
    serialization ~12% of the time, but this scalar survives reliably — so a
    "who are the <denominator>?" follow-up grounds on it (not the member-list
    length). Sourced from the metric-threshold count row's ``denominator``; skips
    topic-coverage aggregates, whose denominator is the covered universe rather
    than a metric-value population.
    """
    if result.result_type != "metric_count":
        return None
    for row in _result_rows(result):
        if (
            getattr(row, "count_kind", None) == "threshold_count"
            and getattr(row, "coverage_reason", None) != "topic_coverage_count"
        ):
            return row.denominator
    return None


def _result_district_refs(result: ResultSet) -> list[QueryContextDistrictRef]:
    """Build prior-result district refs, capped at _MAX_CONTEXT_RESULT_REFS.

    Prefers count-result ``qualifying_district_ids`` (the threshold count's
    canonical district list) before falling back to row districts; both
    sources are size-limited so the next planner turn's context payload
    stays bounded. The cap was raised from 25 to 200 in the M3 fix series
    so a covered-universe-sized prior set (~133) round-trips.
    """
    refs: list[QueryContextDistrictRef] = []
    seen: set[int] = set()
    qualifying_ids: list[int] = []
    for row in _result_rows(result):
        for district_id in getattr(row, "qualifying_district_ids", []):
            if district_id not in qualifying_ids:
                qualifying_ids.append(district_id)
    if qualifying_ids and result.selection is not None:
        qualifying = set(qualifying_ids)
        for district in result.selection.districts:
            if district.district_id not in qualifying or district.district_id in seen:
                continue
            seen.add(district.district_id)
            refs.append(
                QueryContextDistrictRef(
                    district_id=district.district_id,
                    district_name=district.district_name,
                    state=district.state,
                )
            )
            if len(refs) >= _MAX_CONTEXT_RESULT_REFS:
                return refs
    for row in _result_rows(result):
        district_id = row.district_id
        if district_id is None or district_id in seen:
            continue
        seen.add(district_id)
        refs.append(
            QueryContextDistrictRef(
                district_id=district_id,
                district_name=row.district_name,
                state=row.state,
            )
        )
        if len(refs) >= _MAX_CONTEXT_RESULT_REFS:
            break
    return refs


def _result_metric_refs(result: ResultSet) -> list[QueryContextMetricRef]:
    refs: list[QueryContextMetricRef] = []
    seen: set[int] = set()
    for row in _result_rows(result):
        if row.metric_id in seen:
            continue
        seen.add(row.metric_id)
        refs.append(
            QueryContextMetricRef(
                metric_id=row.metric_id,
                metric_name=row.metric_name,
            )
        )
        if len(refs) >= _MAX_CONTEXT_RESULT_REFS:
            break
    return refs


def _result_rows(result: ResultSet) -> list:
    if getattr(result, "result_type", None) != "composite_ranking":
        return list(result.rows)
    return [
        row
        for child in getattr(result, "children", [])
        for row in getattr(child, "rows", [])
    ]


def _attach_resolution_manifest_metadata(
    manifest,
    resolution_report: CatalogResolutionReport | None,
):
    """Attach resolver diagnostics to the existing manifest metadata surface."""

    if resolution_report is None:
        return manifest
    return manifest.model_copy(
        update={
            "metadata": {
                **manifest.metadata,
                "catalog_resolution": {
                    **resolution_report.model_dump(mode="json"),
                    "summary_status": resolution_report.summary_status(),
                    "summary_methods": resolution_report.summary_methods(),
                },
            }
        }
    )


def _attach_recall_manifest_metadata(
    manifest,
    recall_report: RecallReport | None,
):
    """Attach shadow recall diagnostics to the existing manifest metadata."""

    if recall_report is None:
        return manifest
    return manifest.model_copy(
        update={
            "metadata": {
                **manifest.metadata,
                "catalog_recall": recall_report.model_dump(mode="json"),
            }
        }
    )


def _merge_recall_reports(
    first: RecallReport | None,
    second: RecallReport | None,
) -> RecallReport | None:
    if first is None:
        return second
    if second is None:
        return first
    return RecallReport(
        query=first.query,
        batches=[*first.batches, *second.batches],
    )


def _turn_trace_attributes(turn: PlannerTurn) -> dict[str, object]:
    """Return compact planner attributes for the root turn span."""

    attributes: dict[str, object] = {
        "route": turn.route,
        "planner_confidence": turn.confidence,
    }
    if turn.query_plan is not None:
        attributes.update(
            {
                "operation": turn.query_plan.operation,
                "inherit_from_session": turn.query_plan.inherit_from_session,
                "selection_scope": turn.query_plan.selection.scope,
                "selection_districts": turn.query_plan.selection.districts,
                "selection_states": turn.query_plan.selection.states,
                "metrics": [metric.name for metric in turn.query_plan.metrics],
                "output_format": turn.query_plan.output.format,
                "temporal_intent": turn.query_plan.temporal.intent,
                "temporal_academic_year": turn.query_plan.temporal.academic_year,
                "temporal_academic_years": turn.query_plan.temporal.academic_years,
            }
        )
    if turn.clarification is not None:
        attributes.update(
            {
                "clarification_missing_fields": turn.clarification.missing_fields,
                "clarification_candidates": turn.clarification.candidates,
            }
        )
    if turn.direct_response is not None:
        attributes["direct_reason"] = turn.direct_response.reason
    return attributes


def _result_trace_attributes(result) -> dict[str, object]:
    criteria = getattr(result, "criteria", None) or []
    if not criteria:
        return {}
    qualifying_sets = [
        set(criterion.qualifying_district_ids) for criterion in criteria
    ]
    qualifying_intersection = (
        set.intersection(*qualifying_sets) if qualifying_sets else set()
    )
    return {
        "criteria_count": len(criteria),
        "criteria_labels": [criterion.label for criterion in criteria],
        "criteria_metric_ids": [
            f"{criterion.criterion_id}:{','.join(str(mid) for mid in criterion.metric_ids)}"
            for criterion in criteria
        ],
        "criteria_qualifying_counts": [
            len(criterion.qualifying_district_ids) for criterion in criteria
        ],
        "criteria_intersection_count": len(qualifying_intersection),
        "criteria_intersection_district_ids": sorted(qualifying_intersection),
        "criteria_excluded_count": result.excluded_count,
    }
