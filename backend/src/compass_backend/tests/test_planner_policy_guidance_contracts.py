"""Tests for the policy_guidance planner contract extension."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.contracts.planning import (
    ClarificationRequest,
    DirectResponse,
    PlannerTurn,
    PolicyGuidancePlan,
    QueryPlan,
    SelectionSpec,
)


# ---------------------------------------------------------------------------
# PolicyGuidancePlan — base contract (Literal-bound subclasses tested separately
# via the planner factory in test_planner_policy_guidance_route)


def _plan(**overrides: object) -> PolicyGuidancePlan:
    base: dict[str, object] = {
        "topic_ids": ["general-salary"],
        "layers": ["exemplars"],
        "intent_summary": "User asked for an exemplary salary policy district.",
    }
    base.update(overrides)
    return PolicyGuidancePlan(**base)  # type: ignore[arg-type]


def test_plan_round_trip() -> None:
    plan = _plan()
    assert plan.topic_ids == ["general-salary"]
    assert plan.layers == ["exemplars"]
    assert plan.focus_terms == []
    assert plan.primary_topic_id is None


def test_plan_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PolicyGuidancePlan(  # type: ignore[call-arg]
            topic_ids=["general-salary"],
            layers=["exemplars"],
            intent_summary="ok",
            unexpected="bad",
        )


def test_plan_requires_at_least_one_topic() -> None:
    with pytest.raises(ValidationError):
        _plan(topic_ids=[])


def test_plan_caps_topics_at_four() -> None:
    """Soft cap — single user prompts rarely span more than 2-3 NCTQ topics."""
    with pytest.raises(ValidationError):
        _plan(topic_ids=["a", "b", "c", "d", "e"])


def test_plan_requires_at_least_one_layer() -> None:
    with pytest.raises(ValidationError):
        _plan(layers=[])


def test_plan_layers_must_be_known_kinds() -> None:
    with pytest.raises(ValidationError):
        _plan(layers=["unknown_layer"])


def test_plan_intent_summary_required_non_empty() -> None:
    with pytest.raises(ValidationError):
        _plan(intent_summary="")


def test_plan_primary_topic_must_be_in_topic_ids() -> None:
    with pytest.raises(ValidationError, match="primary_topic_id"):
        _plan(
            topic_ids=["general-salary"],
            primary_topic_id="leave",
        )


def test_plan_primary_topic_in_topics_ok() -> None:
    plan = _plan(
        topic_ids=["general-salary", "leave"],
        primary_topic_id="leave",
    )
    assert plan.primary_topic_id == "leave"


def test_plan_multi_topic_multi_layer() -> None:
    plan = _plan(
        topic_ids=["general-salary", "benefits"],
        layers=["stances", "rationales", "exemplars"],
    )
    assert len(plan.topic_ids) == 2
    assert len(plan.layers) == 3


def test_plan_focus_terms_are_normalized_and_deduped() -> None:
    plan = _plan(focus_terms=[" Parental Leave ", "parental leave", "Performance Pay"])
    assert plan.focus_terms == ["parental leave", "performance pay"]


def test_plan_caps_focus_terms_at_six() -> None:
    with pytest.raises(ValidationError):
        _plan(focus_terms=["a", "b", "c", "d", "e", "f", "g"])


# ---------------------------------------------------------------------------
# PlannerTurn — extended require_matching_route_payload validator


def test_planner_turn_policy_guidance_route_ok() -> None:
    turn = PlannerTurn(
        route="policy_guidance",
        confidence=0.9,
        policy_guidance=_plan(),
    )
    assert turn.route == "policy_guidance"


def test_planner_turn_policy_guidance_route_requires_payload() -> None:
    with pytest.raises(ValidationError, match="policy_guidance"):
        PlannerTurn(
            route="policy_guidance",
            confidence=0.9,
            policy_guidance=None,
        )


def test_planner_turn_policy_guidance_route_rejects_extra_payloads() -> None:
    """Route discrimination must stay strict — exactly one payload per route."""
    with pytest.raises(ValidationError, match="unexpected payloads"):
        PlannerTurn(
            route="policy_guidance",
            confidence=0.9,
            policy_guidance=_plan(),
            direct_response=DirectResponse(message="hi", reason="greeting"),
        )


def test_planner_turn_existing_routes_reject_policy_guidance_payload() -> None:
    """The reverse — direct/clarify/execute routes can't carry policy_guidance."""
    with pytest.raises(ValidationError, match="unexpected payloads"):
        PlannerTurn(
            route="direct",
            confidence=0.9,
            direct_response=DirectResponse(message="hi", reason="greeting"),
            policy_guidance=_plan(),
        )


def test_planner_turn_route_literal_includes_policy_guidance() -> None:
    """Catch typos in PlannerRoute Literal at type-construction time."""
    from compass_backend.contracts.planning import PlannerRoute
    from typing import get_args

    assert "policy_guidance" in get_args(PlannerRoute)
    # And the existing routes are still there (backwards compat)
    assert {"direct", "clarify", "execute"}.issubset(set(get_args(PlannerRoute)))


# ---------------------------------------------------------------------------
# Round-trip checks — make sure existing routes still build cleanly


def test_existing_direct_route_still_builds() -> None:
    turn = PlannerTurn(
        route="direct",
        confidence=1.0,
        direct_response=DirectResponse(message="Hi!", reason="greeting"),
    )
    assert turn.route == "direct"


def test_existing_execute_route_still_builds() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            question="What is starting salary in Dallas ISD?",
            selection=SelectionSpec(scope="named_districts", districts=["Dallas ISD"]),
            metrics=[{"name": "starting salary", "role": "primary"}],  # type: ignore[arg-type]
        ),
    )
    assert turn.route == "execute"


def test_existing_clarify_route_still_builds() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question="Which district?",
            missing_fields=["district"],
        ),
    )
    assert turn.route == "clarify"
