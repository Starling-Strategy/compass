"""Drift guard: the follow-up-reference snippet must cover every prior-result
district sentinel the resolver actually recognises.

The single source of truth is ``_SELECTION_REFERENT_SENTINELS`` in
``compass_backend.planning.planner`` (consumed by
``_selection_districts_are_referent`` / ``_normalize_plan_selection_with_context``).
The planner-guidance snippet only *references* that set, so this test fails loudly
if a sentinel is added to the code without updating the snippet prose (issue #1381
fixed a 7-vs-12 drift). The check is one-directional on purpose: code must be a
subset of what the snippet lists. The reverse is NOT asserted because the snippet's
<scope> line legitimately also names natural-language phrases (e.g. "name them",
"the ones from before") that are trigger phrases / delta-detection markers, not
district-list sentinels.
"""

from __future__ import annotations

from compass_backend.instructions.loader import load_planner_guidance
from compass_backend.planning.planner import _SELECTION_REFERENT_SENTINELS


def test_follow_up_reference_lists_every_code_sentinel() -> None:
    """Every sentinel in the code frozenset appears (quoted) in the snippet body."""
    body = load_planner_guidance("follow-up-reference.md")
    missing = sorted(
        sentinel
        for sentinel in _SELECTION_REFERENT_SENTINELS
        if f'"{sentinel}"' not in body
    )
    assert not missing, (
        "follow-up-reference.md has drifted from "
        "_SELECTION_REFERENT_SENTINELS; missing quoted sentinels: "
        f"{missing}. Update the snippet's sentinel bullet to match the code."
    )
