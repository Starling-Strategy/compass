"""Tests for the clarify-stylist service."""

from __future__ import annotations

import pytest

from compass_backend.answer_layer.clarify import compose_clarify_question_async
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts.answer_layer import ClarifyDraft


class _FakeStylistAgent:
    def __init__(self, *, draft=None, raise_exc=None, capture=None):
        self._draft = draft
        self._raise = raise_exc
        self._capture = capture if capture is not None else []

    @property
    def captured_briefs(self):
        return self._capture

    async def run(self, prompt: str):
        self._capture.append(prompt)
        if self._raise is not None:
            raise self._raise
        return _FakeResult(self._draft)


class _FakeResult:
    def __init__(self, draft):
        self.output = draft


@pytest.mark.asyncio
async def test_compose_clarify_question_uses_stylist_question_when_present():
    candidates = [
        MetricCandidate(metric_id=10, name="Principal base salary"),
        MetricCandidate(metric_id=11, name="Teacher-leader stipend"),
    ]
    stylist = _FakeStylistAgent(
        draft=ClarifyDraft(
            question="Are you asking about principals or teacher-leaders?"
        )
    )
    result = await compose_clarify_question_async(
        "principal pay",
        operation="lookup",
        candidates=candidates,
        stylist_agent=stylist,
    )
    assert result.question == "Are you asking about principals or teacher-leaders?"
    assert result.missing_fields == ["metric"]
    assert result.candidates == ["Principal base salary", "Teacher-leader stipend"]


@pytest.mark.asyncio
async def test_compose_clarify_question_falls_back_to_fstring_on_stylist_exception():
    candidates = [
        MetricCandidate(metric_id=10, name="A"),
        MetricCandidate(metric_id=11, name="B"),
    ]
    stylist = _FakeStylistAgent(raise_exc=RuntimeError("gateway down"))
    result = await compose_clarify_question_async(
        "thing",
        operation="lookup",
        candidates=candidates,
        stylist_agent=stylist,
    )
    assert "I found a few Compass metrics" in result.question
    assert "\"thing\"" in result.question
    assert result.candidates == ["A", "B"]


@pytest.mark.asyncio
async def test_compose_clarify_question_passes_adjudicator_hint_to_brief():
    candidates = [
        MetricCandidate(metric_id=10, name="A"),
        MetricCandidate(metric_id=11, name="B"),
    ]
    stylist = _FakeStylistAgent(
        draft=ClarifyDraft(question="Q"),
    )
    await compose_clarify_question_async(
        "p",
        operation="lookup",
        candidates=candidates,
        adjudicator_hint="hint text",
        stylist_agent=stylist,
    )
    assert stylist.captured_briefs, "stylist was never called"
    prompt = stylist.captured_briefs[0]
    # The brief is serialized as JSON; assert the hint shows up verbatim.
    assert "hint text" in prompt
    assert "adjudicator_hint" in prompt


@pytest.mark.asyncio
async def test_compose_clarify_question_falls_back_when_output_validates_to_empty():
    candidates = [
        MetricCandidate(metric_id=10, name="A"),
        MetricCandidate(metric_id=11, name="B"),
    ]
    # A draft that fails validation (empty question) — model_validate raises.
    class _BadResult:
        output = {"question": ""}

    class _BadAgent:
        async def run(self, prompt):
            return _BadResult()

    result = await compose_clarify_question_async(
        "p",
        operation="lookup",
        candidates=candidates,
        stylist_agent=_BadAgent(),
    )
    # Falls back to f-string
    assert "I found a few Compass metrics" in result.question
