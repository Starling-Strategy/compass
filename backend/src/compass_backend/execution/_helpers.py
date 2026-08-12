"""Free helper functions for the deterministic query executor.

These were extracted from `executor.py` (which was 1,852 LOC) so that the
DeterministicQueryExecutor class and its operation/scoping method groups
can each live in their own files without duplicating these helpers. The
behavior is unchanged; the module boundary is the only edit.

Used by `executor.py` (`execute()` dispatch) and by the mixins in
`operations.py` and `scoping.py`.
"""

from __future__ import annotations

from typing import Any

from compass_backend.artifacts import ResultSelection, ResultSet, SelectedDistrict
from compass_backend.catalog import (
    CatalogResolutionCandidate,
    CatalogResolutionEntity,
    CatalogResolutionReport,
    MetricBundleResolution,
    MetricCandidate,
    MetricResolution,
    NCESFieldCandidate,
    ProfileFieldRankRow,
)
from compass_backend.catalog.reporting import (
    CatalogResolutionEntityType,
    report_from_entities,
)
from compass_backend.contracts.planning import (
    LimitSpec,
    QueryPlan,
    SortStepSpec,
)

# Backward-compat re-exports (the implementations live in _clarify_helpers,
# a leaf module with no back-deps into execution/, so answer_layer/clarify.py
# can import them without the deferred-import workaround).
from compass_backend._clarify_helpers import (
    _metric_clarification as _metric_clarification,
)
from compass_backend.contracts.validation import (
    ResolvedMetricAuthority,
    ResolvedProfileFieldAuthority,
    ResolvedSelectionAuthority,
    ValidationAuthority,
)

from .selection import direction as _plan_direction
from .types import ExecutionOutcome, ExecutionSuccess

_UNSUPPORTED_SHAPE_MESSAGE = (
    "I need one district group and one Compass metric to answer that from "
    "governed data. Try naming the group and the metric you want to compare."
)


def _cap_list(items: list[Any] | None, limit: int) -> list[Any] | None:
    """Cap an attribute list to ``limit`` items plus a '…' sentinel when truncated.

    Used to keep Logfire attributes bounded for large metric/district selections.
    Returns ``None`` for ``None`` input so callers preserve "absent" semantics.
    """

    if items is None:
        return None
    items_list = list(items)
    if len(items_list) <= limit:
        return items_list
    return [*items_list[:limit], "…"]


def _execution_metric_specs(plan: QueryPlan):
    """Return metric specs that produce result cells, excluding selection filters."""

    return [
        metric
        for metric in plan.metrics
        if metric.role not in {"filter", "grouping"}
    ]


def _flatten_metric_groups(
    metric_groups: list[tuple[str, str, list[MetricCandidate]]],
) -> list[MetricCandidate]:
    metrics: list[MetricCandidate] = []
    seen_metric_ids: set[int] = set()
    for _criterion_id, _label, group_metrics in metric_groups:
        for metric in group_metrics:
            if metric.metric_id in seen_metric_ids:
                continue
            metrics.append(metric)
            seen_metric_ids.add(metric.metric_id)
    return metrics


def _primary_metric_spec(plan: QueryPlan):
    metrics = _execution_metric_specs(plan)
    if not metrics or metrics[0].role != "primary":
        return None
    return metrics[0]


def _primary_metric_is_policy_metric(plan: QueryPlan) -> bool:
    metric = _primary_metric_spec(plan)
    return metric is not None


def _lookup_academic_year(
    plan: QueryPlan,
    *,
    current_academic_year: str,
) -> str:
    """Return the academic year an exact lookup is allowed to fetch."""

    if plan.temporal.intent == "specific_year" and plan.temporal.academic_year:
        return plan.temporal.academic_year
    if plan.temporal.intent == "current" and plan.temporal.academic_year:
        return plan.temporal.academic_year
    return current_academic_year


def _lookup_allows_recent_fallback(plan: QueryPlan) -> bool:
    """Return whether missing lookup rows may disclose recent reviewed values."""

    return plan.temporal.intent in {"current", "latest_available"}


def _lookup_chart_requires_numeric_metric_resolution(plan: QueryPlan) -> bool:
    if plan.output.format != "chart":
        return False
    # Both prior-result inherit modes materialize named_districts from a prior
    # result (rows or, for #393, the count's population), so they share the
    # exemption from forced numeric metric resolution.
    return plan.inherit_selection_from not in {
        "prior_result_rows",
        "prior_result_population",
    }


