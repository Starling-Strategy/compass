"""Deterministic follow-up turn/planner-run builders for chat orchestration.

Pure constructors used by the policy-guidance and data-detail follow-up
resolvers: the exemplar-clarification turn and the two deterministic
``PlannerRun`` evidence wrappers that stand in for a real planner call when a
follow-up is resolved from typed memory. Extracted verbatim from
`orchestration/chat.py` (#1130 decomposition). The ``_maybe_resolve_*``
resolvers that call these stay in `chat` because they set span attributes that
tests monkeypatch on the chat-module namespace. Behaviour-preserving move — no
logic changes.
"""

from collections.abc import Sequence

from compass_backend.contracts import (
    ClarificationRequest,
    PlannerGuidanceEvidence,
    PlannerRunEvidence,
    PolicyGuidanceExemplarRef,
)
from compass_backend.contracts.planning import PlannerTurn
from compass_backend.planning import PlannerRun

def _policy_guidance_exemplar_clarification_turn(
    exemplar_refs: Sequence[PolicyGuidanceExemplarRef],
) -> PlannerTurn:
    candidates = [
        f"{ref.district} ({ref.subtopic})" for ref in exemplar_refs[:5]
    ]
    return PlannerTurn(
        route="clarify",
        confidence=1.0,
        clarification=ClarificationRequest(
            question="Which approved exemplar should I expand?",
            missing_fields=["scope"],
            candidates=candidates,
        ),
    )


def _deterministic_data_detail_planner_run(
    turn: PlannerTurn,
    *,
    trace_id: str | None,
    matched_phrase: str | None,
) -> PlannerRun:
    return PlannerRun(
        turn=turn,
        evidence=PlannerRunEvidence(
            model="deterministic-data-detail-followup",
            trace_id=trace_id,
            planner_guidance=[
                PlannerGuidanceEvidence(
                    name="data-detail-followup-resolver",
                    metadata={"mode": "top_result_lookup"},
                    matched_phrase=matched_phrase,
                )
            ],
        ),
    )


def _deterministic_clarification_choice_planner_run(
    turn: PlannerTurn,
    *,
    trace_id: str | None,
    selected_value: str,
) -> PlannerRun:
    """Evidence wrapper for a clarify resumed by a clicked structured option (#1348).

    Stands in for a real planner call: the user clicked a grounded option, so
    the resume is deterministic (no model call). The selected machine handle is
    recorded for auditability.
    """

    return PlannerRun(
        turn=turn,
        evidence=PlannerRunEvidence(
            model="deterministic-clarification-choice",
            trace_id=trace_id,
            planner_guidance=[
                PlannerGuidanceEvidence(
                    name="clarification-choice-resolver",
                    metadata={"selected_option": selected_value},
                )
            ],
        ),
    )


def _deterministic_policy_guidance_planner_run(
    turn: PlannerTurn,
    *,
    trace_id: str | None,
    matched_phrase: str | None,
    mode: str,
) -> PlannerRun:
    return PlannerRun(
        turn=turn,
        evidence=PlannerRunEvidence(
            model="deterministic-policy-guidance-followup",
            trace_id=trace_id,
            planner_guidance=[
                PlannerGuidanceEvidence(
                    name="policy-guidance-followup-resolver",
                    metadata={"mode": mode},
                    matched_phrase=matched_phrase,
                )
            ],
        ),
    )
