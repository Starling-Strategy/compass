"""Structured-output failure recovery for chat-turn orchestration.

When Pydantic AI exhausts the planner's output-validation budget it raises
``UnexpectedModelBehavior``; this module detects that case and synthesizes a
controlled clarification (or a governed repair for known launch-scope prompts)
so the turn degrades gracefully instead of erroring. Extracted verbatim from
`orchestration/chat.py` (#1130 decomposition). The performance-pay request
classifiers and metric-name constants travel with this cluster because they
are private dependencies of ``_known_structured_output_failure_turn`` and are
referenced nowhere else. Behaviour-preserving move — no logic changes.
"""

from pydantic_ai.exceptions import UnexpectedModelBehavior

from compass_backend.contracts import (
    ClarificationRequest,
    PendingQueryContext,
)
from compass_backend.contracts.planning import (
    DirectResponse,
    FilterSpec,
    LimitSpec,
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    SortSpec,
)
from compass_backend.rendering.rescue_clarification import (
    RescueClarificationRef,
    rescue_clarification_question_for,
)
from compass_backend.orchestration.turn_context import _normalize_user_text

def _is_planner_structured_output_failure(exc: UnexpectedModelBehavior) -> bool:
    """Return true for Pydantic AI structured planner output exhaustion.

    The exhaustion message changed across Pydantic AI versions: pre-1.99 said
    "Exceeded maximum retries (N) for output validation"; >=1.99 says
    "Exceeded maximum output retries (N)". Match either so the planner's
    output-budget-exhaustion path keeps converting to a rescue clarification.
    """

    msg = str(exc).lower()
    return "output validation" in msg or "output retries" in msg


_PERFORMANCE_PAY_MAX_BONUS_METRIC = (
    "Maximum annual performance pay bonus, if eligible"
)
_PERFORMANCE_PAY_MIN_BONUS_METRIC = (
    "Minimum annual performance pay bonus, if eligible"
)

# Differentiated-pay inventory offer-anchors (#1216). Verbatim from
# differentiated-pay-inventory.md; drift-guarded in tests/snippet_metric_names.py.
_DIFF_PAY_PERFORMANCE_RATINGS_METRIC = (
    "Performance pay based on evaluation ratings, unrelated to salary schedule"
)
_DIFF_PAY_HARD_SCHOOL_METRIC = (
    "District offers additional pay for teaching in schools classified as "
    "hard-to-staff"
)
_DIFF_PAY_HARD_SUBJECT_METRIC = (
    'District offers additional pay for teaching subjects deemed "hard to staff"'
)
_DIFF_PAY_NATIONAL_BOARD_METRIC = "Additional pay for National Board Certification"


def _planner_structured_output_failure_turn(
    message: str | None = None,
) -> PlannerTurn:
    """Return a controlled clarification when planner output is malformed.

    W1-08 (#848): prose composed via the typed rescue-clarification
    registry; ``is_rescue_fallback=True`` lets the downstream
    enrichment guard recognize this turn without prose comparison.
    """

    repaired = _known_structured_output_failure_turn(message)
    if repaired is not None:
        return repaired

    fallback_question = rescue_clarification_question_for(
        RescueClarificationRef(
            code="fallback_no_anchor",
            district_count=0,
            metric_name=None,
        )
    )
    rescue_missing_fields = ["comparison_group", "metric"]
    return PlannerTurn(
        route="clarify",
        confidence=0.0,
        clarification=ClarificationRequest(
            question=fallback_question,
            missing_fields=rescue_missing_fields,
            is_rescue_fallback=True,
            # #1613: durable over-clarify marker that survives shape-guard
            # enrichment (which clears is_rescue_fallback). Both must be set
            # here on the raw fallback turn.
            rescue_origin=True,
            pending_context=PendingQueryContext(
                missing_fields=rescue_missing_fields,
            ),
        ),
    )


