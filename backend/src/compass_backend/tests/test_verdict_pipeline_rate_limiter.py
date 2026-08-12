"""Issue #1277: judge-path gateway rate limiting in the verdict pipeline.

The pa-eval sweep that ended ``partial`` was dominated by a ~566-error
``judge_prompt`` wave, not the chat path. Those judge LLM calls run
in-process inside :func:`evaluate_and_write` (and the criterion evaluators it
fans out under the ``MAX_CONCURRENT_JUDGES`` semaphore — a *concurrency cap*,
not a rate limiter). During a sweep they go through the same Pydantic AI
Gateway key as pa-eval's own ``/chat/simple`` calls and count toward the same
~80 sonnet req/min ceiling, but nothing paced them.

These tests pin the additive, optional ``rate_limiter`` hook that lets pa-eval
thread its :class:`GatewayRateLimiter` into the judge path while leaving the
live L1 path (``rate_limiter=None``) a strict no-op:

  (a) a gateway-calling criterion (``judge_prompt`` / ``scenario_fit``)
      acquires the limiter before its evaluator runs;
  (b) a local-only criterion (``deterministic`` / ``span_assertion``) never
      acquires it (no gateway call to pace);
  (c) the live path (``rate_limiter=None``) is unaffected;
  (d) ``MAX_CONCURRENT_JUDGES`` is env-tunable via
      ``COMPASS_MAX_CONCURRENT_JUDGES`` so sweeps can lower judge concurrency.

All mocks — no live gateway, DB, or backend calls.
"""

from __future__ import annotations

import importlib

import pytest

from compass_backend.quality import verdict_pipeline as vp
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.criteria import (
    COMPASS_CRITERIA_SET_VERSION,
    CriterionRecord,
    VerdictRecord,
)
from compass_backend.quality.verdict_pipeline import evaluate_one_criterion
from compass_backend.tests.conftest import (
    make_criterion_record,
    make_evaluator_context,
)


def _criterion(*, code: str = "test_crit", check_type: str = "judge_prompt") -> CriterionRecord:
    return make_criterion_record(
        id=42,
        criterion_code=code,
        text="test criterion",
        check_type=check_type,
        priority=10,
        version_hash="hash-v1",
    )


def _ctx(*, route: str | None = "execute") -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="sess-A",
        turn_index=1,
        answer_text="The answer.",
        route=route,
        trace_id="trace-A",
        triggered_by="sweep_run",
    )


class _StubEvaluator:
    """Returns a fixed pass verdict and records that it ran."""

    def __init__(self) -> None:
        self.call_count = 0

    async def evaluate(self, ctx: CompassEvaluatorContext) -> VerdictRecord:
        self.call_count += 1
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            criterion_id=42,
            criterion_version_hash="hash-v1",
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source="judge_prompt",
            scope="turn",
            outcome="pass",
            reason="stub reason",
            evidence={
                "answer_excerpt": ctx.answer_text,
                "artifact_snapshot": {},
                "copied_span_attributes": {},
            },  # type: ignore[arg-type]
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
        )


class _RecordingLimiter:
    """Records acquire ordering relative to the evaluator call."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.acquire_count = 0

    async def acquire(self) -> None:
        self.acquire_count += 1
        self.events.append("acquire")


class _OrderingEvaluator(_StubEvaluator):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def evaluate(self, ctx: CompassEvaluatorContext) -> VerdictRecord:
        self.events.append("evaluate")
        return await super().evaluate(ctx)


def _patch_dispatcher(monkeypatch: pytest.MonkeyPatch, evaluator: _StubEvaluator) -> None:
    monkeypatch.setattr(
        "compass_backend.quality.verdict_pipeline.criterion_record_to_evaluator",
        lambda crit: evaluator,
    )


# ── (a) judge path acquires the limiter ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("check_type", ["judge_prompt", "scenario_fit"])
async def test_gateway_criterion_acquires_limiter_before_evaluate(
    monkeypatch: pytest.MonkeyPatch, check_type: str
) -> None:
    """A gateway-calling criterion awaits limiter.acquire() before its evaluator."""
    events: list[str] = []
    evaluator = _OrderingEvaluator(events)
    _patch_dispatcher(monkeypatch, evaluator)
    limiter = _RecordingLimiter(events)

    verdict = await evaluate_one_criterion(
        ctx=_ctx(),
        criterion=_criterion(check_type=check_type),
        rate_limiter=limiter,
    )

    assert verdict is not None and verdict.outcome == "pass"
    assert limiter.acquire_count == 1
    assert events == ["acquire", "evaluate"], (
        f"limiter must be acquired before the evaluator runs; got {events}"
    )


# ── (b) local-only criteria never pace ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("check_type", ["deterministic", "span_assertion"])
async def test_local_criterion_does_not_acquire_limiter(
    monkeypatch: pytest.MonkeyPatch, check_type: str
) -> None:
    """Deterministic / span_assertion criteria make no gateway call → no pacing."""
    events: list[str] = []
    evaluator = _OrderingEvaluator(events)
    _patch_dispatcher(monkeypatch, evaluator)
    limiter = _RecordingLimiter(events)

    verdict = await evaluate_one_criterion(
        ctx=_ctx(),
        criterion=_criterion(check_type=check_type),
        rate_limiter=limiter,
    )

    assert verdict is not None and verdict.outcome == "pass"
    assert limiter.acquire_count == 0, "local-only criterion must not consume a rate slot"
    assert events == ["evaluate"]


@pytest.mark.asyncio
async def test_route_skipped_judge_does_not_acquire_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A judge skipped by applicable_routes makes no gateway call → no pacing."""
    events: list[str] = []
    evaluator = _OrderingEvaluator(events)
    _patch_dispatcher(monkeypatch, evaluator)
    limiter = _RecordingLimiter(events)

    crit = _criterion(check_type="judge_prompt")
    crit.payload.applicable_routes = ["data"]  # ctx.route is "execute" → skipped

    verdict = await evaluate_one_criterion(
        ctx=_ctx(route="execute"),
        criterion=crit,
        rate_limiter=limiter,
    )

    assert verdict is not None and verdict.outcome == "error"
    assert verdict.reason is not None and verdict.reason.startswith("not applicable:")
    assert limiter.acquire_count == 0, "route-skipped judge makes no gateway call"
    assert evaluator.call_count == 0


