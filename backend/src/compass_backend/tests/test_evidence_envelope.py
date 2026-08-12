"""Tests for the SSN-201 evidence envelope on compass.verdicts rows.

The envelope is the contract that every L2 verdict — regardless of which
evaluator wrote it — carries the same three top-level keys so the Scorecard
can reproduce a turn's state after Logfire trace eviction:

  - `artifact_snapshot`     (copy of ctx.artifact_snapshot)
  - `answer_excerpt`        (ctx.answer_text truncated to 500 chars)
  - `copied_span_attributes` (copy of ctx.span_attributes)

Evaluator-specific evidence (e.g. `flagged_phrases`, `judge_model`,
`validator_name`) is preserved alongside; the envelope keys win on any
collision so the contract holds.
"""

from __future__ import annotations

import pytest

from compass_backend.db.rows import CriterionRecord
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.criteria import (
    RecordDeterministicEvaluator,
    RecordSpanEvaluator,
)
from compass_backend.quality.evidence import (
    ANSWER_EXCERPT_MAX_CHARS,
    build_evidence_envelope,
)
from compass_backend.tests.conftest import (
    _DEFAULT_CRITERION_PAYLOADS,
    make_criterion_record,
    make_evaluator_context,
)

# Mirror of the three SSN-201 envelope fields. The contract is now enforced by
# the `VerdictEvidence` model itself (see db/rows.py); this local constant keeps
# the assertions in this file readable.
_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"artifact_snapshot", "answer_excerpt", "copied_span_attributes"}
)


_DEFAULT_SNAPSHOT = {"table": [{"district_name": "Houston ISD", "display_value": "196,000"}]}


def _ctx(
    *,
    answer_text: str = "Houston ISD ranks 1st with 196,000 students.",
    artifact_snapshot: dict | None = None,
    span_attributes: dict | None = None,
    trace_id: str | None = None,
) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="sess-envelope",
        turn_index=0,
        answer_text=answer_text,
        artifact_snapshot=_DEFAULT_SNAPSHOT.copy() if artifact_snapshot is None else artifact_snapshot,
        span_attributes={} if span_attributes is None else span_attributes,
        trace_id=trace_id,
    )


def _criterion(validator_name: str, *, check_type: str = "deterministic", id: int = 100) -> CriterionRecord:
    if check_type == "deterministic":
        payload: dict = {"validator_name": validator_name}
    else:
        payload = dict(_DEFAULT_CRITERION_PAYLOADS[check_type])
    return make_criterion_record(
        id=id,
        criterion_code=f"test_{validator_name}",
        text="test criterion",
        category="accuracy",
        check_type=check_type,
        severity="reject",
        priority=50,
        payload=payload,
        version_hash="vh-test",
    )


# ── 1. build_evidence_envelope: pure helper contract ──────────────────────────


def test_envelope_has_all_three_required_keys_with_empty_ctx() -> None:
    """Every envelope has the three SSN-201 keys, even from an empty context."""
    ctx = _ctx(answer_text="", artifact_snapshot={}, span_attributes={})
    envelope = build_evidence_envelope(ctx).model_dump()
    assert _ENVELOPE_KEYS <= envelope.keys()
    assert envelope["artifact_snapshot"] == {}
    assert envelope["answer_excerpt"] == ""
    assert envelope["copied_span_attributes"] == {}


def test_envelope_copies_artifact_snapshot_at_top_level() -> None:
    """Adding a top-level key to ctx.artifact_snapshot after envelope build does not leak in.

    Note: copy is shallow on purpose — verdicts are persisted to JSONB
    immediately so reference sharing is short-lived. Top-level isolation
    is what matters for the SSN-201 snapshot contract.
    """
    snap = {"table": [{"district_name": "Houston ISD", "display_value": "196,000"}]}
    ctx = _ctx(artifact_snapshot=snap)
    envelope = build_evidence_envelope(ctx).model_dump()
    snap["new_top_level"] = "added after build"
    assert "new_top_level" not in envelope["artifact_snapshot"]


def test_envelope_truncates_long_answer_to_max_chars() -> None:
    """Long answer text is truncated to ANSWER_EXCERPT_MAX_CHARS."""
    long_answer = "x" * (ANSWER_EXCERPT_MAX_CHARS + 100)
    ctx = _ctx(answer_text=long_answer)
    envelope = build_evidence_envelope(ctx).model_dump()
    assert len(envelope["answer_excerpt"]) == ANSWER_EXCERPT_MAX_CHARS


def test_envelope_preserves_short_answer_verbatim() -> None:
    """Short answers are kept verbatim (no truncation)."""
    short_answer = "Houston ISD ranks 1st."
    ctx = _ctx(answer_text=short_answer)
    envelope = build_evidence_envelope(ctx).model_dump()
    assert envelope["answer_excerpt"] == short_answer


