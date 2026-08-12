"""Turn-failure taxonomy tagging for the Compass turn pipeline.

Move 0 of the capability-led plan
(docs/plans/2026-05-26-data-fidelity-clarify-vs-answer-investigation.md):
classify every assistant turn into a small, queryable taxonomy so the
40% production clarify rate can be decomposed by failure mode.

This module is pure observability — it never raises into the orchestration
hot path, and the classification function has no side effects. The
attribute-setter swallows telemetry-only failures via
``set_span_attributes``.

Deferred for follow-up PRs (need deeper changes):
  - ``was_repeat_of_session`` — needs user-message fingerprinting on
    ``ConversationMemory``.
  - ``referent_continuity`` — needs the planner to mark when a turn
    inherits a prior referent.
  - ``post_clarify_abandonment`` — cross-turn metric, owned by the
    quality pipeline rather than this per-turn tagger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from compass_backend.contracts.planning import (
    ClarificationRequest,
    PlannerTurn,
)
from compass_backend.execution.types import ExecutionOutcome
from compass_backend.observability import set_span_attributes


OutcomeClass = Literal[
    "executed_ok",
    "clarify",
    "refusal",
    "no_results",
    "policy_guidance",
    "publication",
    "direct",
]
"""Top-level outcome bucket for a Compass turn.

  - ``executed_ok``: planner routed ``execute`` and the executor returned
    a non-empty validated result.
  - ``clarify``: planner asked the user a question (see ``ClarifySource``
    for the originating subsystem).
  - ``refusal``: executor returned ``ExecutionRefusal`` — request was
    shape-supported but couldn't produce data.
  - ``no_results``: executor returned ``ExecutionSuccess`` but the result
    rows were empty. Distinct from ``refusal`` because the query
    succeeded; the data universe just didn't have a match.
  - ``policy_guidance``: planner routed to the deterministic
    policy_guidance lane.
  - ``publication``: planner routed to the deterministic publication lane,
    citing NCTQ's published writing.
  - ``direct``: planner authored a direct chitchat answer (no execution).
"""


ClarifySource = Literal[
    "planner_route",
    "execution_clarification",
    "rescue_fallback",
    "recognition_blocker",
]
"""Where the clarify outcome originated.

  - ``rescue_fallback``: ``ClarificationRequest.is_rescue_fallback`` set
    — planner output failed validation and the system substituted a safe
    structured ask.
  - ``recognition_blocker``: the planner emitted a typed
    ``PlannerTurn.recognition_blockers`` hard blocker that converted its
    draft into a clarify before execution.
  - ``execution_clarification``: executor returned ``ExecutionClarification``
    after running catalog resolution.
  - ``planner_route``: planner directly chose ``route='clarify'`` without
    any of the above.
