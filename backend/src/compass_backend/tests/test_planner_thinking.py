"""Planner thinking policy regressions for SSN-271."""

from __future__ import annotations

import anyio
import pytest
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.exceptions import ModelHTTPError

from compass_backend.agents import AgentProfile, model_settings_for_profile
from compass_backend.contracts import (
    ConversationMemory,
    DirectResponse,
    MetricSpec,
    PlannerTurn,
    QueryContext,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.planning import PlannerDeps, run_planner
from compass_backend.planning.thinking import decide_planner_thinking


class RecordingPlannerAgent:
    """Fake planner that records per-run model settings."""

    def __init__(self, turn: PlannerTurn) -> None:
        self.turn = turn
        self.kwargs: list[dict[str, object]] = []

    async def run(self, user_prompt: str, **kwargs):
        self.kwargs.append(kwargs)
        return AgentRunResult(output=self.turn)


class UnsupportedThinkingPlannerAgent(RecordingPlannerAgent):
    """Fake planner that rejects thinking once, then succeeds."""

    async def run(self, user_prompt: str, **kwargs):
        self.kwargs.append(kwargs)
        if len(self.kwargs) == 1:
            raise ModelHTTPError(
                400,
                "test-model",
                {"error": {"message": "unsupported parameter: thinking"}},
            )
        return AgentRunResult(output=self.turn)


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=1.0,
        direct_response=DirectResponse(message="ok", reason="test"),
    )


def _query_context() -> QueryContext:
    return QueryContext(
        query_plan=QueryPlan(
            question="Compare starting salaries.",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Chicago Public Schools", "Denver Public Schools"],
            ),
            metrics=[MetricSpec(name="starting salary")],
        ),
        result_type="metric_lookup",
        row_count=2,
    )


def test_selected_policy_keeps_simple_lookup_off() -> None:
    decision = decide_planner_thinking(
        PlannerDeps(message="What is starting salary for Dallas ISD?"),
        policy="selected",
        max_effort="high",
    )

    assert decision.enabled is False
    assert decision.effort is None
    assert decision.reason == "simple_or_unsupported_shape"


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (
            "Of the 10 largest districts by enrollment, rank salaries highest to lowest.",
            "multi_step_selection_sort",
        ),
        (
            "How many districts actually watch teachers teach more than a couple times a year?",
            "count_filter_denominator",
        ),
        (
            "Show salary trends over time for Denver.",
            "historical_longitudinal",
        ),
        (
            "What peer districts are comparable to Denver by NCES profile?",
            "peer_nces_profile",
        ),
        (
            "What does the research say about teacher leave policy?",
            "research_publication_bundle",
        ),
    ],
)
def test_selected_policy_enables_complex_shapes(message: str, reason: str) -> None:
    decision = decide_planner_thinking(
        PlannerDeps(message=message),
        policy="selected",
        max_effort="medium",
    )

    assert decision.enabled is True
    assert decision.effort == "medium"
    assert decision.reason == reason


def test_selected_policy_enables_followup_context() -> None:
    decision = decide_planner_thinking(
        PlannerDeps(
            message="Sort those highest first.",
            memory=ConversationMemory(latest_query_context=_query_context()),
        ),
        policy="selected",
        max_effort="low",
    )

    assert decision.enabled is True
    assert decision.effort == "low"
    assert decision.reason == "followup_reference"


def test_selected_policy_enables_target_scenario_ids() -> None:
    decision = decide_planner_thinking(
        PlannerDeps(
            message="Which districts have the highest starting salaries?",
            scenario_id="3",
        ),
        policy="selected",
        max_effort="medium",
    )

    assert decision.enabled is True
    assert decision.reason == "target_scenario"


def test_run_planner_overlays_thinking_without_mutating_base_settings() -> None:
    agent = RecordingPlannerAgent(_direct_turn())
    before = model_settings_for_profile(AgentProfile.PLANNER)

    async def run_it():
        return await run_planner(
            "Sort those highest first.",
            model="test-model",
            agent=agent,
            context=_query_context(),
            planner_thinking_policy="selected",
            planner_thinking_max_effort="high",
        )

    run = anyio.run(run_it)

    settings = agent.kwargs[0]["model_settings"]
    assert settings["thinking"] == "high"
    assert settings["temperature"] == 1.0
    assert model_settings_for_profile(AgentProfile.PLANNER) == before
    assert run.evidence is not None
    assert run.evidence.thinking_enabled is True
    assert run.evidence.thinking_effort == "high"
    assert run.evidence.thinking_policy == "selected"
    assert run.evidence.thinking_policy_reason == "followup_reference"
    assert run.evidence.planner_duration_ms is not None


def test_run_planner_retries_without_thinking_for_unsupported_provider() -> None:
    agent = UnsupportedThinkingPlannerAgent(_direct_turn())

    async def run_it():
        return await run_planner(
            "Sort those highest first.",
            model="test-model",
            agent=agent,
            context=_query_context(),
            planner_thinking_policy="all",
            planner_thinking_max_effort="medium",
        )

    run = anyio.run(run_it)

    assert len(agent.kwargs) == 2
    assert agent.kwargs[0]["model_settings"]["thinking"] == "medium"
    assert agent.kwargs[0]["model_settings"]["temperature"] == 1.0
    assert "thinking" not in agent.kwargs[1]["model_settings"]
    assert agent.kwargs[1]["model_settings"]["temperature"] == 0.0
    assert run.evidence is not None
    assert run.evidence.thinking_enabled is True
    assert run.evidence.thinking_provider_error is not None
    assert "unsupported parameter" in run.evidence.thinking_provider_error


def test_thinking_fallback_catches_anthropic_strict_grammar_limit() -> None:
    agent = UnsupportedThinkingPlannerAgent(_direct_turn())

    async def fail_with_grammar_limit(user_prompt: str, **kwargs):
        agent.kwargs.append(kwargs)
        if len(agent.kwargs) == 1:
            raise ModelHTTPError(
                400,
                "test-model",
                {
                    "error": {
                        "message": (
                            "The compiled grammar is too large, which would cause "
                            "performance issues. Simplify your tool schemas or "
                            "reduce the number of strict tools."
                        )
                    }
                },
            )
        return AgentRunResult(output=agent.turn)

    agent.run = fail_with_grammar_limit

    async def run_it():
        return await run_planner(
            "Of the 10 largest districts by enrollment, rank salaries highest to lowest.",
            model="test-model",
            agent=agent,
            planner_thinking_policy="selected",
            planner_thinking_max_effort="medium",
        )

    run = anyio.run(run_it)

    assert len(agent.kwargs) == 2
    assert agent.kwargs[0]["model_settings"]["thinking"] == "medium"
    assert "thinking" not in agent.kwargs[1]["model_settings"]
    assert agent.kwargs[1]["model_settings"]["temperature"] == 0.0
    assert run.evidence is not None
    assert "compiled grammar is too large" in run.evidence.thinking_provider_error
