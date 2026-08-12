"""Profile-field plan normalization for Compass planner output."""

from __future__ import annotations

from compass_backend.contracts.planning import (
    LimitSpec,
    MetricSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    SortSpec,
    SortStepSpec,
)

_PROFILE_RANKING_SCOPES = {
    "all_covered_districts",
    "state",
    "largest_districts",
}


def normalize_turn_profile_ranking_intent(turn: PlannerTurn) -> PlannerTurn:
    """Promote profile lookup ranking shapes to the executable rank operation."""

    if turn.route != "execute" or turn.query_plan is None:
        return turn
    normalized = normalize_plan_profile_ranking_intent(turn.query_plan)
    if normalized == turn.query_plan:
        return turn
    return turn.model_copy(update={"query_plan": normalized})


def normalize_plan_profile_ranking_intent(plan: QueryPlan) -> QueryPlan:
    """Return a rank plan when profile lookup was used for a ranking request."""

    plan = _promote_profile_sort_field(plan)
    plan = _strip_profile_metric_duplicates(plan)
    plan = _strip_profile_display_policy_presentation(plan)

    if (
        plan.operation in {"rank", "lookup"}
        and plan.profile_fields
        and plan.metrics
        and plan.selection.scope in _PROFILE_RANKING_SCOPES
        and not _has_profile_selection_step(plan)
    ):
        sort_step = _selection_step_from_profile_field(plan)
        if sort_step is not None:
            return plan.model_copy(
                update={
                    "operation": "rank",
                    "sort_steps": [*plan.sort_steps, sort_step],
                }
            )

    if (
        plan.operation == "rank"
        and plan.profile_fields
        and not plan.metrics
        and plan.selection.scope in _PROFILE_RANKING_SCOPES
    ):
        sort_step = _profile_ranking_sort_step(plan)
        if sort_step is None:
            sort_step = SortStepSpec(
                phase="presentation",
                field=plan.profile_fields[0].name,
                direction=_rank_direction(plan),
                key_type="profile_field",
                limit=plan.limit,
            )
        sort_steps = [
            step
            for step in plan.sort_steps
            if not (step.phase == "presentation" and step.key_type == "profile_field")
        ]
        sort_steps.append(sort_step)
        selection = plan.selection
        if plan.selection.scope == "largest_districts":
            selection = SelectionSpec(scope="all_covered_districts")
        return plan.model_copy(
            update={
                "selection": selection,
                "metrics": [MetricSpec(name=plan.profile_fields[0].name)],
                "profile_fields": [],
                "sort": plan.sort
                or SortSpec(
                    field=sort_step.field,
                    direction=sort_step.direction,
                    nulls=sort_step.nulls,
                ),
                "sort_steps": sort_steps,
            }
        )

    if (
        plan.operation != "profile_lookup"
        or not plan.profile_fields
        or plan.selection.scope not in _PROFILE_RANKING_SCOPES
    ):
        return plan

    sort_step = _profile_ranking_sort_step(plan)
    if sort_step is None:
        return plan

    sort_steps = [
        step
        for step in plan.sort_steps
        if not (step.phase == "presentation" and step.key_type == "profile_field")
    ]
    sort_steps.append(sort_step)
    selection = plan.selection
    if plan.selection.scope == "largest_districts":
        selection = SelectionSpec(scope="all_covered_districts")

    return plan.model_copy(
        update={
            "operation": "rank",
            "selection": selection,
            "metrics": list(plan.metrics)
            or [MetricSpec(name=plan.profile_fields[0].name)],
            "profile_fields": [],
            "sort": plan.sort
            or SortSpec(
                field=sort_step.field,
                direction=sort_step.direction,
                nulls=sort_step.nulls,
            ),
            "sort_steps": sort_steps,
        }
    )


def _has_profile_selection_step(plan: QueryPlan) -> bool:
    return any(
        step.phase == "selection"
        and step.key_type == "profile_field"
        for step in plan.sort_steps
    )


