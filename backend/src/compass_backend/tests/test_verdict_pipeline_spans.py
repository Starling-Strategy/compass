"""Tests for compass.quality.verdict_pipeline span instrumentation (Tier 2.4 audit).

The L2 verdict pipeline is fire-and-forget background work that previously emitted
zero Logfire signal. When a criterion failed to evaluate, operators only saw a
stdlib log line; verdict-write failures were invisible. This module locks in the
new compass.quality.verdict_pipeline + compass.quality.evaluate_criterion span
contract added in the Tier 2.4 audit.

The tests mock select_criteria_for_turn, criterion_record_to_evaluator, and
write_one so no live DB or LLM is needed — every test runs in <50ms and verifies
exactly what the span carries.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import logfire.testing
import pytest
from pydantic import SecretStr

from compass_backend.config import settings as runtime_settings
from compass_backend.tests._evidence_fixtures import default_evidence_dict
from compass_backend.contracts.chat import ChatResponse
from compass_backend.contracts.planning import (
    DirectResponse,
    PlannerTurn,
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


def _enable_logfire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force settings.logfire_enabled True so compass_span emits real spans."""
    monkeypatch.setattr(
        runtime_settings,
        "logfire_token",
        SecretStr("pylf_v1_us_real_token_for_verdict_test"),
    )


def _fake_session(session_id: str | None = None) -> SessionState:
    sid = session_id or "sess-" + uuid.uuid4().hex[:8]
    now = datetime.now(tz=timezone.utc)
    return SessionState(
        session_id=sid,
        created_at=now,
        updated_at=now,
        turn_count=1,
    )


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.5,
        direct_response=DirectResponse(
            message="Hello.",
            reason="Greeting does not require data.",
        ),
    )


def _make_response(session_id: str = "sess-X", trace_id: str = "trace-X") -> ChatResponse:
    return ChatResponse(
        message="ok",
        turn=_direct_turn(),
        session=_fake_session(session_id),
        snapshot_id="snap-X",
        result=None,
        validation=None,
        manifest=None,
        trace_id=trace_id,
        logfire_url=None,
        message_ids=[],
    )


def _criterion(
    *,
    criterion_code: str,
    criterion_id: int = 1,
    check_type: str = "deterministic",
) -> CriterionRecord:
    return make_criterion_record(
        id=criterion_id,
        criterion_code=criterion_code,
        text="test criterion",
        category="process_integrity",
        check_type=check_type,
        priority=0,
        version_hash="v1",
    )


def _verdict(*, criterion: CriterionRecord, outcome: str, session_id: str) -> VerdictRecord:
    return VerdictRecord(
        session_id=session_id,
        turn_index=1,
        criterion_id=criterion.id,
        criterion_version_hash=criterion.version_hash,
        criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
        judge_source=criterion.check_type,  # type: ignore[arg-type]
        scope="turn",
        outcome=outcome,  # type: ignore[arg-type]
        reason="test verdict",
        evidence=default_evidence_dict(criterion_code=criterion.criterion_code),
        trace_id="trace-X",
        triggered_by="user_turn",
    )


