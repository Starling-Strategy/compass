"""Load-time guarantees for the typed CriterionRecord.payload union.

Before the discriminated-union landed, `payload: dict` accepted any shape and a
malformed row produced `outcome='error'` verdicts on every evaluation rather
than failing fast. These tests pin the new contract: bad payload shapes raise
at `CriterionRecord(...)` construction with the offending criterion's code in
the message.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.db.rows import (
    CriterionRecord,
    DeterministicCriterionPayload,
    JudgePromptCriterionPayload,
    ScenarioFitCriterionPayload,
    SpanAssertionCriterionPayload,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _base_kwargs(check_type: str, *, criterion_code: str = "test_criterion") -> dict:
    """Common kwargs all CriterionRecord variants share."""
    return {
        "id": 1,
        "criterion_code": criterion_code,
        "text": "test criterion",
        "category": "accuracy",
        "check_type": check_type,
        "severity": "warn",
        "priority": 0,
        "is_mandatory_global": False,
        "scenario_ids": [],
        "topic_tags": [],
        "intent_tags": [],
        "version_hash": "v1",
        "active": True,
    }


# ── 1. Happy paths: a valid payload resolves to the right typed variant ──────


def test_deterministic_payload_resolves_to_deterministic_variant() -> None:
    rec = CriterionRecord(**_base_kwargs("deterministic"), payload={"validator_name": "x"})
    assert isinstance(rec.payload, DeterministicCriterionPayload)
    assert rec.payload.validator_name == "x"


def test_judge_prompt_payload_resolves_to_judge_prompt_variant() -> None:
    rec = CriterionRecord(**_base_kwargs("judge_prompt"), payload={"judge_prompt": "rubric"})
    assert isinstance(rec.payload, JudgePromptCriterionPayload)
    assert rec.payload.judge_prompt == "rubric"


def test_span_assertion_empty_payload_is_legal() -> None:
    """Span assertions can ship with empty payload — target_spans column does the work."""
    rec = CriterionRecord(**_base_kwargs("span_assertion"), payload={})
    assert isinstance(rec.payload, SpanAssertionCriterionPayload)
    assert rec.payload.assertion is None


def test_span_assertion_legacy_assertion_payload_is_legal() -> None:
    """Legacy {assertion: {tool, called_before}} shape still accepted (Gap 4 backwards compat)."""
    rec = CriterionRecord(
        **_base_kwargs("span_assertion"),
        payload={"assertion": {"tool": "find_metric", "called_before": "composer"}},
    )
    assert isinstance(rec.payload, SpanAssertionCriterionPayload)
    assert rec.payload.assertion is not None
    assert rec.payload.assertion.tool == "find_metric"
    assert rec.payload.assertion.called_before == "composer"


def test_scenario_fit_payload_resolves_to_scenario_fit_variant() -> None:
    rec = CriterionRecord(
        **_base_kwargs("scenario_fit"),
        payload={"judge_prompt_template": "{answer_text}"},
    )
    assert isinstance(rec.payload, ScenarioFitCriterionPayload)
    assert rec.payload.judge_prompt_template == "{answer_text}"


# ── 2. Required-field enforcement: missing key → load-time error ─────────────


def test_deterministic_with_missing_validator_name_fails() -> None:
    """Replaces the old 'missing validator_name → runtime error verdict' contract.

    Before the discriminated union, this row would load happily and then produce
    `outcome='error'` on every evaluation. Now it fails at construction with
    the missing field named in the validation error.
    """
    with pytest.raises(ValidationError) as exc_info:
        CriterionRecord(**_base_kwargs("deterministic"), payload={})
    assert "validator_name" in str(exc_info.value)


def test_deterministic_with_empty_validator_name_fails() -> None:
    """`validator_name: str = Field(min_length=1)` rejects the empty string."""
    with pytest.raises(ValidationError):
        CriterionRecord(**_base_kwargs("deterministic"), payload={"validator_name": ""})


def test_scenario_fit_with_missing_judge_prompt_template_fails() -> None:
    """Mirrors the deterministic case for the scenario_fit lane."""
    with pytest.raises(ValidationError) as exc_info:
        CriterionRecord(**_base_kwargs("scenario_fit"), payload={})
    assert "judge_prompt_template" in str(exc_info.value)


def test_judge_prompt_with_missing_judge_prompt_is_allowed() -> None:
    """Judge-prompt rows may omit `judge_prompt` — runtime falls back to CriterionRecord.text."""
    rec = CriterionRecord(**_base_kwargs("judge_prompt"), payload={})
    assert isinstance(rec.payload, JudgePromptCriterionPayload)
    assert rec.payload.judge_prompt is None


# ── 3. Common base fields work across variants ───────────────────────────────


def test_applicable_routes_is_accepted_on_any_check_type() -> None:
    """applicable_routes lives on the common base; verdict_pipeline reads it for all check_types."""
    for check_type, payload in (
        ("deterministic", {"validator_name": "x", "applicable_routes": ["execute"]}),
        ("judge_prompt", {"judge_prompt": "r", "applicable_routes": ["execute", "clarify"]}),
        ("span_assertion", {"applicable_routes": ["execute"]}),
        ("scenario_fit", {"judge_prompt_template": "t", "applicable_routes": ["execute"]}),
    ):
        rec = CriterionRecord(**_base_kwargs(check_type), payload=payload)
        assert rec.payload.applicable_routes == payload["applicable_routes"]


def test_description_is_accepted_on_any_check_type() -> None:
    """description is documentation-only but the schema accepts it on every variant."""
    rec = CriterionRecord(
        **_base_kwargs("deterministic"),
        payload={"validator_name": "x", "description": "doc string"},
    )
    assert rec.payload.description == "doc string"


def test_extra_keys_are_allowed_on_deterministic_payload() -> None:
    """The deterministic dispatch path forwards the payload dict to the validator
    function, which may read ad-hoc keys. `extra='allow'` on the base preserves
    that contract."""
    rec = CriterionRecord(
        **_base_kwargs("deterministic"),
        payload={"validator_name": "x", "custom_threshold": 0.88},
    )
    assert rec.payload.model_dump()["custom_threshold"] == 0.88


# ── 4. Discriminator mismatch and check_type wiring ──────────────────────────


def test_payload_check_type_must_match_row_check_type() -> None:
    """If a caller hand-constructs CriterionRecord with a typed payload that
    doesn't match the row-level check_type (only possible via in-memory
    construction, not via DB load), the after-validator surfaces the mismatch.
    """
    deterministic_payload = DeterministicCriterionPayload(validator_name="x")
    with pytest.raises(ValueError, match="does not match row check_type"):
        CriterionRecord(**_base_kwargs("judge_prompt"), payload=deterministic_payload)


def test_payload_dict_without_check_type_is_resolved_via_row_discriminator() -> None:
    """DB rows carry check_type as a separate column, not inside the payload jsonb.
    The before-validator injects the row-level check_type into the payload dict
    so the discriminated union resolves correctly. This pins that contract.
    """
    rec = CriterionRecord(
        **_base_kwargs("deterministic"),
        payload={"validator_name": "x"},  # no check_type inside
    )
    assert rec.payload.check_type == "deterministic"


def test_each_check_type_resolves_to_its_own_variant() -> None:
    """Discriminator wiring spot-check across all four variants."""
    pairs = [
        ("deterministic", {"validator_name": "x"}, DeterministicCriterionPayload),
        ("judge_prompt", {}, JudgePromptCriterionPayload),
        ("span_assertion", {}, SpanAssertionCriterionPayload),
        ("scenario_fit", {"judge_prompt_template": "t"}, ScenarioFitCriterionPayload),
    ]
    for check_type, payload, expected_cls in pairs:
        rec = CriterionRecord(**_base_kwargs(check_type), payload=payload)
        assert isinstance(rec.payload, expected_cls), (
            f"check_type={check_type!r} resolved to {type(rec.payload).__name__}, "
            f"expected {expected_cls.__name__}"
        )