"""


@dataclass(frozen=True)
class TurnTaxonomyTags:
    """Flat, Logfire-friendly tag bundle for one assistant turn."""

    outcome_class: OutcomeClass
    clarify_source: ClarifySource | None = None
    missing_slots: tuple[str, ...] = ()
    entity_resolution_resolved_count: int = 0
    entity_resolution_ambiguous_count: int = 0
    entity_resolution_unresolved_count: int = 0
    entity_resolution_unsupported_count: int = 0
    entity_resolution_total_mentions: int = 0


def classify_turn(
    *,
    turn: PlannerTurn,
    execution_outcome: ExecutionOutcome | None,
    result_row_count: int | None,
) -> TurnTaxonomyTags:
    """Map the assembled turn context to a TurnTaxonomyTags bundle.

    Pure function — does not touch spans, the DB, or session state.
    Uses the ``kind`` discriminator on ``ExecutionOutcome`` rather than
    isinstance so the classifier stays trivially mockable in tests.
    """

    entity_counts = _ZERO_ENTITY_COUNTS
    outcome_kind = _outcome_kind(execution_outcome)

    if turn.route == "direct":
        return TurnTaxonomyTags(
            outcome_class="direct",
            **entity_counts,
        )

    if turn.route == "policy_guidance":
        return TurnTaxonomyTags(
            outcome_class="policy_guidance",
            **entity_counts,
        )

    if turn.route == "publication":
        return TurnTaxonomyTags(
            outcome_class="publication",
            **entity_counts,
        )

    if turn.route == "clarify":
        return TurnTaxonomyTags(
            outcome_class="clarify",
            clarify_source=_clarify_source(turn, outcome_kind),
            missing_slots=_missing_slots(turn.clarification, execution_outcome),
            **entity_counts,
        )

    # route == "execute"
    if outcome_kind == "clarification":
        exec_clar = getattr(execution_outcome, "clarification", None)
        return TurnTaxonomyTags(
            outcome_class="clarify",
            clarify_source="execution_clarification",
            missing_slots=_missing_slots(exec_clar, execution_outcome),
            **entity_counts,
        )
    if outcome_kind == "refusal":
        return TurnTaxonomyTags(
            outcome_class="refusal",
            **entity_counts,
        )
    if outcome_kind == "success":
        if result_row_count is not None and result_row_count == 0:
            return TurnTaxonomyTags(
                outcome_class="no_results",
                **entity_counts,
            )
        return TurnTaxonomyTags(
            outcome_class="executed_ok",
            **entity_counts,
        )

    # execute route with no execution_outcome (orchestration short-circuit
    # before execute ran). Surface as clarify with the typed recognition
    # blocker source if the planner set one, otherwise as planner_route.
    return TurnTaxonomyTags(
        outcome_class="clarify",
        clarify_source=_clarify_source(turn, None),
        missing_slots=_missing_slots(turn.clarification, None),
        **entity_counts,
    )


def _outcome_kind(execution_outcome: ExecutionOutcome | None) -> str | None:
    """Pull the discriminator value off an execution outcome."""
    if execution_outcome is None:
        return None
    return getattr(execution_outcome, "kind", None)


def apply_turn_taxonomy(span: object, tags: TurnTaxonomyTags) -> None:
    """Attach the taxonomy bundle to a Logfire span. Telemetry-only."""

    attrs: dict[str, object] = {
        "taxonomy_outcome_class": tags.outcome_class,
        "taxonomy_missing_slots": ",".join(tags.missing_slots),
        "taxonomy_entity_resolution_resolved_count": tags.entity_resolution_resolved_count,
        "taxonomy_entity_resolution_ambiguous_count": tags.entity_resolution_ambiguous_count,
        "taxonomy_entity_resolution_unresolved_count": tags.entity_resolution_unresolved_count,
        "taxonomy_entity_resolution_unsupported_count": tags.entity_resolution_unsupported_count,
        "taxonomy_entity_resolution_total_mentions": tags.entity_resolution_total_mentions,
    }
    if tags.clarify_source is not None:
        attrs["taxonomy_clarify_source"] = tags.clarify_source
    set_span_attributes(span, **attrs)


def _clarify_source(
    turn: PlannerTurn,
    outcome_kind: str | None,
) -> ClarifySource:
    """Pick the most specific source label that applies.

    Uses typed signals where present:
    - ``ClarificationRequest.is_rescue_fallback`` (W1-08 marker) for
      planner-output-validation fallbacks
    - ``PlannerTurn.recognition_blockers`` (W1-01 marker) for
      recognition-driven clarifies
    - ``isinstance(execution_outcome, ExecutionClarification)`` for
      executor-surfaced ambiguity
    """

    clarification = turn.clarification
    if clarification is not None and clarification.is_rescue_fallback:
        return "rescue_fallback"
    if getattr(turn, "recognition_blockers", None):
        return "recognition_blocker"
    if outcome_kind == "clarification":
        return "execution_clarification"
    return "planner_route"


def _missing_slots(
    clarification: ClarificationRequest | None,
    execution_outcome: ExecutionOutcome | None,
) -> tuple[str, ...]:
    """Pull missing-field labels from whichever clarification surfaced first.

    ``MissingField`` is a ``Literal[...]`` alias in planning.py, so each
    entry is already the string label (e.g. ``"district"``).
    """

    if clarification is not None and clarification.missing_fields:
        return tuple(str(slot) for slot in clarification.missing_fields)
    if _outcome_kind(execution_outcome) == "clarification":
        exec_clar = getattr(execution_outcome, "clarification", None)
        if exec_clar is not None and exec_clar.missing_fields:
            return tuple(str(slot) for slot in exec_clar.missing_fields)
    return ()


# Entity-resolution counts were derived from the planning-recognition
# report, which the pipeline never populated (the advisor was tests-only
# and the orchestration field always held None). The counts therefore stay
# zero; the flat fields remain on TurnTaxonomyTags so the span shape and the
# #968 telemetry contract are unchanged.
_ZERO_ENTITY_COUNTS: dict[str, int] = {
    "entity_resolution_resolved_count": 0,
    "entity_resolution_ambiguous_count": 0,
    "entity_resolution_unresolved_count": 0,
    "entity_resolution_unsupported_count": 0,
    "entity_resolution_total_mentions": 0,
}
