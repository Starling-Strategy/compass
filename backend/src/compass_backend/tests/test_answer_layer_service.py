"""Service behavior for the guarded Compass answer layer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from compass_backend.agents import model_settings as agent_model_settings
from compass_backend.answer_layer.service import (
    allowed_answer_layer_result_types,
    improve_answer,
)
from compass_backend.config import Settings
from compass_backend.contracts.answer_layer import AnswerDraft
from compass_backend.contracts.rendering import ResponseManifest


DETERMINISTIC_BODY = """Denver's starting salary is $55,000 for 2024-2025. [1]

This uses currently reviewed data and may not include every schedule lane.

| District | State | Starting salary | Sources |
| --- | --- | ---: | --- |
| Denver Public Schools | CO | $55,000 | [1] |

Sources

- [1] Denver collective bargaining agreement
"""

IMPROVED_BODY = """Compass has reviewed starting-salary data for Denver for 2024-2025.

This uses currently reviewed data and may not include every schedule lane.

| District | State | Starting salary | Sources |
| --- | --- | ---: | --- |
| Denver Public Schools | CO | $55,000 | [1] |

Sources

- [1] Denver collective bargaining agreement
"""


class FakeStylist:
    def __init__(
        self,
        *,
        draft: AnswerDraft | None = None,
        error: Exception | None = None,
    ) -> None:
        self.draft = draft
        self.error = error
        self.prompts: list[str] = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        assert self.draft is not None
        return SimpleNamespace(output=self.draft)


def _manifest(result_type: str | None = "metric_lookup") -> ResponseManifest:
    return ResponseManifest(
        body=DETERMINISTIC_BODY,
        status="rendered",
        result_type=result_type,
        validation_valid=True,
        metadata={"question": "What is Denver's salary schedule?"},
    )


def test_settings_answer_layer_defaults_are_gated_and_allow_expected_types() -> None:
    settings = Settings()

    assert settings.answer_layer_mode == "gated"
    assert settings.answer_layer_result_types == (
        "metric_lookup,metric_ranking,metric_count,policy_guidance"
    )
    assert allowed_answer_layer_result_types(settings.answer_layer_result_types) == {
        "metric_lookup",
        "metric_ranking",
        "metric_count",
        "policy_guidance",
    }


def test_settings_rejects_unknown_answer_layer_mode() -> None:
    with pytest.raises(ValidationError):
        Settings(answer_layer_mode="always")  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_improve_answer_off_mode_preserves_body_without_model_call() -> None:
    stylist = FakeStylist(draft=AnswerDraft(body=IMPROVED_BODY))

    body, report = await improve_answer(
        _manifest(),
        mode="off",
        allowed_result_types="metric_lookup",
        stylist_agent=stylist,
    )

    assert body == DETERMINISTIC_BODY
    assert report.status == "disabled"
    assert report.attempted is False
    assert stylist.prompts == []


@pytest.mark.anyio
async def test_improve_answer_shadow_mode_keeps_body_and_records_safe_report() -> None:
    stylist = FakeStylist(draft=AnswerDraft(body=IMPROVED_BODY))

    body, report = await improve_answer(
        _manifest(),
        mode="shadow",
        allowed_result_types="metric_lookup",
        stylist_agent=stylist,
    )

    metadata = report.to_manifest_metadata()
    assert body == DETERMINISTIC_BODY
    assert report.status == "shadow_generated"
    assert report.attempted is True
    assert report.accepted is False
    assert "draft_available" not in metadata
    assert "findings" not in metadata
    assert "draft_body" not in metadata
    assert stylist.prompts and "deterministic_body" in stylist.prompts[0]


@pytest.mark.anyio
async def test_improve_answer_gated_mode_accepts_unchanged_draft() -> None:
    stylist = FakeStylist(draft=AnswerDraft(body=DETERMINISTIC_BODY))

    body, report = await improve_answer(
        _manifest(),
        mode="gated",
        allowed_result_types="metric_lookup",
        stylist_agent=stylist,
    )

    assert body == DETERMINISTIC_BODY
    assert report.status == "accepted"
    assert report.accepted is True
    assert report.findings == ()


@pytest.mark.anyio
async def test_improve_answer_gated_mode_returns_safe_rewrite() -> None:
    stylist = FakeStylist(draft=AnswerDraft(body=IMPROVED_BODY))

    body, report = await improve_answer(
        _manifest(),
        mode="gated",
        allowed_result_types="metric_lookup",
        stylist_agent=stylist,
    )

    assert body == IMPROVED_BODY
    assert report.status == "accepted"
    assert report.accepted is True
    assert report.findings == ()


@pytest.mark.anyio
async def test_improve_answer_gated_mode_rejects_unsafe_draft_and_falls_back() -> None:
    unsafe_body = IMPROVED_BODY.replace("$55,000", "$56,000", 1)
    stylist = FakeStylist(draft=AnswerDraft(body=unsafe_body))

    body, report = await improve_answer(
        _manifest(),
        mode="gated",
        allowed_result_types="metric_lookup",
        stylist_agent=stylist,
    )

    assert body == DETERMINISTIC_BODY
    assert report.status == "rejected"
    assert report.accepted is False
    assert any(finding.code == "new_numeric_token" for finding in report.findings)


@pytest.mark.anyio
async def test_improve_answer_falls_back_when_model_call_fails() -> None:
    stylist = FakeStylist(error=RuntimeError("model unavailable"))

    body, report = await improve_answer(
        _manifest(),
        mode="gated",
        allowed_result_types="metric_lookup",
        stylist_agent=stylist,
    )

    assert body == DETERMINISTIC_BODY
    assert report.status == "fallback"
    assert report.accepted is False
    assert [finding.code for finding in report.findings] == [
        "answer_layer_exception"
    ]


@pytest.mark.anyio
async def test_improve_answer_skips_disallowed_result_type() -> None:
    stylist = FakeStylist(draft=AnswerDraft(body=IMPROVED_BODY))

    body, report = await improve_answer(
        _manifest("metric_trend"),
        mode="gated",
        allowed_result_types="metric_lookup",
        stylist_agent=stylist,
    )

    assert body == DETERMINISTIC_BODY
    assert report.status == "disabled"
    assert report.reason == "result_type_not_allowed"
    assert stylist.prompts == []


class _SlowStylist:
    async def run(self, _prompt: str):
        await asyncio.sleep(5)
        raise AssertionError("should have timed out")


@pytest.mark.anyio
async def test_improve_answer_times_out_to_deterministic_body(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_model_settings.agent_model_settings,
        "answer_stylist_timeout_seconds",
        0.05,
    )
    body, report = await improve_answer(
        _manifest(),
        mode="gated",
        allowed_result_types="metric_lookup",
        stylist_agent=_SlowStylist(),
    )
    assert body == DETERMINISTIC_BODY
    assert report.status == "fallback"
    assert report.attempted is True
