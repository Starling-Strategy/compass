"""SSN-201 evidence envelope for compass.verdicts rows.

Every VerdictRecord written to compass.verdicts carries the same three required
keys in its `evidence` payload so the Scorecard can reproduce a turn's state
after Logfire trace eviction. The contract is enforced by the
`VerdictEvidence` model in `compass_backend.db.rows`; this module is the
factory that builds an envelope from a `CompassEvaluatorContext`.

Per the SSN-201 memo at
`docs/research/2026-05-14-compass-defining-good-and-self-evaluation.md`, the
required keys are:

  - `artifact_snapshot`     — `ctx.artifact_snapshot.model_dump(exclude_defaults=True)`
  - `answer_excerpt`        — `ctx.answer_text` truncated to ANSWER_EXCERPT_MAX_CHARS.
    A display/audit preview and replay CHECKSUM, not the grading text. Offline
    replay/calibration hydrate the full answer from `compass.chat_messages`
    (`hydrate_full_answers`) and verify this excerpt is a prefix. Replay skips
    unrecoverable rows; calibration `sample` falls back to this excerpt when
    hydration fails (#1477 / #1480).
  - `copied_span_attributes` — `dict(ctx.span_attributes)`

Evaluator-specific keys (e.g. `flagged_phrases`, `judge_model`,
`validator_name`) are carried via a typed `CheckSpecific` model with
`extra='allow'`. The model declares every well-known field so silent
drift in field names or types surfaces at envelope construction instead
of at dashboard read time. Callsites may still pass a raw dict — it gets
coerced into `CheckSpecific` at the boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from compass_backend.db.rows import ANSWER_EXCERPT_MAX_CHARS, VerdictEvidence
from compass_backend.quality._evaluator_context import CompassEvaluatorContext


# ── Closed vocabulary for skip_reason (closes audit Schema #2 silent-typo risk)
#
# Every value below corresponds to one current evaluator-side skip branch.
# A new skip path adds a value here in lockstep with the new branch — without
# it, the evaluator's CheckSpecific construction fails with a Literal mismatch
# at the first verdict write, instead of silently shipping a new value the
# Scorecard's bucket logic doesn't recognize.
SkipReason = Literal[
    "no_trace_id",
    "no_logfire_token",
    "no_pydantic_ai_gateway_api_key",
    "no_expected_output",
    "empty_trace",
    # Offline/sweep lane could not read the trace (queued/truncated/wrong
    # scope) — recorded as `not applicable:` so the Scorecard excludes it
    # rather than treating the uninformative empty trace as a failure (U3 (a)).
    "empty_trace_offline_lane",
    "bad_template_placeholder",
    "route_not_applicable",
    "structural_diagnostic_case",
]


class CheckSpecific(BaseModel):
    """Typed schema for the evaluator-specific portion of a verdict's evidence.

    The audit (Schema #2) flagged that the previous `check_specific: dict[str, Any]`
    let renamed/typo'd field names live silently in the evidence ledger. This
    model documents every well-known shape under one type:

    - **Skip variants** carry `skip_reason` (closed Literal vocabulary).
    - **Deterministic validator** variants carry `validator_name` +
      `criterion_code` (+ optional `known_validators` / `error_type`).
    - **Judge variants** (RecordJudgmentEvaluator + RecordScenarioFitEvaluator)
      carry `judge_model`, `judge_duration_ms`, `score`, etc.
    - **Span variants** carry `target_spans` / `matched_target` /
      `matched_span_name` / `span_count` (preferred mode) or `tool` /
      `called_before` (legacy assertion mode).
    - **Route gating** (verdict_pipeline) carries `ctx_route` /
      `applicable_routes`.

    `extra='allow'` so deterministic validators that emit ad-hoc keys
    (citation_format's regex output, citation_titles_resolved's per-citation
    diagnostics) still pass through unchanged.

    Callsites typically pass a plain dict; `build_evidence_envelope` coerces
    via `model_validate`. Field-name typos and type mismatches now fail at
    envelope construction, not at Scorecard read time.
    """

    model_config = ConfigDict(extra="allow")

    # ── Skip / error variants ────────────────────────────────────────────
    skip_reason: SkipReason | None = None
    error: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    missing_key: str | None = None

    # ── Deterministic evaluator ──────────────────────────────────────────
    criterion_code: str | None = None
    validator_name: str | None = None
    known_validators: list[str] | None = None

    # ── Judge variants (RecordJudgmentEvaluator + RecordScenarioFitEvaluator)
    judge_model: str | None = None
    judge_duration_ms: int | None = None
    score: float | None = None
    applicability: str | None = None
    expected_behaviour: str | None = None
    case_transcript_turns: int | None = None
    interpolated_prompt_length: int | None = None

    # ── Span assertion (preferred + legacy modes) ────────────────────────
    target_spans: list[str] | None = None
    matched_target: str | None = None
    matched_span_name: str | None = None
    span_count: int | None = None
    tool: str | None = None
    called_before: str | None = None
    trace_id: str | None = None

    # ── Route gating (verdict_pipeline applicable_routes pre-check) ──────
    ctx_route: str | None = None
    applicable_routes: list[str] | None = None


def build_evidence_envelope(
    ctx: CompassEvaluatorContext,
    check_specific: CheckSpecific | Mapping[str, Any] | None = None,
) -> VerdictEvidence:
    """Return the typed evidence envelope for a verdict.

    `check_specific` accepts either a typed `CheckSpecific` instance or a
    plain mapping. Mappings are coerced via `CheckSpecific.model_validate`,
    so field-name typos and type mismatches surface at envelope
    construction. The three SSN-201 envelope keys are guaranteed by the
    `VerdictEvidence` model; envelope keys win on collision with
    `check_specific` per the SSN-201 contract.
    """
    if check_specific is None:
        cs_dict: dict[str, Any] = {}
    elif isinstance(check_specific, CheckSpecific):
        # Explicit-None fields stay in the dump so callsites that set e.g.
        # `matched_target=None` to signal "no match" keep that semantics.
        cs_dict = check_specific.model_dump(exclude_unset=True)
    else:
        # Mapping (or dict) — validate through CheckSpecific to catch typos
        # and type mismatches before the row reaches compass.verdicts,
        # then pass the ORIGINAL dict to the envelope so explicit-None
        # fields (e.g. `matched_target=None`) round-trip unchanged.
        cs_dict = dict(check_specific)
        CheckSpecific.model_validate(cs_dict)

    # Build as a dict so envelope keys win on collision with `check_specific`
    # per the SSN-201 contract — `dict.update` order is explicit and matches
    # the pre-typing flat-merge behavior.
    data: dict[str, Any] = cs_dict
    data["artifact_snapshot"] = ctx.artifact_snapshot.model_dump(exclude_defaults=True)
    data["answer_excerpt"] = ctx.answer_text[:ANSWER_EXCERPT_MAX_CHARS]
    data["copied_span_attributes"] = dict(ctx.span_attributes)
    return VerdictEvidence.model_validate(data)


__all__ = [
    "ANSWER_EXCERPT_MAX_CHARS",
    "CheckSpecific",
    "SkipReason",
    "build_evidence_envelope",
]
