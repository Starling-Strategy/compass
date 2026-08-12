"""Pydantic row shapes for `compass.*` tables.

These types own the persistence-layer shape of database rows. They were
previously declared in `execution/types.py` (`MetricAnswerRow`) and
`quality/criteria.py` (the b-spine record set) and consumed back upward by
db repository functions — an inverted import direction relative to the
layer order documented in `src/compass_backend/README.md`.

By centralizing the shapes here, db repositories no longer reach upward
into `execution/` or `quality/`. The original modules re-export these
names so existing callers (executors, evaluators, tests) keep working
without changes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from compass_backend.artifacts import CitationSource

# ─── CriterionRecord.category — closed vocabulary ─────────────────────────────
#
# The Scorecard dashboard buckets verdict rows by `criteria.category`. Before
# this Literal, `category: str` accepted any value, so a typo in a seed
# migration (e.g. 'citation' vs 'citations') silently created a new dashboard
# bucket instead of failing at row load.
#
# Vocabulary below is the full set of distinct values currently inserted by
# `INSERT INTO compass.criteria` in `src/compass_data_sync/migrations/*.sql`
# (12 values, parsed 2026-05-26). Add a new value here in lockstep with the
# migration that introduces it.
CriterionCategory = Literal[
    "accuracy",
    "citation_accuracy",
    "citations",
    "citations-exports",
    "data_fidelity",
    "filter_accuracy",
    "observability",
    "process_integrity",
    "scenario_fit",
    "selection_accuracy",
    "sort_accuracy",
    "surface_consistency",
]

# ─── Verdict outcome — the three terminal states a verdict can carry ──────────
#
# Single authority for the `compass.verdicts.outcome` vocabulary. Distinct from
# the broader `outcome_bucket` Literal on `VerdictRecord`
# (pass/fail/skip/trace_missing/error), the DB-computed reporting axis that is
# deliberately a superset. Import this alias wherever a verdict or validator
# outcome is typed so the three-state vocabulary lives in one place.
VerdictOutcome = Literal["pass", "fail", "error"]


class MetricAnswerRow(BaseModel):
    """Raw policy answer row used by deterministic query execution."""

    model_config = ConfigDict(extra="forbid")

    answer_id: int | None = None
    district_id: int
    district_name: str = Field(min_length=1)
    state: str | None = None
    metric_id: int
    metric_name: str = Field(min_length=1)
    value: str | int | float | None = None
    # Parse-once typed numeric. `value` is stringly-typed (a rendered cell like
    # "45,602" or "12.5%"); every numeric consumer used to re-parse it at the
    # point of use, and a bare float("45,602") raised a ValueError that 500'd the
    # turn (the #1772 crash class). Parse it ONCE here at the row boundary via the
    # single authority (execution/_text_utils.parse_compass_numeric_value) and let
    # consumers read this typed field instead of re-parsing `value`. None when
    # `value` is missing or non-numeric (e.g. "Yes"), exactly like
    # MetricValueRow.numeric_value / ProfileFieldRankRow.value.
    numeric_value: float | None = None
    academic_year: str = Field(min_length=1)
    citations: list[CitationSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_numeric_value(self) -> Self:
        """Parse `value` once into `numeric_value` at construction (the boundary).

        Lazy import: db/ must not reach upward into execution/ at module load
        (the inverted-import problem this module was created to fix — see the
        module docstring), so the one cross-layer reference lives inside the
        validator body, not at module top. The parser propagates None and never
        raises, so a comma-formatted currency string can never crash row load.
        """

        if self.numeric_value is None:
            from compass_backend.execution._text_utils import (
                parse_compass_numeric_value,
            )

            self.numeric_value = parse_compass_numeric_value(self.value)
        return self


# ─── Spec B-spine shapes (compass.{scenarios,cases,criteria,verdicts} rows) ───────


class StepInput(BaseModel):
    """One input step within a Case (typed shape inside Case.inputs per ADR)."""

    prompt: str
    step_complete_when: str | None = None


class StepExpectation(BaseModel):
    """Per-step expected output (typed shape inside Case.expected_output.steps[*])."""

    step_index: int
    expected_output: str  # prose; structural targets deferred to follow-on spec


class ScenarioExpectation(BaseModel):
    """Arc-level expected output for a Case."""

    expected_behaviour: str
    steps: list[StepExpectation] = Field(default_factory=list)


class ScenarioRecord(BaseModel):
    """Pydantic shape of compass.scenarios rows."""

    model_config = ConfigDict(extra="forbid")

    id: int
    scenario_code: str
    expected_behaviour: str
    accuracy_primary_dimension: str
    topic_tags: list[str] = Field(default_factory=list)
    intent_tags: list[str] = Field(default_factory=list)
    version_hash: str
    active: bool = True


class CaseRecord(BaseModel):
    """Pydantic shape of compass.cases rows."""

    model_config = ConfigDict(extra="forbid")

    id: int
    scenario_id: int
    case_code: str
    inputs: list[StepInput]  # parsed from JSONB
    expected_output: ScenarioExpectation  # parsed from JSONB
    metadata: dict = Field(default_factory=dict)
    version_hash: str
    active: bool = True


# ─── CriterionRecord.payload — typed discriminated union ──────────────────────────
#
# The `payload` jsonb on compass.criteria carries check_type-specific config:
# the validator name to dispatch (deterministic), the rubric prose (judge_prompt),
# the legacy tool assertion (span_assertion), or the prompt template (scenario_fit).
#
# Before this change `payload: dict` accepted any shape; a row that shipped with the
# wrong keys for its `check_type` produced `outcome='error'` verdicts on every
# evaluation rather than failing at load. The discriminated union below moves that
# check to `_criterion_from_row`, so a malformed migration fails at startup with
# the offending criterion's code in the traceback.
#
# `applicable_routes` and `description` are common across all four variants (any
# check_type can carry them; the routes field gates evaluator dispatch in
# `verdict_pipeline.evaluate_and_write`).
#
# `extra='allow'` on the base preserves forward-compat for the deterministic
# dispatch path, which passes the whole payload as a dict to the validator
# function (`criteria.py:_RecordDeterministicEvaluator.evaluate`); some
# validators read ad-hoc keys.


class _CriterionPayloadBase(BaseModel):
    """Common fields across all CriterionRecord.payload variants."""

    model_config = ConfigDict(extra="allow")

    applicable_routes: list[str] | None = Field(
        default=None,
        description=(
            "If set, the criterion only evaluates when ctx.route is in this list. "
            "Read by verdict_pipeline.evaluate_and_write before evaluator dispatch."
        ),
    )
    description: str | None = Field(
        default=None,
        description="Documentation-only — not consumed by any evaluator.",
    )


class DeterministicCriterionPayload(_CriterionPayloadBase):
    """Payload for check_type='deterministic' — dispatches to a registry validator."""

    check_type: Literal["deterministic"] = "deterministic"
    validator_name: str = Field(
        min_length=1,
        description="Key into compass_backend.quality.validators.registry.",
    )


class JudgePromptCriterionPayload(_CriterionPayloadBase):
    """Payload for check_type='judge_prompt' — drives the LLM judge agent."""

    check_type: Literal["judge_prompt"] = "judge_prompt"
    judge_prompt: str | None = Field(
        default=None,
        description=(
            "Rubric prose passed to the judge agent. Falls back to "
            "CriterionRecord.text when None — see RecordJudgmentEvaluator."
        ),
    )


class LegacySpanAssertion(BaseModel):
    """Pre-Gap-4 shape stored under `SpanAssertionCriterionPayload.assertion`.

    Modern criteria use the top-level `CriterionRecord.target_spans` column
    for span-name presence checks; this model only validates the legacy
    payload shape that older seeded rows still carry. New criteria should
    leave `assertion=None` and populate `target_spans` instead.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    called_before: str = Field(min_length=1)
    rationale: str | None = None