def _profile_presentation_step(plan: QueryPlan) -> SortStepSpec | None:
    for step in plan.sort_steps:
        if step.phase == "presentation" and step.key_type == "profile_field":
            return step
    return None


def _policy_presentation_step(plan: QueryPlan) -> SortStepSpec | None:
    for step in plan.sort_steps:
        if step.phase == "presentation" and step.key_type == "policy_metric":
            return step
    return None


def _profile_selection_step(plan: QueryPlan) -> SortStepSpec | None:
    for step in plan.sort_steps:
        if step.phase == "selection" and step.key_type == "profile_field":
            return step
    if plan.profile_fields:
        return SortStepSpec(
            phase="selection",
            field=plan.profile_fields[0].name,
            direction=_plan_direction(plan),
            key_type="profile_field",
            limit=plan.limit,
            defaulted=True,
        )
    if plan.selection.scope == "largest_districts":
        return SortStepSpec(
            phase="selection",
            field="enrollment",
            direction="desc",
            key_type="profile_field",
            limit=plan.limit,
        )
    return None


def _sort_step_limit(
    step: SortStepSpec,
    plan_limit: LimitSpec | None,
) -> int | None:
    if step.limit is not None:
        if step.limit.kind == "all":
            return None
        return step.limit.count
    if plan_limit is not None:
        if plan_limit.kind == "all":
            return None
        return plan_limit.count
    return None


def _profile_ranking_limit(
    plan: QueryPlan,
    step: SortStepSpec,
    *,
    default_limit: int,
) -> int | None:
    # Explicit kind="all" at plan or step level means no cap.
    if (
        (step.limit is not None and step.limit.kind == "all")
        or (plan.limit is not None and plan.limit.kind == "all")
    ):
        return None
    explicit_limit = _sort_step_limit(step, plan.limit)
    if explicit_limit is not None:
        return explicit_limit
    if plan.selection.scope == "largest_districts":
        return default_limit
    return None


def _selection_from_profile_rows(
    plan: QueryPlan,
    rows: list[ProfileFieldRankRow],
    *,
    states: set[str],
) -> ResultSelection:
    return ResultSelection(
        scope=plan.selection.scope,
        districts=[
            SelectedDistrict(
                district_id=row.district_id,
                district_name=row.district_name,
                state=row.state,
            )
            for row in rows
        ],
        states=sorted(states),
    )


def _plan_concept_phrases(plan: QueryPlan) -> list[str]:
    """Return planner phrases that can map to unsupported catalog concepts."""

    phrases = [metric.name for metric in _execution_metric_specs(plan)]
    phrases.extend(field.name for field in plan.profile_fields)
    phrases.append(plan.question)
    return phrases


def _unsupported_resolution_report(
    plan: QueryPlan,
    entities: list[CatalogResolutionEntity],
) -> CatalogResolutionReport:
    return report_from_entities(
        question=plan.question,
        operation=plan.operation,
        entities=entities,
    )


def _resolution_report_from_outcome(
    plan: QueryPlan,
    outcome: ExecutionOutcome,
) -> CatalogResolutionReport:
    """Build debug metadata from actual execution authority and artifacts."""

    authority = outcome.authority if isinstance(outcome, ExecutionSuccess) else None
    result = outcome.result if isinstance(outcome, ExecutionSuccess) else None

    entities: list[CatalogResolutionEntity] = []
    if authority is not None:
        entities.extend(_selection_authority_entities(plan, authority, result))
        entities.extend(_topic_authority_entities(plan, authority))
        entities.extend(_metric_authority_entities(plan, authority))
        entities.extend(_profile_field_entities(authority))

    if result is not None and result.result_type == "peer_comparison":
        entities.append(
            CatalogResolutionEntity(
                input_phrase="peer districts",
                entity_type="peer_method",
                status="approved",
                resolution_method="catalog_search",
                approved_key="deterministic_nces_peer_set",
                label="Deterministic NCES-style peer set",
                provenance="CatalogResolver.resolve_peer_set",
            )
        )

    return report_from_entities(
        question=plan.question,
        operation=plan.operation,
        entities=entities,
    )


