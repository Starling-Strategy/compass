"""Deterministic prefilter rules for criterion selection.

Pure-Python functions: no DB calls, no LLM calls, no async. Each rule takes a
CompassEvaluatorContext and returns a set of criterion_codes that should fire
given the turn's observable properties.

These are Safeguard 3 of the 4-safeguard criterion classifier defined in
ADR Decision: Classifier safeguards:

  1. Mandatory globals   — always fire; loaded from DB by the classifier
  2. Scenario-required   — additive; injected from compass.scenarios fixture
  3. Deterministic prefilters — this module; pure Python; no AI, no DB
  4. AI classifier        — additive; Literal[*codes] output; Type I impossible

Rules are composed in select_criteria_for_turn() in classifier.py. The
prefilter layer fires BEFORE the AI classifier; its codes are added unconditionally
regardless of what the AI returns.

Rule naming: each function starts with `_rule_`. New rules are registered
in PREFILTER_RULES at module bottom. Each rule must be:
  - Free of side effects
  - Stable: same context → same output on every call
  - Documented: what observable property triggers it, and why that maps to Sort Accuracy
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from compass_backend.quality._evaluator_context import CompassEvaluatorContext

logger = logging.getLogger(__name__)

# ─── Rule implementations ────────────────────────────────────────────────────


def _rule_sort_descending(ctx: CompassEvaluatorContext) -> bool:
    """Fire when the user's intent contains 'sort' AND there is a table artifact.

    Sort Accuracy criterion: did Compass use descending order when the user
    asked for a ranking (top-N)?  Only applicable when both signals are present:
    - intent signals sorting (contains the word 'sort')
    - a table was produced (ctx.artifact_snapshot.table is non-empty)

    Returns True when BOTH conditions hold.
    """
    intent = (ctx.intent or "").lower()
    has_sort_intent = "sort" in intent
    has_table = bool(ctx.artifact_snapshot.table)
    return has_sort_intent and has_table


# Words that mark the user as asking for a sort/rank/re-order operation in
# the current turn. Mirrors `_rule_sort_descending` but with the broader
# re-sort vocabulary the rubric tests for ("re-sort", "reorder", "rank by",
# etc.). Reads from ctx.intent which the planner derives from the user
# message (QueryPlan.operation + temporal.intent).
_SORT_INTENT_KEYWORDS: tuple[str, ...] = (
    "sort",
    "rank",
    "order",
    "reorder",
    "re-order",
    "re-sort",
)


_REFUSAL_BODY_SENTINELS: tuple[str, ...] = (
    # `_message_for_turn` fallback at orchestration/chat.py.
    "deterministic execution does not support this query shape",
    "i couldn't safely run that request as structured",
    # `render_response` validation-failed body at rendering/writer.py.
    "validation failed before rendering",
    # Planner rescue clarification prose surfaced when typed enrichment
    # cannot anchor on prior context.
    "i could not structure that request safely",
    "couldn't structure this follow-up",
    "couldn't structure that request",
)


def _response_looks_like_follow_up_refusal(ctx: CompassEvaluatorContext) -> bool:
    """Return True when the turn's user-visible answer matches an M3 refusal mode.

    The M3 milestone catalogs three failure modes that the
    `preserves_prior_context` rubric must catch: the planner rescue
    clarification, the unsupported-shape fallback prose, and the
    validation-failed prose. Each is detectable from the rendered
    `answer_text` — they are deterministic sentinels, not LLM output.
    Pure substring match on a casefolded copy keeps the check stable.
    """
    body = (ctx.answer_text or "").lower()
    if not body:
        return False
    return any(sentinel in body for sentinel in _REFUSAL_BODY_SENTINELS)


def _rule_preserves_prior_context(ctx: CompassEvaluatorContext) -> bool:
    """Fire on follow-up turns when there is prior context to preserve.

    Sort-and-context criterion. Two distinct dispatch shapes share this code:

    1. **Sort/re-sort on a table** (legacy trigger; Phase 6A scope). The user
       asks Compass to re-order a prior comparison set and a table was rendered
       this turn. The judge then grades whether the new table is the prior set
       re-sorted.
    2. **Follow-up refusal with prior context to preserve** (M3 scope; this
       broadening). The session already produced one or more result_refs, the
       current turn is a follow-up, and the response is one of the documented
       refusal/clarify-rescue strings. Without this branch the prefilter
       silently skipped exactly the M3 failure pattern the rubric exists to
       catch.

    The pre-Phase-6A behaviour was `turn_index > 0` alone, which fired on
    every multi-turn case and was graded `not_applicable` on most of them
    (PR #967 calibration). Today's union pairs each fire with a concrete
    artifact the judge can grade — either a table (shape 1) or a refusal
    body whose presence-with-prior-context is itself the failure (shape 2).
    """
    if ctx.turn_index is None or ctx.turn_index <= 0:
        return False
    intent = (ctx.intent or "").lower()
    has_sort_intent = any(kw in intent for kw in _SORT_INTENT_KEYWORDS)
    has_table = bool(ctx.artifact_snapshot.table)
    if has_sort_intent and has_table:
        return True
    # M3 broadening: a follow-up refusal when prior context exists is the
    # signal the rubric needs to evaluate. `recent_routes` is checked
    # alongside `prior_result_ref_count` so a pure first-turn refusal (no
    # prior exchange at all) still skips.
    if (
        ctx.prior_result_ref_count > 0
        and bool(ctx.recent_routes)
        and _response_looks_like_follow_up_refusal(ctx)
    ):
        return True
    return False


def _rule_respects_user_limit(ctx: CompassEvaluatorContext) -> bool:
    """Fire when a table artifact is present (proxy for a 'top-N' response).

    Sort Accuracy criterion: if the user asked for 'top 5' and Compass
    returns a table, the table must have exactly 5 data rows. The presence
    of a table is a sufficient signal to activate this criterion — the
    evaluator itself will verify the row count against the user limit.

    Returns True when ctx.artifact_snapshot.table is non-empty.
    """
    return bool(ctx.artifact_snapshot.table)


# ─── Registry ────────────────────────────────────────────────────────────────

# Maps criterion_code → rule predicate.
# When a rule returns True, its code is added to the prefilter selection set.
# Keys MUST match the criterion_code values seeded in compass.criteria.
PREFILTER_RULES: dict[str, Callable[[CompassEvaluatorContext], bool]] = {
    "sort_descending": _rule_sort_descending,
    "preserves_prior_context": _rule_preserves_prior_context,
    "respects_user_limit": _rule_respects_user_limit,
}


def apply_prefilters(ctx: CompassEvaluatorContext) -> set[str]:
    """Return the set of criterion_codes that deterministic rules fire for ctx.

    Callers (classifier.py) union this result with mandatory globals and
    AI classifier output. No rule here fires for criterion codes outside
    PREFILTER_RULES — undefined codes are never returned.

    Args:
        ctx: The turn context to evaluate against each registered rule.

    Returns:
        Set of criterion_codes whose rule returned True.
    """
    fired: set[str] = set()
    for code, rule_fn in PREFILTER_RULES.items():
        try:
            if rule_fn(ctx):
                fired.add(code)
        except Exception:
            # Prefilter failures must never break selection. Log and skip.
            logger.warning("prefilter rule %r raised; skipping", code, exc_info=True)
    return fired


__all__ = [
    "apply_prefilters",
    "PREFILTER_RULES",
    # Individual rules exported for direct unit testing
    "_rule_sort_descending",
    "_rule_preserves_prior_context",
    "_rule_respects_user_limit",
]
