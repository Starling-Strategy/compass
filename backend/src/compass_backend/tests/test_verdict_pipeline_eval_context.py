"""Tests for EvalContext threading in verdict_pipeline.evaluate_and_write.

Task 1.2: EvalContext lets pa-eval bake sweep metadata (sweep_run_id,
scenario_id, case_id, step_index) into VerdictRecords at write time,
eliminating the post-write UPDATE stamp.

Two behaviours under test:
  A. With eval_context  → VerdictRecords carry sweep metadata.
  B. Without eval_context → VerdictRecords default to None (live-chat path).

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_verdict_pipeline_eval_context.py -x --tb=short
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from compass_backend.contracts.chat import ChatResponse
from compass_backend.contracts.planning import (
    DirectResponse,
    PlannerTurn,
)
from compass_backend.contracts.session import SessionState
from compass_backend.db.rows import ScenarioExpectation
from compass_backend.tests._evidence_fixtures import default_evidence_dict
from compass_backend.quality import verdict_pipeline as vp
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.criteria import (
    COMPASS_CRITERIA_SET_VERSION,
    CriterionRecord,
    VerdictRecord,
)
from compass_backend.quality.verdict_pipeline import EvalContext
from compass_backend.tests.conftest import make_criterion_record


# ── helpers ───────────────────────────────────────────────────────────────────


def _fake_session(session_id: str | None = None) -> SessionState:
    sid = session_id or "sess-" + uuid.uuid4().hex[:8]
    now = datetime.now(tz=timezone.utc)
    return SessionState(session_id=sid, created_at=now, updated_at=now, turn_count=1)


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.5,
        direct_response=DirectResponse(message="Hi.", reason="greeting"),
    )


def _make_response(*, session_id: str = "sess-eval-ctx") -> ChatResponse:
    return ChatResponse(
        message="ok",
        turn=_direct_turn(),
        session=_fake_session(session_id),
        snapshot_id="snap-eval-ctx",
        result=None,
        validation=None,
        manifest=None,
        trace_id="trace-eval-ctx",
        logfire_url=None,
        message_ids=[],
    )


def _criterion(criterion_code: str = "test_criterion") -> CriterionRecord:
    return make_criterion_record(
        id=42,
        criterion_code=criterion_code,
        text="test criterion",
        category="process_integrity",
        priority=10,
        is_mandatory_global=True,
    )


class _StubEvaluator:
    """Returns a passing VerdictRecord; preserves ctx fields needed for assertions."""

    async def evaluate(self, ctx: Any) -> VerdictRecord:
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            step_index=ctx.step_index,
            case_id=ctx.case_id,
            scenario_id=ctx.scenario_id,
            criterion_id=42,
            criterion_version_hash="hash-test_criterion-v1",
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source="deterministic",
            scope="turn",
            outcome="pass",
            reason="stub pass",
            evidence=default_evidence_dict(),
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
            sweep_run_id=ctx.sweep_run_id,
        )


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected: list[CriterionRecord],
    evaluator: _StubEvaluator,
) -> list[VerdictRecord]:
    """Patch select_criteria_for_turn, criterion_record_to_evaluator, write_one."""
    captured: list[VerdictRecord] = []

    async def _fake_select(ctx, pool=None, use_ai_classifier=True, source=None):
        return selected

    def _fake_to_evaluator(crit: CriterionRecord) -> _StubEvaluator:
        return evaluator

    async def _fake_write_one(verdict: VerdictRecord, *, pool=None, source=None) -> str:
        captured.append(verdict)
        return "uuid-" + verdict.criterion_version_hash

    async def _fake_pool(source=None):
        return object()

    monkeypatch.setattr(vp, "select_criteria_for_turn", _fake_select)
    monkeypatch.setattr(vp, "criterion_record_to_evaluator", _fake_to_evaluator)
    monkeypatch.setattr(vp, "write_one", _fake_write_one)
    monkeypatch.setattr(vp, "_get_or_create_pool", _fake_pool)
    return captured


# ── Test A: With eval_context → sweep metadata baked into VerdictRecords ──────


@pytest.mark.asyncio
async def test_eval_context_populates_verdict_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VerdictRecords produced under an EvalContext carry all four sweep fields.

    This is the pa-eval path: sweep_run_id, scenario_id, case_id, step_index
    must all appear on every VerdictRecord written so no post-write UPDATE is
    needed.
    """
    crit = _criterion("sort_accuracy")
    evaluator = _StubEvaluator()
    captured = _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    ctx = EvalContext(
        sweep_run_id="run-abc-123",
        scenario_id=7,
        case_id=42,
        step_index=2,
    )
    response = _make_response()
    await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="sweep_run",
        eval_context=ctx,
    )

    assert len(captured) == 1, "exactly one verdict written"
    v = captured[0]
    assert v.sweep_run_id == "run-abc-123", "sweep_run_id must come from EvalContext"
    assert v.scenario_id == 7, "scenario_id must come from EvalContext"
    assert v.case_id == 42, "case_id must come from EvalContext"
    assert v.step_index == 2, "step_index must come from EvalContext"


# ── Test B: Without eval_context → fields default to None (live-chat path) ────


