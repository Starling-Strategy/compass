"""Execution-stage helpers for chat-turn orchestration.

Pure helpers that bracket plan execution: tagging the root span with the
Move-0 turn-failure taxonomy (observability only) and deciding whether a
multi-metric validation failure should pivot to a typed clarification.
Extracted verbatim from `orchestration/chat.py` (#1130 decomposition). The
execute/render/validate/policy-guidance orchestrators that drive this stage
stay in `chat` because they call ``render_response`` / ``validate_result`` /
``get_library`` / ``set_span_attributes`` — names tests monkeypatch on the
chat-module namespace. Behaviour-preserving move — no logic changes.
"""

from compass_backend.contracts.planning import QueryPlan
from compass_backend.contracts.validation import ValidationReport
from compass_backend.turn_taxonomy import apply_turn_taxonomy, classify_turn

from compass_backend.orchestration.result_diagnostics import _result_rows
from compass_backend.orchestration.turn_context import _TurnContext

def _tag_turn_failure_taxonomy(ctx: _TurnContext, turn_span) -> None:
    """Attach the Move-0 turn-failure taxonomy to the root span.

    Pure observability — wrapped in try/except because telemetry must
    never fail a chat turn. See compass_backend/turn_taxonomy.py.
    """

    if ctx.turn is None:
        return
    try:
        row_count: int | None
        if ctx.result is not None:
            row_count = len(_result_rows(ctx.result))
        else:
            row_count = None
        tags = classify_turn(
            turn=ctx.turn,
            execution_outcome=ctx.executed_outcome,
            result_row_count=row_count,
        )
        apply_turn_taxonomy(turn_span, tags)
    except Exception:
        # Telemetry-only. Never fail a turn for a tagging issue.
        return



def _validation_failure_offers_clarification(
    plan: QueryPlan,
    validation: ValidationReport,
) -> bool:
    """Return True when this validation failure should pivot to typed clarify.

    M3 #1013: a multi-metric ranking/lookup ran, the executor returned a
    result, but the validator rejected the multi-metric shape. The default
    path falls through to the generic "validation failed before rendering"
    body — exactly the M3 failure mode the `preserves_prior_context` rubric
    flags. Offering the user a concrete choice between intersection / union
    / separate tables matches the planner prompt's documented vocabulary.

    The pivot only applies when the plan carries 2+ execution metrics. For
    single-metric failures there is no meaningful render choice to offer
    and the generic prose stays as a debugging surface.
    """
    if validation.valid:
        return False
    return (
        len([m for m in plan.metrics if (m.role or "primary") != "grouping"])
        >= 2
    )
