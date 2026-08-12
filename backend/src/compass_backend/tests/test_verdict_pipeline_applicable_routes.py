"""Tests for the applicable_routes pre-check in verdict_pipeline.evaluate_and_write.

A criterion can declare `payload.applicable_routes: list[str]` to opt out of
routes where it's never meaningful. When the current turn's route isn't in
the list, the pipeline writes a "not applicable" verdict and skips evaluator
dispatch entirely — including the judge LLM call for judge_prompt criteria.

Closes the cross-dimension over-selection pattern observed in the 2026-05-18
fresh sweep:
  * `execution_dispatched_for_execute_route` (span_assertion) firing on
    clarify / direct / policy_guidance turns (no execution span by design)
  * `uncertainty_acknowledgment` (judge_prompt) firing on clarify turns
    (asking for clarification IS the correct response, not a failure)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from compass_backend.contracts.chat import ChatResponse
from compass_backend.tests._evidence_fixtures import default_evidence_dict
from compass_backend.contracts.planning import (
    ClarificationRequest,
    DirectResponse,
    MetricSpec,
    PlannerTurn,
    PolicyGuidancePlan,
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


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.5,
        direct_response=DirectResponse(
            message="Hi.", reason="greeting, no data needed"
        ),
    )


def _clarify_turn() -> PlannerTurn:
    return PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question="Which year?",
            missing_fields=["time_period"],
        ),
    )


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


def _policy_guidance_turn() -> PlannerTurn:
    return PlannerTurn(
        route="policy_guidance",
        confidence=0.85,
        policy_guidance=PolicyGuidancePlan(
            topic_ids=["parental-leave"],
            layers=["stances"],
            intent_summary="surface NCTQ's stance on parental leave",
        ),
    )


def _make_response(*, turn: PlannerTurn, session_id: str = "sess-A") -> ChatResponse:
    return ChatResponse(
        message="ok",
        turn=turn,
        session=_fake_session(session_id),
        snapshot_id="snap-A",
        result=None,
        validation=None,
        manifest=None,
        trace_id="trace-A",
        logfire_url=None,
        message_ids=[],
    )


def _criterion(
    *,
    criterion_code: str,
    check_type: str = "span_assertion",
    payload: dict | None = None,
) -> CriterionRecord:
    extra = {} if payload is None else {"payload": payload}
    return make_criterion_record(
        id=99,
        criterion_code=criterion_code,
        text="test criterion",
        category="process_integrity",
        check_type=check_type,
        priority=10,
        is_mandatory_global=True,
        **extra,
    )


class _StubEvaluator:
    """Records whether evaluate() was called; returns a passing verdict if it is."""

    def __init__(self) -> None:
        self.called = False

    async def evaluate(self, ctx: Any) -> VerdictRecord:
        self.called = True
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            criterion_id=99,
            criterion_version_hash="hash-X",
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source="span_assertion",
            scope="turn",
            outcome="pass",
            reason="stub pass",
            evidence=default_evidence_dict(),
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
        )


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected: list[CriterionRecord],
    evaluator: _StubEvaluator,
) -> list[VerdictRecord]:
    """Mock select_criteria_for_turn, criterion_record_to_evaluator, write_one."""
    captured: list[VerdictRecord] = []

    async def _fake_select(ctx, pool=None, use_ai_classifier=True, source=None):
        return selected

    def _fake_to_evaluator(crit: CriterionRecord) -> _StubEvaluator:
        return evaluator

    async def _fake_write_one(verdict: VerdictRecord, *, pool=None, source=None) -> str:
        captured.append(verdict)
        return "uuid-" + verdict.criterion_version_hash

    async def _fake_pool(source=None):
        return object()  # sentinel; never used downstream

    monkeypatch.setattr(vp, "select_criteria_for_turn", _fake_select)
    monkeypatch.setattr(vp, "criterion_record_to_evaluator", _fake_to_evaluator)
    monkeypatch.setattr(vp, "write_one", _fake_write_one)
    monkeypatch.setattr(vp, "_get_or_create_pool", _fake_pool)
    return captured


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_applicable_routes_skips_evaluator_when_route_not_in_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion with applicable_routes=['execute'] on a clarify turn:
    pre-check writes "not applicable" verdict, evaluator NOT called."""
    crit = _criterion(
        criterion_code="execution_dispatched_for_execute_route",
        payload={"applicable_routes": ["execute"]},
    )
    evaluator = _StubEvaluator()
    captured = _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    response = _make_response(turn=_clarify_turn())
    written = await vp.evaluate_and_write(response, pool=object())  # type: ignore[arg-type]

    assert len(captured) == 1, "exactly one verdict should be written"
    assert evaluator.called is False, "evaluator must NOT be called when route excluded"
    v = captured[0]
    assert v.outcome == "error"
    assert v.reason.startswith("not applicable: route is 'clarify'")
    assert "applicable_routes" in v.evidence.model_dump()
    assert len(written.verdict_ids) == 1


