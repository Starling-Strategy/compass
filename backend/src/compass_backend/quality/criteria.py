"""Runtime evaluators for compass.criteria rows (b-spine).

This module holds the `RecordEvaluator` hierarchy that turns one
`CriterionRecord` (from compass.criteria) + one `CompassEvaluatorContext`
into a persistence-ready `VerdictRecord` (compass.verdicts). The factory
`criterion_record_to_evaluator(c)` dispatches by `c.check_type`:

    check_type='deterministic'  →  RecordDeterministicEvaluator
    check_type='judge_prompt'   →  RecordJudgmentEvaluator
    check_type='span_assertion' →  RecordSpanEvaluator
    check_type='scenario_fit'   →  RecordScenarioFitEvaluator
                                   (imported from quality/scenario_fit.py)

Live L2 (verdict_pipeline.evaluate_one_criterion) and the pa-eval offline
adapter (`_PydanticRecordEvalsAdapter`) both construct one
`CompassEvaluatorContext` per turn and route it through the same
evaluator instance, so the dispatched code paths are identical regardless
of caller.

`JudgmentResult` is the structured output type for judge_prompt criteria
(three-state: pass | fail | not_applicable, with reason that must cite
concrete response text). `_judge_agent`, `_judge_usage_limits`, and
`validate_judgment_result` are shared helpers used by both
`RecordJudgmentEvaluator` and `RecordScenarioFitEvaluator`.

Legacy `GoldenCriterion` hierarchy that this module previously co-hosted
was deleted in the post-b-spine cleanup once the Critic Console was
retired — there are no consumers of `compass.golden_criteria` in the
runtime path. See PRs #931 / #961 for that work.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_ai import Agent, ModelRetry, UsageLimits
from pydantic_evals.evaluators import (
    EvaluationReason,
    Evaluator as PydanticEvalsEvaluator,
    EvaluatorContext,
)

from compass_backend.agents.judge import build_judge_agent, judge_usage_limits
from compass_backend.agents.model_settings import AgentProfile, agent_model_settings
from compass_backend.config import settings
from compass_backend.db.rows import (
    CaseRecord,
    CriterionRecord,
    DeterministicCriterionPayload,
    JudgePromptCriterionPayload,
    ScenarioExpectation,
    ScenarioRecord,
    SpanAssertionCriterionPayload,
    StepExpectation,
    StepInput,
    VerdictOutcome,
    VerdictRecord,
)
from compass_backend.instructions.loader import load_model_instructions
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality._infra import gateway_api_key_present
from compass_backend.quality.evidence import build_evidence_envelope
from compass_backend.quality.scenario_fit import RecordScenarioFitEvaluator


# Set-version label for compass.criteria-backed verdicts. PR 6's query layer
# (scorecard, drill-down) will reference this same constant.
COMPASS_CRITERIA_SET_VERSION = "compass_criteria_v1"


# ─── Spec B-spine row shapes (compass.{scenarios,cases,criteria,verdicts}) ───
#
# Moved to `compass_backend.db.rows` so the db layer owns persistence shapes
# and no longer imports upward from `quality/`. The names are imported above
# and remain re-exported from this module's namespace for back-compat with
# callers and tests that still use `from compass_backend.quality.criteria
# import CriterionRecord`.


class JudgmentResult(BaseModel):
    """Structured output from the LLM judge.

    Single-discriminator design: exactly one of three states applies. This
    mirrors the three-state ``compass.verdicts.outcome`` vocabulary
    (``pass``/``fail``/``error``) at the judge API layer so the schema makes
    the contract explicit instead of asking downstream code to interpret
    two booleans together.

    Mapping to ``VerdictRecord.outcome`` (performed by the consumer
    evaluators):
      ``verdict='pass'``           → ``outcome='pass'``
      ``verdict='fail'``           → ``outcome='fail'``
      ``verdict='not_applicable'`` → ``outcome='error'`` with a
                                     ``'not applicable: '`` reason prefix, so
                                     the Scorecard's ``_is_excluded_from_score``
                                     drops the trial from both numerator and
                                     denominator (matching the deterministic-
                                     validator convention).
    """

    model_config = ConfigDict(extra="forbid")

    # ``reason`` is declared BEFORE ``verdict`` on purpose. pydantic-ai
    # serializes structured output with JSON properties in field-declaration
    # order, so the judge writes its justification first and only then commits
    # to the label — articulating the evidence before naming the outcome,
    # rather than rationalizing a label after the fact. See #1295.
    reason: str = Field(
        min_length=1,
        max_length=500,
        description="One-sentence justification, citing concrete text from the response.",
    )
    verdict: Literal["pass", "fail", "not_applicable"] = Field(
        description=(
            "'pass' = response satisfies the rubric. "
            "'fail' = response violates the rubric. "
            "'not_applicable' = rubric does not apply to this response (e.g. "
            "the criterion expects a follow-up sort but this is turn 1, or "
            "the response is a greeting/clarification/refusal with no "
            "referent to evaluate). Do NOT use 'pass' as a substitute for "
            "'not_applicable' — they have different downstream effects "
            "('pass' is included in the scorecard numerator; "
            "'not_applicable' is excluded from both numerator and "
            "denominator)."
        ),
    )

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, value: object) -> object:
        """Normalize surrounding whitespace before validation.

        Doing this in a declared before-validator (rather than silently in the
        output validator) keeps cleanup distinct from the semantic check: an
        empty/whitespace-only reason still fails ``min_length`` / the output
        validator, but a well-formed reason with stray padding is normalized
        here without the output validator appearing to rewrite a "valid" field.
        """

        return value.strip() if isinstance(value, str) else value


JUDGE_INSTRUCTIONS = load_model_instructions("judge.md")
_JUDGE_AGENT: Agent[None, JudgmentResult] | None = None


def _judge_agent() -> Agent[None, JudgmentResult]:
    """Lazy-build a singleton judge Agent so we don't construct it per turn."""

    global _JUDGE_AGENT
    if _JUDGE_AGENT is None:
        _JUDGE_AGENT = build_judge_agent(
            output_type=JudgmentResult,
            instructions=JUDGE_INSTRUCTIONS,
            output_validator=validate_judgment_result,
        )
    return _JUDGE_AGENT


