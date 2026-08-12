"""Tests for check_types filter in verdict_pipeline.evaluate_and_write.

Task 4.1: COMPASS_VERDICT_CHECK_TYPES env var + --check-types CLI flag.

Three behaviours under test:
  A. check_types=('deterministic',)  -> only deterministic criteria fire.
  B. check_types=None                -> all criteria fire (today's behavior).
  C. check_types=()                  -> treated as None, all criteria fire.

Plus unit tests for _resolve_check_type_filter (the env-var reader in verdict_pipeline).

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_verdict_pipeline_check_types.py -x --tb=short
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
        direct_response=DirectResponse(message="Hi.", reason="greeting"),
    )


def _make_response(*, session_id: str = "sess-check-types") -> ChatResponse:
    return ChatResponse(
        message="ok",
        turn=_direct_turn(),
        session=_fake_session(session_id),
        snapshot_id="snap-check-types",
        result=None,
        validation=None,
        manifest=None,
        trace_id="trace-check-types",
        logfire_url=None,
        message_ids=[],
    )


def _criterion(
    criterion_code: str,
    check_type: str = "deterministic",
    criterion_id: int = 1,
) -> CriterionRecord:
    return make_criterion_record(
        id=criterion_id,
        criterion_code=criterion_code,
        text=f"test criterion {criterion_code}",
        category="process_integrity",
        check_type=check_type,
        priority=10,
        is_mandatory_global=True,
    )


class _StubEvaluator:
    """Returns a passing VerdictRecord; records which criterion_code was called."""

    def __init__(self, crit: CriterionRecord) -> None:
        self._crit = crit

    async def evaluate(self, ctx: Any) -> VerdictRecord:
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            step_index=ctx.step_index,
            case_id=ctx.case_id,
            scenario_id=ctx.scenario_id,
            criterion_id=self._crit.id,
            criterion_version_hash=self._crit.version_hash,
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source=self._crit.check_type,
            scope="scenario_fit" if self._crit.check_type == "scenario_fit" else "turn",
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
) -> list[VerdictRecord]:
    """Patch select_criteria_for_turn, criterion_record_to_evaluator, write_one."""
    captured: list[VerdictRecord] = []

    async def _fake_select(ctx, pool=None, use_ai_classifier=True, source=None):
        return selected

    def _fake_to_evaluator(crit: CriterionRecord) -> _StubEvaluator:
        return _StubEvaluator(crit)

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


# Build a mixed criteria list: one of each check_type.
def _mixed_criteria() -> list[CriterionRecord]:
    return [
        _criterion("det_check", check_type="deterministic", criterion_id=1),
        _criterion("judge_check", check_type="judge_prompt", criterion_id=2),
        _criterion("span_check", check_type="span_assertion", criterion_id=3),
    ]


# ── Test A: check_types filter includes only matching ─────────────────────────


@pytest.mark.asyncio
async def test_check_types_filter_includes_only_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the deterministic criterion should fire when check_types=('deterministic',).

    The criteria list has one of each check_type. With the filter set to
    ('deterministic',), only the deterministic criterion's verdict is written.
    """
    criteria = _mixed_criteria()
    captured = _patch_pipeline(monkeypatch, selected=criteria)

    response = _make_response()
    verdict_ids = await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="sweep_run",
        check_types=("deterministic",),
    )

    assert len(verdict_ids.verdict_ids) == 1, (
        f"Expected exactly 1 verdict (deterministic only), got {len(verdict_ids.verdict_ids)}"
    )
    assert len(captured) == 1, f"Expected 1 written verdict, got {len(captured)}"
    assert captured[0].criterion_id == 1, (
        f"Expected criterion_id=1 (det_check), got {captured[0].criterion_id}"
    )


# ── Test B: check_types=None includes all ─────────────────────────────────────


@pytest.mark.asyncio
async def test_check_types_none_includes_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 3 criteria fire when check_types=None (today's behavior preserved)."""
    criteria = _mixed_criteria()
    captured = _patch_pipeline(monkeypatch, selected=criteria)

    response = _make_response()
    verdict_ids = await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="sweep_run",
        check_types=None,
    )

    assert len(verdict_ids.verdict_ids) == 3, (
        f"Expected 3 verdicts (all types), got {len(verdict_ids.verdict_ids)}"
    )
    assert len(captured) == 3, f"Expected 3 written verdicts, got {len(captured)}"
    written_ids = {v.criterion_id for v in captured}
    assert written_ids == {1, 2, 3}, (
        f"Expected criterion_ids {{1,2,3}}, got {written_ids}"
    )


