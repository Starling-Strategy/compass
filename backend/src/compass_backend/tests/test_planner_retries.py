"""Tests for the planner output_retries=0 variance fix (Track 4.2 / C12).

When output_retries > 0, the Pydantic AI framework can silently re-roll the
planner at temperature=0.0, allowing different valid QueryPlans to be emitted
across calls.  Setting output_retries=0 means the FIRST validator failure
immediately surfaces as an ``UnexpectedModelBehavior`` exception, which
``orchestration/chat.py`` converts into the polite
``_planner_structured_output_failure_turn()`` clarification.

See: ``docs/implementation-notes/pr-a-variance-fix-pending-context-20260520.md``
"""

from __future__ import annotations


from compass_backend.orchestration.chat import _planner_structured_output_failure_turn
from compass_backend.planning.planner import create_planner_agent


# ---------------------------------------------------------------------------
# Test 1 — Doctrinal lock-in: output_retries must be 0
# ---------------------------------------------------------------------------


def test_planner_output_retries_is_zero() -> None:
    """Variance fix: create_planner_agent must use output_retries=0.

    At temperature=0.0 the LLM can still emit different valid QueryPlans on
    retry; both pass validators; only the final emission reaches the user.
    Setting retries=0 forces ModelRetry to surface as user-visible
    clarification rather than silent re-emission.
    """
    agent = create_planner_agent("test")
    # Pydantic AI >=1.99 renamed the internal _max_result_retries ->
    # _max_output_retries (the output-side retry budget). Same intent: it must
    # be 0 so the first output-validator ModelRetry surfaces immediately.
    assert agent._max_output_retries == 0, (
        "planner output retry budget must be 0 to prevent silent variance; "
        f"got {agent._max_output_retries!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — When output_retries=0, the failure path emits a clarification turn
# ---------------------------------------------------------------------------


def test_model_retry_surfaces_as_clarification_function() -> None:
    """_planner_structured_output_failure_turn returns route='clarify' with confidence=0.0.

    With output_retries=0, any ModelRetry raised by the output validator
    exhausts the retry budget immediately and raises UnexpectedModelBehavior.
    orchestration/chat.py catches that exception and calls
    _planner_structured_output_failure_turn() to produce a user-facing
    clarification instead of an error.  This test asserts that the
    clarification turn has the expected shape.
    """
    turn = _planner_structured_output_failure_turn()
    assert turn.route == "clarify", (
        f"Expected route='clarify', got route={turn.route!r}"
    )
    assert turn.confidence == 0.0, (
        f"Expected confidence=0.0, got confidence={turn.confidence!r}"
    )
    assert turn.clarification is not None, (
        "Expected clarification payload to be set on the failure turn"
    )
