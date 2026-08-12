"""Tests for the cross-state-comparison anchor-preservation guard (#1511 / #1069).

The planner-output validator must reject a fresh ``named_districts`` follow-up
that restricts to comparison state(s) which do not contain the prior anchor
while omitting the anchor from ``selection.districts`` — the exact anchor-drop
that made Compass falsely claim Burlington School District VT was uncovered on
"how does that compare to Vermont?" after a Dallas ISD turn.

These are pure validator unit tests — no DB, no LLM. The guard reads the prior
anchor's resolved state from typed ``result_districts`` and the current plan's
typed selection/filters; it never re-reads user prose.
"""

from __future__ import annotations

import pytest
from pydantic_ai import ModelRetry

from compass_backend.contracts import (
    FilterSpec,
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    QueryContext,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.contracts.session import ConversationMemory
from compass_backend.planning.planner import (
    PlannerDeps,
    validate_planner_turn_quality,
)


def _dallas_context() -> QueryContext:
    """Prior turn established Dallas ISD (TX) as the anchor."""
    return QueryContext(
        query_plan=QueryPlan(
            question="Annual base salary for a first year teacher with a bachelor's degree",
            operation="lookup",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Dallas Independent School District"],
            ),
            metrics=[MetricSpec(name="annual base salary")],
        ),
        result_type="metric_lookup",
        result_districts=[
            {
                "district_id": 100,
                "district_name": "Dallas Independent School District",
                "state": "TX",
            }
        ],
    )


def _deps_with_dallas() -> PlannerDeps:
    return PlannerDeps(
        message="how does that compare to vermont?",
        memory=ConversationMemory(latest_query_context=_dallas_context()),
    )


def _turn(plan: QueryPlan) -> PlannerTurn:
    return PlannerTurn(route="execute", confidence=0.9, query_plan=plan)


def test_anchor_drop_to_comparison_state_is_rejected() -> None:
    """named_districts restricted to VT, anchor omitted → ModelRetry (#1511).

    This is the failing shape: the planner emits a fresh named_districts plan
    naming only a Vermont district (or the anchor) but with a VT state filter
    that excludes Dallas (TX), so the anchor silently vanishes.
    """
    plan = QueryPlan(
        question="how does that compare to vermont?",
        operation="lookup",
        inherit_from_session=False,
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Burlington School District"],
            states=["VT"],
        ),
        metrics=[MetricSpec(name="annual base salary")],
        output=OutputSpec(format="table"),
    )
    with pytest.raises(ModelRetry, match="anchor"):
        validate_planner_turn_quality(_turn(plan), _deps_with_dallas())


def test_anchor_drop_via_state_filter_is_rejected() -> None:
    """A kind='state' FilterSpec restricting to VT, anchor omitted → ModelRetry."""
    plan = QueryPlan(
        question="how does that compare to vermont?",
        operation="lookup",
        inherit_from_session=False,
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Burlington School District"],
        ),
        filters=[
            FilterSpec(field="state", operator="equals", value="VT", kind="state")
        ],
        metrics=[MetricSpec(name="annual base salary")],
        output=OutputSpec(format="table"),
    )
    with pytest.raises(ModelRetry, match="anchor"):
        validate_planner_turn_quality(_turn(plan), _deps_with_dallas())


def test_anchor_named_with_comparison_passes() -> None:
    """The correct shape — anchor + comparison named, no excluding state filter."""
    plan = QueryPlan(
        question="how does that compare to vermont?",
        operation="lookup",
        inherit_from_session=False,
        selection=SelectionSpec(
            scope="named_districts",
            districts=[
                "Dallas Independent School District",
                "Burlington School District",
            ],
        ),
        metrics=[MetricSpec(name="annual base salary")],
        output=OutputSpec(format="table"),
    )
    turn = _turn(plan)
    assert validate_planner_turn_quality(turn, _deps_with_dallas()) is turn


def test_state_filter_that_includes_anchor_passes() -> None:
    """A state filter that still contains the anchor's state is not an anchor-drop."""
    plan = QueryPlan(
        question="compare to other texas districts",
        operation="lookup",
        inherit_from_session=False,
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Houston Independent School District"],
            states=["TX"],
        ),
        metrics=[MetricSpec(name="annual base salary")],
        output=OutputSpec(format="table"),
    )
    turn = _turn(plan)
    assert validate_planner_turn_quality(turn, _deps_with_dallas()) is turn


def test_no_prior_anchor_is_not_rejected() -> None:
    """A fresh first-turn named_districts plan with a state filter is untouched."""
    plan = QueryPlan(
        question="show burlington vermont",
        operation="lookup",
        inherit_from_session=False,
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Burlington School District"],
            states=["VT"],
        ),
        metrics=[MetricSpec(name="annual base salary")],
        output=OutputSpec(format="table"),
    )
    deps = PlannerDeps(message="show burlington vermont")
    turn = _turn(plan)
    assert validate_planner_turn_quality(turn, deps) is turn


def test_no_state_filter_is_not_rejected() -> None:
    """Without a restricting state filter there is no silent anchor exclusion."""
    plan = QueryPlan(
        question="how does that compare to burlington?",
        operation="lookup",
        inherit_from_session=False,
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Burlington School District"],
        ),
        metrics=[MetricSpec(name="annual base salary")],
        output=OutputSpec(format="table"),
    )
    turn = _turn(plan)
    assert validate_planner_turn_quality(turn, _deps_with_dallas()) is turn
