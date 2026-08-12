"""Integration tests for the policy_guidance planner route.

Verifies:
- make_bound_planner_turn produces a subclass with a Literal-enum schema
  for topic_ids and primary_topic_id
- Direct construction with valid topic_ids works; invalid topic_ids raise
- TestModel-driven planner runs return PlannerTurn with a valid
  PolicyGuidancePlan when policy_guidance is the chosen route
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from compass_backend.planning.planner import (
    PLANNER_INSTRUCTIONS,
    create_planner_agent,
    make_bound_planner_turn,
)
from compass_backend.policy_guidance import (
    PolicyGuidanceLibrary,
    Stance,
    TopicGuidance,
)


def _toy_library() -> PolicyGuidanceLibrary:
    """Two-topic library: salary + leave. Enough to exercise multi-topic shapes."""
    salary_stance = Stance(
        stance_id="stance:general-salary-frontloaded",
        topic_id="general-salary",
        title="Frontloaded Schedules",
        body="Frontload teacher pay.",
    )
    leave_stance = Stance(
        stance_id="stance:leave-paid-parental",
        topic_id="leave",
        title="Paid Parental",
        body="Provide parental leave.",
    )
    return PolicyGuidanceLibrary.build(
        topics={
            "general-salary": TopicGuidance(
                topic_id="general-salary",
                canonical_topic="General Salary",
                aliases=("salary", "starting salary"),
                canonical_url=None,
                topic_brief="Salary brief.",
                stances=(salary_stance,),
                rationales=(),
                exemplars=(),
            ),
            "leave": TopicGuidance(
                topic_id="leave",
                canonical_topic="Leave",
                aliases=("leave", "parental leave"),
                canonical_url=None,
                topic_brief="Leave brief.",
                stances=(leave_stance,),
                rationales=(),
                exemplars=(),
            ),
        }
    )


# ---------------------------------------------------------------------------
# make_bound_planner_turn — schema-level checks


def test_bound_class_topic_ids_field_is_literal_enum() -> None:
    """The whole point of binding: JSON schema includes an enum constraint
    so the LLM can't emit unknown topic_ids at generation time."""
    library = _toy_library()
    Bound = make_bound_planner_turn(library)
    schema = Bound.model_json_schema()
    plan_schema = schema["$defs"]["BoundPolicyGuidancePlan"]
    topic_field = plan_schema["properties"]["topic_ids"]
    assert topic_field["type"] == "array"
    assert sorted(topic_field["items"]["enum"]) == ["general-salary", "leave"]


def test_bound_class_primary_topic_id_field_is_literal_enum() -> None:
    library = _toy_library()
    Bound = make_bound_planner_turn(library)
    schema = Bound.model_json_schema()
    plan_schema = schema["$defs"]["BoundPolicyGuidancePlan"]
    primary_schema = plan_schema["properties"]["primary_topic_id"]
    enum_branch = next(
        branch for branch in primary_schema["anyOf"] if "enum" in branch
    )
    assert sorted(enum_branch["enum"]) == ["general-salary", "leave"]


def test_bound_class_construction_accepts_valid_topic() -> None:
    library = _toy_library()
    Bound = make_bound_planner_turn(library)
    turn = Bound(
        route="policy_guidance",
        confidence=0.9,
        policy_guidance={
            "topic_ids": ["general-salary"],
            "layers": ["exemplars"],
            "intent_summary": "User wants salary exemplar.",
        },
    )
    assert turn.policy_guidance.topic_ids == ["general-salary"]


def test_bound_class_construction_rejects_unknown_topic() -> None:
    library = _toy_library()
    Bound = make_bound_planner_turn(library)
    with pytest.raises(ValidationError):
        Bound(
            route="policy_guidance",
            confidence=0.9,
            policy_guidance={
                "topic_ids": ["never-heard-of-this"],
                "layers": ["exemplars"],
                "intent_summary": "x",
            },
        )


def test_bound_class_rejects_unknown_primary_topic() -> None:
    library = _toy_library()
    Bound = make_bound_planner_turn(library)
    with pytest.raises(ValidationError):
        Bound(
            route="policy_guidance",
            confidence=0.9,
            policy_guidance={
                "topic_ids": ["general-salary"],
                "layers": ["exemplars"],
                "intent_summary": "x",
                "primary_topic_id": "evaluation",  # not in library
            },
        )


def test_make_bound_planner_turn_rejects_empty_library() -> None:
    """A library with zero topics can't form a Literal[*] enum (Literal[]
    is invalid). Better to fail explicitly at agent build than mysteriously
    later."""
    empty = PolicyGuidanceLibrary.build(topics={})
    with pytest.raises(ValueError, match="no topics"):
        make_bound_planner_turn(empty)


def test_unbound_planner_falls_back_to_str_topic_ids() -> None:
    """Without a library, create_planner_agent uses the unbound contract.
    Useful for early-bootstrap tests where wiring the library would be
    circular. Behavior at agent construction; no JSON schema check needed."""
    agent = create_planner_agent("test")
    assert agent is not None  # builds without raising


