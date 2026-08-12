"""Catalog-owned resolution metadata for execution and gate diagnostics."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

CatalogResolutionEntityType = Literal[
    "district",
    "metric",
    "metric_bundle",
    "topic",
    "profile_field",
    "region",
    "peer_method",
    "source_concept",
    "unsupported_concept",
]
# Report-layer rollup status for a resolved/rejected phrase, surfaced in
# diagnostics. Deliberately DISTINCT from
# ``catalog.resolution.CatalogResolutionStatus`` (the resolver's per-resolution
# outcome: approved/ambiguous/conditional/out_of_universe/rejected). The two
# vocabularies overlap on approved/ambiguous/out_of_universe but are not the
# same set, so they are separate types — renamed here to end the #1588 name
# clash (same name, different members, two modules).
CatalogReportStatus = Literal[
    "approved",
    "ambiguous",
    "unsupported",
    "unresolved",
    "out_of_universe",
]
CatalogResolutionMethod = Literal[
    "exact",
    "alias",
    "candidate_adjudicated",
    "catalog_search",
    "topic_narrowing",
    "static_catalog",
    "unsupported_catalog",
    "unresolved",
    "out_of_universe",
]


class CatalogResolutionCandidate(BaseModel):
    """One bounded candidate considered by catalog resolution."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    entity_type: CatalogResolutionEntityType
    metadata: dict[str, Any] = Field(default_factory=dict)


class CatalogResolutionEntity(BaseModel):
    """One phrase resolved or rejected by catalog authority."""

    model_config = ConfigDict(extra="forbid")

    input_phrase: str = Field(min_length=1)
    entity_type: CatalogResolutionEntityType
    status: CatalogReportStatus
    resolution_method: CatalogResolutionMethod
    approved_ids: list[str] = Field(default_factory=list)
    approved_key: str | None = None
    label: str | None = None
    candidates: list[CatalogResolutionCandidate] = Field(default_factory=list)
    provenance: str = Field(min_length=1)
    message: str | None = None


class CatalogResolutionBlocker(BaseModel):
    """Catalog resolution outcome that should stop or clarify execution."""

    model_config = ConfigDict(extra="forbid")

    input_phrase: str = Field(min_length=1)
    entity_type: CatalogResolutionEntityType
    status: CatalogReportStatus
    message: str = Field(min_length=1)
    candidates: list[CatalogResolutionCandidate] = Field(default_factory=list)


class CatalogResolutionReport(BaseModel):
    """Internal report of actual catalog authority used for one plan."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    entities: list[CatalogResolutionEntity] = Field(default_factory=list)
    blockers: list[CatalogResolutionBlocker] = Field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        """Return whether catalog resolution produced blocking outcomes."""

        return bool(self.blockers)

    @property
    def unsupported_blockers(self) -> list[CatalogResolutionBlocker]:
        """Return blockers that represent unsupported catalog concepts."""

        return [blocker for blocker in self.blockers if blocker.status == "unsupported"]

    def summary_methods(self) -> list[str]:
        """Return unique resolution methods for debug and gate reports."""

        methods: list[str] = []
        for entity in self.entities:
            if entity.resolution_method not in methods:
                methods.append(entity.resolution_method)
        return methods

    def summary_status(self) -> CatalogReportStatus:
        """Return the highest-severity status for this report."""

        priority: tuple[CatalogReportStatus, ...] = (
            "unsupported",
            "ambiguous",
            "unresolved",
            "out_of_universe",
            "approved",
        )
        statuses = {entity.status for entity in self.entities}
        for status in priority:
            if status in statuses:
                return status
        return "approved"

    def merged(self, *reports: "CatalogResolutionReport | None") -> Self:
        """Return a report containing this report plus later report entities."""

        entities = list(self.entities)
        blockers = list(self.blockers)
        for report in reports:
            if report is None:
                continue
            entities.extend(report.entities)
            blockers.extend(report.blockers)
        return self.model_copy(update={"entities": entities, "blockers": blockers})


def blocker_for_entity(entity: CatalogResolutionEntity) -> CatalogResolutionBlocker:
    """Build the standard blocker shape for a non-approved entity."""

    return CatalogResolutionBlocker(
        input_phrase=entity.input_phrase,
        entity_type=entity.entity_type,
        status=entity.status,
        message=entity.message
        or f"Compass could not resolve {entity.input_phrase!r} for execution.",
        candidates=entity.candidates,
    )


def report_from_entities(
    *,
    question: str,
    operation: str,
    entities: list[CatalogResolutionEntity],
) -> CatalogResolutionReport:
    """Build a report and derive blockers from non-approved entities."""

    return CatalogResolutionReport(
        question=question,
        operation=operation,
        entities=entities,
        blockers=[
            blocker_for_entity(entity)
            for entity in entities
            if entity.status != "approved"
        ],
    )