# ── (c) live path (limiter=None) is unaffected ────────────────────────────────


@pytest.mark.asyncio
async def test_live_path_no_limiter_runs_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rate_limiter defaults to None (live L1) — the judge runs with no pacing."""
    events: list[str] = []
    evaluator = _OrderingEvaluator(events)
    _patch_dispatcher(monkeypatch, evaluator)

    verdict = await evaluate_one_criterion(
        ctx=_ctx(),
        criterion=_criterion(check_type="judge_prompt"),
    )

    assert verdict is not None and verdict.outcome == "pass"
    assert events == ["evaluate"]


@pytest.mark.asyncio
async def test_evaluate_and_write_threads_limiter_into_judges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evaluate_and_write forwards its rate_limiter into each judge criterion."""
    from compass_backend.tests.test_verdict_pipeline_parallel import (
        _make_response,
        _patch_pipeline_per_criterion,
    )

    events: list[str] = []
    codes = ["judge_a", "judge_b"]
    selected = [_criterion(code=c, check_type="judge_prompt") for c in codes]
    evaluators = {c: _OrderingEvaluator(events) for c in codes}
    _patch_pipeline_per_criterion(
        monkeypatch, selected=selected, evaluator_by_code=evaluators
    )
    limiter = _RecordingLimiter(events)

    written = await vp.evaluate_and_write(
        _make_response(),
        pool=object(),  # type: ignore[arg-type]
        rate_limiter=limiter,
    )

    assert len(written.verdict_ids) == 2
    # Two judges → two acquires, each before its evaluator.
    assert limiter.acquire_count == 2


@pytest.mark.asyncio
async def test_evaluate_and_write_default_no_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live path calls evaluate_and_write with no rate_limiter → no pacing."""
    from compass_backend.tests.test_verdict_pipeline_parallel import (
        _make_response,
        _patch_pipeline_per_criterion,
    )

    events: list[str] = []
    selected = [_criterion(code="judge_a", check_type="judge_prompt")]
    evaluators = {"judge_a": _OrderingEvaluator(events)}
    _patch_pipeline_per_criterion(
        monkeypatch, selected=selected, evaluator_by_code=evaluators
    )

    written = await vp.evaluate_and_write(
        _make_response(),
        pool=object(),  # type: ignore[arg-type]
    )

    assert len(written.verdict_ids) == 1
    assert events == ["evaluate"], "live path must not pace the judge"


# ── (d) COMPASS_MAX_CONCURRENT_JUDGES is env-tunable ──────────────────────────


def test_max_concurrent_judges_env_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    """COMPASS_MAX_CONCURRENT_JUDGES lowers the judge concurrency cap on reload."""
    monkeypatch.setenv("COMPASS_MAX_CONCURRENT_JUDGES", "3")
    monkeypatch.delenv("COMPASS_VERDICT_FANOUT", raising=False)
    reloaded = importlib.reload(vp)
    try:
        assert reloaded.MAX_CONCURRENT_JUDGES == 3
    finally:
        monkeypatch.delenv("COMPASS_MAX_CONCURRENT_JUDGES", raising=False)
        importlib.reload(vp)


def test_max_concurrent_judges_legacy_env_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy COMPASS_VERDICT_FANOUT name keeps working for back-compat."""
    monkeypatch.delenv("COMPASS_MAX_CONCURRENT_JUDGES", raising=False)
    monkeypatch.setenv("COMPASS_VERDICT_FANOUT", "5")
    reloaded = importlib.reload(vp)
    try:
        assert reloaded.MAX_CONCURRENT_JUDGES == 5
    finally:
        monkeypatch.delenv("COMPASS_VERDICT_FANOUT", raising=False)
        importlib.reload(vp)
