"""W1-07 (#846): data-bearing direct turns are rerouted to clarify.

The orchestration guard `_is_data_bearing_direct_turn` flags direct-route
responses whose message contains data tokens (dollar amounts, percentages,
≥3-digit numbers, ranking phrasing). When triggered, the orchestration
converts the turn to a typed clarify turn before any later rendering
stage can present the unauthorized claim.
"""

from __future__ import annotations

import pytest

from compass_backend.contracts.planning import (
    ClarificationRequest,
    DirectResponse,
    MetricSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.orchestration.chat import (
    _is_data_bearing_direct_turn,
    _reroute_direct_to_clarify,
)


def _direct_turn(message: str) -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.85,
        direct_response=DirectResponse(message=message, reason="greeting"),
    )


def test_greeting_direct_turn_passes_guard() -> None:
    """A plain greeting / capability question must not be rerouted."""

    turn = _direct_turn("Hi! I can help you explore NCTQ teacher policy data.")
    assert _is_data_bearing_direct_turn(turn) is False


def test_capability_response_passes_guard() -> None:
    turn = _direct_turn(
        "I can answer questions about district pay, leave, evaluations, and more."
    )
    assert _is_data_bearing_direct_turn(turn) is False


def test_data_inventory_direct_turn_can_name_ranking_capability() -> None:
    turn = PlannerTurn(
        route="direct",
        confidence=0.85,
        direct_response=DirectResponse(
            reason="performance pay data inventory",
            message=(
                "Compass has reviewed data for performance pay. You can ask "
                "Compass to rank districts by maximum annual performance pay bonus."
            ),
        ),
    )

    assert _is_data_bearing_direct_turn(turn) is False


@pytest.mark.parametrize(
    "data_bearing_message",
    [
        "Chicago's starting salary is $50,000.",
        "About 45% of teachers receive that benefit.",
        "Enrollment is around 12345 students.",
        "Here are the top 10 districts by salary.",
        "Ranking districts by FRPL share, the largest 5 are...",
        "$120,000",
        "Bottom 3 districts: ...",
    ],
)
def test_data_bearing_direct_turns_are_flagged(data_bearing_message: str) -> None:
    turn = _direct_turn(data_bearing_message)
    assert _is_data_bearing_direct_turn(turn) is True, (
        f"W1-07 guard missed a data-bearing direct response: {data_bearing_message!r}"
    )


def test_reroute_direct_to_clarify_returns_typed_clarify_turn() -> None:
    original = _direct_turn("Chicago's starting salary is $50,000.")
    rerouted = _reroute_direct_to_clarify(original)
    assert rerouted.route == "clarify"
    assert rerouted.clarification is not None
    assert rerouted.direct_response is None
    assert rerouted.confidence <= 0.5
    assert rerouted.clarification.missing_fields == ["metric"]


def test_non_direct_routes_are_not_data_bearing_candidates() -> None:
    """The guard is scoped to route='direct' only — execute / clarify /
    policy_guidance turns have their own contracts."""

    execute_turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank districts by salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
        ),
    )
    assert _is_data_bearing_direct_turn(execute_turn) is False

    clarify_turn = PlannerTurn(
        route="clarify",
        confidence=0.7,
        clarification=ClarificationRequest(
            question="Which lane?",
            missing_fields=["metric"],
            candidates=["BA", "MA"],
        ),
    )
    assert _is_data_bearing_direct_turn(clarify_turn) is False


def test_direct_response_with_metric_token_is_rerouted() -> None:
    """W1-07 acceptance: combining the two helpers proves the end-to-end
    fix — a direct turn with a dollar-amount metric token is converted
    to clarify."""

    original = _direct_turn("Yes, Denver pays $45,000 for first-year teachers.")
    assert _is_data_bearing_direct_turn(original) is True
    rerouted = _reroute_direct_to_clarify(original)
    assert rerouted.route == "clarify"
    assert "governed Compass data" in rerouted.clarification.question
