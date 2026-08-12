"""Live planner test for compensation-intersection prompts.

Replaces `test_run_planner_promotes_compensation_intersections_to_lookup`,
which pinned the contract of the deleted `_promote_compensation_intersection_to_lookup`
override. The override's job was post-hoc rewriting of planner output for these
prompts; live replay (.context/gap1-replay/) confirmed the planner produces
functionally-correct plans unaided and the override's role-relabeling was
cosmetic (execution treats role=primary and role=comparison identically per
`quality/validators/metrics.py:144` and `execution/lookup.py:406`).

Wave 2 Bucket 1 extends this test to also assert that the planner sets
`requires_all_metrics=True` for these prompts, after the typed field was added
to `QueryPlan`. That assertion replaces the deleted `_lookup_plan_has_intersection_intent`
heuristic in `execution/_helpers.py` which previously inferred intersection
intent from question prose (`" both "`, `" plus "`, compensation terms).

This test runs against the real planner LLM. Marked `@pytest.mark.llm` so it is
excluded from the default CI run (`-m "not slow and not llm"`). Run on demand:

    set -a && source .env && set +a && \\
        PYTHONPATH=src uv run pytest \\
        src/compass_backend/tests/test_planner_compensation_intersection_live.py \\
        -m llm -v
"""

from __future__ import annotations

import pytest

from compass_backend.agents.model_settings import agent_model_settings
from compass_backend.planning import CachedPlannerAgent
from compass_backend.planning.planner import PlannerDeps
from compass_backend.policy_guidance.library import get_library, load_default_library


CANONICAL_PROMPTS = [
    "Which districts combine bonuses for being a highly effective teacher "
    + "with extra pay for working in a shortage area or hard-to-staff school?",
    "Which districts offer both performance pay bonuses AND extra pay for "
    + "working in hard-to-staff schools?",
    "Find districts that link pay to both teacher quality and school assignment.",
    "Which districts use targeted compensation - bonuses for performance "
    + "plus bonuses for high-need schools?",
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
@pytest.mark.parametrize("prompt", CANONICAL_PROMPTS)
async def test_planner_produces_intersection_lookup_shape_unaided(
    _planner_agent: CachedPlannerAgent,
    prompt: str,
) -> None:
    """For canonical compensation-intersection prompts, the planner alone
    must produce a lookup over all_covered_districts with two metrics —
    no post-planner override required.

    Role assignment (primary vs comparison on the second metric) is NOT
    asserted because the execution + validation layers treat both as the
    same execution role. The override that previously normalized role was
    cosmetic.
    """

    deps = PlannerDeps(prompt, current_academic_year="2024 - 2025")
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
    assert plan.selection.scope == "all_covered_districts", (
        f"scope={plan.selection.scope!r}, expected 'all_covered_districts' "
        f"for: {prompt!r}"
    )
    assert len(plan.metrics) == 2, (
        f"len(metrics)={len(plan.metrics)}, expected 2 for: {prompt!r}"
    )
    assert plan.filters == [], (
        f"filters={plan.filters!r}, expected [] for: {prompt!r}"
    )
    assert plan.requires_all_metrics is True, (
        f"requires_all_metrics={plan.requires_all_metrics!r}, expected True "
        f"for: {prompt!r}. Planner must set this typed field instead of execution "
        f"re-inferring intersection intent from prose."
    )
