"""Live planner test for multi-district comparison prompts.

Replaces `test_run_planner_promotes_complete_dallas_denver_dcps_evaluation_prompt`,
which pinned the contract of the deleted `_promote_known_complete_clarification`
overlay. The overlay's job was post-hoc rewriting a planner clarification into
a hardcoded `lookup` plan for the specific Dallas/Denver/DCPS + evaluation
phrasing; live replay (.context/bucket-a-replay/multi_district.json) confirmed
the planner produces a correct multi-district lookup unaided for both the
original Dallas/Denver/DCPS prompt AND a variant trio (Houston/Austin/Fort
Worth), proving the pattern is general — not Dallas/Denver/DCPS-specific.

This test runs against the real planner LLM. Marked `@pytest.mark.llm` so it is
excluded from the default CI run (`-m "not slow and not llm"`). Run on demand:

    set -a && source .env && set +a && \\
        PYTHONPATH=src uv run pytest \\
        src/compass_backend/tests/test_planner_named_district_comparison_live.py \\
        -m llm -v
"""

from __future__ import annotations

import pytest

from compass_backend.agents.model_settings import agent_model_settings
from compass_backend.planning import CachedPlannerAgent
from compass_backend.planning.planner import PlannerDeps
from compass_backend.policy_guidance.library import get_library, load_default_library


# Each tuple: (prompt, tuple-of-district-alternatives). Each inner tuple lists
# acceptable substrings for one named district — the planner may expand "DC"
# to "DC Public Schools", "District of Columbia Public Schools", or "DCPS"
# (observed across runs). The test passes if at least one alternative per
# district appears (case-insensitive substring) in the planner's emitted
# selection.districts.
MULTI_DISTRICT_PROMPTS: list[tuple[str, tuple[tuple[str, ...], ...]]] = [
    (
        "What are the differences in how Dallas, Denver, and DCPS evaluate "
        "their teachers?",
        (
            ("dallas",),
            ("denver",),
            ("dcps", "dc public schools", "district of columbia"),
        ),
    ),
    (
        "How do Dallas, Denver, and DCPS handle teacher evaluations differently?",
        (
            ("dallas",),
            ("denver",),
            ("dcps", "dc public schools", "district of columbia"),
        ),
    ),
    (
        "Compare Dallas, Denver, and DC's teacher evaluation policies side by "
        "side.",
        (
            ("dallas",),
            ("denver",),
            ("dcps", "dc public schools", "district of columbia"),
        ),
    ),
    (
        "How do Houston, Austin, and Fort Worth differ on teacher evaluations?",
        (("houston",), ("austin",), ("fort worth",)),
    ),
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
@pytest.mark.parametrize(
    ("prompt", "expected_district_alternatives"),
    MULTI_DISTRICT_PROMPTS,
)
async def test_planner_emits_multi_district_lookup_unaided(
    _planner_agent: CachedPlannerAgent,
    prompt: str,
    expected_district_alternatives: tuple[tuple[str, ...], ...],
) -> None:
    """For prompts that name two or more covered districts and ask how they
    differ, the planner alone must emit a `named_districts` lookup over those
    districts — no post-planner overlay required.

    The deleted overlay (`_promote_known_complete_clarification`) checked for
    Dallas + Denver + DCPS + an evaluation/comparison phrase and synthesized
    a hardcoded `lookup` plan against those three districts with a single
    `MetricSpec(name="teacher evaluations differently")`. Live replay against
    the real planner showed this is unnecessary; the planner picks `lookup`
    + `scope="named_districts"` correctly and emits richer per-district
    evaluation metric phrases (e.g. "teacher evaluation system",
    "evaluation criteria", "observation frequency") that the catalog
    adjudicator can resolve. This test pins the structural contract; the
    metric phrasing is the catalog adjudicator's job, not the planner's.
    """

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
    assert plan.selection.scope == "named_districts", (
        f"scope={plan.selection.scope!r}, expected 'named_districts' "
        f"for: {prompt!r}"
    )
    district_names = " ".join(d.lower() for d in plan.selection.districts)
    for alternatives in expected_district_alternatives:
        if not any(needle.lower() in district_names for needle in alternatives):
            pytest.fail(
                f"district names {district_names!r} missing any of "
                f"{alternatives!r} for: {prompt!r}"
            )
