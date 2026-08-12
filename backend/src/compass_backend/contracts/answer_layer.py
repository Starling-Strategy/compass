"""Contracts for the guarded Compass answer layer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


AnswerLayerMode = Literal["off", "shadow", "gated"]
AnswerLayerStatus = Literal[
    "disabled",
    "shadow_generated",
    "accepted",
    "rejected",
    "fallback",
]


class AnswerFact(BaseModel):
    """One sealed fact the answer layer may reference."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    value: str = Field(min_length=1)
    source_markers: tuple[str, ...] = ()


class ImmutableBlock(BaseModel):
    """Markdown span the answer layer must preserve exactly."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["table", "sources", "methodology", "section_heading", "other"]
    text: str = Field(min_length=1)
    block_id: str | None = None


class AdjacentMetric(BaseModel):
    """A label-only reference to a metric the stylist may name as an alternative."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)


class ClarifyBrief(BaseModel):
    """Sealed input to the clarify stylist."""

    model_config = ConfigDict(extra="forbid")

    metric_phrase: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    candidate_labels: tuple[str, ...] = Field(min_length=1)
    adjudicator_hint: str | None = None


class ClarifyDraft(BaseModel):
    """Structured output from the clarify stylist."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)


class AnswerBrief(BaseModel):
    """Sealed deterministic answer evidence sent to the answer stylist."""

    model_config = ConfigDict(extra="forbid")

    deterministic_body: str = Field(min_length=1)
    user_question: str = ""
    route: str = "execute"
    result_type: str | None = None
    artifact_id: str | None = None
    manifest_metadata: dict[str, object] = Field(default_factory=dict)
    facts: tuple[AnswerFact, ...] = ()
    immutable_blocks: tuple[ImmutableBlock, ...] = ()
    numeric_tokens: tuple[str, ...] = ()
    source_markers: tuple[str, ...] = ()
    caveat_fragments: tuple[str, ...] = ()
    allowed_nctq_context: tuple[str, ...] = ()
    suggested_followups: tuple[str, ...] = ()
    # #1240: True when the user asked for a chart but the data could not support
    # one. The stylist mentions this naturally (default.md <chart_unavailable>);
    # the structured signal survives stylization, unlike appended markdown.
    chart_unavailable: bool = False
    style_notes: tuple[str, ...] = ()
    adjacent_metrics: tuple[AdjacentMetric, ...] = ()
    attached_artifacts: tuple[str, ...] = ()


class AnswerDraft(BaseModel):
    """Structured output from the answer stylist."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    used_fact_labels: tuple[str, ...] = ()
    preserved_block_ids: tuple[str, ...] = ()


class AnswerLayerFinding(BaseModel):
    """Structured validation finding for answer-layer review."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class AnswerLayerReport(BaseModel):
    """Safety decision for one answer-layer attempt."""

    model_config = ConfigDict(extra="forbid")

    mode: AnswerLayerMode
    attempted: bool = False
    accepted: bool = False
    status: AnswerLayerStatus | None = None
    reason: str | None = None
    findings: tuple[AnswerLayerFinding, ...] = ()
    draft_body: str | None = None

    def to_manifest_metadata(self) -> dict[str, object]:
        """Return client-safe manifest metadata without internal diagnostics."""

        return {
            "mode": self.mode,
            "attempted": self.attempted,
            "accepted": self.accepted,
            "status": self.status,
        }


__all__ = [
    "AdjacentMetric",
    "AnswerBrief",
    "AnswerDraft",
    "AnswerFact",
    "AnswerLayerFinding",
    "AnswerLayerMode",
    "AnswerLayerReport",
    "AnswerLayerStatus",
    "ClarifyBrief",
    "ClarifyDraft",
    "ImmutableBlock",
]