def test_envelope_captures_full_multi_district_answer() -> None:
    """A realistic multi-district answer (longer than the old 500-char cap) is
    captured in full, so completeness/coverage judges replayed from evidence see
    the whole table — not a truncated snippet that looks incomplete (#1418).
    """
    # ~2.6k chars: a 10-row salary table with prose + a sources block. Well
    # past the old 500 cap; this is exactly the shape a completeness judge needs.
    rows = "\n".join(
        f"| District {i} USD | ${40000 + i * 1000:,} | [{i}] |" for i in range(1, 60)
    )
    answer = (
        "Here are the districts sorted by first-year salary.\n\n"
        "| District | Starting salary | Sources |\n| --- | ---: | --- |\n"
        f"{rows}\n\nSources\n- [1] CBA documents\n"
    )
    assert len(answer) > 500  # would have been truncated under the old cap
    ctx = _ctx(answer_text=answer)
    envelope = build_evidence_envelope(ctx).model_dump()
    assert envelope["answer_excerpt"] == answer  # captured in full
    # The cap must comfortably exceed a typical multi-district answer so the
    # completeness use case is actually served.
    assert ANSWER_EXCERPT_MAX_CHARS >= 4000


def test_envelope_merges_check_specific_evidence() -> None:
    """Check-specific evidence is preserved alongside envelope keys."""
    ctx = _ctx()
    check_specific = {"flagged_phrases": ["not in the database"], "violation_count": 1}
    envelope = build_evidence_envelope(ctx, check_specific=check_specific).model_dump()
    # Both envelope keys and check_specific keys are present, flat
    assert _ENVELOPE_KEYS <= envelope.keys()
    assert envelope["flagged_phrases"] == ["not in the database"]
    assert envelope["violation_count"] == 1


def test_envelope_keys_win_on_collision_with_check_specific() -> None:
    """If check_specific names an envelope key, the envelope value wins (contract)."""
    ctx = _ctx(artifact_snapshot={"table": [{"district_name": "Real", "display_value": "1"}]})
    bad = {"artifact_snapshot": {"forged": "data"}, "answer_excerpt": "wrong"}
    envelope = build_evidence_envelope(ctx, check_specific=bad).model_dump()
    assert envelope["artifact_snapshot"] == {"table": [{"district_name": "Real", "display_value": "1"}]}
    assert envelope["answer_excerpt"] == ctx.answer_text  # short, not truncated


def test_envelope_handles_none_check_specific() -> None:
    """check_specific=None is allowed and produces an envelope with only the 3 required keys."""
    ctx = _ctx()
    envelope = build_evidence_envelope(ctx, check_specific=None).model_dump()
    assert set(envelope.keys()) == set(_ENVELOPE_KEYS)


# ── 2. RecordDeterministicEvaluator: envelope on success + error paths ────────


@pytest.mark.asyncio
async def test_deterministic_verdict_carries_envelope_on_success() -> None:
    """Success path (known validator → real outcome) writes envelope keys into evidence."""
    ev = RecordDeterministicEvaluator(criterion=_criterion("citation_format"))
    ctx = _ctx(answer_text="Plain prose without citations.")
    verdict = await ev.evaluate(ctx)
    assert _ENVELOPE_KEYS <= set(verdict.evidence.model_dump().keys())
    # The validator's own evidence is also preserved (e.g., malformed_count, citation_count)
    assert verdict.outcome in ("pass", "fail")


@pytest.mark.asyncio
async def test_deterministic_verdict_carries_envelope_on_unknown_validator() -> None:
    """Error path (unknown validator_name) still has envelope keys."""
    ev = RecordDeterministicEvaluator(criterion=_criterion("not_a_real_validator"))
    verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "error"
    assert _ENVELOPE_KEYS <= set(verdict.evidence.model_dump().keys())
    # The diagnostic evidence is still present
    evidence_dict = verdict.evidence.model_dump()
    assert evidence_dict["validator_name"] == "not_a_real_validator"
    assert "known_validators" in evidence_dict


def test_deterministic_criterion_with_missing_validator_name_fails_at_construction() -> None:
    """Replaces the old 'missing validator_name → error verdict' test.

    After the CriterionRecord.payload discriminated-union landed, a deterministic
    criterion with an empty payload (no validator_name) is rejected at load
    time rather than producing per-evaluation error verdicts. The 'unknown but
    present' validator_name path is still covered by
    test_dispatch_unknown_validator_name_returns_error above.
    """
    c = _criterion("any_name")
    with pytest.raises(ValueError):  # pydantic.ValidationError is a ValueError subclass
        CriterionRecord(**{**c.__dict__, "payload": {}})


# ── 3. RecordSpanEvaluator: envelope on error paths ───────────────────────────


