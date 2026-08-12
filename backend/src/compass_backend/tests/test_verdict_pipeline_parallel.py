"""Regression tests for parallel judge_prompt criteria evaluation.

The pa-eval sweep that ran in ~3 minutes on 2026-05-19 ballooned to 30+
minutes on 2026-05-20 because Phase 1-4 PRs added ~6 new judge_prompt
criteria, each adding a sequential Haiku call to every step. The fix
fans the per-criterion evaluation out under
`asyncio.Semaphore(MAX_CONCURRENT_JUDGES)` (default 8). These tests
lock in the invariants that make the fan-out safe.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

import pytest

from compass_backend.contracts.chat import ChatResponse
from compass_backend.tests._evidence_fixtures import default_evidence_dict
from compass_backend.contracts.planning import (
    MetricSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    TemporalSpec,
)
from compass_backend.contracts.session import SessionState
from compass_backend.quality import verdict_pipeline as vp
from compass_backend.quality.criteria import (
    COMPASS_CRITERIA_SET_VERSION,
    CriterionRecord,
    VerdictRecord,
)
from compass_backend.tests.conftest import make_criterion_record


# ── helpers ───────────────────────────────────────────────────────────────────


def _fake_session(session_id: str | None = None) -> SessionState:
    sid = session_id or "sess-" + uuid.uuid4().hex[:8]
    now = datetime.now(tz=timezone.utc)
    return SessionState(session_id=sid, created_at=now, updated_at=now, turn_count=1)


def _execute_turn() -> PlannerTurn:
    return PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="rank",
            question="rank districts by starting teacher salary",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting teacher salary", role="primary")],
            temporal=TemporalSpec(academic_year="2024-25", intent="current"),
        ),
    )


def _make_response() -> ChatResponse:
    return ChatResponse(
        message="ok",
        turn=_execute_turn(),
        session=_fake_session(),
        snapshot_id="snap-A",
        result=None,
        validation=None,
        manifest=None,
        trace_id="trace-A",
        logfire_url=None,
        message_ids=[],
    )


def _criterion(criterion_code: str) -> CriterionRecord:
    return make_criterion_record(
        id=hash(criterion_code) & 0x7FFFFFFF,
        criterion_code=criterion_code,
        text=f"test {criterion_code}",
        category="process_integrity",
        check_type="judge_prompt",
        priority=10,
        is_mandatory_global=True,
        payload={},
    )


class _SleepyEvaluator:
    """Evaluator that sleeps for `delay_s` seconds, then returns a pass verdict
    stamped with `code` so the test can verify order preservation."""

    def __init__(self, *, code: str, delay_s: float) -> None:
        self.code = code
        self.delay_s = delay_s

    async def evaluate(self, ctx: Any) -> VerdictRecord:
        await asyncio.sleep(self.delay_s)
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            criterion_id=hash(self.code) & 0x7FFFFFFF,
            criterion_version_hash=f"hash-{self.code}-v1",
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source="judge_prompt",
            scope="turn",
            outcome="pass",
            reason=self.code,  # used by tests to verify order
            evidence=default_evidence_dict(),
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
        )


class _RaisingEvaluator:
    """Raises on evaluate() — triggers the synthetic error verdict path."""

    def __init__(self, *, exc_message: str = "boom") -> None:
        self.exc_message = exc_message

    async def evaluate(self, ctx: Any) -> VerdictRecord:
        raise RuntimeError(self.exc_message)


class _ConcurrencyProbe:
    """Shared counter that records peak simultaneously in-flight evaluators.

    Pattern mirrors ``test_catalog_recall_concurrency.py``: instead of timing
    wall-clock against a magic threshold (which flakes under CI load), each
    evaluator increments ``in_flight`` on entry and the probe tracks the peak.
    A serial ``for crit in selected:`` loop peaks at 1; concurrent fan-out
    peaks at the number of dispatched criteria.

    The ``asyncio.Barrier`` makes the proof deterministic rather than relying
    on yields landing in a lucky order: every evaluator must reach the barrier
    before any may proceed, so the peak is recorded only once *all* of them are
    genuinely in flight at the same instant. Under a serial loop the first
    evaluator blocks at the barrier (the others are never dispatched), so the
    pipeline cannot complete — the test bounds that with a short timeout and
    asserts it did not trip.
    """

    def __init__(self, *, parties: int) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self._barrier = asyncio.Barrier(parties)

    async def enter(self) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        # Block until every dispatched evaluator is simultaneously in flight.
        await self._barrier.wait()
        self.in_flight -= 1


class _ProbingEvaluator:
    """Records concurrency via a shared probe, then returns a pass verdict."""

    def __init__(self, *, code: str, probe: _ConcurrencyProbe) -> None:
        self.code = code
        self.probe = probe

    async def evaluate(self, ctx: Any) -> VerdictRecord:
        await self.probe.enter()
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            criterion_id=hash(self.code) & 0x7FFFFFFF,
            criterion_version_hash=f"hash-{self.code}-v1",
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source="judge_prompt",
            scope="turn",
            outcome="pass",
            reason=self.code,
            evidence=default_evidence_dict(),
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
        )


def _patch_pipeline_per_criterion(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected: list[CriterionRecord],
    evaluator_by_code: dict[str, Any],
) -> list[VerdictRecord]:
    """Mock select / to_evaluator / write_one so the test owns the inputs."""
    captured: list[VerdictRecord] = []

    async def _fake_select(ctx, pool=None, use_ai_classifier=True, source=None):
        return selected

    def _fake_to_evaluator(crit: CriterionRecord):
        return evaluator_by_code[crit.criterion_code]

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


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_criteria_evaluate_in_parallel_not_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three criteria must run concurrently, not one-at-a-time.

    This is THE perf regression test. It used to assert a 0.6s wall-clock
    ceiling, which flaked under CI load. It now uses a counter-based
    concurrency probe (same pattern as `test_catalog_recall_concurrency.py`):
    each evaluator records peak simultaneously-in-flight count. A serial
    `for crit in selected:` loop peaks at 1; concurrent fan-out under
    `Semaphore(MAX_CONCURRENT_JUDGES)` peaks at all three. The probe's
    `asyncio.Barrier` makes the result independent of wall-clock timing.
    """
    codes = ["judge_a", "judge_b", "judge_c"]
    selected = [_criterion(c) for c in codes]
    probe = _ConcurrencyProbe(parties=len(codes))
    evaluators = {c: _ProbingEvaluator(code=c, probe=probe) for c in codes}
    _patch_pipeline_per_criterion(
        monkeypatch, selected=selected, evaluator_by_code=evaluators
    )

    # Bound the run so a serial regression (first evaluator deadlocks on the
    # barrier) surfaces as a fast, deterministic failure rather than a hang.
    async with asyncio.timeout(5):
        written = await vp.evaluate_and_write(_make_response(), pool=object())  # type: ignore[arg-type]

    assert len(written.verdict_ids) == 3, "all three verdicts should be written"
    # Serial evaluation peaks at 1 in-flight; concurrent fan-out peaks at three.
    assert probe.max_in_flight == len(codes), (
        f"peak in-flight={probe.max_in_flight} suggests sequential evaluation; "
        f"parallel fan-out should run all {len(codes)} criteria concurrently"
    )


