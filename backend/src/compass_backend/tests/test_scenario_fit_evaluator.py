"""Unit tests for RecordScenarioFitEvaluator (PR 5 Task 5.3).

Covers:
  1. pass when judge returns passed=True
  2. fail when judge returns passed=False
  3. error when ctx.expected_output is None
  4. error when payload is missing judge_prompt_template
  5. prompt includes rendered step text in order
  6. criterion_record_to_evaluator dispatches scenario_fit to RecordScenarioFitEvaluator
  7. judge exception produces error verdict with exception message

All tests are offline — the judge agent is always stubbed.

Run with:
    PYTHONPATH=src uv run pytest \\
        src/compass_backend/tests/test_scenario_fit_evaluator.py -x --tb=short
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from compass_backend.db.rows import CriterionRecord, ScenarioExpectation, StepExpectation
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.criteria import (
    JudgmentResult,
    criterion_record_to_evaluator,
)
from compass_backend.quality.scenario_fit import RecordScenarioFitEvaluator
from compass_backend.tests.conftest import (
    make_criterion_record,
    make_evaluator_context,
)


# ─── Shared fixtures ──────────────────────────────────────────────────────────


_TEMPLATE = (
    "Does the answer satisfy the expectation?\n\n"
    "EXPECTED BEHAVIOUR:\n{expected_behaviour}\n\n"
    "EXPECTED STEPS:\n{expected_steps}\n\n"
    "ACTUAL ANSWER:\n{answer_text}"
)


def _criterion(
    *,
    criterion_code: str = "SCENARIO_FIT_001",
    payload: dict | None = None,
) -> CriterionRecord:
    return make_criterion_record(
        id=101,
        criterion_code=criterion_code,
        text="Scenario fit criterion",
        category="scenario_fit",
        check_type="scenario_fit",
        priority=50,
        is_mandatory_global=True,
        payload=payload if payload is not None else {"judge_prompt_template": _TEMPLATE},
        version_hash="scenario-fit-v1",
    )


def _ctx(
    *,
    expected_output: ScenarioExpectation | None = None,
    answer_text: str = "The district policy is X.",
    case_transcript: list[dict[str, str]] | None = None,
    case_metadata: dict | None = None,
) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="aaaa1111-aaaa-1111-aaaa-111111111111",
        turn_index=0,
        step_index=0,
        case_id=7,
        scenario_id=3,
        answer_text=answer_text,
        triggered_by="sweep_run",
        sweep_run_id="sweep-abc-123",
        expected_output=expected_output,
        case_transcript=case_transcript or [],
        case_metadata=case_metadata or {},
    )


def _fake_agent_result(*, passed: bool, reason: str = "matches") -> MagicMock:
    """Build a fake agent result whose .output is a JudgmentResult."""
    result = MagicMock()
    result.output = JudgmentResult(
        verdict="pass" if passed else "fail",
        reason=reason,
    )
    return result


# ─── Test 1: pass when judge returns passed=True ─────────────────────────────


@pytest.mark.asyncio
async def test_scenario_fit_pass_when_judge_returns_pass() -> None:
    """outcome='pass' and judge_source='scenario_fit' when agent returns passed=True."""
    c = _criterion()
    ev = RecordScenarioFitEvaluator(criterion=c)
    ctx = _ctx(
        expected_output=ScenarioExpectation(
            expected_behaviour="Explain the district's teacher evaluation policy.",
            steps=[],
        )
    )

    with (
        patch.dict("os.environ", {"PYDANTIC_AI_GATEWAY_API_KEY": "test-key"}),
        patch(
            "compass_backend.quality.criteria._judge_agent",
            return_value=AsyncMock(
                run=AsyncMock(return_value=_fake_agent_result(passed=True, reason="matches expected"))
            ),
        ),
    ):
        verdict = await ev.evaluate(ctx)

    assert verdict.outcome == "pass"
    assert verdict.judge_source == "scenario_fit"
    assert verdict.scope == "scenario_fit"
    assert verdict.criterion_id == 101
    assert verdict.criterion_version_hash == "scenario-fit-v1"
    assert verdict.case_id == 7
    assert verdict.scenario_id == 3
    assert verdict.sweep_run_id == "sweep-abc-123"


# ─── Test 2: fail when judge returns passed=False ─────────────────────────────


@pytest.mark.asyncio
async def test_scenario_fit_fail_when_judge_returns_fail() -> None:
    """outcome='fail' when agent returns passed=False."""
    c = _criterion()
    ev = RecordScenarioFitEvaluator(criterion=c)
    ctx = _ctx(
        expected_output=ScenarioExpectation(
            expected_behaviour="Explain teacher evaluation policy.",
            steps=[],
        )
    )

    with (
        patch.dict("os.environ", {"PYDANTIC_AI_GATEWAY_API_KEY": "test-key"}),
        patch(
            "compass_backend.quality.criteria._judge_agent",
            return_value=AsyncMock(
                run=AsyncMock(
                    return_value=_fake_agent_result(
                        passed=False,
                        reason="misses critical element about tenure",
                    )
                )
            ),
        ),
    ):
        verdict = await ev.evaluate(ctx)

    assert verdict.outcome == "fail"
    assert verdict.judge_source == "scenario_fit"
    assert "misses critical element" in verdict.reason


# ─── Test 3: error when expected_output is None ──────────────────────────────


@pytest.mark.asyncio
async def test_scenario_fit_error_when_expected_output_is_none() -> None:
    """outcome='error' with 'no expected_output' reason when ctx.expected_output is None."""
    c = _criterion()
    ev = RecordScenarioFitEvaluator(criterion=c)
    ctx = _ctx(expected_output=None)

    verdict = await ev.evaluate(ctx)

    assert verdict.outcome == "error"
    assert verdict.judge_source == "scenario_fit"
    assert "no expected_output" in verdict.reason
    assert verdict.evidence.model_dump()["skip_reason"] == "no_expected_output"


# ─── Test 4: missing judge_prompt_template now fails at load time ─────────────


def test_scenario_fit_criterion_with_missing_template_fails_at_construction() -> None:
    """Replaces the old 'missing judge_prompt_template → runtime error verdict' contract.

    With the typed CriterionPayload union, a scenario_fit criterion that ships
    with no judge_prompt_template raises ValidationError at row load rather
    than producing 'misconfigured' error verdicts on every evaluation. The
    coverage is also exercised by
    tests/test_criterion_payload_types.py::test_scenario_fit_with_missing_judge_prompt_template_fails.
    """
    with pytest.raises(ValueError):  # pydantic.ValidationError is a ValueError subclass
        _criterion(payload={})


# ─── Test 5: prompt includes rendered steps in order ─────────────────────────


@pytest.mark.asyncio
async def test_scenario_fit_renders_steps_in_prompt() -> None:
    """The interpolated prompt sent to the judge includes 'Step N: ...' lines in order."""
    c = _criterion()
    ev = RecordScenarioFitEvaluator(criterion=c)
    ctx = _ctx(
        expected_output=ScenarioExpectation(
            expected_behaviour="Policy summary.",
            steps=[
                StepExpectation(step_index=1, expected_output="Identifies the district."),
                StepExpectation(step_index=0, expected_output="Greets the user."),
            ],
        )
    )

    captured_prompts: list[str] = []

    async def _capture_run(prompt: str, **kwargs):
        captured_prompts.append(prompt)
        return _fake_agent_result(passed=True, reason="ok")

    with (
        patch.dict("os.environ", {"PYDANTIC_AI_GATEWAY_API_KEY": "test-key"}),
        patch(
            "compass_backend.quality.criteria._judge_agent",
            return_value=AsyncMock(run=_capture_run),
        ),
    ):
        verdict = await ev.evaluate(ctx)

    assert verdict.outcome == "pass", f"Unexpected outcome: {verdict.outcome}"
    assert len(captured_prompts) == 1, "Expected exactly one agent.run() call"
    prompt = captured_prompts[0]

    # Steps should appear sorted by step_index (0 before 1)
    idx_step0 = prompt.find("Step 0: Greets the user.")
    idx_step1 = prompt.find("Step 1: Identifies the district.")
    assert idx_step0 != -1, f"'Step 0' not found in prompt:\n{prompt}"
    assert idx_step1 != -1, f"'Step 1' not found in prompt:\n{prompt}"
    assert idx_step0 < idx_step1, "Step 0 should appear before Step 1 in the prompt"


@pytest.mark.asyncio
async def test_scenario_fit_uses_case_transcript_when_present() -> None:
    """Case-level scenario_fit judges the full transcript instead of one answer_text."""
    c = _criterion()
    ev = RecordScenarioFitEvaluator(criterion=c)
    ctx = _ctx(
        expected_output=ScenarioExpectation(
            expected_behaviour="Answer across both turns.",
            steps=[],
        ),
        answer_text="Only the final answer.",
        case_transcript=[
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow-up"},
            {"role": "assistant", "content": "Final answer"},
        ],
    )

    captured_prompts: list[str] = []

    async def _capture_run(prompt: str, **kwargs):
        captured_prompts.append(prompt)
        return _fake_agent_result(passed=True, reason="ok")

    with (
        patch.dict("os.environ", {"PYDANTIC_AI_GATEWAY_API_KEY": "test-key"}),
        patch(
            "compass_backend.quality.criteria._judge_agent",
            return_value=AsyncMock(run=_capture_run),
        ),
    ):
        verdict = await ev.evaluate(ctx)

    assert verdict.outcome == "pass"
    prompt = captured_prompts[0]
    assert "1. USER: First question" in prompt
    assert "4. ASSISTANT: Final answer" in prompt
    assert "Only the final answer." not in prompt
    assert verdict.evidence.model_dump()["case_transcript_turns"] == 4


# ─── Test 6: dispatch routes scenario_fit to RecordScenarioFitEvaluator ──────


def test_dispatch_routes_scenario_fit_check_type() -> None:
    """criterion_record_to_evaluator returns RecordScenarioFitEvaluator for check_type='scenario_fit'."""
    c = _criterion()
    ev = criterion_record_to_evaluator(c)
    assert isinstance(ev, RecordScenarioFitEvaluator), (
        f"Expected RecordScenarioFitEvaluator, got {type(ev).__name__}"
    )
    # RecordScenarioFitEvaluator is NOT a subclass of RecordEvaluator (it's a peer
    # dataclass) but it quacks the same interface. Verify the criterion pass-through.
    assert ev.criterion is c


# ─── Test 7: judge exception produces error verdict ──────────────────────────


@pytest.mark.asyncio
async def test_scenario_fit_judge_exception_returns_error_verdict() -> None:
    """An exception from the judge agent produces outcome='error' with the exception in reason."""
    c = _criterion()
    ev = RecordScenarioFitEvaluator(criterion=c)
    ctx = _ctx(
        expected_output=ScenarioExpectation(
            expected_behaviour="Explain policy.",
            steps=[],
        )
    )

    async def _raise(*args, **kwargs):
        raise RuntimeError("boom — gateway down")

    with (
        patch.dict("os.environ", {"PYDANTIC_AI_GATEWAY_API_KEY": "test-key"}),
        patch(
            "compass_backend.quality.criteria._judge_agent",
            return_value=AsyncMock(run=_raise),
        ),
    ):
        verdict = await ev.evaluate(ctx)

    assert verdict.outcome == "error"
    assert verdict.judge_source == "scenario_fit"
    assert "boom" in verdict.reason
    assert "RuntimeError" in verdict.reason
    assert verdict.criterion_id == 101


# ─── Test 8: not_applicable for structural-diagnostic cases (Refs #939) ───────


@pytest.mark.asyncio
async def test_scenario_fit_not_applicable_for_structural_diagnostic_case() -> None:
    """A structural-diagnostic case records a not_applicable verdict without calling the judge.

    SCENARIO_FIT_001 is a generic answer-text judge. Structural-diagnostic
    FOUNDATION cases (whose `category` equals their `structural_diagnostic_dimension`,
    e.g. Process Integrity) carry expectations about route/result_type/artifact/
    verdict contracts that an answer-text judge structurally cannot evaluate.
    Instead of running the judge (and instead of silently skipping), the
    evaluator emits a RECORDED not_applicable verdict: outcome='error' with a
    reason prefixed 'not applicable:'. The scorecard's _is_not_applicable
    excludes these from both numerator and denominator.
    """
    c = _criterion()
    ev = RecordScenarioFitEvaluator(criterion=c)
    ctx = _ctx(
        expected_output=ScenarioExpectation(
            expected_behaviour=(
                "The route, result type, artifact payload, and persisted "
                "verdict contract agree."
            ),
            steps=[],
        ),
        case_metadata={
            "category": "Process Integrity",
            "structural_diagnostic_dimension": "Process Integrity",
            "structural_diagnostic_archetype": "contract_shape",
            "scorecard_dimension": "Selection Accuracy",
        },
    )

    judge = AsyncMock(run=AsyncMock(return_value=_fake_agent_result(passed=True)))
    with (
        patch.dict("os.environ", {"PYDANTIC_AI_GATEWAY_API_KEY": "test-key"}),
        patch(
            "compass_backend.quality.criteria._judge_agent",
            return_value=judge,
        ),
    ):
        verdict = await ev.evaluate(ctx)

    assert verdict.outcome == "error"
    assert verdict.judge_source == "scenario_fit"
    assert verdict.scope == "scenario_fit"
    assert verdict.reason.startswith("not applicable:")
    assert "structural diagnostic" in verdict.reason.lower()
    # The recorded verdict still carries the real FK + sweep provenance.
    assert verdict.criterion_id == 101
    assert verdict.case_id == 7
    assert verdict.sweep_run_id == "sweep-abc-123"
    assert verdict.evidence.model_dump()["skip_reason"] == "structural_diagnostic_case"
    # The judge must NOT have been called — the gate is deterministic.
    judge.run.assert_not_awaited()


# ─── Test 9: ordinary case with a rehomed label still runs the judge ──────────


@pytest.mark.asyncio
async def test_scenario_fit_runs_normally_for_regression_case_with_rehomed_label() -> None:
    """A real regression case that merely carries a rehomed structural label runs the judge.

    The REGR-M1 all-covered-district planner cases (#1248) carry a secondary
    `structural_diagnostic_dimension` label for provenance, but their `category`
    is 'regression'/'m1-closure-gate' — NOT the structural dimension. They are
    genuine product regression cases the answer-text judge CAN evaluate, so the
    not_applicable gate must NOT fire for them.
    """
    c = _criterion()
    ev = RecordScenarioFitEvaluator(criterion=c)
    ctx = _ctx(
        expected_output=ScenarioExpectation(
            expected_behaviour="Emits an executable all-covered-district chart plan.",
            steps=[],
        ),
        case_metadata={
            "category": "regression",
            "structural_diagnostic_dimension": "Process Integrity",
            "structural_diagnostic_archetype": "no_fallback_masking",
            "scorecard_dimension": "Selection Accuracy",
        },
    )

    judge = AsyncMock(run=AsyncMock(return_value=_fake_agent_result(passed=True, reason="ok")))
    with (
        patch.dict("os.environ", {"PYDANTIC_AI_GATEWAY_API_KEY": "test-key"}),
        patch(
            "compass_backend.quality.criteria._judge_agent",
            return_value=judge,
        ),
    ):
        verdict = await ev.evaluate(ctx)

    assert verdict.outcome == "pass"
    assert verdict.judge_source == "scenario_fit"
    judge.run.assert_awaited_once()