class SpanAssertionCriterionPayload(_CriterionPayloadBase):
    """Payload for check_type='span_assertion' — legacy `assertion` shape.

    Preferred mode (Gap 4 PR C) uses the top-level `CriterionRecord.target_spans`
    column for span-name presence checks; payload is documentation-only in that
    mode. Pre-Gap-4 rows used `payload.assertion.tool` — kept as an optional
    typed field for backwards compat.
    """

    check_type: Literal["span_assertion"] = "span_assertion"
    assertion: LegacySpanAssertion | None = Field(
        default=None,
        description=(
            "Legacy {tool, called_before} shape; preferred mode uses "
            "CriterionRecord.target_spans."
        ),
    )


class ScenarioFitCriterionPayload(_CriterionPayloadBase):
    """Payload for check_type='scenario_fit' — drives RecordScenarioFitEvaluator."""

    check_type: Literal["scenario_fit"] = "scenario_fit"
    judge_prompt_template: str = Field(
        min_length=1,
        description=(
            "Template with {expected_behaviour}, {expected_steps}, {answer_text} "
            "placeholders. Interpolated then passed to the judge agent."
        ),
    )


CriterionPayload = Annotated[
    DeterministicCriterionPayload
    | JudgePromptCriterionPayload
    | SpanAssertionCriterionPayload
    | ScenarioFitCriterionPayload,
    Field(discriminator="check_type"),
]


