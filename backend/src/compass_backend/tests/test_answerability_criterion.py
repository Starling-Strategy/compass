"""Unit tests for the answerability_rescue_fallback_is_failure criterion.

All tests are offline — no DB, no gateway, no Logfire.

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_answerability_criterion.py -x --tb=short
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from compass_backend.db.rows import CriterionRecord
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.criteria import (
    RecordDeterministicEvaluator,
    criterion_record_to_evaluator,
)


def test_context_has_clarification_rescue_origin_field() -> None:
    """CompassEvaluatorContext must carry clarification_rescue_origin."""
    ctx = CompassEvaluatorContext(
        session_id="test-session-id",
        turn_index=1,
        clarification_rescue_origin=True,
    )
    assert ctx.clarification_rescue_origin is True


def test_context_clarification_rescue_origin_defaults_false() -> None:
    """clarification_rescue_origin defaults to False for back-compat."""
    ctx = CompassEvaluatorContext(
        session_id="test-session-id",
        turn_index=1,
    )
    assert ctx.clarification_rescue_origin is False


def _make_chat_response(
    *,
    route: str = "clarify",
    rescue_origin: bool = False,
) -> MagicMock:
    """Minimal ChatResponse stub for _build_context testing."""
    clarification = MagicMock()
    clarification.rescue_origin = rescue_origin

    turn = MagicMock()
    turn.route = route
    turn.query_plan = None
    turn.clarification = clarification if route == "clarify" else None

    session = MagicMock()
    session.session_id = "test-session-id"
    session.turn_count = 1
    session.memory = None

    manifest = MagicMock()
    manifest.metadata = {}

    response = MagicMock()
    response.turn = turn
    response.session = session
    response.result = None
    response.message = "I need one district group..."
    response.trace_id = "abc123"
    response.manifest = manifest

    return response


def test_build_context_sets_rescue_origin_true_on_rescue_clarify() -> None:
    """_build_context propagates rescue_origin=True from the clarification."""
    from compass_backend.quality.verdict_pipeline import _build_context

    response = _make_chat_response(route="clarify", rescue_origin=True)
    ctx = _build_context(response)
    assert ctx.clarification_rescue_origin is True


def test_build_context_sets_rescue_origin_false_on_genuine_clarify() -> None:
    """_build_context propagates rescue_origin=False from the clarification."""
    from compass_backend.quality.verdict_pipeline import _build_context

    response = _make_chat_response(route="clarify", rescue_origin=False)
    ctx = _build_context(response)
    assert ctx.clarification_rescue_origin is False


def test_build_context_sets_rescue_origin_false_on_execute_route() -> None:
    """clarification_rescue_origin is False when route is execute (no clarification)."""
    from compass_backend.quality.verdict_pipeline import _build_context

    response = _make_chat_response(route="execute", rescue_origin=False)
    ctx = _build_context(response)
    assert ctx.clarification_rescue_origin is False


def _answerability_criterion() -> CriterionRecord:
    return CriterionRecord(
        id=200,
        criterion_code="answerability_rescue_fallback_is_failure",
        text="A rescue-origin clarification is always an over-clarify.",
        category="selection_accuracy",
        check_type="deterministic",
        severity="warn",
        priority=10,
        is_mandatory_global=True,
        scenario_ids=[],
        topic_tags=[],
        intent_tags=[],
        payload={
            "validator_name": "answerability_rescue_fallback_is_failure",
            "applicable_routes": ["clarify"],
            "description": "Fails when route=clarify and rescue_origin=True.",
        },
        version_hash="answerability-rescue-fallback-v1",
        active=True,
    )


def _clarify_ctx(*, rescue_origin: bool, route: str = "clarify") -> CompassEvaluatorContext:
    return CompassEvaluatorContext(
        session_id="test-session-id",
        turn_index=1,
        route=route,
        clarification_rescue_origin=rescue_origin,
        triggered_by="sweep_run",
    )


@pytest.mark.asyncio
async def test_answerability_validator_fails_on_rescue_origin() -> None:
    """outcome='fail' (with rescue_origin evidence) when the clarify is rescue-origin."""
    from compass_backend.quality.validators.registry import lookup

    fn = lookup("answerability_rescue_fallback_is_failure")
    assert fn is not None, "validator not registered"
    ctx = _clarify_ctx(rescue_origin=True)
    result = await fn(ctx, {"validator_name": "answerability_rescue_fallback_is_failure"})
    assert result.outcome == "fail"
    assert "rescue fallback" in result.reason.lower()
    assert result.evidence == {"rescue_origin": True}


@pytest.mark.asyncio
async def test_answerability_validator_passes_on_genuine_clarify() -> None:
    """outcome='pass' (with rescue_origin evidence) on a considered planner clarify."""
    from compass_backend.quality.validators.registry import lookup

    fn = lookup("answerability_rescue_fallback_is_failure")
    assert fn is not None, "validator not registered"
    ctx = _clarify_ctx(rescue_origin=False)
    result = await fn(ctx, {"validator_name": "answerability_rescue_fallback_is_failure"})
    assert result.outcome == "pass"
    # Symmetric with the fail branch: pin the pass-branch evidence payload too.
    assert result.evidence == {"rescue_origin": False}


@pytest.mark.asyncio
async def test_answerability_criterion_route_skipped_on_execute() -> None:
    """The applicable_routes=['clarify'] gate skips the criterion on execute turns.

    This pins the load-bearing, inverted-pattern part of the design: a
    mandatory-global criterion scoped to ['clarify'] must NOT run to a vacuous
    'pass' on every execute/direct/policy_guidance turn — the route pre-check in
    evaluate_one_criterion returns a 'not applicable' error verdict (which the
    Scorecard drops from numerator and denominator) before the validator runs.
    The route-skip branch is DB-free and makes no gateway call, so this stays
    offline.
    """
    from compass_backend.quality.verdict_pipeline import evaluate_one_criterion

    ctx = _clarify_ctx(rescue_origin=False, route="execute")
    verdict = await evaluate_one_criterion(ctx=ctx, criterion=_answerability_criterion())
    assert verdict is not None
    assert verdict.outcome == "error"
    assert "not applicable" in verdict.reason.lower()
    assert "clarify" in verdict.reason


def test_criterion_dispatches_to_deterministic_evaluator() -> None:
    """criterion_record_to_evaluator routes the answerability criterion to RecordDeterministicEvaluator."""
    c = _answerability_criterion()
    ev = criterion_record_to_evaluator(c)
    assert isinstance(ev, RecordDeterministicEvaluator)
