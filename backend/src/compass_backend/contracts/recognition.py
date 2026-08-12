"""Advisory recognition contracts for planning-time catalog awareness."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PlanningRecognitionEntityType = Literal[
    "district",
    "metric",
    "metric_bundle",
    "profile_field",
    "peer_set",
    "unsupported_concept",
]
PlanningRecognitionStatus = Literal[
    "resolved",
    "ambiguous",
    "unsupported",
    "unresolved",
    "not_applicable",
]
PlanningRecognitionAction = Literal[
    "proceed",
    "clarify",
    "confirm_default",
    "refuse",
    "ignore",
]


class PlanningRecognitionCandidate(BaseModel):
    """A governed candidate found during advisory recognition."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    entity_type: PlanningRecognitionEntityType
    catalog_ref: str | None = Field(
        default=None,
        description=(
            "Debug-only catalog reference. Never include this in planner-visible "
            "context because the Planner must not choose execution IDs."
        ),
    )
    reason: str | None = None

    def planner_context(self) -> dict[str, object]:
        """Return planner-visible candidate data with execution refs removed."""

        payload: dict[str, object] = {
            "label": self.label,
            "entity_type": self.entity_type,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


class PlanningRecognitionMention(BaseModel):
    """Recognition result for one typed planner phrase."""

    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(min_length=1)
    entity_type: PlanningRecognitionEntityType
    status: PlanningRecognitionStatus
    recommended_action: PlanningRecognitionAction
    candidates: list[PlanningRecognitionCandidate] = Field(default_factory=list)
    message: str | None = None
    source_field: str | None = None

    def planner_context(self) -> dict[str, object]:
        """Return planner-visible recognition data with debug refs removed."""

        payload: dict[str, object] = {
            "phrase": self.phrase,
            "entity_type": self.entity_type,
            "status": self.status,
            "recommended_action": self.recommended_action,
            "candidates": [
                candidate.planner_context() for candidate in self.candidates
            ],
        }
        if self.message:
            payload["message"] = self.message
        if self.source_field:
            payload["source_field"] = self.source_field
        return payload


class PlanningRecognitionReport(BaseModel):
    """Advisory recognition facts produced before final execution."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["planner_draft", "manual_test"] = "planner_draft"
    mentions: list[PlanningRecognitionMention] = Field(default_factory=list)

    @property
    def requires_planner_finalization(self) -> bool:
        """Return whether the Planner should see the report before final route."""

        return any(
            mention.recommended_action in {"clarify", "confirm_default", "refuse"}
            for mention in self.mentions
        )

    @property
    def has_blockers(self) -> bool:
        """Return whether recognition found a clarify/refuse blocker."""

        return any(
            mention.recommended_action in {"clarify", "refuse"}
            for mention in self.mentions
        )

    def planner_context(self) -> dict[str, object]:
        """Return non-authoritative, planner-visible recognition context."""

        return {
            "source": self.source,
            "requires_planner_finalization": self.requires_planner_finalization,
            "mentions": [mention.planner_context() for mention in self.mentions],
        }
