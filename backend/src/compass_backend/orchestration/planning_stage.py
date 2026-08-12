"""Planning-stage helpers for chat-turn orchestration.

Pure(-ish) helpers around the planner run: the data-bearing direct-turn guard
and its reroute, deterministic turn normalizers, the dispatchability check, the
session-context merge, and the rescue-clarification enrichment predicate.
Extracted from `orchestration/chat.py` (#1130 decomposition). The functions
that call ``run_planner`` / ``set_span_attributes`` (``_run_planner_for_turn``,
``_promote_pending_context_for_turn``, ``_enrich_rescue_with_prior_context``,
``_apply_post_merge_shape_guard``) stay in `chat` because tests monkeypatch
those names on the chat-module namespace.
"""

import re

from compass_backend.contracts import ClarificationRequest
from compass_backend.contracts.planning import PlannerTurn
from compass_backend.observability import compass_span
from compass_backend.execution.shape_check import (
    ranking_delegates_to_multi_metric_lookup,
)
from compass_backend.planning import (
    PlannerRun,
    merge_turn_with_session_context,
    normalize_ba_ma_salary_rank_turn,
    normalize_turn_profile_ranking_intent,
    normalize_turn_temporal_intent,
)
from compass_backend.planning.negative_filters import (
    normalize_turn_negative_policy_filters,
)

from compass_backend.orchestration.turn_context import _TurnContext

_DATA_BEARING_DIRECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$\s*\d"),
    re.compile(r"\d+\s*%"),
    re.compile(r"\b\d{3,}\b"),
    re.compile(r"\b(rank|ranking|top \d+|bottom \d+|largest \d+)\b", re.IGNORECASE),
)


def _message_is_data_bearing(message: str) -> bool:
    """Return True when a raw message contains data-shaped tokens.

    Pure yes/no trigger gate over ``_DATA_BEARING_DIRECT_PATTERNS`` (dollar
    amount, percentage, multi-digit number, or ranking phrasing). Used by the
    W1-07 (#846) direct-turn guard below and by the #1096 Track A empty-clarify
    recovery (which runs it on the raw user message). It only *gates whether to
    attempt* governed grounding — it never reads prose to ground a referent, so
    it stays within the allowed trigger-gate shape under backend guardrail #4.
    """

    return any(pattern.search(message) for pattern in _DATA_BEARING_DIRECT_PATTERNS)


def _is_data_bearing_direct_turn(turn: PlannerTurn) -> bool:
    """Return True when a direct-route turn's message contains data tokens.

    W1-07 (#846) guard. The direct route was always meant for greetings,
    capability questions, and explicitly non-data turns. Any direct turn
    whose response contains a dollar amount, percentage, multi-digit
    number, or ranking phrasing is the model claiming data — and that
    claim must route through governed catalog + execution, not bypass it.
    """

    if turn.route != "direct" or turn.direct_response is None:
        return False
    if turn.direct_response.reason.casefold().strip().endswith("data inventory"):
        return False
    return _message_is_data_bearing(turn.direct_response.message)


def _reroute_direct_to_clarify(turn: PlannerTurn) -> PlannerTurn:
    """Build a typed clarify turn that asks the user to restate their data request.

    Used by the W1-07 (#846) data-bearing direct-route guard. The
    original direct response is discarded — its claim was unauthorized.
    """

    return PlannerTurn(
        route="clarify",
        confidence=min(turn.confidence, 0.5),
        clarification=ClarificationRequest(
            question=(
                "I want to make sure I answer this from governed Compass data "
                "rather than free-form. Can you restate what district(s), "
                "metric, and time period you need?"
            ),
            missing_fields=["metric"],
            candidates=[],
        ),
    )


def _merge_session_context_for_turn(ctx: _TurnContext) -> None:
    """Carry session-scoped query context into this turn's planner output."""

    assert ctx.session_state is not None
    assert ctx.turn is not None
    with compass_span(
        "compass.session.merge_context",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        route=ctx.turn.route,
    ):
        ctx.turn = merge_turn_with_session_context(
            ctx.turn,
            ctx.session_memory.latest_query_context,
        )


def _normalize_planner_turn(ctx: _TurnContext) -> None:
    """Apply deterministic planner-turn normalizers before execution."""

    assert ctx.session_state is not None
    assert ctx.turn is not None
    assert ctx.planner_run is not None
    with compass_span(
        "compass.planner.temporal_normalize",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        route=ctx.turn.route,
    ):
        ctx.turn = normalize_turn_temporal_intent(
            ctx.turn,
            message=ctx.request.message,
            current_academic_year=ctx.app_settings.current_academic_year,
        )
        ctx.turn = normalize_turn_profile_ranking_intent(ctx.turn)
        ctx.turn = normalize_turn_negative_policy_filters(
            ctx.turn,
            message=ctx.request.message,
        )
        ctx.turn = normalize_ba_ma_salary_rank_turn(ctx.turn)
        ctx.planner_run = PlannerRun(
            turn=ctx.turn,
            evidence=ctx.planner_run.evidence,
        )


def _plan_likely_dispatchable(plan) -> bool:
    """Return True when the executor has a delegation path for this plan shape.

    ``execution.shape_check.unsupported_shape_hint`` is intentionally narrow:
    it only knows the per-operation ``*_shape_is_supported`` predicates and
    cannot see the executor's internal delegations (e.g., the multi-metric
    rank → lookup hand-off at
    ``execution/operations.py::_execute_ranking`` lines 636-646, or the
    profile-ordered ranking dispatch). When one of those delegation patterns
    is plausible we skip the orchestrator-level shape guard and let the
    executor route the plan; if execution genuinely fails the existing
    refusal path still fires.

    Keeps the guard conservative — it should only convert refusal-bound
    plans, never plans the executor would have handled. Reads typed
    ``QueryPlan`` fields only.
    """
    if plan.operation == "rank":
        if ranking_delegates_to_multi_metric_lookup(plan):
            return True
        # Profile-ordered rankings (e.g., "rank by FRPL share, show salary")
        # have their own dispatch via sort_steps + profile selection.
        if any(
            step.key_type == "profile_field"
            for step in (plan.sort_steps or [])
        ):
            return True
    return False


def _rescue_clarification_can_be_enriched(ctx: _TurnContext) -> bool:
    """Return True when the rescue clarification should be replaced.

    W1-08 (#848): detection rides the typed
    ``ClarificationRequest.is_rescue_fallback`` marker so the rescue
    prose can evolve without breaking the enrichment guard.

    Fires when the planner emitted the safe-structure clarification,
    regardless of whether prior context exists. Two branches downstream:
      - Prior QueryContext with result_districts present → anchored typed
        clarification naming the prior district count + metric.
      - No prior context (e.g., turn 2 of a clarify→clarify chain like
        M3c-1 "BA and MA in one table") → rephrase-suggestion clarification
        that breaks the generic fallback prose so the milestone acceptance
        "never return the generic refusal" holds.
    """
    if ctx.turn is None or ctx.turn.route != "clarify":
        return False
    if ctx.turn.clarification is None:
        return False
    if not ctx.turn.clarification.is_rescue_fallback:
        return False
    return True