class _StubEvaluator:
    """Returns a predetermined verdict (or raises) when evaluate() is called."""

    def __init__(self, verdict: VerdictRecord | None = None, raise_exc: Exception | None = None):
        self._verdict = verdict
        self._raise_exc = raise_exc

    async def evaluate(self, ctx: Any) -> VerdictRecord:
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._verdict is not None
        return self._verdict


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected: list[CriterionRecord] | Exception,
    evaluators: dict[str, _StubEvaluator] | None = None,
) -> list[VerdictRecord]:
    """Wire mocks for select_criteria_for_turn, criterion_record_to_evaluator, write_one.

    Returns a list that captures every VerdictRecord passed to write_one (for assertion).
    """
    captured: list[VerdictRecord] = []

    async def _fake_select(ctx, pool=None, use_ai_classifier=True, source=None):
        if isinstance(selected, Exception):
            raise selected
        return selected

    def _fake_to_evaluator(crit: CriterionRecord) -> _StubEvaluator:
        if evaluators and crit.criterion_code in evaluators:
            return evaluators[crit.criterion_code]
        # Default: a stub that returns a passing verdict.
        return _StubEvaluator(verdict=_verdict(criterion=crit, outcome="pass", session_id="sess-X"))

    async def _fake_write_one(verdict: VerdictRecord, *, pool=None, source=None) -> str:
        captured.append(verdict)
        return "uuid-" + verdict.criterion_version_hash

    monkeypatch.setattr(vp, "select_criteria_for_turn", _fake_select)
    monkeypatch.setattr(vp, "criterion_record_to_evaluator", _fake_to_evaluator)
    monkeypatch.setattr(vp, "write_one", _fake_write_one)
    return captured


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_pipeline_span_records_completed_outcome_with_aggregates(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compass.quality.verdict_pipeline records outcome=completed + counts by verdict outcome."""
    _enable_logfire(monkeypatch)
    response = _make_response()

    c_pass = _criterion(criterion_code="alpha", criterion_id=1)
    c_fail = _criterion(criterion_code="beta", criterion_id=2)
    _patch_pipeline(
        monkeypatch,
        selected=[c_pass, c_fail],
        evaluators={
            "alpha": _StubEvaluator(
                verdict=_verdict(criterion=c_pass, outcome="pass", session_id=response.session.session_id),
            ),
            "beta": _StubEvaluator(
                verdict=_verdict(criterion=c_fail, outcome="fail", session_id=response.session.session_id),
            ),
        },
    )

    written = await vp.evaluate_and_write(response, pool=object())
    assert len(written.verdict_ids) == 2

    spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.quality.verdict_pipeline"
    ]
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["outcome"] == "completed"
    assert attrs["selected_count"] == 2
    assert attrs["written_count"] == 2
    assert attrs["pass_count"] == 1
    assert attrs["fail_count"] == 1
    assert attrs["error_count"] == 0


@pytest.mark.asyncio
async def test_verdict_pipeline_span_records_no_criteria_selected(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compass.quality.verdict_pipeline carries outcome=no_criteria_selected when classifier picks nothing."""
    _enable_logfire(monkeypatch)
    response = _make_response()
    _patch_pipeline(monkeypatch, selected=[])

    written = await vp.evaluate_and_write(response, pool=object())
    assert written.verdict_ids == []

    spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.quality.verdict_pipeline"
    ]
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["outcome"] == "no_criteria_selected"
    assert attrs["selected_count"] == 0


@pytest.mark.asyncio
async def test_verdict_pipeline_span_records_selection_error(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compass.quality.verdict_pipeline carries outcome=selection_error when classifier raises."""
    _enable_logfire(monkeypatch)
    response = _make_response()
    _patch_pipeline(monkeypatch, selected=RuntimeError("classifier exploded"))

    written = await vp.evaluate_and_write(response, pool=object())
    assert written.verdict_ids == []

    spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.quality.verdict_pipeline"
    ]
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["outcome"] == "selection_error"
    assert attrs["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_evaluate_criterion_sub_spans_carry_per_criterion_outcome(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-criterion compass.quality.evaluate_criterion sub-spans carry criterion_code + outcome."""
    _enable_logfire(monkeypatch)
    response = _make_response()

    c_pass = _criterion(criterion_code="alpha", criterion_id=1, check_type="deterministic")
    c_error = _criterion(criterion_code="beta", criterion_id=2, check_type="judge_prompt")
    _patch_pipeline(
        monkeypatch,
        selected=[c_pass, c_error],
        evaluators={
            "alpha": _StubEvaluator(
                verdict=_verdict(criterion=c_pass, outcome="pass", session_id=response.session.session_id),
            ),
            "beta": _StubEvaluator(raise_exc=ValueError("evaluator boom")),
        },
    )

    written = await vp.evaluate_and_write(response, pool=object())
    # Both verdicts written — the error path writes a synthetic error VerdictRecord.
    assert len(written.verdict_ids) == 2

    sub_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.quality.evaluate_criterion"
    ]
    assert len(sub_spans) == 2

    by_code = {s["attributes"]["criterion_code"]: s["attributes"] for s in sub_spans}
    assert by_code["alpha"]["check_type"] == "deterministic"
    assert by_code["alpha"]["outcome"] == "pass"
    assert by_code["beta"]["check_type"] == "judge_prompt"
    assert by_code["beta"]["outcome"] == "error"
    assert by_code["beta"]["error_type"] == "ValueError"

    # Parent aggregates reflect the per-criterion picture
    parent = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.quality.verdict_pipeline"
    ][0]
    parent_attrs = parent["attributes"]
    assert parent_attrs["pass_count"] == 1
    assert parent_attrs["error_count"] == 1
