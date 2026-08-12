"""W1-08 (#848): rescue detection is typed (is_rescue_fallback), not prose-equality.

Pre-W1-08 the rescue-enrichment guard compared
``ctx.turn.clarification.question`` to a verbatim
``_PLANNER_STRUCTURE_FAILURE_MESSAGE`` constant. Changing the rescue
wording silently broke the rescue path. The fix routes the detection
through a typed bool field on ``ClarificationRequest`` so the prose can
evolve without breaking the guard.

This test pins the contract: the guard ignores prose entirely and rides
the typed marker.
"""

from __future__ import annotations

from compass_backend.config import settings as _settings
from compass_backend.contracts import (
    ChatRequest,
    ClarificationRequest,
    ConversationMemory,
    PlannerTurn,
)
from compass_backend.contracts.session import SessionState, new_id
from unittest.mock import MagicMock

from compass_backend.orchestration.chat import (
    _enrich_rescue_with_prior_context,
    _planner_structured_output_failure_turn,
    _rescue_clarification_can_be_enriched,
    _TurnContext,
)


def _ctx_with_clarify(clarification: ClarificationRequest) -> _TurnContext:
    session = SessionState(
        session_id=new_id(),
        memory=ConversationMemory(),
    )
    ctx = _TurnContext(
        request=ChatRequest(message="trigger"),
        app_settings=_settings,
        auth_user=None,
        trace_id=None,
    )
    ctx.session_state = session
    ctx.turn = PlannerTurn(
        route="clarify",
        confidence=0.0,
        clarification=clarification,
    )
    return ctx


def test_rescue_decision_is_typed_not_string_matched() -> None:
    """The guard rides ClarificationRequest.is_rescue_fallback. Prose
    changes do not affect detection; toggling the bool does."""

    # Prose A — arbitrary string. Typed marker False → guard False.
    ctx_a = _ctx_with_clarify(
        ClarificationRequest(
            question="Some clarify prose that has nothing to do with rescue.",
            missing_fields=["metric"],
            is_rescue_fallback=False,
        )
    )
    assert _rescue_clarification_can_be_enriched(ctx_a) is False

    # Same prose — but typed marker True → guard True.
    ctx_b = _ctx_with_clarify(
        ClarificationRequest(
            question="Some clarify prose that has nothing to do with rescue.",
            missing_fields=["metric"],
            is_rescue_fallback=True,
        )
    )
    assert _rescue_clarification_can_be_enriched(ctx_b) is True

    # Different prose, typed marker True → guard still True (prose-independent).
    ctx_c = _ctx_with_clarify(
        ClarificationRequest(
            question="Entirely different wording about the rescue fallback path.",
            missing_fields=["comparison_group", "metric"],
            is_rescue_fallback=True,
        )
    )
    assert _rescue_clarification_can_be_enriched(ctx_c) is True


def test_planner_structured_output_failure_turn_carries_typed_marker() -> None:
    """The fallback turn built by _planner_structured_output_failure_turn
    sets is_rescue_fallback=True so downstream code can detect it
    without prose comparison."""

    turn = _planner_structured_output_failure_turn()
    assert turn.route == "clarify"
    assert turn.clarification is not None
    assert turn.clarification.is_rescue_fallback is True
    # #1613: the raw fallback turn also carries the DURABLE origin marker.
    assert turn.clarification.rescue_origin is True
    # Prose comes from the typed registry helper, not a verbatim constant.
    assert turn.clarification.question  # non-empty
    assert turn.clarification.missing_fields == ["comparison_group", "metric"]


def test_enrichment_clears_transient_flag_but_preserves_durable_rescue_origin() -> None:
    """#1613: the shape-guard enrichment rebuilds the rescue turn with
    is_rescue_fallback=False (so the enrichment gate cannot re-fire and loop),
    but MUST keep rescue_origin=True so the answerability eval criterion still
    sees the over-clarify on the persisted turn.

    Empty ConversationMemory → no prior QueryContext → the no-anchor branch of
    _enrich_rescue_with_prior_context (the path case 1115 hit). turn_span is a
    MagicMock since this test pins the rebuilt turn, not the telemetry.
    """
    ctx = _ctx_with_clarify(
        ClarificationRequest(
            question="raw rescue prose",
            missing_fields=["comparison_group", "metric"],
            is_rescue_fallback=True,
            rescue_origin=True,
        )
    )
    _enrich_rescue_with_prior_context(ctx, MagicMock())

    assert ctx.turn is not None
    assert ctx.turn.clarification is not None
    # Transient flag cleared (prevents re-triggering the enrichment gate)...
    assert ctx.turn.clarification.is_rescue_fallback is False
    # ...but the durable origin marker survives the rebuild.
    assert ctx.turn.clarification.rescue_origin is True


def test_clarification_request_default_is_not_rescue_fallback() -> None:
    """A normal clarify turn from the planner must default
    is_rescue_fallback=False so the rescue guard doesn't fire spuriously."""

    normal = ClarificationRequest(
        question="Which lane?",
        missing_fields=["metric"],
        candidates=["BA", "MA"],
    )
    assert normal.is_rescue_fallback is False