@pytest.mark.asyncio
async def test_span_verdict_carries_envelope_when_no_trace_id() -> None:
    """SpanEvaluator no-trace_id error verdict still has envelope keys."""
    c = _criterion("any_assertion", check_type="span_assertion")
    c = CriterionRecord(
        **{**c.__dict__, "payload": {"assertion": {"tool": "find_metric", "called_before": "composer"}}}
    )
    ev = RecordSpanEvaluator(criterion=c)
    verdict = await ev.evaluate(_ctx(trace_id=None))
    assert verdict.outcome == "error"
    assert _ENVELOPE_KEYS <= set(verdict.evidence.model_dump().keys())
    # The diagnostic skip_reason is still present
    assert verdict.evidence.model_dump()["skip_reason"] == "no_trace_id"


# ── 4. Envelope reflects context state (artifact + span_attributes pass-through) ──


@pytest.mark.asyncio
async def test_envelope_reflects_actual_context_artifact_state() -> None:
    """The envelope's artifact_snapshot is the same data the ctx carried, not a stub."""
    table_rows = [
        {"district_name": "Houston ISD", "display_value": "196,000"},
        {"district_name": "LAUSD", "display_value": "566,000"},
    ]
    ctx = _ctx(artifact_snapshot={"table": table_rows}, span_attributes={"model": "claude-sonnet-4-6"})
    ev = RecordDeterministicEvaluator(criterion=_criterion("citation_format"))
    verdict = await ev.evaluate(ctx)
    assert verdict.evidence.artifact_snapshot["table"] == table_rows
    assert verdict.evidence.copied_span_attributes == {"model": "claude-sonnet-4-6"}


# ── 5. CheckSpecific typed coercion (Schema #2) ───────────────────────────────


class TestCheckSpecificTypedCoercion:
    """`build_evidence_envelope` validates `check_specific` against the
    `CheckSpecific` Pydantic model — typo'd or wrongly-typed well-known fields
    fail at envelope construction instead of shipping silently into the
    verdict ledger.
    """

    def test_unknown_skip_reason_rejected_at_envelope_build(self) -> None:
        """SkipReason is a closed Literal vocabulary — new branches must add
        their value to the union, not invent it ad-hoc."""
        from pydantic import ValidationError

        ctx = _ctx()
        with pytest.raises(ValidationError) as exc_info:
            build_evidence_envelope(
                ctx, check_specific={"skip_reason": "not_a_real_skip_reason"}
            )
        assert "skip_reason" in str(exc_info.value)

    def test_wrong_type_on_well_known_field_rejected(self) -> None:
        """`score` is float; passing a string fails at envelope build."""
        from pydantic import ValidationError

        ctx = _ctx()
        with pytest.raises(ValidationError):
            build_evidence_envelope(
                ctx, check_specific={"judge_model": "haiku", "score": "high"}
            )

    def test_extra_keys_still_passthrough(self) -> None:
        """`extra='allow'` — deterministic validators emitting ad-hoc keys
        (citation_format's regex output, etc.) still round-trip."""
        ctx = _ctx()
        evidence = build_evidence_envelope(
            ctx,
            check_specific={
                "validator_name": "citation_format",
                "regex_matches": 3,
                "non_compliant_urls": ["https://example.com"],
            },
        )
        assert evidence.model_dump()["regex_matches"] == 3
        assert evidence.model_dump()["non_compliant_urls"] == ["https://example.com"]

    def test_explicit_none_field_round_trips(self) -> None:
        """Callsites that explicitly pass `matched_target=None` to signal
        'no match' must keep that signal in the evidence dict."""
        ctx = _ctx()
        evidence = build_evidence_envelope(
            ctx,
            check_specific={
                "target_spans": ["compass.auth.api_key"],
                "matched_target": None,
                "span_count": 5,
            },
        )
        assert evidence.model_dump()["matched_target"] is None
        assert evidence.model_dump()["span_count"] == 5

    def test_known_skip_reasons_accepted(self) -> None:
        """Spot-check that every value in the SkipReason Literal flows through.

        If this list drifts from the Literal, a future evaluator's skip path
        either fires this test (good — caught at test time) or fires the
        ValidationError at envelope build (also good — caught before write)."""
        from compass_backend.quality.evidence import SkipReason
        import typing

        ctx = _ctx()
        for reason in typing.get_args(SkipReason):
            evidence = build_evidence_envelope(
                ctx, check_specific={"skip_reason": reason}
            )
            assert evidence.model_dump()["skip_reason"] == reason

    def test_typed_check_specific_instance_accepted(self) -> None:
        """Callsites may pass a typed `CheckSpecific` directly; envelope
        coalesces it the same way."""
        from compass_backend.quality.evidence import CheckSpecific

        ctx = _ctx()
        evidence = build_evidence_envelope(
            ctx,
            check_specific=CheckSpecific(
                judge_model="claude-haiku-4-5",
                judge_duration_ms=42,
                score=1.0,
            ),
        )
        dumped = evidence.model_dump()
        assert dumped["judge_model"] == "claude-haiku-4-5"
        assert dumped["judge_duration_ms"] == 42
        assert dumped["score"] == 1.0