# ---------------------------------------------------------------------------
# Planner instructions — sanity check that the policy_guidance section is in


def test_planner_instructions_describe_policy_guidance_route() -> None:
    """The LLM needs explicit routing rules for the new route."""
    text = PLANNER_INSTRUCTIONS
    assert 'route="policy_guidance"' in text
    assert "stances" in text
    assert "rationales" in text
    assert "exemplars" in text
    assert "focus_terms" in text
    assert 'focus_terms=["parental leave"]' in text
    assert 'focus_terms=["performance pay"]' in text
    assert "Great health coverage" in text
    assert "exemplary health coverage" in text
    assert 'topic_ids=["benefits"]' in text
    assert 'focus_terms=["health benefits"]' in text
    assert "After a `policy_guidance` response" in text
    assert "details for the top one" in text
    assert "performance-pay metric" in text
    assert "percentage health-premium" in text
    # Layer-selection rules from llms.txt are present
    assert '"a district that does' in text


def test_planner_instructions_treat_comparable_districts_as_peer_comparison() -> None:
    text = PLANNER_INSTRUCTIONS
    normalized = " ".join(text.split())
    assert "peer_comparison" in text
    assert "peer language" in text
    assert "anchor in `selection.districts`" in normalized


# ---------------------------------------------------------------------------
# TestModel-driven planner agent runs (no real LLM)


@pytest.mark.asyncio
async def test_test_model_run_returns_policy_guidance_turn() -> None:
    """Stubbed-LLM run returns a PolicyGuidancePlan that round-trips through
    the bound output_type. This is the closest-to-runtime contract test we
    can do without spending tokens."""
    library = _toy_library()
    Bound = make_bound_planner_turn(library)

    stub = TestModel(
        custom_output_args={
            "route": "policy_guidance",
            "confidence": 0.9,
            "policy_guidance": {
                "topic_ids": ["general-salary"],
                "layers": ["exemplars"],
                "intent_summary": "User asked for an exemplary salary district.",
            },
        }
    )
    agent = Agent(stub, output_type=Bound, instructions=PLANNER_INSTRUCTIONS)
    result = await agent.run("Show me a district with great salary policy.")
    assert result.output.route == "policy_guidance"
    plan = result.output.policy_guidance
    assert plan is not None
    assert plan.topic_ids == ["general-salary"]
    assert plan.layers == ["exemplars"]


@pytest.mark.asyncio
async def test_test_model_multi_topic_multi_layer_run() -> None:
    library = _toy_library()
    Bound = make_bound_planner_turn(library)

    stub = TestModel(
        custom_output_args={
            "route": "policy_guidance",
            "confidence": 0.85,
            "policy_guidance": {
                "topic_ids": ["general-salary", "leave"],
                "layers": ["stances", "rationales", "exemplars"],
                "intent_summary": "Full picture across two topics.",
                "primary_topic_id": "general-salary",
            },
        }
    )
    agent = Agent(stub, output_type=Bound, instructions=PLANNER_INSTRUCTIONS)
    result = await agent.run("Tell me about salary and leave policies.")
    plan = result.output.policy_guidance
    assert plan is not None
    assert plan.topic_ids == ["general-salary", "leave"]
    assert plan.primary_topic_id == "general-salary"
    assert set(plan.layers) == {"stances", "rationales", "exemplars"}


@pytest.mark.asyncio
async def test_test_model_unknown_topic_at_runtime_rejected() -> None:
    """If TestModel emits a topic outside the library's enum, the bound
    output_type's validator rejects the run. This proves the Literal[*]
    constraint is actually load-bearing — not just documentation."""
    library = _toy_library()
    Bound = make_bound_planner_turn(library)

    stub = TestModel(
        custom_output_args={
            "route": "policy_guidance",
            "confidence": 0.9,
            "policy_guidance": {
                "topic_ids": ["unknown-topic"],
                "layers": ["exemplars"],
                "intent_summary": "x",
            },
        }
    )
    agent = Agent(stub, output_type=Bound, instructions=PLANNER_INSTRUCTIONS)
    with pytest.raises(Exception):  # Pydantic-AI may wrap; just confirm it raises
        await agent.run("any prompt")


@pytest.mark.asyncio
async def test_existing_clarify_route_still_round_trips_through_bound() -> None:
    """Sanity check: extending PlannerTurn with policy_guidance didn't break
    the existing routes' validator path."""
    library = _toy_library()
    Bound = make_bound_planner_turn(library)

    stub = TestModel(
        custom_output_args={
            "route": "clarify",
            "confidence": 0.4,
            "clarification": {
                "question": "Which district group?",
                "missing_fields": ["district"],
            },
        }
    )
    agent = Agent(stub, output_type=Bound, instructions=PLANNER_INSTRUCTIONS)
    result = await agent.run("hello")
    assert result.output.route == "clarify"
    assert result.output.policy_guidance is None
