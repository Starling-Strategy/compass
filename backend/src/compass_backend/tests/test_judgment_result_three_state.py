"""Three-state contract for JudgmentResult.verdict.

Locks in the schema/dispatch surface introduced by the audit-fix that
collapsed ``JudgmentResult.passed: bool`` + ``not_applicable: bool`` into a
single ``verdict: Literal['pass', 'fail', 'not_applicable']`` discriminator.

The mapping that downstream code (RecordJudgmentEvaluator,
RecordScenarioFitEvaluator, JudgmentEvaluator) must implement:

    verdict='pass'           → outcome='pass'
    verdict='fail'           → outcome='fail'
    verdict='not_applicable' → outcome='error' with 'not applicable: ' prefix

Also exercises Pydantic validation:
  - extra fields rejected (model_config = extra='forbid')
  - missing verdict rejected
  - invalid verdict literal rejected

Run with:
    PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_judgment_result_three_state.py -x --tb=short
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.db.rows import CriterionRecord, VerdictRecord
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.criteria import (
    JudgmentResult,
    RecordJudgmentEvaluator,
    criterion_record_to_evaluator,
)
from compass_backend.tests.conftest import (
    make_criterion_record,
    make_evaluator_context,
)


# ─── 0. Field order: reason MUST precede verdict ──────────────────────────────


def test_judgment_result_declares_reason_before_verdict() -> None:
    """``reason`` is declared before ``verdict`` so the judge reasons first.

    pydantic-ai serializes structured output with JSON properties in field
    declaration order. If ``verdict`` comes first the model commits to the
    label before it has written any justification — post-hoc rationalization.
    Declaring ``reason`` first forces the model to articulate the evidence
    before it names the outcome. See #1295.
    """
    fields = list(JudgmentResult.model_fields)
    assert fields.index("reason") < fields.index("verdict"), fields


# ─── 1. Schema-level: the three-state contract ────────────────────────────────


def test_judgment_result_accepts_pass() -> None:
    out = JudgmentResult(verdict="pass", reason="response satisfies rubric")
    assert out.verdict == "pass"


def test_judgment_result_accepts_fail() -> None:
    out = JudgmentResult(verdict="fail", reason="response violates rubric")
    assert out.verdict == "fail"


def test_judgment_result_accepts_not_applicable() -> None:
    out = JudgmentResult(
        verdict="not_applicable",
        reason="no prior context to evaluate against",
    )
    assert out.verdict == "not_applicable"


def test_judgment_result_rejects_missing_verdict() -> None:
    with pytest.raises(ValidationError):
        JudgmentResult(reason="reason text")  # type: ignore[call-arg]


def test_judgment_result_rejects_invalid_verdict_literal() -> None:
    with pytest.raises(ValidationError):
        JudgmentResult(verdict="maybe", reason="reason text")  # type: ignore[arg-type]


def test_judgment_result_rejects_legacy_passed_field() -> None:
    """extra='forbid' rejects the old `passed: bool` keyword.

    A judge implementation still emitting the old shape will fail loudly at
    parse time instead of being silently coerced.
    """
    with pytest.raises(ValidationError):
        JudgmentResult(passed=True, reason="reason text")  # type: ignore[call-arg]


def test_judgment_result_rejects_legacy_not_applicable_field() -> None:
    """extra='forbid' rejects the old `not_applicable: bool` keyword."""
    with pytest.raises(ValidationError):
        JudgmentResult(
            verdict="pass",
            reason="reason text",
            not_applicable=True,  # type: ignore[call-arg]
        )


# ─── 2. Dispatch contract: verdict → VerdictRecord.outcome ────────────────────


_DEFAULT_JUDGE_PAYLOAD = {
    "judge_prompt": "Does the response cite specific source language?",
}


def _make_judge_criterion(criterion_code: str = "test_judge") -> CriterionRecord:
    return make_criterion_record(
        id=42,
        criterion_code=criterion_code,
        category="citation_accuracy",
        check_type="judge_prompt",
        priority=50,
        payload=dict(_DEFAULT_JUDGE_PAYLOAD),
        version_hash="hash-test-judge-v1",
    )


def _make_ctx() -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="11111111-1111-1111-1111-111111111111",
        turn_index=0,
        answer_text="Some assistant answer text.",
    )


class _StubAgent:
    """Stand-in for the judge Agent that returns a fixed JudgmentResult."""

    def __init__(self, output: JudgmentResult) -> None:
        self._output = output

    async def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        class _R:
            pass

        r = _R()
        r.output = self._output  # type: ignore[attr-defined]
        return r


async def _run_with_stub(stub_output: JudgmentResult, monkeypatch) -> VerdictRecord:
    from compass_backend.quality import criteria as criteria_mod

    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "test-key")
    c = _make_judge_criterion()
    ev = criterion_record_to_evaluator(c)
    assert isinstance(ev, RecordJudgmentEvaluator)

    original = criteria_mod._JUDGE_AGENT
    criteria_mod._JUDGE_AGENT = _StubAgent(stub_output)  # type: ignore[assignment]
    try:
        return await ev.evaluate(_make_ctx())
    finally:
        criteria_mod._JUDGE_AGENT = original


@pytest.mark.asyncio
async def test_verdict_pass_maps_to_outcome_pass(monkeypatch) -> None:
    verdict_record = await _run_with_stub(
        JudgmentResult(verdict="pass", reason="cited source language"),
        monkeypatch,
    )
    assert verdict_record.outcome == "pass"
    assert verdict_record.reason == "cited source language"
    assert verdict_record.evidence.model_dump()["score"] == 1.0


@pytest.mark.asyncio
async def test_verdict_fail_maps_to_outcome_fail(monkeypatch) -> None:
    verdict_record = await _run_with_stub(
        JudgmentResult(verdict="fail", reason="paraphrased without citation"),
        monkeypatch,
    )
    assert verdict_record.outcome == "fail"
    assert verdict_record.reason == "paraphrased without citation"
    assert verdict_record.evidence.model_dump()["score"] == 0.0


@pytest.mark.asyncio
async def test_verdict_not_applicable_maps_to_outcome_error_with_prefix(
    monkeypatch,
) -> None:
    """verdict='not_applicable' → outcome='error' with 'not applicable:' prefix.

    The prefix is load-bearing: Scorecard's _is_excluded_from_score parses it
    to drop the trial from both numerator and denominator.
    """
    verdict_record = await _run_with_stub(
        JudgmentResult(
            verdict="not_applicable",
            reason="this turn has no prior context to evaluate",
        ),
        monkeypatch,
    )
    assert verdict_record.outcome == "error"
    assert verdict_record.reason.startswith("not applicable:")
    assert "this turn has no prior context" in verdict_record.reason
    assert verdict_record.evidence.model_dump()["applicability"] == "skipped"
