"""Tests for the policy-guidance advisory-followup planner snippet (#1688).

Voice & Tone case 433 (CITE-MIGRATED-254-C00): after a benefits exemplar turn,
the follow-up "Should we prioritize improving pay or improving benefits?" is an
advisory/comparative question that weighs two policy priorities. The planner was
mapping it to a multi-topic exemplar dump ("Here is NCTQ policy guidance for
General Salary and Benefits." + both bundles), ignoring the prioritization
framing (scenario_fit criterion 59 fails). This snippet teaches the planner to
answer the weighing question from stances + rationales instead, and must fire on
the reporter's literal turn-2 prompt while staying off non-comparative asks.

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_policy_guidance_advisory_followup_snippet.py --tb=short
"""

from __future__ import annotations

from types import SimpleNamespace

from compass_backend.planning.instruction_snippets import (
    POLICY_GUIDANCE_ADVISORY_FOLLOWUP,
    select_planner_instruction_snippets,
)


def _names(message: str, recent_routes: tuple[str, ...]) -> list[str]:
    deps = SimpleNamespace(
        message=message, query_context=None, recent_routes=recent_routes
    )
    return [s.name for s in select_planner_instruction_snippets(deps)]


# ─── Snippet selection: fires on the case-433 advisory follow-up ──────────────


def test_snippet_fires_on_reporter_literal_followup() -> None:
    # The #1688 / case-433 literal turn-2 prompt, after a policy_guidance turn.
    assert POLICY_GUIDANCE_ADVISORY_FOLLOWUP.name in _names(
        "Should we prioritize improving pay or improving benefits?",
        recent_routes=("policy_guidance",),
    )


def test_snippet_fires_on_which_matters_more_phrasing() -> None:
    assert POLICY_GUIDANCE_ADVISORY_FOLLOWUP.name in _names(
        "Which matters more for retention, salary or benefits?",
        recent_routes=("policy_guidance",),
    )


def test_snippet_is_exclusive_preempting_exemplar_followup() -> None:
    # Priority 4 + exclusive: it is the only snippet selected for this turn, so
    # the exemplar-detail POLICY_GUIDANCE_FOLLOWUPS snippet cannot also fire.
    names = _names(
        "Should we prioritize improving pay or improving benefits?",
        recent_routes=("policy_guidance",),
    )
    assert names == [POLICY_GUIDANCE_ADVISORY_FOLLOWUP.name]


# ─── Snippet selection: stays OFF when the gate conditions are absent ─────────


def test_snippet_requires_prior_policy_guidance_route() -> None:
    # Same prompt, but the prior turn was an execute turn — the advisory
    # follow-up snippet must not fire (no policy-guidance referent to weigh).
    assert POLICY_GUIDANCE_ADVISORY_FOLLOWUP.name not in _names(
        "Should we prioritize improving pay or improving benefits?",
        recent_routes=("execute",),
    )


def test_snippet_requires_comparative_or_framing() -> None:
    # "prioritize" without a second priority to weigh ("or") is not a
    # pay-vs-benefits weighing — the required group keeps the snippet off.
    assert POLICY_GUIDANCE_ADVISORY_FOLLOWUP.name not in _names(
        "Should we prioritize improving benefits?",
        recent_routes=("policy_guidance",),
    )


def test_snippet_yields_to_detail_followup_not_overfiring() -> None:
    # "which is more important, the source or the contract language?" reads as a
    # detail follow-up about a prior exemplar, not a policy weighing turn. The
    # blocked_phrases gate must keep the advisory snippet off and let the
    # exemplar-detail POLICY_GUIDANCE_FOLLOWUPS snippet handle it instead.
    names = _names(
        "Which is more important, the source or the contract language?",
        recent_routes=("policy_guidance",),
    )
    assert POLICY_GUIDANCE_ADVISORY_FOLLOWUP.name not in names
    assert "policy-guidance-followups" in names


# ─── Recipe body (the governed shape it teaches) ──────────────────────────────


def test_recipe_steers_to_stances_and_rationales_not_exemplar_dump() -> None:
    body = POLICY_GUIDANCE_ADVISORY_FOLLOWUP.body.lower()
    assert "policy_guidance" in body
    assert "stances" in body
    assert "rationales" in body
    # It must explicitly steer AWAY from the multi-topic exemplar dump.
    assert "exemplar" in body
    assert "not " in body