def _selection_authority_entities(
    plan: QueryPlan,
    authority: ValidationAuthority,
    result: ResultSet | None,
) -> list[CatalogResolutionEntity]:
    if authority.selection is None:
        return []
    selection = authority.selection
    if selection.district_ids:
        label_by_id = {}
        if result is not None and result.selection is not None:
            label_by_id = {
                district.district_id: _district_label(
                    district.district_name,
                    district.state,
                )
                for district in result.selection.districts
            }
        phrases = plan.selection.districts or [
            label_by_id.get(district_id, str(district_id))
            for district_id in selection.district_ids
        ]
        return [
            CatalogResolutionEntity(
                input_phrase=phrases[index] if index < len(phrases) else str(district_id),
                entity_type="district",
                status="approved",
                resolution_method="catalog_search",
                approved_ids=[str(district_id)],
                label=label_by_id.get(district_id),
                provenance="CatalogResolver.resolve_districts",
            )
            for index, district_id in enumerate(selection.district_ids)
        ]
    if selection.states:
        return [
            CatalogResolutionEntity(
                input_phrase=", ".join(selection.states),
                entity_type="region",
                status="approved",
                resolution_method="static_catalog",
                approved_ids=selection.states,
                label="State filter",
                provenance="execution.selection.requested_states",
            )
        ]
    return []


def _metric_authority_entities(
    plan: QueryPlan,
    authority: ValidationAuthority,
) -> list[CatalogResolutionEntity]:
    if not authority.metrics:
        return []
    metric_specs = _execution_metric_specs(plan)
    if len(metric_specs) == 1 and len(authority.metrics) > 1:
        return [
            CatalogResolutionEntity(
                input_phrase=metric_specs[0].name,
                entity_type="metric_bundle",
                status="approved",
                resolution_method="catalog_search",
                approved_ids=[str(metric.metric_id) for metric in authority.metrics],
                label=metric_specs[0].name,
                candidates=[
                    CatalogResolutionCandidate(
                        candidate_id=str(metric.metric_id),
                        label=metric.metric_name,
                        entity_type="metric",
                        metadata={"role": metric.role},
                    )
                    for metric in authority.metrics
                ],
                provenance="CatalogResolver.resolve_metric_bundle",
            )
        ]
    entities: list[CatalogResolutionEntity] = []
    for index, metric in enumerate(authority.metrics):
        phrase = metric_specs[index].name if index < len(metric_specs) else metric.metric_name
        entities.append(
            CatalogResolutionEntity(
                input_phrase=phrase,
                entity_type="metric",
                status="approved",
                resolution_method="catalog_search",
                approved_ids=[str(metric.metric_id)],
                label=metric.metric_name,
                provenance="CatalogResolver.resolve_metric_bundle",
            )
        )
    return entities


def _topic_authority_entities(
    plan: QueryPlan,
    authority: ValidationAuthority,
) -> list[CatalogResolutionEntity]:
    entities: list[CatalogResolutionEntity] = []
    seen: set[tuple[str, ...]] = set()
    metric_specs = _execution_metric_specs(plan)
    for index, metric in enumerate(authority.metrics):
        raw_frame = metric.metadata.get("topic_frame")
        if not isinstance(raw_frame, dict):
            continue
        topic_ids = [
            f"topic:{topic_id}" for topic_id in raw_frame.get("topic_ids", [])
        ]
        subtopic_ids = [
            f"subtopic:{subtopic_id}"
            for subtopic_id in raw_frame.get("subtopic_ids", [])
        ]
        approved_ids = topic_ids + subtopic_ids
        key = tuple(approved_ids)
        if not approved_ids or key in seen:
            continue
        seen.add(key)
        phrase = (
            metric_specs[index].name
            if index < len(metric_specs)
            else metric.metric_name
        )
        entities.append(
            CatalogResolutionEntity(
                input_phrase=phrase,
                entity_type="topic",
                status="approved",
                resolution_method="topic_narrowing",
                approved_ids=approved_ids,
                label="Topic frame",
                candidates=[
                    CatalogResolutionCandidate(
                        candidate_id=approved_id,
                        label=approved_id,
                        entity_type="topic",
                        metadata={
                            "metric_ids": raw_frame.get("metric_ids", []),
                            "provenance": raw_frame.get("provenance"),
                        },
                    )
                    for approved_id in approved_ids
                ],
                provenance=str(
                    raw_frame.get("provenance")
                    or "CatalogResolver.topic_narrowing"
                ),
            )
        )
    return entities


def _profile_field_entities(
    authority: ValidationAuthority,
) -> list[CatalogResolutionEntity]:
    return [
        CatalogResolutionEntity(
            input_phrase=field.input_phrase,
            entity_type="profile_field",
            status="approved",
            resolution_method=str(field.metadata.get("resolution_method", "catalog_search")),
            approved_key=field.field_key,
            label=field.label,
            provenance="CatalogResolver.resolve_profile_field",
        )
        for field in authority.profile_fields
    ]