@pytest.mark.asyncio
async def test_gather_preserves_submission_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow criterion first, fast criterion second — verdicts must come back
    in `selected` order, not completion order. Downstream pipeline_span
    attributes depend on this alignment."""
    selected = [_criterion("slow"), _criterion("fast")]
    evaluators = {
        "slow": _SleepyEvaluator(code="slow", delay_s=0.2),
        "fast": _SleepyEvaluator(code="fast", delay_s=0.01),
    }
    captured = _patch_pipeline_per_criterion(
        monkeypatch, selected=selected, evaluator_by_code=evaluators
    )

    await vp.evaluate_and_write(_make_response(), pool=object())  # type: ignore[arg-type]

    # `reason` carries the criterion code (set by _SleepyEvaluator).
    # If as_completed were used, "fast" would precede "slow".
    assert [v.reason for v in captured] == ["slow", "fast"], (
        "verdicts must be written in `selected` order, not completion order"
    )


@pytest.mark.asyncio
async def test_one_evaluator_raising_does_not_block_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One judge raises; the others must still return their verdicts and the
    raising one must produce an `outcome='error'` synthetic verdict — same
    semantics as the pre-parallelization loop."""
    selected = [
        _criterion("ok_1"),
        _criterion("raises"),
        _criterion("ok_2"),
    ]
    evaluators = {
        "ok_1": _SleepyEvaluator(code="ok_1", delay_s=0.01),
        "raises": _RaisingEvaluator(exc_message="judge gateway 500"),
        "ok_2": _SleepyEvaluator(code="ok_2", delay_s=0.01),
    }
    captured = _patch_pipeline_per_criterion(
        monkeypatch, selected=selected, evaluator_by_code=evaluators
    )

    written = await vp.evaluate_and_write(_make_response(), pool=object())  # type: ignore[arg-type]

    assert len(written.verdict_ids) == 3, "every criterion produces a verdict, even on error"
    assert captured[0].outcome == "pass" and captured[0].reason == "ok_1"
    assert captured[1].outcome == "error"
    assert "judge gateway 500" in captured[1].reason
    assert captured[2].outcome == "pass" and captured[2].reason == "ok_2"


