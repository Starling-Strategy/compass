"""Contracts for the guarded Compass answer layer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.contracts.answer_layer import (
    AnswerBrief,
    AnswerDraft,
    AnswerFact,
    AnswerLayerFinding,
    AnswerLayerReport,
    ClarifyBrief,
    ClarifyDraft,
    ImmutableBlock,
)


def test_answer_layer_contracts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AnswerDraft(body="A valid-looking body.", unexpected=True)  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        AnswerBrief(
            deterministic_body="Compass rendered body.",
            result_type="metric_lookup",
            unknown="not allowed",  # type: ignore[call-arg]
        )


def test_answer_brief_keeps_sealed_authority_fields_typed() -> None:
    brief = AnswerBrief(
        user_question="Show starting salaries for Denver.",
        deterministic_body="Denver starting salary is $55,000 in 2024-2025. [1]",
        result_type="metric_lookup",
        facts=(AnswerFact(label="district", value="Denver Public Schools"),),
        immutable_blocks=(
            ImmutableBlock(kind="table", text="| District | Value |\n| --- | ---: |\n| Denver | $55,000 |"),
        ),
        numeric_tokens=("$55,000", "2024", "2025"),
        source_markers=("[1]",),
        caveat_fragments=("reviewed data",),
        allowed_nctq_context=("NCTQ has curated examples for this topic.",),
    )

    assert brief.result_type == "metric_lookup"
    assert brief.immutable_blocks[0].kind == "table"
    assert brief.source_markers == ("[1]",)


def test_answer_layer_report_metadata_excludes_draft_body() -> None:
    report = AnswerLayerReport(
        mode="shadow",
        attempted=True,
        accepted=False,
        reason="new_numeric_token",
        findings=(
            AnswerLayerFinding(
                code="new_numeric_token",
                message="Draft introduced $99,999.",
            ),
        ),
        draft_body="Do not send this draft to clients through metadata.",
    )

    metadata = report.to_manifest_metadata()

    assert metadata["mode"] == "shadow"
    assert metadata["accepted"] is False
    assert "findings" not in metadata
    assert "reason" not in metadata
    assert "draft_available" not in metadata
    assert "draft_body" not in metadata


def test_clarify_brief_requires_non_empty_phrase_and_candidates():
    with pytest.raises(ValidationError):
        ClarifyBrief(
            metric_phrase="",
            operation="lookup",
            candidate_labels=("a",),
        )
    with pytest.raises(ValidationError):
        ClarifyBrief(
            metric_phrase="teacher salary",
            operation="lookup",
            candidate_labels=(),
        )


def test_clarify_brief_accepts_adjudicator_hint():
    brief = ClarifyBrief(
        metric_phrase="principal pay",
        operation="lookup",
        candidate_labels=("Principal base salary", "Teacher-leader stipend"),
        adjudicator_hint="Admin pay or teacher-leader pay?",
    )
    assert brief.adjudicator_hint == "Admin pay or teacher-leader pay?"


def test_clarify_brief_adjudicator_hint_defaults_to_none():
    brief = ClarifyBrief(
        metric_phrase="principal pay",
        operation="lookup",
        candidate_labels=("a", "b"),
    )
    assert brief.adjudicator_hint is None


def test_clarify_draft_requires_non_empty_question():
    with pytest.raises(ValidationError):
        ClarifyDraft(question="")
    draft = ClarifyDraft(question="Are you asking about admin pay or stipend?")
    assert draft.question == "Are you asking about admin pay or stipend?"
