"""Unit tests for verdict_pipeline.evaluate_one_criterion (Phase 3a).

The helper extracted from the closure inside evaluate_and_write so L1
(live chat), L2 (pa-eval sweep), and L3 (pa-eval replay-criterion) all
go through the same dispatcher. Previously the L1/L2 logic lived as an
inner async closure with no module-level handle, so these branches were
not directly testable.

Covers:

- Route-skip path: `applicable_routes` excludes ctx.route → returns a
  "not applicable" error verdict without calling the evaluator.
- Happy path: evaluator returns a verdict → helper passes it through.
- Evaluator-raises path: evaluator throws → helper catches, returns a
  structured error VerdictRecord.
- Routes gate is inert when ctx.route is None (replay-path shape).
"""

from __future__ import annotations


import pytest

from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.criteria import (
    COMPASS_CRITERIA_SET_VERSION,
    CriterionRecord,
    VerdictRecord,
)
from compass_backend.quality.verdict_pipeline import evaluate_one_criterion
from compass_backend.tests.conftest import (
    _DEFAULT_CRITERION_PAYLOADS,
    make_criterion_record,
    make_evaluator_context,
)


def _criterion(
    *,
    code: str = "test_crit",
    check_type: str = "judge_prompt",
    applicable_routes: list[str] | None = None,
    payload: dict | None = None,
) -> CriterionRecord:
    base_payload = dict(payload or _DEFAULT_CRITERION_PAYLOADS[check_type])
    if applicable_routes is not None:
        base_payload["applicable_routes"] = applicable_routes
    return make_criterion_record(
        id=42,
        criterion_code=code,
        text="test criterion",
        category="accuracy",
        check_type=check_type,
        priority=10,
        payload=base_payload,
        version_hash="hash-v1",
    )


def _ctx(*, route: str | None = None) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="sess-A",
        turn_index=1,
        answer_text="The answer.",
        route=route,
        trace_id="trace-A",
    )


class _StubEvaluator:
    """Records call count; returns a fixed VerdictRecord on evaluate()."""

    def __init__(self, *, outcome: str = "pass", raises: Exception | None = None):
        self.outcome = outcome
        self.raises = raises
        self.call_count = 0

    async def evaluate(self, ctx: CompassEvaluatorContext) -> VerdictRecord:
        self.call_count += 1
        if self.raises is not None:
            raise self.raises
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            criterion_id=42,
            criterion_version_hash="hash-v1",
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source="judge_prompt",
            scope="turn",
            outcome=self.outcome,  # type: ignore[arg-type]
            reason="stub reason",
            evidence={
                "answer_excerpt": ctx.answer_text,
                "artifact_snapshot": {},
                "copied_span_attributes": {},
            },  # type: ignore[arg-type]
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
        )


def _patch_dispatcher(monkeypatch: pytest.MonkeyPatch, evaluator: _StubEvaluator) -> None:
    monkeypatch.setattr(
        "compass_backend.quality.verdict_pipeline.criterion_record_to_evaluator",
        lambda crit: evaluator,
    )


# ── route gating ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_gated_returns_not_applicable_without_evaluator_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A criterion declaring applicable_routes=['data'] returns 'not applicable'
    on a clarify-route ctx and never invokes the evaluator."""
    evaluator = _StubEvaluator()
    _patch_dispatcher(monkeypatch, evaluator)

    crit = _criterion(applicable_routes=["data", "execute"])
    ctx = _ctx(route="clarify")

    verdict = await evaluate_one_criterion(ctx=ctx, criterion=crit)

    assert verdict is not None
    assert verdict.outcome == "error"
    assert verdict.reason is not None and verdict.reason.startswith("not applicable:")
    assert "clarify" in verdict.reason
    assert evaluator.call_count == 0  # evaluator never invoked


@pytest.mark.asyncio
async def test_route_gate_inert_when_ctx_route_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replay path constructs a ctx with route=None. The gate must let
    that ctx through to the evaluator — guarding only fires when ctx.route
    is a concrete value that doesn't match applicable_routes."""
    evaluator = _StubEvaluator(outcome="pass")
    _patch_dispatcher(monkeypatch, evaluator)

    crit = _criterion(applicable_routes=["data"])
    ctx = _ctx(route=None)

    verdict = await evaluate_one_criterion(ctx=ctx, criterion=crit)

    assert verdict is not None
    assert verdict.outcome == "pass"
    assert evaluator.call_count == 1


@pytest.mark.asyncio
async def test_route_in_applicable_set_runs_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = _StubEvaluator(outcome="pass")
    _patch_dispatcher(monkeypatch, evaluator)

    crit = _criterion(applicable_routes=["execute", "clarify"])
    ctx = _ctx(route="execute")

    verdict = await evaluate_one_criterion(ctx=ctx, criterion=crit)

    assert verdict is not None
    assert verdict.outcome == "pass"
    assert evaluator.call_count == 1


# ── happy path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_returns_evaluator_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = _StubEvaluator(outcome="fail")
    _patch_dispatcher(monkeypatch, evaluator)

    crit = _criterion()
    ctx = _ctx(route="execute")

    verdict = await evaluate_one_criterion(ctx=ctx, criterion=crit)

    assert verdict is not None
    assert verdict.outcome == "fail"
    assert verdict.reason == "stub reason"
    assert evaluator.call_count == 1


# ── evaluator raises ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluator_exception_returns_error_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = _StubEvaluator(raises=RuntimeError("judge boom"))
    _patch_dispatcher(monkeypatch, evaluator)

    crit = _criterion()
    ctx = _ctx(route="execute")

    verdict = await evaluate_one_criterion(ctx=ctx, criterion=crit)

    assert verdict is not None
    assert verdict.outcome == "error"
    assert verdict.reason is not None
    assert "evaluator raised" in verdict.reason
    assert "RuntimeError" in verdict.reason
    assert "judge boom" in verdict.reason
    assert evaluator.call_count == 1
