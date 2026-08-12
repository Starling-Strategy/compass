"""Load-time guarantees for the typed VerdictRecord.evidence envelope.

Before the `VerdictEvidence` model existed, `evidence: dict` accepted any
shape — a malformed envelope shipped to `compass.verdicts` and only got
noticed when a human opened Logfire and found the field they expected was
missing. These tests pin the new contract: bad envelopes raise at
`VerdictRecord(...)` construction with the offending field named in the
validation error.

Pairs with `test_criterion_payload_types.py` (the other half of the
persistence-seam typing pair landed in PR #881).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from compass_backend.db.rows import (
    ANSWER_EXCERPT_MAX_CHARS,
    VerdictEvidence,
    VerdictRecord,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _valid_envelope(**extras: object) -> dict:
    """Return a minimal valid envelope dict + any extra per-evaluator keys."""
    return {
        "artifact_snapshot": {},
        "answer_excerpt": "",
        "copied_span_attributes": {},
        **extras,
    }


def _base_verdict_kwargs() -> dict:
    """Common kwargs the surrounding VerdictRecord needs."""
    return {
        "session_id": "sess-test",
        "turn_index": 0,
        "criterion_id": 1,
        "criterion_version_hash": "vh-1",
        "criterion_set_version": "set-1",
        "judge_source": "deterministic",
        "scope": "turn",
        "outcome": "pass",
        "reason": "test reason",
        "triggered_by": "user_turn",
    }


# ── 1. Happy path: a valid envelope dict coerces into VerdictEvidence ─────────


def test_minimal_envelope_dict_coerces_to_typed_evidence_on_verdict() -> None:
    verdict = VerdictRecord(**_base_verdict_kwargs(), evidence=_valid_envelope())
    assert isinstance(verdict.evidence, VerdictEvidence)
    assert verdict.evidence.artifact_snapshot == {}
    assert verdict.evidence.answer_excerpt == ""
    assert verdict.evidence.copied_span_attributes == {}


def test_typed_envelope_passed_directly_works() -> None:
    evidence = VerdictEvidence(
        artifact_snapshot={"table": [{"district_name": "Houston ISD"}]},
        answer_excerpt="Houston ISD ranks 1st.",
        copied_span_attributes={"model": "claude-sonnet-4-6"},
    )
    verdict = VerdictRecord(**_base_verdict_kwargs(), evidence=evidence)
    assert verdict.evidence is evidence  # passed-through, not re-constructed
    assert verdict.evidence.artifact_snapshot["table"][0]["district_name"] == "Houston ISD"


# ── 2. Required-field enforcement: each missing envelope key → load error ─────


@pytest.mark.parametrize(
    "missing_key",
    ["artifact_snapshot", "answer_excerpt", "copied_span_attributes"],
)
def test_missing_envelope_key_fails_at_construction(missing_key: str) -> None:
    """Each of the three required SSN-201 envelope keys is enforced."""
    envelope = _valid_envelope()
    del envelope[missing_key]
    with pytest.raises(ValidationError) as exc_info:
        VerdictRecord(**_base_verdict_kwargs(), evidence=envelope)
    assert missing_key in str(exc_info.value)


# ── 3. answer_excerpt length is bounded ───────────────────────────────────────


def test_answer_excerpt_under_max_length_passes() -> None:
    envelope = _valid_envelope(answer_excerpt="x" * ANSWER_EXCERPT_MAX_CHARS)
    verdict = VerdictRecord(**_base_verdict_kwargs(), evidence=envelope)
    assert len(verdict.evidence.answer_excerpt) == ANSWER_EXCERPT_MAX_CHARS


def test_answer_excerpt_over_max_length_fails() -> None:
    envelope = _valid_envelope(answer_excerpt="x" * (ANSWER_EXCERPT_MAX_CHARS + 1))
    with pytest.raises(ValidationError) as exc_info:
        VerdictRecord(**_base_verdict_kwargs(), evidence=envelope)
    assert "answer_excerpt" in str(exc_info.value)


# ── 4. extra='allow' preserves per-evaluator keys ─────────────────────────────


def test_per_evaluator_keys_are_preserved() -> None:
    """The validator/judge writes flat per-evaluator fields alongside the envelope."""
    envelope = _valid_envelope(
        validator_name="citation_format",
        known_validators=["citation_format", "currency_format_canonical"],
        violation_count=2,
    )
    verdict = VerdictRecord(**_base_verdict_kwargs(), evidence=envelope)
    # extras are stored and round-trippable via model_dump
    dumped = verdict.evidence.model_dump()
    assert dumped["validator_name"] == "citation_format"
    assert dumped["known_validators"] == ["citation_format", "currency_format_canonical"]
    assert dumped["violation_count"] == 2


def test_per_evaluator_key_collision_with_envelope_caller_wins() -> None:
    """If the caller passes both envelope and per-evaluator keys, the merge
    is dict-semantic (last assignment wins). The factory in quality/evidence.py
    is responsible for ordering envelope keys after check_specific so the
    envelope wins on collision — that ordering is asserted in
    test_evidence_envelope.py::test_envelope_keys_win_on_collision_with_check_specific.
    This test pins that the model itself doesn't reject the collision.
    """
    envelope = {
        "artifact_snapshot": {"real": "data"},
        "answer_excerpt": "real",
        "copied_span_attributes": {},
        # an evaluator might shadow an envelope key — model accepts it
    }
    verdict = VerdictRecord(**_base_verdict_kwargs(), evidence=envelope)
    assert verdict.evidence.artifact_snapshot == {"real": "data"}


# ── 5. Round-trip serialization for the DB write boundary ────────────────────


def test_model_dump_json_round_trip_preserves_envelope_and_extras() -> None:
    """db/verdicts.py:77 writes via verdict.evidence.model_dump_json() — round-trip preserves shape."""
    envelope = _valid_envelope(
        artifact_snapshot={"table": [{"district_name": "Houston ISD"}]},
        answer_excerpt="ranked 1st",
        copied_span_attributes={"trace_id": "abc"},
        validator_name="citation_format",
        violation_count=0,
    )
    verdict = VerdictRecord(**_base_verdict_kwargs(), evidence=envelope)

    serialized = verdict.evidence.model_dump_json()
    parsed = json.loads(serialized)

    # All three envelope keys present
    assert parsed["artifact_snapshot"] == {"table": [{"district_name": "Houston ISD"}]}
    assert parsed["answer_excerpt"] == "ranked 1st"
    assert parsed["copied_span_attributes"] == {"trace_id": "abc"}
    # Extra keys also present
    assert parsed["validator_name"] == "citation_format"
    assert parsed["violation_count"] == 0


# ── 6. Direct VerdictEvidence construction (no surrounding VerdictRecord) ─────


def test_verdict_evidence_can_be_constructed_directly() -> None:
    """Useful for unit tests that build evidence in isolation."""
    ev = VerdictEvidence(
        artifact_snapshot={},
        answer_excerpt="",
        copied_span_attributes={},
    )
    assert isinstance(ev, VerdictEvidence)


def test_verdict_evidence_construction_rejects_missing_required_field() -> None:
    """Smoke check that the model fails the same way whether built directly
    or through a wrapping VerdictRecord."""
    with pytest.raises(ValidationError):
        VerdictEvidence(answer_excerpt="x", copied_span_attributes={})  # type: ignore[call-arg]