def _known_structured_output_failure_turn(
    message: str | None,
) -> PlannerTurn | None:
    """Repair high-confidence governed prompts after structured-output failure.

    This runs only after Pydantic AI rejects the planner output shape. The
    phrases below mirror governed planner guidance and emit the same typed
    plans the planner is instructed to produce, so known launch-scope prompts
    do not degrade into a generic clarification solely because the model
    returned malformed JSON.
    """

    normalized = _normalize_user_text(message)
    if not normalized:
        return None

    # #1216: governed differentiated-pay inventory repair (mirrors the
    # DIFFERENTIATED_PAY_INVENTORY snippet). The "Do any ...?" existence framing
    # makes the model deviate from the delivered recipe, so without this the turn
    # degrades to the generic rescue (the perf-pay repair below only covers
    # "performance pay"). Scoped to the governed Census-South region; the
    # executor expands "the South" to its member states.
    if _is_differentiated_pay_inventory_request(normalized):
        selection = _differentiated_pay_south_selection(normalized)
        if selection is not None:
            return PlannerTurn(
                route="execute",
                confidence=0.86,
                query_plan=QueryPlan(
                    operation="lookup",
                    question=(
                        message
                        or "Find Southern districts offering differentiated pay."
                    ),
                    selection=selection,
                    metrics=[
                        MetricSpec(
                            name=_DIFF_PAY_PERFORMANCE_RATINGS_METRIC,
                            role="primary",
                        ),
                        MetricSpec(
                            name=_DIFF_PAY_HARD_SCHOOL_METRIC, role="comparison"
                        ),
                        MetricSpec(
                            name=_DIFF_PAY_HARD_SUBJECT_METRIC, role="comparison"
                        ),
                        MetricSpec(
                            name=_DIFF_PAY_NATIONAL_BOARD_METRIC, role="comparison"
                        ),
                    ],
                    limit=LimitSpec(kind="all"),
                    output=OutputSpec(format="table"),
                ),
            )

    if "performance pay" not in normalized:
        return None

    if _is_performance_pay_inventory_request(normalized):
        return PlannerTurn(
            route="direct",
            confidence=0.9,
            direct_response=DirectResponse(
                reason="governed performance pay data inventory",
                message=(
                    "Compass has reviewed district data for performance pay "
                    "bonus amounts, including "
                    f"{_PERFORMANCE_PAY_MIN_BONUS_METRIC} and "
                    f"{_PERFORMANCE_PAY_MAX_BONUS_METRIC}. You can ask Compass "
                    "to rank districts by maximum annual performance pay bonus, "
                    "filter for substantial bonuses, compare named districts, "
                    "or narrow the results by state or region."
                ),
            ),
        )

    if _is_real_performance_pay_threshold_request(normalized):
        return PlannerTurn(
            route="execute",
            confidence=0.86,
            query_plan=QueryPlan(
                operation="lookup",
                question=message or "Find districts with real performance pay.",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=[MetricSpec(name=_PERFORMANCE_PAY_MAX_BONUS_METRIC)],
                filters=[
                    FilterSpec(
                        field=_PERFORMANCE_PAY_MAX_BONUS_METRIC,
                        operator="greater_than_or_equal",
                        value=5000,
                        threshold_hint="real (not token)",
                    )
                ],
                sort=SortSpec(
                    field=_PERFORMANCE_PAY_MAX_BONUS_METRIC,
                    direction="desc",
                ),
                output=OutputSpec(format="table"),
                count_kind="threshold_count",
            ),
        )

    return None

def _is_performance_pay_inventory_request(normalized: str) -> bool:
    return any(
        phrase in normalized
        for phrase in (
            "what data do you have",
            "what info do you have",
            "what information do you have",
            "what data is available",
            "what data have you got",
        )
    )


def _is_differentiated_pay_inventory_request(normalized: str) -> bool:
    """Mirror the DIFFERENTIATED_PAY_INVENTORY snippet trigger/block contract.

    True for the unspecified differentiated-pay *family* question, so the
    structured-output-failure backstop emits the same governed inventory plan
    the planner snippet prescribes instead of the generic rescue when the model
    deviates on the "Do any ...?" framing (#1216). Trigger + block phrases are
    kept identical to instruction_snippets.DIFFERENTIATED_PAY_INVENTORY so the
    floor and the snippet agree on what counts as the inventory shape.
    """

    if not any(
        trigger in normalized
        for trigger in ("differentiated pay", "differentiated-pay")
    ):
        return False
    return not any(
        blocked in normalized
        for blocked in (
            "what data",
            "what info",
            "what information",
            "how many",
            "count",
        )
    )


def _differentiated_pay_south_selection(normalized: str) -> SelectionSpec | None:
    """Scope the differentiated-pay backstop to the governed Census-South region.

    Only the South is governed here so the backstop never hardcodes Southern
    scope onto a differently-named region; the executor expands the governed
    "the South" phrase to its member states. Other geographies fall through to
    the generic rescue (unchanged behaviour) rather than being mis-scoped.
    """

    if "south" in normalized:
        return SelectionSpec(scope="state", states=["the South"])
    return None


def _is_real_performance_pay_threshold_request(normalized: str) -> bool:
    return (
        "real" in normalized
        and (
            "token" in normalized
            or "not just" in normalized
            or "substantial" in normalized
        )
    )
