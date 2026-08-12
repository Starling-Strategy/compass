"""Unit tests for the turn-failure taxonomy classifier (Move 0)."""

from __future__ import annotations

from typing import Any

from dataclasses import dataclass

from compass_backend.contracts.planning import (
    ClarificationRequest,
    DirectResponse,
    MetricSpec,
    PlannerTurn,
    PolicyGuidancePlan,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.contracts.recognition import PlanningRecognitionMention
from compass_backend.turn_taxonomy import (
    TurnTaxonomyTags,
    apply_turn_taxonomy,
    classify_turn,
)


# ─── stub execution outcomes (avoid Pydantic ResultSet/ValidationAuthority
# construction in unit tests; classify_turn duck-types on `.kind`) ──────────


@dataclass
class _StubSuccess:
    kind: str = "success"


@dataclass
class _StubRefusal:
    kind: str = "refusal"


@dataclass
class _StubClarification:
    clarification: ClarificationRequest
    kind: str = "clarification"


def _planner_clarify(
    *,
    is_rescue_fallback: bool = False,
    recognition_blockers: list | None = None,
    missing_fields: list[str] | None = None,
) -> PlannerTurn:
    return PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question="Which district?",
            missing_fields=missing_fields or ["district"],
            is_rescue_fallback=is_rescue_fallback,
        ),
        recognition_blockers=recognition_blockers or [],
    )


def _planner_execute() -> PlannerTurn:
    return PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="lookup",
            question="What is Dallas ISD's salary?",
            selection=SelectionSpec(
                scope="named_districts", districts=["Dallas ISD"]
            ),
            metrics=[MetricSpec(name="starting salary")],
        ),
    )


def _planner_direct() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.9,
        direct_response=DirectResponse(
            message="Hi! I'm Compass.", reason="greeting"
        ),
    )


def _planner_policy_guidance() -> PlannerTurn:
    return PlannerTurn(
        route="policy_guidance",
        confidence=0.9,
        policy_guidance=PolicyGuidancePlan(
            topic_ids=["leave"],
            intent_summary="Leave guidance",
            layers=["stances"],
            primary_topic_id="leave",
        ),
    )


def _exec_success() -> _StubSuccess:
    return _StubSuccess()


def _exec_refusal() -> _StubRefusal:
    return _StubRefusal()


def _exec_clarification(missing_fields: list[str]) -> _StubClarification:
    return _StubClarification(
        clarification=ClarificationRequest(
            question="Which?",
            missing_fields=missing_fields,
        )
    )


# ─── outcome_class branches ──────────────────────────────────────────────────


def test_direct_route_classifies_as_direct() -> None:
    tags = classify_turn(
        turn=_planner_direct(),
        execution_outcome=None,
        result_row_count=None,
    )
    assert tags.outcome_class == "direct"
    assert tags.clarify_source is None


def test_policy_guidance_route_classifies_as_policy_guidance() -> None:
    tags = classify_turn(
        turn=_planner_policy_guidance(),
        execution_outcome=None,
        result_row_count=None,
    )
    assert tags.outcome_class == "policy_guidance"


def test_execute_success_with_rows_classifies_as_executed_ok() -> None:
    tags = classify_turn(
        turn=_planner_execute(),
        execution_outcome=_exec_success(),
        result_row_count=5,
    )
    assert tags.outcome_class == "executed_ok"


def test_execute_success_with_zero_rows_classifies_as_no_results() -> None:
    tags = classify_turn(
        turn=_planner_execute(),
        execution_outcome=_exec_success(),
        result_row_count=0,
    )
    assert tags.outcome_class == "no_results"


def test_execution_refusal_classifies_as_refusal() -> None:
    tags = classify_turn(
        turn=_planner_execute(),
        execution_outcome=_exec_refusal(),
        result_row_count=None,
    )
    assert tags.outcome_class == "refusal"


# ─── clarify_source branches ─────────────────────────────────────────────────