# ── Test C: check_types=() treated as None ────────────────────────────────────


@pytest.mark.asyncio
async def test_check_types_empty_tuple_treated_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit check_types=() behaves identically to None — all criteria fire.

    An empty tuple means "no constraint" (the caller resolved nothing),
    not "filter to zero". The `if check_types:` guard treats () as falsy.
    """
    criteria = _mixed_criteria()
    captured = _patch_pipeline(monkeypatch, selected=criteria)

    response = _make_response()
    verdict_ids = await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="sweep_run",
        check_types=(),
    )

    assert len(verdict_ids.verdict_ids) == 3, (
        f"Expected 3 verdicts (empty tuple = no filter), got {len(verdict_ids.verdict_ids)}"
    )
    assert len(captured) == 3, f"Expected 3 written verdicts, got {len(captured)}"


# ── Scenario fit enrollment: case-level and explicit only ─────────────────────


@pytest.mark.asyncio
async def test_scenario_fit_opt_in_enrolls_active_criterion_with_expected_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L3 can ask for scenario_fit explicitly, even though it is not mandatory-global."""
    selected = [_criterion("det_check", check_type="deterministic", criterion_id=1)]
    scenario_fit = _criterion(
        "scenario_fit_check",
        check_type="scenario_fit",
        criterion_id=4,
    )
    captured = _patch_pipeline(monkeypatch, selected=selected)

    class _Repo:
        async def load_active(self) -> list[CriterionRecord]:
            return [scenario_fit]

    monkeypatch.setattr(vp, "ReadOnlyCriteriaRepository", lambda pool, source=None: _Repo())

    response = _make_response()
    verdict_ids = await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="sweep_run",
        check_types=("scenario_fit",),
        expected_output=ScenarioExpectation(
            expected_behaviour="The answer should satisfy this case.",
            steps=[],
        ),
    )

    assert len(verdict_ids.verdict_ids) == 1
    assert len(captured) == 1
    assert captured[0].criterion_id == 4
    assert captured[0].judge_source == "scenario_fit"
    assert captured[0].scope == "scenario_fit"


