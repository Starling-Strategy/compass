"""Live planner tests for typed temporal intent.

These tests are the safety evidence for deleting temporal prose fallbacks in
``planning/temporal.py``. Runtime normalization still canonicalizes academic
year strings, expands planner-provided windows, and derives specific/comparison
years from explicit year tokens. It no longer tries to infer relative windows,
year ranges, or trend intent from free text like "past 5 years" or "from ...
through ...". The planner must emit those typed temporal slots.

Run on demand:

    set -a && source .env && set +a && \\
        PYTHONPATH=src uv run pytest \\
        src/compass_backend/tests/test_planner_temporal_live.py \\
        -m llm -v
"""

from __future__ import annotations

import pytest

from compass_backend.agents.model_settings import agent_model_settings
from compass_backend.planning import CachedPlannerAgent
from compass_backend.planning.planner import PlannerDeps
from compass_backend.planning.temporal import normalize_academic_year
from compass_backend.policy_guidance.library import get_library, load_default_library


@pytest.fixture(scope="module")
def _planner_agent() -> CachedPlannerAgent:
    load_default_library()
    return CachedPlannerAgent(
        agent_model_settings.planner_model,
        library_provider=get_library,
    )


@pytest.mark.llm
@pytest.mark.asyncio
async def test_planner_emits_relative_trend_window_temporal_unaided(
    _planner_agent: CachedPlannerAgent,
) -> None:
    prompt = (
        "How have teacher starting salaries changed in Anchorage over the past "
        "5 years?"
    )
    deps = PlannerDeps(message=prompt, current_academic_year="2024 - 2025")

    result = await _planner_agent.run(prompt, deps=deps)
    turn = result.output

    assert turn.route == "execute"
    assert turn.query_plan is not None
    plan = turn.query_plan
    assert plan.operation == "trend"
    assert plan.temporal.intent == "relative_trend_window"
    assert plan.temporal.window_years == 5


@pytest.mark.llm
@pytest.mark.asyncio
async def test_planner_emits_year_range_temporal_unaided(
    _planner_agent: CachedPlannerAgent,
) -> None:
    prompt = "Show Anchorage starting salary from 2018-19 through 2020-21."
    deps = PlannerDeps(message=prompt, current_academic_year="2024 - 2025")

    result = await _planner_agent.run(prompt, deps=deps)
    turn = result.output

    assert turn.route == "execute"
    assert turn.query_plan is not None
    plan = turn.query_plan
    assert plan.operation == "trend"
    assert plan.temporal.intent == "year_range"
    assert normalize_academic_year(plan.temporal.start_academic_year) == "2018 - 2019"
    assert normalize_academic_year(plan.temporal.end_academic_year) == "2020 - 2021"


@pytest.mark.llm
@pytest.mark.asyncio
async def test_planner_emits_comparison_years_temporal_unaided(
    _planner_agent: CachedPlannerAgent,
) -> None:
    prompt = "Compare Anchorage starting salary in 2018-19 vs 2024-25."
    deps = PlannerDeps(message=prompt, current_academic_year="2024 - 2025")

    result = await _planner_agent.run(prompt, deps=deps)
    turn = result.output

    assert turn.route == "execute"
    assert turn.query_plan is not None
    plan = turn.query_plan
    assert plan.operation == "trend"
    assert plan.temporal.intent == "comparison_years"
    years = {
        normalize_academic_year(year)
        for year in plan.temporal.academic_years
    }
    assert {"2018 - 2019", "2024 - 2025"}.issubset(years)
