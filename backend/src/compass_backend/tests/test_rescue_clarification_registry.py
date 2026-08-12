"""Tests for the rescue clarification typed registry.

Mirrors `test_methodology_registry.py` shape: exhaustiveness assertion,
plus per-code resolver smoke tests. The registry exists so the chat
orchestrator can construct typed `RescueClarificationRef` values from
typed `QueryContext` fields without inlining f-string prose templates in
`orchestration/`.
"""

from __future__ import annotations

from compass_backend.rendering.rescue_clarification import (
    RescueClarificationRef,
    all_rescue_clarification_codes,
    registered_rescue_clarification_codes,
    rescue_clarification_question_for,
)


def test_registry_is_exhaustive_over_literal() -> None:
    """Every `RescueClarificationCode` Literal value must have a registry entry."""

    declared = all_rescue_clarification_codes()
    registered = registered_rescue_clarification_codes()
    missing = declared - registered
    assert missing == frozenset(), (
        f"RescueClarificationCode values without a registry entry: {sorted(missing)}"
    )


def test_anchored_with_metric_resolver_names_district_count_and_metric() -> None:
    ref = RescueClarificationRef(
        code="anchored_with_metric",
        district_count=12,
        metric_name="Starting salary BA",
    )

    question = rescue_clarification_question_for(ref)

    assert "12" in question
    assert "Starting salary BA" in question
    assert "rephrase" in question.lower()


def test_anchored_no_metric_resolver_omits_metric_name_segment() -> None:
    ref = RescueClarificationRef(
        code="anchored_no_metric",
        district_count=7,
        metric_name=None,
    )

    question = rescue_clarification_question_for(ref)

    assert "7" in question
    # No metric name should appear — fallback used when result_metrics empty.
    assert "[metric name]" in question
    assert "rephrase" in question.lower()


def test_fallback_no_anchor_resolver_offers_supported_patterns() -> None:
    ref = RescueClarificationRef(
        code="fallback_no_anchor",
        district_count=0,
        metric_name=None,
    )

    question = rescue_clarification_question_for(ref)

    # The fallback message should name at least one supported pattern so
    # the user has a concrete rephrase template (M3c-1 acceptance).
    assert "top 10 districts" in question
    assert "compare" in question.lower()
    assert "one metric at a time" not in question
    assert "running each one separately" not in question
    assert "supported query shape" not in question.lower()
    assert "deterministic execution" not in question.lower()


def test_ref_is_immutable() -> None:
    """RescueClarificationRef is frozen — typed payload, no in-place mutation."""

    ref = RescueClarificationRef(code="fallback_no_anchor")
    try:
        ref.district_count = 5  # type: ignore[misc]
    except Exception:
        return  # expected — pydantic frozen
    raise AssertionError("RescueClarificationRef should be frozen")