def test_rescue_fallback_clarify_has_rescue_fallback_source() -> None:
    """W1-08 typed marker takes priority over all other source signals."""
    tags = classify_turn(
        turn=_planner_clarify(is_rescue_fallback=True),
        execution_outcome=None,
        result_row_count=None,
    )
    assert tags.outcome_class == "clarify"
    assert tags.clarify_source == "rescue_fallback"


def test_recognition_blockers_field_drives_recognition_blocker_source() -> None:
    """W1-01 typed marker — recognition_blockers on the PlannerTurn."""
    blocker = PlanningRecognitionMention(
        phrase="Denver",
        entity_type="district",
        status="ambiguous",
        recommended_action="clarify",
    )
    tags = classify_turn(
        turn=_planner_clarify(recognition_blockers=[blocker]),
        execution_outcome=None,
        result_row_count=None,
    )
    assert tags.clarify_source == "recognition_blocker"


def test_execute_route_with_execution_clarification_outcome() -> None:
    """ExecutionClarification — turn stays route=execute when classifier sees
    the raw outcome; orchestration's later turn-swap doesn't affect us."""
    outcome = _exec_clarification(missing_fields=["metric"])
    tags = classify_turn(
        turn=_planner_execute(),
        execution_outcome=outcome,
        result_row_count=None,
    )
    assert tags.outcome_class == "clarify"
    assert tags.clarify_source == "execution_clarification"
    assert tags.missing_slots == ("metric",)


def test_planner_route_clarify_default_source() -> None:
    """Plain planner clarify (no rescue, no blockers, no exec outcome)."""
    tags = classify_turn(
        turn=_planner_clarify(),
        execution_outcome=None,
        result_row_count=None,
    )
    assert tags.outcome_class == "clarify"
    assert tags.clarify_source == "planner_route"
    assert tags.missing_slots == ("district",)


# ─── entity resolution counts ────────────────────────────────────────────────


def test_entity_resolution_counts_are_always_zero() -> None:
    """The recognition-report counts are dead: the pipeline never populated
    a report, so classify_turn pins every entity-resolution count to zero."""
    tags = classify_turn(
        turn=_planner_direct(),
        execution_outcome=None,
        result_row_count=None,
    )
    assert tags.entity_resolution_total_mentions == 0
    assert tags.entity_resolution_resolved_count == 0
    assert tags.entity_resolution_ambiguous_count == 0
    assert tags.entity_resolution_unresolved_count == 0
    assert tags.entity_resolution_unsupported_count == 0


# ─── apply_turn_taxonomy ─────────────────────────────────────────────────────


class _RecordingSpan:
    """Stand-in for a Logfire span; records set_attribute calls."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def test_apply_turn_taxonomy_writes_flat_attributes() -> None:
    span = _RecordingSpan()
    tags = TurnTaxonomyTags(
        outcome_class="clarify",
        clarify_source="rescue_fallback",
        missing_slots=("district", "metric"),
        entity_resolution_resolved_count=1,
        entity_resolution_ambiguous_count=2,
        entity_resolution_total_mentions=3,
    )
    apply_turn_taxonomy(span, tags)
    assert span.attributes["taxonomy_outcome_class"] == "clarify"
    assert span.attributes["taxonomy_clarify_source"] == "rescue_fallback"
    assert span.attributes["taxonomy_missing_slots"] == "district,metric"
    assert span.attributes["taxonomy_entity_resolution_resolved_count"] == 1
    assert span.attributes["taxonomy_entity_resolution_ambiguous_count"] == 2
    assert span.attributes["taxonomy_entity_resolution_total_mentions"] == 3


def test_apply_turn_taxonomy_omits_clarify_source_when_none() -> None:
    span = _RecordingSpan()
    tags = TurnTaxonomyTags(outcome_class="executed_ok")
    apply_turn_taxonomy(span, tags)
    assert "taxonomy_clarify_source" not in span.attributes
    assert span.attributes["taxonomy_outcome_class"] == "executed_ok"