@pytest.mark.asyncio
async def test_fire_and_forget_holds_strong_ref_then_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fire_and_forget must register the task in _BACKGROUND_TASKS so the
    event loop's *weak* user-task references can't GC it mid-flight, and
    must auto-remove it via the discard done-callback on completion."""
    gate = asyncio.Event()

    async def _blocked_evaluate(
        response, *, pool=None, source=None, triggered_by=None, user_message=""
    ):
        await gate.wait()
        return []

    monkeypatch.setattr(vp, "evaluate_and_write", _blocked_evaluate)
    # Snapshot to isolate from any leftover state.
    vp._BACKGROUND_TASKS.clear()

    vp.fire_and_forget(_make_response(), pool=object())  # type: ignore[arg-type]

    # While the task is blocked on `gate`, the strong ref must be present.
    assert len(vp._BACKGROUND_TASKS) == 1, (
        "fire_and_forget must hold a strong ref while the task is in-flight"
    )
    (in_flight,) = tuple(vp._BACKGROUND_TASKS)
    assert not in_flight.done()

    # Release the coroutine; the discard done-callback must remove the task.
    gate.set()
    await in_flight  # let the loop schedule callbacks
    # Yield once more so done-callbacks definitely fire on all loops.
    await asyncio.sleep(0)

    assert vp._BACKGROUND_TASKS == set(), (
        "discard done-callback must remove the task from _BACKGROUND_TASKS"
    )


@pytest.mark.asyncio
async def test_semaphore_caps_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If COMPASS_VERDICT_FANOUT=2 then 4 criteria × 0.2s sleeps run as two
    waves of two — expected wall time ~0.4s, not ~0.2s."""
    # Force the module-level cap down by monkeypatching the constant.
    monkeypatch.setattr(vp, "MAX_CONCURRENT_JUDGES", 2)

    delay = 0.2
    codes = ["a", "b", "c", "d"]
    selected = [_criterion(c) for c in codes]
    evaluators = {c: _SleepyEvaluator(code=c, delay_s=delay) for c in codes}
    _patch_pipeline_per_criterion(
        monkeypatch, selected=selected, evaluator_by_code=evaluators
    )

    start = perf_counter()
    await vp.evaluate_and_write(_make_response(), pool=object())  # type: ignore[arg-type]
    elapsed = perf_counter() - start

    # Two waves of two = ~0.4s. Allow 0.35s floor for CI jitter under-shoot
    # (we want this test to fail if the cap is ignored entirely → ~0.2s).
    assert elapsed >= 0.35, (
        f"elapsed={elapsed:.3f}s — Semaphore cap ignored, ran all 4 in parallel"
    )
    # Upper bound catches accidental serial regression: 4 × 0.2 = 0.8s.
    assert elapsed < 0.7, (
        f"elapsed={elapsed:.3f}s suggests serial fallback; expected two waves of two"
    )