def validate_judgment_result(output: JudgmentResult) -> JudgmentResult:
    """Reject judge outputs that satisfy JSON shape but not Compass semantics."""

    # ``reason`` is already whitespace-normalized by JudgmentResult's
    # before-validator, so this stays a pure semantic check (no silent rewrite).
    if not output.reason:
        raise ModelRetry("JudgmentResult.reason must cite concrete response text.")
    return output


def _judge_usage_limits() -> UsageLimits:
    """Return per-run usage limits for one judgment rule.

    Thin back-compat wrapper around `agents.judge.judge_usage_limits`.
    """

    return judge_usage_limits()


# ── Logfire span-tree fetch (shared by RecordSpanEvaluator) ─────────────────

_LOGFIRE_BASE = "https://logfire-us.pydantic.dev"
_LOGFIRE_USER_AGENT = "compass-backend/1.0"

# Logfire's /v1/query caps the result set when no explicit ``limit`` query
# parameter is sent (100 rows on our deployment). The span tree is fetched
# ``ORDER BY start_timestamp ASC``, so the cap keeps the *earliest* spans and
# drops the tail — and ``compass.persistence.save_turn`` (plus trailing
# ``compass.execution.*`` spans) fire late in a turn. Under the #1308 tool-first
# planner a turn routinely emits >100 spans, so those late spans fell off the
# truncated tree and the span_assertion criteria reported false negatives on
# turns that actually persisted/dispatched. Request the API maximum so the whole
# trace is returned; a single turn never approaches 10k spans. Refs #1325.
_SPAN_TREE_ROW_LIMIT = 10_000


