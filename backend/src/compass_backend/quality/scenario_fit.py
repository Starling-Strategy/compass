"""RecordScenarioFitEvaluator — LLM-judge for scenario-level expectations.

Compares the assistant's response against the case's expected_output
(a ScenarioExpectation with expected_behaviour prose and optional per-step
expectations). The criterion's payload must carry a 'judge_prompt_template'
with {expected_behaviour}, {expected_steps}, and {answer_text} placeholders.

Design notes:
  - Reuses the shared _judge_agent() singleton (JudgmentResult output type) —
    no separate agent for this evaluator.
  - scope='scenario_fit' on the produced VerdictRecord distinguishes these
    verdicts from per-turn judge_prompt verdicts so Scorecard aggregation can
    separate "did it match the spec?" from "did it satisfy a rubric?".
  - ctx.expected_output=None → outcome='error' (live user_turn path, or a
    sweep case that was never hydrated with expected_output). Not a crash.
  - Missing 'judge_prompt_template' in payload → outcome='error' (criterion
    seeding mistake, not a product verdict).
  - Judge agent exception → outcome='error' with the exception repr in reason.

Run the unit tests with:
    PYTHONPATH=src uv run pytest \\
        src/compass_backend/tests/test_scenario_fit_evaluator.py -x --tb=short
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from compass_backend.agents.model_settings import AgentProfile, agent_model_settings
from compass_backend.db.rows import (
    CriterionRecord,
    ScenarioFitCriterionPayload,
    StepExpectation,
    VerdictRecord,
)
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality._infra import gateway_api_key_present
from compass_backend.quality.evidence import build_evidence_envelope


@dataclass
class RecordScenarioFitEvaluator:
    """LLM-judge evaluator that compares a response against case.expected_output.

    Backed by a CriterionRecord with check_type='scenario_fit'. Reads
    criterion.payload['judge_prompt_template'], interpolates
    {expected_behaviour}, {expected_steps}, and {answer_text}, then runs the
    shared _judge_agent() singleton to get a pass/fail judgment.

    Produces a persistence-ready VerdictRecord with:
      - judge_source='scenario_fit'
      - scope='scenario_fit'
      - criterion_id = criterion.id    (real FK into compass.criteria)
      - criterion_version_hash = criterion.version_hash
    """

    criterion: CriterionRecord

    async def evaluate(self, ctx: CompassEvaluatorContext) -> VerdictRecord:
        # Deferred import to avoid circular-import (criteria.py defines
        # _judge_agent and _judge_usage_limits; importing them here pulls in the
        # singleton without creating a module-level cycle).
        from compass_backend.quality.criteria import (
            _judge_agent,
            _judge_usage_limits,
        )

        c = self.criterion

        # 1. Not applicable for structural-diagnostic cases. SCENARIO_FIT_001 is a
        # generic answer-text judge; it never sees the route, result_type, artifact
        # payload, or persisted verdict. Structural-diagnostic FOUNDATION cases
        # (rehomed off the scorecard to Process Integrity / Surface Consistency,
        # #951) carry expectations about exactly those contracts, which the judge
        # structurally cannot evaluate. Record a `not applicable:` verdict — an
        # auditable fact, excluded from both numerator and denominator by the
        # scorecard's _is_not_applicable — rather than judging a case it was never
        # designed to score (#939).
        if _is_structural_diagnostic_case(ctx.case_metadata):
            dimension = ctx.case_metadata.get("structural_diagnostic_dimension")
            return self._build_verdict(
                ctx,
                outcome="error",
                reason=(
                    f"not applicable: structural diagnostic case "
                    f"({dimension}) — the answer-text scenario_fit judge cannot "
                    "evaluate route/result_type/artifact/verdict contracts."
                ),
                check_specific={
                    "skip_reason": "structural_diagnostic_case",
                    "structural_diagnostic_dimension": dimension,
                    "applicability": "skipped",
                },
            )

        # 2. Skip if no expected_output — live user_turn path or unhydrated case.
        if ctx.expected_output is None:
            return self._build_verdict(
                ctx,
                outcome="error",
                reason=(
                    f"criterion '{c.criterion_code}' not evaluated: "
                    "not applicable: no expected_output on context (live user_turn path)"
                ),
                check_specific={"skip_reason": "no_expected_output"},
            )

        # 3. Read prompt template from criterion payload. The discriminated
        # union in db/rows.py makes `judge_prompt_template` a required non-empty
        # string at load time, so this assertion is structural — a payload
        # that reaches us here is guaranteed to carry the template.
        assert isinstance(c.payload, ScenarioFitCriterionPayload), (
            f"RecordScenarioFitEvaluator expected scenario_fit payload; "
            f"got {type(c.payload).__name__} (criterion {c.criterion_code!r})"
        )
        template = c.payload.judge_prompt_template

        # 4. Render expected_steps prose and the case-level answer artifact.
        steps_text = _render_expected_steps(ctx.expected_output.steps)
        answer_text = (
            _render_case_transcript(ctx.case_transcript)
            if ctx.case_transcript
            else ctx.answer_text
        )

        # 5. Interpolate placeholders.
        try:
            interpolated = template.format(
                expected_behaviour=ctx.expected_output.expected_behaviour,
                expected_steps=steps_text,
                answer_text=answer_text,
            )
        except KeyError as exc:
            return self._build_verdict(
                ctx,
                outcome="error",
                reason=(
                    f"criterion '{c.criterion_code}' misconfigured: "
                    f"judge_prompt_template has unknown placeholder {exc}"
                ),
                check_specific={
                    "skip_reason": "bad_template_placeholder",
                    "missing_key": str(exc),
                },
            )

        # 6. Construct prompt.
        prompt = f"RUBRIC ({c.criterion_code} — scenario_fit):\n{interpolated}"

        # 7. Guard: infra check mirrors RecordJudgmentEvaluator.
        if not gateway_api_key_present():
            return self._build_verdict(
                ctx,
                outcome="error",
                reason=(
                    f"criterion '{c.criterion_code}' not evaluated: infra "
                    "unavailable (PYDANTIC_AI_GATEWAY_API_KEY not set)."
                ),
                check_specific={
                    "skip_reason": "no_pydantic_ai_gateway_api_key",
                    "judge_model": agent_model_settings.judge_model,
                },
            )

        # 8. Run shared judge agent.
        agent = _judge_agent()
        start = time.monotonic()
        try:
            result = await agent.run(
                prompt,
                usage_limits=_judge_usage_limits(),
                metadata={
                    "agent_profile": AgentProfile.JUDGE.value,
                    "criterion_code": c.criterion_code,
                    "criterion_id": c.id,
                    "category": c.category,
                    "session_id": ctx.session_id,
                    "turn_index": ctx.turn_index,
                    "scenario_id": ctx.scenario_id,
                    "run_id": ctx.sweep_run_id,
                },
            )
        except Exception as exc:
            return self._build_verdict(
                ctx,
                outcome="error",
                reason=(
                    f"criterion '{c.criterion_code}' scenario_fit judge raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
                check_specific={
                    "judge_model": agent_model_settings.judge_model,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        output = result.output

        # 9. Dispatch the three-state JudgmentResult.verdict to the three-state
        # VerdictRecord.outcome vocabulary. The 'not_applicable' → 'error' bridge
        # mirrors RecordJudgmentEvaluator so the Scorecard's _is_excluded_from_score
        # drops the trial from both numerator and denominator.
        match output.verdict:
            case "pass":
                return self._build_verdict(
                    ctx,
                    outcome="pass",
                    reason=output.reason,
                    check_specific={
                        "judge_model": agent_model_settings.judge_model,
                        "judge_duration_ms": duration_ms,
                        "score": 1.0,
                        "expected_behaviour": ctx.expected_output.expected_behaviour,
                        "case_transcript_turns": len(ctx.case_transcript),
                        "interpolated_prompt_length": len(interpolated),
                    },
                )
            case "fail":
                return self._build_verdict(
                    ctx,
                    outcome="fail",
                    reason=output.reason,
                    check_specific={
                        "judge_model": agent_model_settings.judge_model,
                        "judge_duration_ms": duration_ms,
                        "score": 0.0,
                        "expected_behaviour": ctx.expected_output.expected_behaviour,
                        "case_transcript_turns": len(ctx.case_transcript),
                        "interpolated_prompt_length": len(interpolated),
                    },
                )
            case "not_applicable":
                return self._build_verdict(
                    ctx,
                    outcome="error",
                    reason=f"not applicable: {output.reason}",
                    check_specific={
                        "judge_model": agent_model_settings.judge_model,
                        "judge_duration_ms": duration_ms,
                        "applicability": "skipped",
                    },
                )
            case _:  # pragma: no cover - verdict is a Literal; total-function guard
                # Unreachable for the typed three-state verdict, but keeps
                # ``evaluate`` total: a malformed judge payload becomes a
                # recorded error rather than an implicit ``None`` return.
                return self._build_verdict(
                    ctx,
                    outcome="error",
                    reason=f"judge returned unexpected verdict {output.verdict!r}",
                    check_specific={
                        "judge_model": agent_model_settings.judge_model,
                        "judge_duration_ms": duration_ms,
                    },
                )

    # ── internal helpers ──────────────────────────────────────────────────────

    def _build_verdict(
        self,
        ctx: CompassEvaluatorContext,
        *,
        outcome: str,
        reason: str,
        check_specific: dict | None = None,
    ) -> VerdictRecord:
        """Construct a persistence-ready VerdictRecord with scope='scenario_fit'."""
        from compass_backend.quality.criteria import COMPASS_CRITERIA_SET_VERSION

        c = self.criterion
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            step_index=ctx.step_index,
            case_id=ctx.case_id,
            scenario_id=ctx.scenario_id,
            criterion_id=c.id,
            criterion_version_hash=c.version_hash,
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source="scenario_fit",
            scope="scenario_fit",
            references_turn_indices=list(ctx.references_turn_indices),
            outcome=outcome,  # type: ignore[arg-type]
            reason=reason,
            evidence=build_evidence_envelope(ctx, check_specific=check_specific),
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
            sweep_run_id=ctx.sweep_run_id,
        )

    def to_pydantic_evals(self):  # type: ignore[override]
        """Return a pydantic_evals.Evaluator wrapping this evaluator."""
        from compass_backend.quality.criteria import _PydanticRecordEvalsAdapter

        return _PydanticRecordEvalsAdapter(compass_evaluator=self)  # type: ignore[arg-type]


def _is_structural_diagnostic_case(case_metadata: dict) -> bool:
    """Return True when a case is a structural-diagnostic FOUNDATION case.

    The answer-text scenario_fit judge cannot evaluate route/result_type/
    artifact/verdict contracts. The structural-diagnostic FOUNDATION cases
    (SCORECARD-PROCESS-FOUNDATION / SCORECARD-SURFACE-FOUNDATION, rehomed off the
    scorecard under #951) are exactly those whose `category` equals their
    `structural_diagnostic_dimension` — i.e. the case's primary purpose IS the
    structural diagnostic.

    The discriminator is deliberately `category == structural_diagnostic_dimension`
    rather than `structural_diagnostic_dimension is not None`: the REGR-M1
    all-covered-district planner cases (#1248) also carry a *secondary*
    `structural_diagnostic_dimension` label for provenance, but their `category`
    is `regression` / `m1-closure-gate`. Those are genuine product regression
    cases the judge CAN evaluate, so they must stay enrolled.
    """
    dimension = case_metadata.get("structural_diagnostic_dimension")
    if not dimension:
        return False
    return case_metadata.get("category") == dimension


def _render_expected_steps(steps: list[StepExpectation]) -> str:
    """Render the list of expected steps as readable prose for the judge."""
    if not steps:
        return "(no per-step expectations)"
    lines = [
        f"Step {s.step_index}: {s.expected_output}"
        for s in sorted(steps, key=lambda x: x.step_index)
    ]
    return "\n".join(lines)


def _render_case_transcript(transcript: list[dict[str, str]]) -> str:
    """Render a case transcript as compact role-prefixed dialogue."""
    lines: list[str] = []
    for index, message in enumerate(transcript, start=1):
        role = str(message.get("role") or "unknown").upper()
        content = str(message.get("content") or "").strip()
        lines.append(f"{index}. {role}: {content}")
    return "\n".join(lines)


__all__ = [
    "RecordScenarioFitEvaluator",
    "_is_structural_diagnostic_case",
    "_render_case_transcript",
    "_render_expected_steps",
]
