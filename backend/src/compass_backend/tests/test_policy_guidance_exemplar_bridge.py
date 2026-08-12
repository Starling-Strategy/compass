"""#1755 (B14): policy-guidance exemplars must bridge into the rescue-path
PendingQueryContext on a deictic data follow-up, instead of dropping the
established districts and emitting the no-anchor rescue.

Production repro (session db7d98ab…, trace 019ef061…):
  turn 1: "Give me examples of districts that provide meaningful benefits."
          → route=policy_guidance, 3 exemplar districts (Broward, Wichita KS,
            Jackson MS), each with a real district_id.
  turn 2: "what are the enrollment sizes of these 3 districts?"
          → the planner ran with has_query_context=false, exhausted retries,
            and rescued via guard_path=rescue_enrichment_fallback. The 3
            exemplar district_ids were never bridged from policy_guidance
            memory, so the turn dropped them entirely.

The owning boundary is conversation_memory / orchestration:
``_enrich_rescue_with_prior_context``. When the rescue fires and there is no
prior ``QueryContext`` to anchor on but there IS a
``latest_policy_guidance_context`` with exemplars carrying real district_ids,
and the follow-up message refers back deictically ("these"/"those"), bridge
the exemplar districts into a ``PendingQueryContext`` (named_districts
selection) and emit the ``anchored_no_metric`` clarification — so the next
turn can promote to a profile/metric lookup on exactly those districts rather
than starting from zero context.

This is the carry-forward (clarify) half of #1755 (linkage: Refs). The
deictic guard prevents over-firing on a fresh, unrelated question.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from compass_backend.config import settings as _settings
from compass_backend.contracts import (
    ChatRequest,
    ClarificationRequest,
    ConversationMemory,
    PlannerTurn,
)
from compass_backend.contracts.session import (
    PolicyGuidanceContext,
    PolicyGuidanceExemplarRef,
    SessionState,
    new_id,
)
from compass_backend.orchestration.chat import (
    _enrich_rescue_with_prior_context,
    _rescue_clarification_can_be_enriched,
    _TurnContext,
)
from compass_backend.rendering.rescue_clarification import (
    RescueClarificationRef,
    rescue_clarification_question_for,
)

# The exact three NCTQ-curated benefit exemplars from the production turn 1,
# each with a real district_id (typed int) the bridge reads.
_EXEMPLARS = [
    PolicyGuidanceExemplarRef(
        exemplar_id="benefits-broward",
        topic_id="benefits",
        district="Broward County Public Schools",
        district_id=1200180,
        subtopic="health benefits",
        source_url="https://example.org/broward",
        citation_status="ready",
    ),
    PolicyGuidanceExemplarRef(
        exemplar_id="benefits-wichita",
        topic_id="benefits",
        district="Wichita Public Schools",
        district_id=2014700,
        subtopic="retirement benefits",
        source_url="https://example.org/wichita",
        citation_status="ready",
    ),
    PolicyGuidanceExemplarRef(
        exemplar_id="benefits-jackson",
        topic_id="benefits",
        district="Jackson Public Schools",
        district_id=2800480,
        subtopic="leave benefits",
        source_url="https://example.org/jackson",
        citation_status="ready",
    ),
]

_FALLBACK_NO_ANCHOR_TEXT = rescue_clarification_question_for(
    RescueClarificationRef(code="fallback_no_anchor")
)


def _ctx_for_policy_guidance_followup(message: str) -> _TurnContext:
    """Build the production rescue condition: a rescue-fallback clarify turn,
    NO prior QueryContext, but a latest_policy_guidance_context with 3
    exemplars (the turn-1 benefit districts)."""

    guidance = PolicyGuidanceContext(
        topic_ids=["benefits"],
        layers=["exemplars"],
        focus_terms=["meaningful benefits"],
        primary_topic_id="benefits",
        intent_summary="User asked for examples of districts with meaningful benefits.",
        exemplars=list(_EXEMPLARS),
        turn_index=1,
        snapshot_id=new_id(),
        user_message="Give me examples of districts that provide meaningful benefits.",
    )
    session = SessionState(
        session_id=new_id(),
        memory=ConversationMemory(
            latest_query_context=None,
            latest_policy_guidance_context=guidance,
            recent_routes=["policy_guidance"],
        ),
    )
    ctx = _TurnContext(
        request=ChatRequest(message=message),
        app_settings=_settings,
        auth_user=None,
        trace_id=None,
    )
    ctx.session_state = session
    ctx.turn_index = 2
    ctx.turn = PlannerTurn(
        route="clarify",
        confidence=0.0,
        clarification=ClarificationRequest(
            question="raw rescue prose",
            missing_fields=["comparison_group", "metric"],
            is_rescue_fallback=True,
            rescue_origin=True,
        ),
    )
    return ctx


def test_exemplar_bridge_carries_districts_on_deictic_followup() -> None:
    """The reporter's literal turn-2 prompt references the prior 3 districts
    deictically. The rescue must NOT drop them: it bridges the exemplar
    district names into a named_districts PendingQueryContext and emits the
    anchored_no_metric clarification (not the no-anchor rescue)."""

    ctx = _ctx_for_policy_guidance_followup(
        "what are the enrollment sizes of these 3 districts?"
    )
    # The production trace shows this scenario reaches
    # _enrich_rescue_with_prior_context via guard_path=rescue_enrichment_fallback,
    # i.e. the rescue-enrichment gate has already passed. Pin that here so the
    # unit test exercises the same entry condition the real flow routes through.
    assert _rescue_clarification_can_be_enriched(ctx) is True

    _enrich_rescue_with_prior_context(ctx, MagicMock())

    assert ctx.turn is not None
    assert ctx.turn.clarification is not None
    question = ctx.turn.clarification.question

    # Did NOT emit the no-anchor rescue ("I need one district group and one
    # Compass metric to start.") — the real dropped-districts symptom.
    assert question != _FALLBACK_NO_ANCHOR_TEXT
    assert "I need one district group and one Compass metric" not in question

    # The 3 turn-1 exemplar districts are carried forward in the typed
    # pending context as a named_districts selection (names live here, not in
    # the count-only anchored_no_metric prose).
    pending = ctx.next_pending_context
    assert pending is not None
    assert pending.selection is not None
    assert pending.selection.scope == "named_districts"
    assert pending.selection.districts == [
        "Broward County Public Schools",
        "Wichita Public Schools",
        "Jackson Public Schools",
    ]

    # The clarification carries the same pending context for next-turn promotion.
    assert ctx.turn.clarification.pending_context is pending
    # Durable origin marker survives the rebuild (answerability eval criterion).
    assert ctx.turn.clarification.rescue_origin is True
    # Transient flag cleared so the enrichment gate cannot re-fire.
    assert ctx.turn.clarification.is_rescue_fallback is False


def test_exemplar_bridge_does_not_overfire_without_deictic_reference() -> None:
    """The deictic guard must keep this narrow: a follow-up that does NOT refer
    back to the prior districts ("how many districts pay over $60k?") must fall
    through to the no-anchor rescue, not silently bind the unrelated exemplars."""

    ctx = _ctx_for_policy_guidance_followup(
        "how many districts pay teachers over $60,000?"
    )
    _enrich_rescue_with_prior_context(ctx, MagicMock())

    assert ctx.turn is not None
    assert ctx.turn.clarification is not None
    # No deictic back-reference → no exemplar bridge → the no-anchor rescue.
    assert ctx.turn.clarification.question == _FALLBACK_NO_ANCHOR_TEXT
    pending = ctx.next_pending_context
    assert pending is not None
    assert pending.selection is None