async def _fetch_span_tree(trace_id: str, *, timeout: float = 10.0) -> list[dict]:
    """Query Logfire for one trace's span tree. Returns [] on any failure."""

    token = settings.logfire_read_token
    if token is None:
        return []
    token_str = token.get_secret_value()
    if not token_str or not trace_id:
        return []

    safe_trace = trace_id.replace("'", "''")
    sql = (
        "SELECT span_name, duration * 1000.0 as duration_ms, "
        "start_timestamp, attributes, parent_span_id, span_id "
        "FROM records "
        f"WHERE trace_id = '{safe_trace}' "
        "ORDER BY start_timestamp ASC"
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{_LOGFIRE_BASE}/v1/query",
                params={
                    "sql": sql,
                    "row_oriented": "true",
                    "limit": _SPAN_TREE_ROW_LIMIT,
                },
                headers={
                    "Authorization": f"Bearer {token_str}",
                    "Accept": "application/json",
                    "User-Agent": _LOGFIRE_USER_AGENT,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception:
        return []

    if "rows" in payload:
        return payload["rows"]
    columns = payload.get("columns") or []
    if not columns:
        return []
    names = [c.get("name") for c in columns]
    values = [c.get("values") or [] for c in columns]
    n = max((len(v) for v in values), default=0)
    return [
        {names[j]: values[j][i] if i < len(values[j]) else None for j in range(len(names))}
        for i in range(n)
    ]


def _matches_tool(span: dict, tool_name: str) -> bool:
    if (span.get("span_name") or "").lower().find(tool_name.lower()) >= 0:
        return True
    attrs = span.get("attributes") or {}
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except json.JSONDecodeError:
            attrs = {}
    return str(attrs.get("tool", "")).lower() == tool_name.lower()


# ── CriterionRecord-backed evaluators ────────────────────────────────────────
#
# Each subclass holds a CriterionRecord (from compass.criteria) and produces
# persistence-ready VerdictRecords for compass.verdicts. `_make_record_verdict()`
# stamps real foreign keys onto every row:
#   - criterion_id = c.id          (FK into compass.criteria)
#   - criterion_version_hash = c.version_hash
#
# check_type → judge_source on VerdictRecord (1-to-1, literal strings match):
#   'deterministic'             →  'deterministic'
#   'judge_prompt'              →  'judge_prompt'
#   'span_assertion'            →  'span_assertion'
#   'scenario_fit'              →  'scenario_fit'  (handled by
#                                  RecordScenarioFitEvaluator in scenario_fit.py)


@dataclass
class RecordEvaluator:
    """Base evaluator for CriterionRecord-backed criteria.

    Holds one CriterionRecord (from compass.criteria) and produces a
    persistence-ready VerdictRecord on each `evaluate(ctx)` call. Subclasses
    implement the per-check_type logic; this base provides the shared
    `_make_record_verdict()` helper that stamps the criterion FK and
    version_hash onto every row.
    """

    criterion: CriterionRecord

    async def evaluate(self, ctx: CompassEvaluatorContext) -> VerdictRecord:
        raise NotImplementedError

    def to_pydantic_evals(self) -> PydanticEvalsEvaluator:  # type: ignore[type-arg]
        """Return a pydantic_evals.Evaluator that wraps this RecordEvaluator."""
        cls = globals()["_PydanticRecordEvalsAdapter"]
        return cls(compass_evaluator=self)

    def _make_record_verdict(
        self,
        ctx: CompassEvaluatorContext,
        *,
        outcome: VerdictOutcome,
        reason: str,
        evidence: dict | None = None,
    ) -> VerdictRecord:
        """Construct a persistence-ready VerdictRecord from a CriterionRecord.

        Unlike Evaluator._make_verdict(), this method sets:
          - criterion_id = c.id         (real FK into compass.criteria)
          - criterion_version_hash = c.version_hash  (real per-row hash)
          - judge_source from c.check_type (which uses new literals: 'judge_prompt',
            'span_assertion', 'deterministic' — all 1-to-1 with VerdictRecord.judge_source)
        """
        c = self.criterion
        # CriterionRecord.check_type is Literal['deterministic','judge_prompt','span_assertion','scenario_fit']
        # VerdictRecord.judge_source accepts the same set plus 'inline_deterministic' — direct passthrough.
        judge_source: Literal["deterministic", "judge_prompt", "span_assertion", "inline_deterministic", "scenario_fit"]
        if c.check_type in ("deterministic", "judge_prompt", "span_assertion", "scenario_fit"):
            judge_source = c.check_type  # type: ignore[assignment]
        else:
            judge_source = "deterministic"

        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            step_index=ctx.step_index,
            case_id=ctx.case_id,
            scenario_id=ctx.scenario_id,
            criterion_id=c.id,                      # real FK — PR 6 can INSERT directly
            criterion_version_hash=c.version_hash,   # real per-row hash
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source=judge_source,
            scope="turn",
            references_turn_indices=list(ctx.references_turn_indices),
            outcome=outcome,
            reason=reason,
            evidence=evidence or {},
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
            sweep_run_id=ctx.sweep_run_id,
        )


@dataclass
class RecordDeterministicEvaluator(RecordEvaluator):
    """Dispatch deterministic compass.criteria rows through the validator registry.

    Reads `criterion.payload.validator_name` and looks it up in
    `compass_backend.quality.validators.registry`. Known names run; unknown
    names produce `outcome='error'` with the known set in evidence so operators
    can see "seeded but un-implemented" criteria at a glance.
    """

    async def evaluate(self, ctx: CompassEvaluatorContext) -> VerdictRecord:
        from compass_backend.quality.validators import registry

        c = self.criterion
        assert isinstance(c.payload, DeterministicCriterionPayload), (
            f"RecordDeterministicEvaluator expected deterministic payload; "
            f"got {type(c.payload).__name__} (criterion {c.criterion_code!r})"
        )
        validator_name = c.payload.validator_name
        fn = registry.lookup(validator_name)
        if fn is None:
            return self._make_record_verdict(
                ctx,
                outcome="error",
                reason=(
                    f"criterion '{c.criterion_code}' references unknown "
                    f"validator_name={validator_name!r}"
                ),
                evidence=build_evidence_envelope(
                    ctx,
                    check_specific={
                        "criterion_code": c.criterion_code,
                        "validator_name": validator_name,
                        "known_validators": sorted(registry.known_validators()),
                    },
                ),
            )
        # The validator function signature is `(ctx, dict)`; preserve the
        # dict contract (validators read ad-hoc keys like `applicable_routes`).
        # `exclude={'check_type'}` drops the discriminator copy injected by
        # CriterionRecord._inject_payload_discriminator so the validator sees
        # the same shape the row carried on disk.
        payload_dict = c.payload.model_dump(exclude={"check_type"})
        try:
            outcome = await fn(ctx, payload_dict)
        except Exception as exc:  # validator contract is "never raise"; defensive net
            return self._make_record_verdict(
                ctx,
                outcome="error",
                reason=(
                    f"criterion '{c.criterion_code}' validator "
                    f"{validator_name!r} raised {type(exc).__name__}: {exc}"
                ),
                evidence=build_evidence_envelope(
                    ctx,
                    check_specific={
                        "criterion_code": c.criterion_code,
                        "validator_name": validator_name,
                        "error_type": type(exc).__name__,
                    },
                ),
            )
        return self._make_record_verdict(
            ctx,
            outcome=outcome.outcome,
            reason=outcome.reason,
            evidence=build_evidence_envelope(ctx, check_specific=outcome.evidence),
        )


@dataclass
class RecordJudgmentEvaluator(RecordEvaluator):
    """LLM-judge evaluator backed by a CriterionRecord.

    Reads the judge_prompt from CriterionRecord.payload.judge_prompt. Falls
    back to CriterionRecord.text as the rubric when payload has no judge_prompt.
    """

    async def evaluate(self, ctx: CompassEvaluatorContext) -> VerdictRecord:
        c = self.criterion
        assert isinstance(c.payload, JudgePromptCriterionPayload), (
            f"RecordJudgmentEvaluator expected judge_prompt payload; "
            f"got {type(c.payload).__name__} (criterion {c.criterion_code!r})"
        )
        judge_prompt_text = c.payload.judge_prompt or c.text
        prompt = (
            f"RUBRIC ({c.criterion_code} — {c.category}):\n"
            f"{judge_prompt_text}\n\n"
            f"CONTEXT ATTRIBUTES:\n"
            f"- turn_index: {ctx.turn_index}\n"
            f"- prior_result_ref_count: {ctx.prior_result_ref_count}\n"
            f"- recent_routes: {ctx.recent_routes}\n\n"
            f"ASSISTANT RESPONSE:\n"
            f"{ctx.answer_text}"
        )

        if not gateway_api_key_present():
            return self._make_record_verdict(
                ctx,
                outcome="error",
                reason=(
                    f"criterion '{c.criterion_code}' not evaluated: infra "
                    "unavailable (PYDANTIC_AI_GATEWAY_API_KEY not set)."
                ),
                evidence=build_evidence_envelope(
                    ctx,
                    check_specific={
                        "skip_reason": "no_pydantic_ai_gateway_api_key",
                        "judge_model": agent_model_settings.judge_model,
                    },
                ),
            )

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
            return self._make_record_verdict(
                ctx,
                outcome="error",
                reason=f"criterion '{c.criterion_code}' judge call failed: {type(exc).__name__}: {exc}",
                evidence=build_evidence_envelope(
                    ctx,
                    check_specific={
                        "judge_model": agent_model_settings.judge_model,
                        "error": str(exc),
                    },
                ),
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        output = result.output
        # Dispatch the three-state verdict to the three-state VerdictRecord.outcome
        # vocabulary. The 'not_applicable' → 'error' bridge preserves the
        # deterministic-validator `not applicable:` convention so the Scorecard's
        # _is_excluded_from_score filter drops the trial from both numerator and
        # denominator. Without it, Safeguard 4's additive selection of
        # judge_prompt criteria on non-applicable turns would conflate "judge
        # approved" with "judge had nothing to evaluate" in the published pass rate.
        match output.verdict:
            case "pass":
                return self._make_record_verdict(
                    ctx,
                    outcome="pass",
                    reason=output.reason,
                    evidence=build_evidence_envelope(
                        ctx,
                        check_specific={
                            "judge_model": agent_model_settings.judge_model,
                            "judge_duration_ms": duration_ms,
                            "score": 1.0,
                        },
                    ),
                )
            case "fail":
                return self._make_record_verdict(
                    ctx,
                    outcome="fail",
                    reason=output.reason,
                    evidence=build_evidence_envelope(
                        ctx,
                        check_specific={
                            "judge_model": agent_model_settings.judge_model,
                            "judge_duration_ms": duration_ms,
                            "score": 0.0,
                        },
                    ),
                )
            case "not_applicable":
                return self._make_record_verdict(
                    ctx,
                    outcome="error",
                    reason=f"not applicable: {output.reason}",
                    evidence=build_evidence_envelope(
                        ctx,
                        check_specific={
                            "judge_model": agent_model_settings.judge_model,
                            "judge_duration_ms": duration_ms,
                            "applicability": "skipped",
                        },
                    ),
                )
            case _:  # pragma: no cover - verdict is a Literal; total-function guard
                # Unreachable for the typed three-state verdict, but keeps
                # ``evaluate`` total: a malformed judge payload becomes a
                # recorded error rather than an implicit ``None`` return.
                return self._make_record_verdict(
                    ctx,
                    outcome="error",
                    reason=f"judge returned unexpected verdict {output.verdict!r}",
                    evidence=build_evidence_envelope(
                        ctx,
                        check_specific={
                            "judge_model": agent_model_settings.judge_model,
                            "judge_duration_ms": duration_ms,
                        },
                    ),
                )


def _matches_target_span(span: dict, target_name: str) -> bool:
    """Substring match on span_name, mirroring _matches_tool's style.

    Substring (not exact) keeps the seeded names forgiving: a seeded
    'compass.execution.' matches any operation suffix (lookup, rank, count,
    trend, profile_lookup), and a future renaming from 'compass.auth.api_key'
    to 'compass.auth.api_key.validate' still passes a criterion whose
    target_spans entry is 'compass.auth.api_key'.

    The trade-off: target_spans entries must be specific enough that no
    accidental substring matches occur. All 5 seeded names today use the
    'compass.<area>.<verb>' shape; the leading 'compass.' is enough to keep
    matches inside the Compass-owned span namespace.
    """
    name = (span.get("span_name") or "").lower()
    return target_name.lower() in name if target_name else False


@dataclass
class RecordSpanEvaluator(RecordEvaluator):
    """Span evaluator backed by a CriterionRecord.

    Two modes, chosen by what's populated on the criterion:

    1. **target_spans populated** (Gap 4 PR C, the preferred shape today):
       Passes when ANY span whose name substring-matches one of
       ``criterion.target_spans`` appears in the turn's trace. Used by the 5
       seeded span_assertion criteria asserting key Compass machinery fired
       (auth, execution dispatch, catalog adjudication, SSE done, persistence).

    2. **target_spans empty/None, payload.assertion.tool populated**
       (legacy 'naive tool called' shape — kept for backwards compat):
       Reads ``payload['assertion']['tool']`` and checks whether any span
       matches via ``_matches_tool``. Returns ``outcome='fail'`` if no
       assertion is configured at all.

    Both modes share the same error short-circuits: ``ctx.trace_id`` missing,
    ``LOGFIRE_READ_TOKEN`` unset, or the Logfire query returning an empty
    trace all produce ``outcome='error'`` with a diagnostic ``skip_reason``.
    """

    async def evaluate(self, ctx: CompassEvaluatorContext) -> VerdictRecord:
        c = self.criterion
        if not ctx.trace_id:
            return self._make_record_verdict(
                ctx,
                outcome="error",
                reason=f"criterion '{c.criterion_code}' not evaluated: no trace_id on context.",
                evidence=build_evidence_envelope(
                    ctx, check_specific={"skip_reason": "no_trace_id"}
                ),
            )

        if settings.logfire_read_token is None:
            return self._make_record_verdict(
                ctx,
                outcome="error",
                reason=f"criterion '{c.criterion_code}' not evaluated: infra unavailable (LOGFIRE_READ_TOKEN not set).",
                evidence=build_evidence_envelope(
                    ctx, check_specific={"skip_reason": "no_logfire_token"}
                ),
            )

        spans = await _fetch_span_tree(ctx.trace_id)
        if not spans:
            # In the offline/sweep lane the Logfire read token frequently
            # cannot see the trace at evaluation time -- the trace is still
            # queued for ingestion, was truncated, or the token is scoped to a
            # different project (#1325/#1326 history). An empty trace there is
            # *uninformative*, not a failure: we cannot tell whether the
            # asserted machinery fired, so recording `error` on every execute
            # turn buried real signal (19 errors in the recent sweep). Route
            # the sweep/replay lanes to the `not applicable:` convention (see
            # quality/scorecard.py::_is_not_applicable) so the Scorecard drops
            # the trial from numerator AND denominator. Absence of trace data
            # is NOT a pass -- a real missing-span fail still requires a
            # readable trace whose target span is genuinely absent (Mode 1
            # below). The live user_turn lane keeps the diagnostic error.
            if ctx.triggered_by in ("sweep_run", "manual_replay"):
                return self._make_record_verdict(
                    ctx,
                    outcome="error",
                    reason=(
                        f"not applicable: criterion '{c.criterion_code}' has no "
                        f"readable span tree in the {ctx.triggered_by} lane "
                        f"(trace queued/truncated/out-of-scope -- not a turn failure)"
                    ),
                    evidence=build_evidence_envelope(
                        ctx,
                        check_specific={
                            "skip_reason": "empty_trace_offline_lane",
                            "trace_id": ctx.trace_id,
                            "triggered_by": ctx.triggered_by,
                        },
                    ),
                )
            return self._make_record_verdict(
                ctx,
                outcome="error",
                reason=(
                    f"criterion '{c.criterion_code}' not evaluated: trace empty "
                    f"(queued? truncated? token wrong scope?)"
                ),
                evidence=build_evidence_envelope(
                    ctx,
                    check_specific={"skip_reason": "empty_trace", "trace_id": ctx.trace_id},
                ),
            )

        # Mode 1: target_spans populated — span-name presence check (Gap 4 PR C).
        if c.target_spans:
            matched = next(
                (
                    (target, s.get("span_name"))
                    for target in c.target_spans
                    for s in spans
                    if _matches_target_span(s, target)
                ),
                None,
            )
            if matched is not None:
                target, matched_span_name = matched
                return self._make_record_verdict(
                    ctx,
                    outcome="pass",
                    reason=(
                        f"criterion '{c.criterion_code}': span '{matched_span_name}' "
                        f"matched target '{target}'"
                    ),
                    evidence=build_evidence_envelope(
                        ctx,
                        check_specific={
                            "target_spans": list(c.target_spans),
                            "matched_target": target,
                            "matched_span_name": matched_span_name,
                            "span_count": len(spans),
                        },
                    ),
                )
            return self._make_record_verdict(
                ctx,
                outcome="fail",
                reason=(
                    f"criterion '{c.criterion_code}': none of target_spans "
                    f"{list(c.target_spans)} appeared in {len(spans)} trace spans"
                ),
                evidence=build_evidence_envelope(
                    ctx,
                    check_specific={
                        "target_spans": list(c.target_spans),
                        "matched_target": None,
                        "span_count": len(spans),
                    },
                ),
            )

        # Mode 2: legacy payload.assertion.tool shape (backwards compat).
        assert isinstance(c.payload, SpanAssertionCriterionPayload), (
            f"RecordSpanEvaluator expected span_assertion payload; "
            f"got {type(c.payload).__name__} (criterion {c.criterion_code!r})"
        )
        assertion = c.payload.assertion
        tool = assertion.tool if assertion else ""
        any_called = any(_matches_tool(s, tool) for s in spans) if tool else False
        called_before = assertion.called_before if assertion else "?"
        return self._make_record_verdict(
            ctx,
            outcome="pass" if any_called else "fail",
            reason=(
                f"criterion '{c.criterion_code}' ({tool} before {called_before}) "
                f"[naive: 'tool called at all']: "
                + ("tool was called" if any_called else "tool was NOT called")
            ),
            evidence=build_evidence_envelope(
                ctx, check_specific={"tool": tool, "span_count": len(spans)}
            ),
        )


# ── 5b. Adapter for pydantic_evals integration (RecordEvaluator) ─────────────


@dataclass
class _PydanticRecordEvalsAdapter(PydanticEvalsEvaluator[Any, Any, Any]):
    """Wraps a RecordEvaluator as a pydantic_evals.Evaluator.

    Mirrors _PydanticEvalsAdapter but for the CriterionRecord-backed evaluators.
    """

    compass_evaluator: RecordEvaluator

    async def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:  # type: ignore[override]
        compass_ctx_data = (ctx.metadata or {}).get("compass_ctx")
        if compass_ctx_data is None:
            raise RuntimeError(
                "_PydanticRecordEvalsAdapter requires ctx.metadata['compass_ctx']."
            )
        compass_ctx = (
            compass_ctx_data
            if isinstance(compass_ctx_data, CompassEvaluatorContext)
            else CompassEvaluatorContext(**compass_ctx_data)
        )
        verdict = await self.compass_evaluator.evaluate(compass_ctx)
        passed = verdict.outcome == "pass"
        return EvaluationReason(value=passed, reason=verdict.reason)


# ── 6. Factory (CriterionRecord path) ────────────────────────────────────────

def criterion_record_to_evaluator(c: CriterionRecord) -> RecordEvaluator:
    """Pick the right RecordEvaluator for a CriterionRecord from compass.criteria.

    This is the new factory path (Spec B PR 5). VerdictRecords produced by
    evaluators from this factory have:
      - criterion_id = c.id           (real FK into compass.criteria)
      - criterion_version_hash = c.version_hash (real per-row hash)

    These VerdictRecords are persistence-ready for compass.verdicts via PR 6's
    VerdictsRepository.write_one() — no FK violation on INSERT.

    check_type mapping (new literals on compass.criteria):
      'deterministic'  → RecordDeterministicEvaluator
      'judge_prompt'   → RecordJudgmentEvaluator
      'span_assertion' → RecordSpanEvaluator

    Args:
        c: CriterionRecord from compass.criteria (loaded by ReadOnlyCriteriaRepository).

    Returns:
        A RecordEvaluator subclass appropriate for c.check_type.

    Raises:
        TypeError: if c.check_type is not a recognised literal (schema constraint
                   should prevent this in practice).
    """
    if c.check_type == "judge_prompt":
        return RecordJudgmentEvaluator(criterion=c)
    if c.check_type == "deterministic":
        return RecordDeterministicEvaluator(criterion=c)
    if c.check_type == "span_assertion":
        return RecordSpanEvaluator(criterion=c)
    if c.check_type == "scenario_fit":
        return RecordScenarioFitEvaluator(criterion=c)
    raise TypeError(
        f"criterion_record_to_evaluator: unhandled check_type={c.check_type!r} "
        f"for criterion_code={c.criterion_code!r}"
    )


__all__ = [
    # B-spine shapes (compass.{scenarios,cases,criteria,verdicts})
    "CaseRecord",
    "CriterionRecord",
    "ScenarioExpectation",
    "ScenarioRecord",
    "StepExpectation",
    "StepInput",
    "VerdictRecord",
    # Evaluator context adapter (B-spine PR 4)
    "CompassEvaluatorContext",
    # Set-version constants
    "COMPASS_CRITERIA_SET_VERSION",
    # CriterionRecord-backed evaluators (B-spine PR 5)
    "RecordDeterministicEvaluator",
    "RecordEvaluator",
    "RecordJudgmentEvaluator",
    "RecordSpanEvaluator",
    "criterion_record_to_evaluator",
    # pydantic_evals bridge (adapters are underscore-private; tests import by name directly)
]