@pytest.mark.asyncio
async def test_no_eval_context_leaves_sweep_fields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VerdictRecords produced without EvalContext have None sweep fields.

    This is the live /chat/simple path via fire_and_forget: no eval_context
    is passed, so sweep_run_id, scenario_id, case_id are None. step_index
    is also None in this path (VerdictRecord default).
    """
    crit = _criterion("sort_accuracy")
    evaluator = _StubEvaluator()
    captured = _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    response = _make_response()
    await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="user_turn",
        # eval_context omitted — default None
    )

    assert len(captured) == 1, "exactly one verdict written"
    v = captured[0]
    assert v.sweep_run_id is None, "no EvalContext → sweep_run_id must be None"
    assert v.scenario_id is None, "no EvalContext → scenario_id must be None"
    assert v.case_id is None, "no EvalContext → case_id must be None"
    # step_index may be None (VerdictRecord default) — confirm no bogus value injected
    assert v.step_index is None, "no EvalContext → step_index must be None"


# ── Test C: EvalContext default step_index=0 ──────────────────────────────────


@pytest.mark.asyncio
async def test_eval_context_step_index_defaults_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EvalContext.step_index defaults to 0 when not specified.

    Single-step scenarios don't need to pass step_index explicitly.
    """
    crit = _criterion("sort_accuracy")
    evaluator = _StubEvaluator()
    captured = _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    ctx = EvalContext(
        sweep_run_id="run-xyz",
        scenario_id=1,
        case_id=10,
        # step_index omitted — should default to 0
    )
    response = _make_response()
    await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="sweep_run",
        eval_context=ctx,
    )

    assert len(captured) == 1
    assert captured[0].step_index == 0, "EvalContext.step_index default is 0"


# ── Helpers for expected_output tests ─────────────────────────────────────────


class _CtxCapturingEvaluator:
    """Records the CompassEvaluatorContext passed to evaluate(); returns a pass verdict."""

    def __init__(self) -> None:
        self.captured_ctx: CompassEvaluatorContext | None = None

    async def evaluate(self, ctx: CompassEvaluatorContext) -> VerdictRecord:
        self.captured_ctx = ctx
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            step_index=ctx.step_index,
            case_id=ctx.case_id,
            scenario_id=ctx.scenario_id,
            criterion_id=42,
            criterion_version_hash="hash-test_criterion-v1",
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source="deterministic",
            scope="turn",
            outcome="pass",
            reason="stub pass",
            evidence=default_evidence_dict(),
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
            sweep_run_id=ctx.sweep_run_id,
        )


def _patch_pipeline_with_ctx_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected: list[CriterionRecord],
    ctx_evaluator: _CtxCapturingEvaluator,
) -> None:
    """Patch pipeline internals; evaluator captures context instead of returning verdicts."""

    async def _fake_select(ctx, pool=None, use_ai_classifier=True, source=None):
        return selected

    def _fake_to_evaluator(crit: CriterionRecord) -> _CtxCapturingEvaluator:
        return ctx_evaluator

    async def _fake_write_one(verdict: VerdictRecord, *, pool=None, source=None) -> str:
        return "uuid-" + verdict.criterion_version_hash

    async def _fake_pool(source=None):
        return object()

    monkeypatch.setattr(vp, "select_criteria_for_turn", _fake_select)
    monkeypatch.setattr(vp, "criterion_record_to_evaluator", _fake_to_evaluator)
    monkeypatch.setattr(vp, "write_one", _fake_write_one)
    monkeypatch.setattr(vp, "_get_or_create_pool", _fake_pool)


# ── Test D: expected_output populates CompassEvaluatorContext.expected_output ──


@pytest.mark.asyncio
async def test_expected_output_populates_context_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evaluate_and_write forwards expected_output into CompassEvaluatorContext.

    When the pa-eval runner passes expected_output=ScenarioExpectation(...),
    the evaluator receives a ctx where ctx.expected_output.expected_behaviour
    matches what was supplied — ready for the scenario_fit evaluator (Task 5.3).
    """
    crit = _criterion("scenario_fit")
    ctx_evaluator = _CtxCapturingEvaluator()
    _patch_pipeline_with_ctx_capture(monkeypatch, selected=[crit], ctx_evaluator=ctx_evaluator)

    expectation = ScenarioExpectation(expected_behaviour="foo", steps=[])
    response = _make_response()
    await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="sweep_run",
        expected_output=expectation,
    )

    assert ctx_evaluator.captured_ctx is not None, "evaluator was not called"
    ctx = ctx_evaluator.captured_ctx
    assert ctx.expected_output is not None, "expected_output must not be None"
    assert ctx.expected_output.expected_behaviour == "foo"


# ── Test E: no expected_output leaves context field None ──────────────────────


@pytest.mark.asyncio
async def test_no_expected_output_leaves_field_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evaluate_and_write without expected_output leaves ctx.expected_output as None.

    This is the live fire_and_forget path: no expected_output is passed, so
    CompassEvaluatorContext.expected_output stays None. Existing evaluators
    that never read this field are unaffected.
    """
    crit = _criterion("sort_accuracy")
    ctx_evaluator = _CtxCapturingEvaluator()
    _patch_pipeline_with_ctx_capture(monkeypatch, selected=[crit], ctx_evaluator=ctx_evaluator)

    response = _make_response()
    await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="user_turn",
        # expected_output omitted — default None
    )

    assert ctx_evaluator.captured_ctx is not None, "evaluator was not called"
    assert ctx_evaluator.captured_ctx.expected_output is None
