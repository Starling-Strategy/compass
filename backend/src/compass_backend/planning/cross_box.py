"""Catalog-backed repair for planner phrases placed in the wrong typed slot."""

from __future__ import annotations

import re
from dataclasses import dataclass

from compass_backend.catalog import (
    CatalogConceptAuthority,
    CatalogResolver,
    MetricBundleResolution,
    MetricResolution,
    ProfileFieldResolution,
)
from compass_backend.contracts.planning import (
    ClarificationRequest,
    LimitSpec,
    MetricSpec,
    PendingQueryContext,
    PlannerTurn,
    QueryPlan,
    SortDirection,
    SortStepSpec,
    pending_context_from_plan,
)


@dataclass(frozen=True)
class _MovedProfileMetric:
    """One metric-slot phrase that catalog authority approved as a profile field."""

    metric: MetricSpec
    authority: CatalogConceptAuthority

    @property
    def field_phrase(self) -> str:
        return self.metric.name


async def review_cross_box_planner_turn(
    turn: PlannerTurn,
    catalog: CatalogResolver,
) -> PlannerTurn:
    """Repair planner turns where a valid concept landed in the wrong box.

    The review is intentionally typed: it reads the planner's ``QueryPlan`` or
    ``ClarificationRequest.pending_context`` slots, then asks ``CatalogResolver``
    where each metric phrase is approved. It does not inspect raw user prose.
    """

    if turn.route == "execute" and turn.query_plan is not None:
        repaired = await repair_cross_box_query_plan(turn.query_plan, catalog)
        if repaired is turn.query_plan:
            return turn
        if isinstance(repaired, ClarificationRequest):
            return PlannerTurn(
                route="clarify",
                confidence=turn.confidence,
                clarification=repaired,
            )
        return turn.model_copy(update={"query_plan": repaired})

    if turn.route == "clarify" and turn.clarification is not None:
        pending_plan = _pending_context_to_plan(
            turn.clarification.pending_context,
            question=turn.clarification.question,
        )
        if pending_plan is None:
            return turn
        repaired = await repair_cross_box_query_plan(pending_plan, catalog)
        if isinstance(repaired, QueryPlan) and repaired != pending_plan:
            return PlannerTurn(
                route="execute",
                confidence=turn.confidence,
                query_plan=repaired,
            )
        if isinstance(repaired, ClarificationRequest):
            return PlannerTurn(
                route="clarify",
                confidence=turn.confidence,
                clarification=repaired,
            )
    return turn


async def repair_cross_box_query_plan(
    plan: QueryPlan,
    catalog: CatalogResolver,
) -> QueryPlan | ClarificationRequest:
    """Return a repaired plan when metric phrases resolve only as profile fields."""

    if plan.operation not in {"rank", "lookup"} or not plan.metrics:
        return plan

    retained_metrics: list[MetricSpec] = []
    moved_profile_metrics: list[_MovedProfileMetric] = []

    for metric in plan.metrics:
        profile_field = await catalog.resolve_profile_field_authority(metric.name)
        if not profile_field.ok:
            retained_metrics.append(metric)
            continue

        metric_bundle = await catalog.resolve_metric_bundle(
            metric.name,
            numeric_only=False,
            limit=5,
            degree_lane=metric.degree_lane,
        )
        authority = _concept_authority_from_metric_and_profile(
            metric.name,
            metric_bundle,
            profile_field,
        )
        approved = set(authority.approved_entity_types)
        metric_has_candidates = bool(
            (authority.metric is not None and authority.metric.candidates)
            or (
                authority.metric_bundle is not None
                and authority.metric_bundle.candidates
            )
        )

        if "profile_field" in approved and (
            "metric" in approved
            or "metric_bundle" in approved
            or metric_has_candidates
        ):
            return _ambiguous_cross_box_clarification(plan, metric.name, authority)

        if "profile_field" in approved:
            moved_profile_metrics.append(
                _MovedProfileMetric(metric=metric, authority=authority)
            )
            continue

        retained_metrics.append(metric)

    if not moved_profile_metrics:
        return plan

    profile_metric = moved_profile_metrics[0]
    sort_step = _profile_sort_step_for_plan(
        plan,
        profile_metric.field_phrase,
        phase="selection" if retained_metrics else "presentation",
    )
    updates: dict[str, object] = {
        "operation": "rank",
        "sort": None if _sort_matches_phrase(plan, profile_metric.field_phrase) else plan.sort,
        "sort_steps": _replace_profile_sort_step(plan, sort_step),
    }
    if retained_metrics:
        updates["metrics"] = retained_metrics
    else:
        # The current QueryPlan contract requires a metric for rank operations.
        # For pure profile rankings, keep the approved profile phrase as the
        # display metric while the sort_step carries profile_field authority.
        updates["metrics"] = [profile_metric.metric]

    return plan.model_copy(update=updates)