def _promote_profile_sort_field(plan: QueryPlan) -> QueryPlan:
    if (
        plan.operation != "rank"
        or plan.sort is None
        or not plan.metrics
        or plan.selection.scope not in _PROFILE_RANKING_SCOPES
        or _has_profile_selection_step(plan)
    ):
        return plan
    field = _profile_field_key(plan.sort.field)
    if field is None:
        return plan
    policy_metrics = [
        metric
        for metric in plan.metrics
        if _profile_field_key(metric.name) is None
    ]
    if not policy_metrics:
        return plan
    sort_step = SortStepSpec(
        phase="selection",
        field=field,
        direction=plan.sort.direction,
        nulls=plan.sort.nulls,
        key_type="profile_field",
        limit=plan.limit,
    )
    return plan.model_copy(
        update={
            "metrics": policy_metrics,
            "sort": None,
            "sort_steps": [*plan.sort_steps, sort_step],
        }
    )


def _strip_profile_metric_duplicates(plan: QueryPlan) -> QueryPlan:
    profile_keys = _profile_sort_keys(plan)
    if not profile_keys:
        return plan
    policy_metrics: list[MetricSpec] = []
    changed = False
    for metric in plan.metrics:
        metric_profile_key = _profile_field_key(metric.name)
        if metric_profile_key is not None and metric_profile_key in profile_keys:
            changed = True
            continue
        policy_metrics.append(metric)
    if not changed or not policy_metrics:
        return plan
    return plan.model_copy(update={"metrics": policy_metrics})


def _strip_profile_display_policy_presentation(plan: QueryPlan) -> QueryPlan:
    profile_keys = {
        field
        for profile_field in plan.profile_fields
        if (field := _profile_field_key(profile_field.name)) is not None
    }
    selection_keys = {
        field
        for step in plan.sort_steps
        if step.phase == "selection" and step.key_type == "profile_field"
        if (field := _profile_field_key(step.field)) is not None
    }
    if not profile_keys or not profile_keys.intersection(selection_keys):
        return plan
    policy_metric_names = {
        metric.name.casefold().strip()
        for metric in plan.metrics
        if _profile_field_key(metric.name) is None
    }
    sort_steps = [
        step
        for step in plan.sort_steps
        if not (
            step.phase == "presentation"
            and step.key_type == "policy_metric"
            and step.field.casefold().strip() in policy_metric_names
        )
    ]
    if len(sort_steps) == len(plan.sort_steps):
        return plan
    return plan.model_copy(update={"sort_steps": sort_steps})


def _profile_sort_keys(plan: QueryPlan) -> set[str]:
    keys = {
        field
        for step in plan.sort_steps
        if step.key_type == "profile_field"
        if (field := _profile_field_key(step.field)) is not None
    }
    keys.update(
        field
        for profile_field in plan.profile_fields
        if (field := _profile_field_key(profile_field.name)) is not None
    )
    if plan.sort is not None:
        field = _profile_field_key(plan.sort.field)
        if field is not None:
            keys.add(field)
    return keys


def _selection_step_from_profile_field(plan: QueryPlan) -> SortStepSpec | None:
    field = _canonical_profile_field_key(plan.profile_fields[0].name)
    limit = plan.limit
    if limit is None and field == "frpl_pct":
        limit = LimitSpec(count=10, kind="top")
    return SortStepSpec(
        phase="selection",
        field=field,
        direction=_rank_direction(plan),
        key_type="profile_field",
        limit=limit,
    )


def _profile_ranking_sort_step(plan: QueryPlan) -> SortStepSpec | None:
    field = plan.sort.field if plan.sort is not None else plan.profile_fields[0].name

    for step in plan.sort_steps:
        if (
            step.phase == "presentation"
            and step.key_type == "profile_field"
        ):
            return step

    if plan.sort is None:
        return None
    return SortStepSpec(
        phase="presentation",
        field=field,
        direction=plan.sort.direction,
        nulls=plan.sort.nulls,
        key_type="profile_field",
        limit=plan.limit,
    )


def _rank_direction(plan: QueryPlan) -> str:
    if plan.sort is not None:
        return plan.sort.direction
    if plan.limit is not None and plan.limit.kind == "bottom":
        return "asc"
    return "desc"


def _canonical_profile_field_key(field_name: str) -> str:
    """Normalize typed profile-field names the planner already emitted."""

    return _profile_field_key(field_name) or field_name


def _profile_field_key(field_name: str) -> str | None:
    tokens = set(
        field_name.casefold()
        .replace("&", " ")
        .replace("-", " ")
        .replace("_", " ")
        .replace("%", " pct ")
        .split()
    )
    if "frpl" in tokens or {"free", "reduced", "lunch"}.issubset(tokens):
        return "frpl_pct"
    return None