@pytest.mark.asyncio
async def test_scenario_fit_records_not_applicable_for_structural_diagnostic_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structural-diagnostic FOUNDATION case records a not_applicable verdict (#939).

    End-to-end at the evaluate_and_write boundary: the pipeline loads the case
    metadata at scenario_fit enrollment, and the real RecordScenarioFitEvaluator
    sees `category == structural_diagnostic_dimension` and emits a recorded
    `not applicable:` verdict (outcome='error') WITHOUT calling the judge. This
    proves the wiring (case_metadata hand-off) and the gate together.
    """
    selected = [_criterion("det_check", check_type="deterministic", criterion_id=1)]
    scenario_fit = _criterion(
        "scenario_fit_check",
        check_type="scenario_fit",
        criterion_id=4,
    )

    # Capture writes; use the REAL evaluator dispatch so the gate runs. The
    # deterministic stub criterion is dropped by the check_types=('scenario_fit',)
    # filter, so only the scenario_fit criterion reaches an evaluator.
    captured: list[VerdictRecord] = []

    async def _fake_select(ctx, pool=None, use_ai_classifier=True, source=None):
        return selected

    async def _fake_write_one(verdict: VerdictRecord, *, pool=None, source=None) -> str:
        captured.append(verdict)
        return "uuid-" + verdict.criterion_version_hash

    async def _fake_pool(source=None):
        return object()

    monkeypatch.setattr(vp, "select_criteria_for_turn", _fake_select)
    monkeypatch.setattr(vp, "write_one", _fake_write_one)
    monkeypatch.setattr(vp, "_get_or_create_pool", _fake_pool)

    class _CritRepo:
        async def load_active(self) -> list[CriterionRecord]:
            return [scenario_fit]

    class _CaseRepo:
        async def load_case_metadata_by_id(self, case_id: int) -> dict:
            return {
                "category": "Process Integrity",
                "structural_diagnostic_dimension": "Process Integrity",
                "structural_diagnostic_archetype": "contract_shape",
                "scorecard_dimension": "Selection Accuracy",
            }

    monkeypatch.setattr(vp, "ReadOnlyCriteriaRepository", lambda pool, source=None: _CritRepo())
    monkeypatch.setattr(vp, "ReadOnlyScenariosV2Repository", lambda pool: _CaseRepo())

    response = _make_response()
    result = await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="sweep_run",
        eval_context=vp.EvalContext(
            sweep_run_id="sweep-sd-1",
            scenario_id=3,
            case_id=1055,
            step_index=None,
        ),
        check_types=("scenario_fit",),
        expected_output=ScenarioExpectation(
            expected_behaviour=(
                "The route, result type, artifact payload, and persisted "
                "verdict contract agree."
            ),
            steps=[],
        ),
    )

    # Exactly one verdict — the recorded not_applicable for scenario_fit.
    assert len(captured) == 1, f"Expected 1 written verdict, got {len(captured)}"
    verdict = captured[0]
    assert verdict.criterion_id == 4
    assert verdict.judge_source == "scenario_fit"
    assert verdict.scope == "scenario_fit"
    assert verdict.outcome == "error"
    assert verdict.reason.startswith("not applicable:")
    assert "structural diagnostic" in verdict.reason.lower()
    # The verdict is recorded (written), not skipped.
    assert len(result.verdict_ids) == 1
    assert result.outcome_counts.get("error") == 1


@pytest.mark.asyncio
async def test_scenario_fit_not_enrolled_without_expected_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scenario_fit check request without case expectations writes no verdicts."""
    selected = [_criterion("det_check", check_type="deterministic", criterion_id=1)]
    captured = _patch_pipeline(monkeypatch, selected=selected)

    def _unexpected_repo(pool, source=None):  # pragma: no cover - only called on regression
        raise AssertionError("scenario_fit repository lookup should require expected_output")

    monkeypatch.setattr(vp, "ReadOnlyCriteriaRepository", _unexpected_repo)

    response = _make_response()
    verdict_ids = await vp.evaluate_and_write(
        response,
        pool=object(),  # type: ignore[arg-type]
        triggered_by="sweep_run",
        check_types=("scenario_fit",),
        expected_output=None,
    )

    assert verdict_ids.verdict_ids == []
    assert captured == []


# ── Unit tests: _resolve_check_type_filter ───────────────────────────────────
#
# Tests the module-level env-var resolver in verdict_pipeline (distinct from the
# CLI's _resolve_check_types which raises typer.BadParameter for unknowns).


def test_resolve_check_type_filter_unset_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var unset → None (all check_types run)."""
    monkeypatch.delenv("COMPASS_VERDICT_CHECK_TYPES", raising=False)
    result = vp._resolve_check_type_filter()
    assert result is None, f"Expected None for unset env var, got {result!r}"


def test_resolve_check_type_filter_empty_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var set to empty string → None (all check_types run)."""
    monkeypatch.setenv("COMPASS_VERDICT_CHECK_TYPES", "")
    result = vp._resolve_check_type_filter()
    assert result is None, f"Expected None for empty env var, got {result!r}"


def test_resolve_check_type_filter_deterministic_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var 'deterministic' → ('deterministic',)."""
    monkeypatch.setenv("COMPASS_VERDICT_CHECK_TYPES", "deterministic")
    result = vp._resolve_check_type_filter()
    assert result == ("deterministic",), (
        f"Expected ('deterministic',), got {result!r}"
    )


def test_resolve_check_type_filter_multiple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var 'deterministic,judge_prompt' → sorted tuple of both."""
    monkeypatch.setenv("COMPASS_VERDICT_CHECK_TYPES", "deterministic,judge_prompt")
    result = vp._resolve_check_type_filter()
    assert result == ("deterministic", "judge_prompt"), (
        f"Expected ('deterministic', 'judge_prompt'), got {result!r}"
    )


def test_resolve_check_type_filter_unknown_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown values are silently dropped; known values pass through."""
    monkeypatch.setenv("COMPASS_VERDICT_CHECK_TYPES", "deterministic,bogus")
    result = vp._resolve_check_type_filter()
    assert result == ("deterministic",), (
        f"Expected ('deterministic',) after dropping 'bogus', got {result!r}"
    )


def test_resolve_check_type_filter_all_unknown_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All-unknown values → None (treat as unfiltered rather than 'block all')."""
    monkeypatch.setenv("COMPASS_VERDICT_CHECK_TYPES", "bogus,also_bogus")
    result = vp._resolve_check_type_filter()
    assert result is None, (
        f"Expected None when all values are unknown, got {result!r}"
    )


def test_resolve_check_type_filter_whitespace_tolerant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace around values is stripped; both types resolve correctly."""
    monkeypatch.setenv("COMPASS_VERDICT_CHECK_TYPES", " deterministic , judge_prompt ")
    result = vp._resolve_check_type_filter()
    assert result == ("deterministic", "judge_prompt"), (
        f"Expected ('deterministic', 'judge_prompt') after whitespace stripping, "
        f"got {result!r}"
    )