@pytest.mark.asyncio
async def test_applicable_routes_skips_evaluator_for_direct_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same shape, different excluded route: applicable=['execute'] on direct → skip."""
    crit = _criterion(
        criterion_code="execution_dispatched_for_execute_route",
        payload={"applicable_routes": ["execute"]},
    )
    evaluator = _StubEvaluator()
    captured = _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    response = _make_response(turn=_direct_turn())
    await vp.evaluate_and_write(response, pool=object())  # type: ignore[arg-type]

    assert evaluator.called is False
    assert captured[0].outcome == "error"
    assert captured[0].reason.startswith("not applicable: route is 'direct'")


@pytest.mark.asyncio
async def test_applicable_routes_skips_evaluator_for_policy_guidance_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """applicable_routes=['execute'] on a policy_guidance turn → skip.

    Locks in the contract that fixed the 2026-05-19 staging incident: pre-PR-#674
    code wrote outcome='fail' for every policy_guidance turn because the
    execution span was correctly absent; post-#674 the pre-check writes
    "not applicable" and skips the evaluator entirely.
    """
    crit = _criterion(
        criterion_code="execution_dispatched_for_execute_route",
        payload={"applicable_routes": ["execute"]},
    )
    evaluator = _StubEvaluator()
    captured = _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    response = _make_response(turn=_policy_guidance_turn())
    await vp.evaluate_and_write(response, pool=object())  # type: ignore[arg-type]

    assert evaluator.called is False
    assert captured[0].outcome == "error"
    assert captured[0].reason.startswith("not applicable: route is 'policy_guidance'")


@pytest.mark.asyncio
async def test_applicable_routes_invokes_evaluator_when_route_in_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion with applicable_routes=['execute'] on an execute turn:
    pre-check passes, evaluator IS called normally."""
    crit = _criterion(
        criterion_code="execution_dispatched_for_execute_route",
        payload={"applicable_routes": ["execute"]},
    )
    evaluator = _StubEvaluator()
    captured = _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    response = _make_response(turn=_execute_turn())
    await vp.evaluate_and_write(response, pool=object())  # type: ignore[arg-type]

    assert evaluator.called is True, "evaluator must run on applicable route"
    assert captured[0].outcome == "pass", "stub evaluator returned pass"


@pytest.mark.asyncio
async def test_applicable_routes_unset_invokes_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A criterion without applicable_routes runs on every turn (backwards compat)."""
    crit = _criterion(criterion_code="legacy_always_fires", payload={})
    evaluator = _StubEvaluator()
    _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    response = _make_response(turn=_clarify_turn())
    await vp.evaluate_and_write(response, pool=object())  # type: ignore[arg-type]

    assert evaluator.called is True, "no applicable_routes ⇒ legacy unconditional fire"


@pytest.mark.asyncio
async def test_applicable_routes_multi_route_includes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """applicable_routes=['execute','direct','policy_guidance'] runs on direct."""
    crit = _criterion(
        criterion_code="uncertainty_acknowledgment",
        check_type="judge_prompt",
        payload={"applicable_routes": ["execute", "direct", "policy_guidance"]},
    )
    evaluator = _StubEvaluator()
    _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    response = _make_response(turn=_direct_turn())
    await vp.evaluate_and_write(response, pool=object())  # type: ignore[arg-type]

    assert evaluator.called is True, "direct is in the applicable set"


@pytest.mark.asyncio
async def test_applicable_routes_multi_route_includes_policy_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """applicable_routes=['execute','direct','policy_guidance'] runs on policy_guidance.

    Positive companion to the direct-includes test above and to the
    policy_guidance-excludes test added in PR #681. Closes the orthogonal
    coverage cell: same multi-route list, a different route in the set.
    """
    crit = _criterion(
        criterion_code="uncertainty_acknowledgment",
        check_type="judge_prompt",
        payload={"applicable_routes": ["execute", "direct", "policy_guidance"]},
    )
    evaluator = _StubEvaluator()
    _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    response = _make_response(turn=_policy_guidance_turn())
    await vp.evaluate_and_write(response, pool=object())  # type: ignore[arg-type]

    assert evaluator.called is True, "policy_guidance is in the applicable set"


@pytest.mark.asyncio
async def test_applicable_routes_multi_route_excludes_clarify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """applicable_routes=['execute','direct','policy_guidance'] skips clarify."""
    crit = _criterion(
        criterion_code="uncertainty_acknowledgment",
        check_type="judge_prompt",
        payload={"applicable_routes": ["execute", "direct", "policy_guidance"]},
    )
    evaluator = _StubEvaluator()
    captured = _patch_pipeline(monkeypatch, selected=[crit], evaluator=evaluator)

    response = _make_response(turn=_clarify_turn())
    await vp.evaluate_and_write(response, pool=object())  # type: ignore[arg-type]

    assert evaluator.called is False, "clarify is excluded; judge LLM call NOT made"
    assert captured[0].reason.startswith("not applicable: route is 'clarify'")
