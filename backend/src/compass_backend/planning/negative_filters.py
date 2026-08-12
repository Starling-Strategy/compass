"""Normalize supported negative policy filters into executable metric filters."""

from __future__ import annotations

import re

from compass_backend.contracts.planning import (
    ClarificationRequest,
    FilterSpec,
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
)

_PARENTAL_LEAVE_OFFERED_METRIC = "Does the district offer paid parental leave?"
_NEGATIVE_POLICY_TOKEN_RE = re.compile(r"\b(no|none|without)\b|not\s+offer")


def normalize_turn_negative_policy_filters(
    turn: PlannerTurn,
    *,
    message: str | None = None,
) -> PlannerTurn:
    """Rewrite known negative policy filter shapes into metric-value filters."""

    if turn.route == "clarify" and _can_promote_parental_leave_clarify(
        turn.clarification,
        message,
    ):
        return turn.model_copy(
            update={
                "route": "execute",
                "query_plan": _parental_leave_negative_lookup_plan(
                    message or turn.clarification.question
                ),
                "clarification": None,
            }
        )

    if turn.route != "execute" or turn.query_plan is None:
        return turn
    normalized = normalize_plan_negative_policy_filters(turn.query_plan)
    if normalized == turn.query_plan:
        return turn
    return turn.model_copy(update={"query_plan": normalized})


def normalize_plan_negative_policy_filters(plan: QueryPlan) -> QueryPlan:
    """Return a supported filter plan for governed negative policy requests."""

    if not _is_parental_leave_negative_filter_plan(plan):
        return plan
    metric = MetricSpec(name=_PARENTAL_LEAVE_OFFERED_METRIC)
    return plan.model_copy(
        update={
            "metrics": [metric],
            "filters": [
                FilterSpec(
                    field=_PARENTAL_LEAVE_OFFERED_METRIC,
                    operator="equals",
                    value=False,
                )
            ],
        }
    )


def _parental_leave_negative_lookup_plan(question: str) -> QueryPlan:
    metric = MetricSpec(name=_PARENTAL_LEAVE_OFFERED_METRIC)
    return QueryPlan(
        operation="lookup",
        question=question,
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[metric],
        filters=[
            FilterSpec(
                field=_PARENTAL_LEAVE_OFFERED_METRIC,
                operator="equals",
                value=False,
            )
        ],
        output=OutputSpec(format="table"),
    )


def _can_promote_parental_leave_clarify(
    clarification: ClarificationRequest | None,
    message: str | None,
) -> bool:
    if clarification is None or not _is_negative_parental_leave_text(message):
        return False
    if clarification.is_rescue_fallback:
        return True
    return not clarification.candidates and bool(
        {"metric", "scope", "comparison_group"} & set(clarification.missing_fields)
    )


def _is_parental_leave_negative_filter_plan(plan: QueryPlan) -> bool:
    if plan.operation not in {"lookup", "rank", "count"}:
        return False
    text = " ".join(
        [
            plan.question,
            *(metric.name for metric in plan.metrics),
            *(filter_spec.field for filter_spec in plan.filters),
            *(
                str(filter_spec.value)
                for filter_spec in plan.filters
                if filter_spec.value is not None
            ),
        ]
    ).casefold()
    return _is_negative_parental_leave_text(text)


def _is_negative_parental_leave_text(text: str | None) -> bool:
    if text is None:
        return False
    normalized = text.casefold()
    if "parental leave" not in normalized:
        return False
    return _NEGATIVE_POLICY_TOKEN_RE.search(normalized) is not None