class CriterionRecord(BaseModel):
    """Pydantic shape of compass.criteria rows (B-spine criterion ledger).

    Distinct from the legacy GoldenCriterion (compass.golden_criteria).
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    criterion_code: str
    text: str
    category: CriterionCategory
    check_type: Literal["deterministic", "judge_prompt", "span_assertion", "scenario_fit"]
    severity: Literal["reject", "warn", "note"]
    priority: int = 0
    is_mandatory_global: bool = False
    scenario_ids: list[int] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    intent_tags: list[str] = Field(default_factory=list)
    payload: CriterionPayload
    version_hash: str
    active: bool = True
    # Gap 4 PR C: span_assertion criteria declare which OpenTelemetry span names
    # they expect to find in the turn's trace. When populated on a span_assertion
    # row, RecordSpanEvaluator passes if any listed span name appears.
    # Nullable / default None — non-span_assertion criteria have no implicit
    # semantic on this column. Kept as list[str] | None so the absence
    # ("column never set") is distinguishable from "empty array intentionally".
    target_spans: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _inject_payload_discriminator(cls, data: Any) -> Any:
        """Copy the row-level check_type onto the payload dict so the union resolves.

        DB rows store check_type as its own column; the payload jsonb does NOT
        carry the discriminator. Loaders (and tests) pass `payload` as a plain
        dict; this validator copies `check_type` into it before the discriminated
        union runs. If payload is already a typed Pydantic instance (e.g. from
        in-memory construction), pass through unchanged.
        """
        if not isinstance(data, dict):
            return data
        check_type = data.get("check_type")
        payload = data.get("payload")
        if isinstance(payload, dict) and check_type and payload.get("check_type") is None:
            data = {**data, "payload": {**payload, "check_type": check_type}}
        return data

    @model_validator(mode="after")
    def _payload_check_type_matches_row(self) -> Self:
        """Guard against in-memory construction that builds a mismatched record."""
        if self.payload.check_type != self.check_type:
            raise ValueError(
                f"CriterionRecord.payload.check_type={self.payload.check_type!r} "
                f"does not match row check_type={self.check_type!r} "
                f"(criterion_code={self.criterion_code!r})"
            )
        return self


# ─── VerdictRecord.evidence — typed envelope ──────────────────────────────────
#
# Every row written to compass.verdicts must carry the same three top-level keys
# in its `evidence` payload so the Scorecard can reproduce a turn's state after
# Logfire trace eviction (SSN-201 contract — see quality/evidence.py docstring).
# Before this model existed, `evidence: dict` accepted any shape; a malformed
# envelope shipped to the verdict ledger and only surfaced when a human opened
# Logfire and found the field they expected was missing.
#
# Per-evaluator keys (judge_model, skip_reason, validator_name, etc.) are
# preserved alongside the envelope via `extra='allow'`. The envelope keys win
# on collision, matching `build_evidence_envelope` behavior.
#
# This completes the persistence-seam typing pair started in PR #881
# (CriterionRecord.payload). Unlike payload, evidence has no runtime readers —
# this is audit-trail hygiene, not a runtime-bug fix.

# The live judge grades the FULL ``ctx.answer_text`` (see
# quality/criteria.py::RecordJudgmentEvaluator) — this cap only bounds the
# *stored* evidence copy, which is a display/audit preview and a replay
# CHECKSUM, not the grading text. Offline tools (``pa-eval replay-criterion``
# / ``calibration``) hydrate the full answer from its canonical source — the
# turn's assistant message in ``compass.chat_messages`` — via
# ``hydrate_full_answers`` and use this excerpt only to verify the hydrated
# answer (it must be a prefix). When the full answer can't be recovered the two
# tools differ: replay-criterion SKIPS the row (never grades a stub), while
# calibration ``sample`` falls back to this excerpt so the row stays labelable —
# the one path where the excerpt still reaches a grader (#1477 / #1480).
# Originally 500 (preview-sized); raised to 8000 (#1472) so the audit copy
# captures the whole answer for ~96% of turns (p95 ~6k) and the checksum prefix
# is long enough to be reliable, while still bounding pathological answers
# (max ~100k).
ANSWER_EXCERPT_MAX_CHARS = 8000


class VerdictEvidence(BaseModel):
    """SSN-201 evidence envelope written to compass.verdicts.evidence."""

    model_config = ConfigDict(extra="allow")

    artifact_snapshot: dict[str, Any] = Field(
        description="ctx.artifact_snapshot.model_dump(exclude_defaults=True) at evaluator-call time."
    )
    answer_excerpt: str = Field(
        max_length=ANSWER_EXCERPT_MAX_CHARS,
        description=(
            f"ctx.answer_text stored up to {ANSWER_EXCERPT_MAX_CHARS} chars. "
            "A display/audit preview and replay CHECKSUM, not the grading text. "
            "The live judge grades the full answer; offline replay/calibration "
            "hydrate it from compass.chat_messages and verify this excerpt is a "
            "prefix. Replay skips unrecoverable rows; calibration `sample` falls "
            "back to this excerpt when hydration fails (#1477 / #1480)."
        ),
    )
    copied_span_attributes: dict[str, Any] = Field(
        description="dict(ctx.span_attributes) at evaluator-call time."
    )


class VerdictRecord(BaseModel):
    """Pydantic shape of compass.verdicts rows."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None  # UUID; None until DB assigns
    session_id: str
    turn_index: int | None = None
    step_index: int | None = None
    case_id: int | None = None
    scenario_id: int | None = None
    criterion_id: int
    criterion_version_hash: str
    criterion_set_version: str
    judge_source: Literal["deterministic", "judge_prompt", "span_assertion", "inline_deterministic", "scenario_fit"]
    scope: Literal["turn", "scenario_fit"]
    references_turn_indices: list[int] = Field(default_factory=list)
    outcome: VerdictOutcome
    reason: str
    evidence: VerdictEvidence
    trace_id: str | None = None
    triggered_by: Literal["user_turn", "sweep_run", "manual_replay"]
    sweep_run_id: str | None = None
    # Read-only: populated by the `verdicts_set_outcome_bucket` trigger in
    # migration 074. Mirrors `scripts/pa_eval/outcome_taxonomy.bucket_verdict`
    # but is computed once at write time and stored on the row, so the
    # scorecard / reports can read it without re-joining `compass.criteria`.
    # Writers should leave this as None — the DB always overwrites.
    outcome_bucket: Literal["pass", "fail", "skip", "trace_missing", "error"] | None = None


__all__ = [
    "MetricAnswerRow",
    "StepInput",
    "StepExpectation",
    "ScenarioExpectation",
    "ScenarioRecord",
    "CaseRecord",
    "CriterionCategory",
    "VerdictOutcome",
    "CriterionRecord",
    "CriterionPayload",
    "DeterministicCriterionPayload",
    "JudgePromptCriterionPayload",
    "SpanAssertionCriterionPayload",
    "ScenarioFitCriterionPayload",
    "VerdictRecord",
    "VerdictEvidence",
    "ANSWER_EXCERPT_MAX_CHARS",
]