def _metric_resolution_report(
    plan: QueryPlan,
    phrase: str,
    resolution: MetricResolution | MetricBundleResolution,
    *,
    entity_type: CatalogResolutionEntityType,
) -> CatalogResolutionReport:
    if getattr(resolution, "ambiguous", False):
        status = "ambiguous"
        method = "catalog_search"
        message = "Multiple Compass metrics matched this phrase."
    elif getattr(resolution, "resolved", None):
        status = "approved"
        method = "catalog_search"
        message = None
    else:
        status = "unresolved"
        method = "unresolved"
        message = "No approved Compass metric resolved for this phrase."
    resolved = list(getattr(resolution, "resolved", []) or [])
    candidates = list(getattr(resolution, "candidates", []) or [])
    entities: list[CatalogResolutionEntity] = []
    topic_entity = _topic_frame_entity(phrase, resolution)
    if topic_entity is not None:
        entities.append(topic_entity)
    entities.append(
        CatalogResolutionEntity(
            input_phrase=phrase,
            entity_type=entity_type,
            status=status,
            resolution_method=method,
            approved_ids=[str(metric.metric_id) for metric in resolved],
            label=phrase,
            candidates=[
                CatalogResolutionCandidate(
                    candidate_id=str(candidate.metric_id),
                    label=candidate.name,
                    entity_type="metric",
                    metadata={
                        **candidate.metadata,
                        "answer_type": candidate.answer_type,
                    },
                )
                for candidate in candidates
            ],
            provenance="CatalogResolver.resolve_metric_bundle",
            message=message,
        )
    )
    return report_from_entities(
        question=plan.question,
        operation=plan.operation,
        entities=entities,
    )


def _topic_frame_entity(
    phrase: str,
    resolution: MetricResolution | MetricBundleResolution,
) -> CatalogResolutionEntity | None:
    topic_frame = getattr(resolution, "topic_frame", None)
    if topic_frame is None or topic_frame.authority is None:
        return None
    authority = topic_frame.authority
    approved_ids = [
        *(f"topic:{topic_id}" for topic_id in authority.topic_ids),
        *(f"subtopic:{subtopic_id}" for subtopic_id in authority.subtopic_ids),
    ]
    if not approved_ids:
        return None
    labels = [
        (
            f"{topic.topic_name} / {topic.subtopic_name}"
            if topic.subtopic_name
            else topic.topic_name
        )
        for topic in topic_frame.candidates
    ]
    return CatalogResolutionEntity(
        input_phrase=phrase,
        entity_type="topic",
        status="approved",
        resolution_method="topic_narrowing",
        approved_ids=approved_ids,
        label=", ".join(labels) if labels else phrase,
        candidates=[
            CatalogResolutionCandidate(
                candidate_id=approved_id,
                label=approved_id,
                entity_type="topic",
                metadata={
                    "metric_ids": authority.metric_ids,
                    "related_content": [
                        link.model_dump(mode="json")
                        for link in authority.related_content
                    ],
                },
            )
            for approved_id in approved_ids
        ],
        provenance=topic_frame.provenance,
    )


def _district_label(name: str, state: str | None) -> str:
    return f"{name}, {state}" if state else name


def _validation_authority(
    plan: QueryPlan,
    selection: ResultSelection,
    metrics: list[MetricCandidate],
    *,
    profile_fields: list[tuple[str, NCESFieldCandidate, str]] | None = None,
) -> ValidationAuthority:
    """Build internal resolved-ID authority for validators."""

    return ValidationAuthority(
        metrics=[
            ResolvedMetricAuthority(
                metric_id=metric.metric_id,
                metric_name=metric.name,
                role="primary" if index == 0 else "comparison",
                metadata=metric.metadata,
            )
            for index, metric in enumerate(metrics)
        ],
        profile_fields=[
            ResolvedProfileFieldAuthority(
                field_key=field.field_key,
                label=field.label,
                input_phrase=input_phrase,
                metadata={"resolution_method": resolution_method},
            )
            for input_phrase, field, resolution_method in (profile_fields or [])
        ],
        selection=ResolvedSelectionAuthority(
            scope=selection.scope,
            district_ids=[
                district.district_id for district in selection.districts
            ],
            states=selection.states,
        ),
    )
