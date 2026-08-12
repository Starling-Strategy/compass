"""Live planner test for ordered metric-count prompts.

Replaces `test_run_planner_promotes_ordered_metric_count_prompt_to_ranking`,
which pinned the contract of the deleted `_promote_ordered_metric_count_to_rank`
overlay. The overlay's job was post-hoc flipping `operation="count"` →
`operation="rank"` for ordered metric-count prompts; live replay
(.context/bucket-a-replay/count_rank.json) confirmed the planner produces
`operation="rank"` unaided when the user asks for ordered district rows.

This test runs against the real planner LLM. Marked `@pytest.mark.llm` so it is
excluded from the default CI run (`-m "not slow and not llm"`). Run on demand:

    set -a && source .env && set +a && \\
        PYTHONPATH=src uv run pytest \\
        src/compass_backend/tests/test_planner_ordered_metric_count_live.py \\
        -m llm -v
"""

from __future__ import annotations

import pytest

from compass_backend.agents.model_settings import agent_model_settings
from compass_backend.planning import CachedPlannerAgent
from compass_backend.planning.planner import PlannerDeps
from compass_backend.policy_guidance.library import get_library, load_default_library


ORDERED_RANK_PROMPTS = [
    "Show me observation counts for Texas districts, lowest first.",
    "Which TX districts require the fewest observations per year, smallest first?",
    "Rank all Texas districts by required observation count, ascending.",
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
@pytest.mark.parametrize("prompt", ORDERED_RANK_PROMPTS)
async def test_planner_emits_rank_for_ordered_count_metric_unaided(
    _planner_agent: CachedPlannerAgent,
    prompt: str,
) -> None:
    """For prompts that ask for ordered districts on a count-named metric,
    the planner alone must emit `operation="rank"` — no post-planner overlay
    required.

    The deleted overlay (`_promote_ordered_metric_count_to_rank`) used 17
    hardcoded phrases to flip `count` to `rank`. Live replay against the real
    planner showed this is unnecessary; the planner picks `rank` correctly
    when the user names ordering ("lowest first", "smallest first",
    "ascending"). This test pins that contract against future planner
    regressions.
    """

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