def _concept_authority_from_metric_and_profile(
    query: str,
    metric_bundle: MetricBundleResolution,
    profile_field: ProfileFieldResolution,
) -> CatalogConceptAuthority:
    if metric_bundle.ambiguous:
        metric = MetricResolution(
            query=query,
            candidates=metric_bundle.candidates,
            ambiguous=True,
            ambiguity_key=metric_bundle.bundle_key,
        )
    elif len(metric_bundle.resolved) == 1:
        metric = MetricResolution(
            query=query,
            resolved=metric_bundle.resolved[0],
            candidates=metric_bundle.candidates,
        )
    else:
        metric = MetricResolution(
            query=query,
            candidates=metric_bundle.candidates,
            ambiguous=bool(metric_bundle.resolved),
            ambiguity_key=metric_bundle.bundle_key,
        )
    return CatalogConceptAuthority(
        query=query,
        metric=metric,
        metric_bundle=metric_bundle,
        profile_field=profile_field,
    )


def _profile_sort_step_for_plan(
    plan: QueryPlan,
    field_phrase: str,
    *,
    phase: str,
) -> SortStepSpec:
    matching_step = _matching_sort_step(plan, field_phrase)
    if matching_step is not None:
        return matching_step.model_copy(
            update={"phase": phase, "key_type": "profile_field"}
        )
    return SortStepSpec(
        phase=phase,
        field=field_phrase,
        direction=_profile_direction(plan, field_phrase),
        key_type="profile_field",
        limit=_profile_limit(plan, field_phrase),
    )


def _replace_profile_sort_step(
    plan: QueryPlan,
    sort_step: SortStepSpec,
) -> list[SortStepSpec]:
    normalized_field = _normalize_phrase(sort_step.field)
    retained = [
        step
        for step in plan.sort_steps
        if not (
            step.phase == sort_step.phase
            and step.key_type == "profile_field"
            and _normalize_phrase(step.field) == normalized_field
        )
    ]
    return [*retained, sort_step]


def _matching_sort_step(plan: QueryPlan, field_phrase: str) -> SortStepSpec | None:
    normalized = _normalize_phrase(field_phrase)
    for step in plan.sort_steps:
        if _normalize_phrase(step.field) == normalized:
            return step
    return None


def _profile_direction(plan: QueryPlan, field_phrase: str) -> SortDirection:
    if plan.sort is not None and _sort_matches_phrase(plan, field_phrase):
        return plan.sort.direction
    step = _matching_sort_step(plan, field_phrase)
    if step is not None:
        return step.direction
    if plan.limit is not None and plan.limit.kind == "bottom":
        return "asc"
    return "desc"


def _profile_limit(plan: QueryPlan, field_phrase: str) -> LimitSpec | None:
    step = _matching_sort_step(plan, field_phrase)
    if step is not None and step.limit is not None:
        return step.limit
    return plan.limit


def _sort_matches_phrase(plan: QueryPlan, field_phrase: str) -> bool:
    return (
        plan.sort is not None
        and _normalize_phrase(plan.sort.field) == _normalize_phrase(field_phrase)
    )


def _ambiguous_cross_box_clarification(
    plan: QueryPlan,
    phrase: str,
    authority: CatalogConceptAuthority,
) -> ClarificationRequest:
    candidates: list[str] = []
    if authority.metric is not None:
        if authority.metric.resolved is not None:
            candidates.append(authority.metric.resolved.name)
        candidates.extend(candidate.name for candidate in authority.metric.candidates)
    if authority.metric_bundle is not None:
        candidates.extend(candidate.name for candidate in authority.metric_bundle.candidates)
    if authority.profile_field is not None and authority.profile_field.resolved is not None:
        candidates.append(authority.profile_field.resolved.label)

    deduped_candidates = list(dict.fromkeys(candidates))
    return ClarificationRequest(
        question=(
            f'I found "{phrase}" in more than one Compass catalog. '
            "Should I treat it as a policy metric or as a district fact?"
        ),
        missing_fields=["metric"],
        candidates=deduped_candidates,
        # Single from-plan builder (#1419): the shared
        # ``pending_context_from_plan`` now carries the full W1-02 (#836) field
        # set (sort_steps / inherit_selection_from / requires_all_metrics /
        # requires_composite_ranking / count_kind) that this local copy
        # previously dropped.
        pending_context=pending_context_from_plan(plan, missing_fields=["metric"]),
    )


def _pending_context_to_plan(
    context: PendingQueryContext | None,
    *,
    question: str,
) -> QueryPlan | None:
    if context is None or context.selection is None:
        return None
    if context.operation is None and not context.metrics and not context.profile_fields:
        return None
    try:
        return QueryPlan(
            operation=context.operation or "lookup",
            question=context.question or question,
            selection=context.selection,
            metrics=context.metrics,
            profile_fields=context.profile_fields,
            filters=context.filters,
            sort=context.sort,
            limit=context.limit,
            output=context.output or {},
            temporal=context.temporal or {},
            similarity=context.similarity,
            peer_overrides=context.peer_overrides,
        )
    except ValueError:
        return None


def _normalize_phrase(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
