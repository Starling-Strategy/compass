"""Regression for #1216 / case 1119 (FEEDBACK-2026-05-ROW-120-DIFFERENTIATED-PAY-SOUTH).

"Do any of the districts in the South offer differentiated pay?" deterministically
returned the canned "I need one district group and one Compass metric to start.
Could you rephrase?" rescue (12/12 on a full-stack N-run). The planner has a governed
snippet for this exact prompt (differentiated-pay-inventory.md), but the "Do ANY …?"
existence framing makes the model deviate; the resulting finalize blocker + retries=0
surfaces as UnexpectedModelBehavior, and the structured-output-failure backstop only
repaired prompts containing "performance pay" — so "differentiated pay" fell through
to the generic rescue.

This pins the governed repair: the backstop now emits the same typed inventory plan
the snippet prescribes (route=execute, lookup over the Census South, the four governed
offer-anchor metrics) instead of the generic rescue.
"""

from compass_backend.orchestration.structured_output_failure import (
    _known_structured_output_failure_turn,
)

DIFF_PAY_SOUTH_PROMPT = "Do any of the districts in the South offer differentiated pay?"

# The four governed offer-anchor metrics, verbatim from
# differentiated-pay-inventory.md (drift-guarded in tests/snippet_metric_names.py).
EXPECTED_METRICS = [
    "Performance pay based on evaluation ratings, unrelated to salary schedule",
    "District offers additional pay for teaching in schools classified as hard-to-staff",
    'District offers additional pay for teaching subjects deemed "hard to staff"',
    "Additional pay for National Board Certification",
]


def test_differentiated_pay_south_repairs_to_execute_inventory() -> None:
    turn = _known_structured_output_failure_turn(DIFF_PAY_SOUTH_PROMPT)
    assert turn is not None, "expected a governed repair, got the generic rescue"
    assert turn.route == "execute"
    assert turn.query_plan is not None
    qp = turn.query_plan
    assert qp.operation == "lookup"
    assert qp.selection.scope == "state"
    assert qp.selection.states == ["the South"]
    assert [m.name for m in qp.metrics] == EXPECTED_METRICS
    assert qp.metrics[0].role == "primary"
    assert all(m.role == "comparison" for m in qp.metrics[1:])
    assert qp.limit is not None and qp.limit.kind == "all"
    assert qp.output.format == "table"


def test_differentiated_pay_count_falls_through_to_generic() -> None:
    # "how many" is a count shape (snippet block phrase) -> no governed repair.
    assert (
        _known_structured_output_failure_turn(
            "How many districts in the South offer differentiated pay?"
        )
        is None
    )


def test_differentiated_pay_data_inventory_falls_through_to_generic() -> None:
    # "what data" is a capability/data-inventory shape -> no governed repair.
    assert (
        _known_structured_output_failure_turn(
            "What data do you have on differentiated pay?"
        )
        is None
    )


def test_non_south_differentiated_pay_not_hardcoded_to_south() -> None:
    # The backstop only governs the South; other geographies must fall through
    # rather than silently forcing Southern scope.
    assert (
        _known_structured_output_failure_turn(
            "Do any districts in the Northeast offer differentiated pay?"
        )
        is None
    )


def test_performance_pay_repair_unchanged() -> None:
    # Byte-for-byte preservation: the existing perf-pay inventory repair still fires.
    turn = _known_structured_output_failure_turn(
        "What data do you have on performance pay?"
    )
    assert turn is not None
    assert turn.route == "direct"
