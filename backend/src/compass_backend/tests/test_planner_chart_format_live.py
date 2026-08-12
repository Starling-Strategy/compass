"""Live planner test for chart/graph format prompts.

Replaces the contract previously enforced by
`execution/_helpers.py:_chart_request_should_execute_as_lookup` — a
post-planner overlay that rewrote `operation="rank"` to `operation="lookup"`
when the question contained "chart"/"graph" but no ranking words. Live
replay (.context/bucket-a-evidence/bucket2-planner.json) confirmed the
planner already emits the right operation unaided per PLANNER_INSTRUCTIONS
lines 164-170: `lookup` for neutral chart prompts (no ranking words),
`rank` for chart prompts with explicit ordering language.

This test runs against the real planner LLM. Marked `@pytest.mark.llm` so
it is excluded from the default CI run (`-m "not slow and not llm"`).
Run on demand:

    set -a && source .env && set +a && \\
        PYTHONPATH=src uv run pytest \\
        src/compass_backend/tests/test_planner_chart_format_live.py \\
        -m llm -v
"""

from __future__ import annotations

import pytest

from compass_backend.agents.model_settings import agent_model_settings
from compass_backend.planning import CachedPlannerAgent
from compass_backend.planning.planner import PlannerDeps
from compass_backend.policy_guidance.library import get_library, load_default_library


# Prompts with chart/graph wording AND no ranking words → planner should
# emit operation="lookup" + output.format="chart".
NEUTRAL_CHART_PROMPTS = [
    "Show me a chart of starting salaries for teachers with a bachelor's "
    + "degree across all districts in your database.",
    "Graph teacher starting pay across all covered districts.",
    "Chart maximum annual paid sick days for all districts.",
    "Show me a graph of performance pay bonuses for every district.",
]

# Prompts with chart/graph wording AND explicit ranking → planner should
# emit operation="rank" + output.format="chart" (the override never
# rewrote these — confirmed by the prose check in the deleted helper).
RANKED_CHART_PROMPTS = [
    "Chart the top 10 districts by starting salary.",
    "Show me a graph ranking districts by maximum paid sick days, highest first.",
    "Graph the lowest performance pay bonuses across all covered districts.",
]

# #1240 (the load-bearing negative direction): plain prompts with NO
# visualization word must NOT yield output.format=="chart". The chart-visibility
# gate is exactly `plan.output.format == "chart"`; a false positive here (chart
# when the user didn't ask) renders an unwanted chart, defeating the gate. The
# first is a plain "best-paying" ranking; the second is case-1053's literal
# "keep the table and citations aligned" repro; the third is a cross-district
# comparison. We assert only the format gate and the execute route — not the
# operation, which legitimately varies by prompt shape (rank vs lookup vs
# compare) and is not what the gate reads.
PLAIN_NON_CHART_PROMPTS = [
    "What are the 10 best-paying districts for new teachers?",
    "Show districts with the highest FRPL share and keep the table and "
    + "citations aligned.",
    "How do starting salaries compare across Boston, Atlanta, and Houston?",
]


@pytest.fixture(scope="module")
def _planner_agent() -> CachedPlannerAgent:
    load_default_library()
    return CachedPlannerAgent(
        agent_model_settings.planner_model,
        library_provider=get_library,
    )


@pytest.mark.llm
@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", NEUTRAL_CHART_PROMPTS)
async def test_planner_emits_lookup_for_neutral_chart_prompts(
    _planner_agent: CachedPlannerAgent,
    prompt: str,
) -> None:
    """For chart/graph prompts without ranking words, the planner alone must
    emit operation='lookup' with output.format='chart'. No post-planner
    overlay should be needed to translate rank→lookup."""

    deps = PlannerDeps(message=prompt, current_academic_year="2024 - 2025")
    result = await _planner_agent.run(prompt, deps=deps)
    turn = result.output

    assert turn.route == "execute", (
        f"planner routed {turn.route!r}, expected 'execute' for: {prompt!r}"
    )
    assert turn.query_plan is not None
    plan = turn.query_plan
    assert plan.operation == "lookup", (
        f"operation={plan.operation!r}, expected 'lookup' for: {prompt!r}"
    )
    assert plan.output.format == "chart", (
        f"output.format={plan.output.format!r}, expected 'chart' for: {prompt!r}"
    )


@pytest.mark.llm
@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", RANKED_CHART_PROMPTS)
async def test_planner_emits_rank_for_chart_prompts_with_ranking_words(
    _planner_agent: CachedPlannerAgent,
    prompt: str,
) -> None:
    """For chart/graph prompts WITH explicit ranking language (top, ranking,
    highest first, lowest, etc.), the planner alone must emit operation='rank'.
    Confirms the contrast case the deleted overlay was supposedly preserving."""

    deps = PlannerDeps(message=prompt, current_academic_year="2024 - 2025")
    result = await _planner_agent.run(prompt, deps=deps)
    turn = result.output

    assert turn.route == "execute", (
        f"planner routed {turn.route!r}, expected 'execute' for: {prompt!r}"
    )
    assert turn.query_plan is not None
    plan = turn.query_plan
    assert plan.operation == "rank", (
        f"operation={plan.operation!r}, expected 'rank' for: {prompt!r}"
    )
    assert plan.output.format == "chart", (
        f"output.format={plan.output.format!r}, expected 'chart' for: {prompt!r}"
    )


@pytest.mark.llm
@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", PLAIN_NON_CHART_PROMPTS)
async def test_planner_does_not_request_chart_for_plain_prompts(
    _planner_agent: CachedPlannerAgent,
    prompt: str,
) -> None:
    """The false-positive direction the chart-visibility gate (#1240) depends
    on: a plain ranking/lookup/comparison prompt with NO visualization word
    must NOT set output.format='chart'. If the planner over-eagerly labels
    these as charts, the gate would render unwanted charts. We pin only the
    format gate and the execute route, not the operation."""

    deps = PlannerDeps(message=prompt, current_academic_year="2024 - 2025")
    result = await _planner_agent.run(prompt, deps=deps)
    turn = result.output

    assert turn.route == "execute", (
        f"planner routed {turn.route!r}, expected 'execute' for: {prompt!r}"
    )
    assert turn.query_plan is not None
    plan = turn.query_plan
    assert plan.output.format != "chart", (
        f"output.format={plan.output.format!r}, expected NOT 'chart' (no "
        f"visualization word) for: {prompt!r}"
    )
