"""Pydantic AI Toolset adapter for advisory Compass catalog recall."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.toolsets import FunctionToolset

from compass_backend.catalog.recall import (
    CatalogRecallService,
    RecallEntityType,
)
from compass_backend.instructions.loader import load_model_instructions


_DEFAULT_TOOL_ENTITY_TYPES: tuple[RecallEntityType, ...] = (
    "metric",
    "metric_bundle",
    "district",
    "profile_field",
    "unsupported_concept",
)
CATALOG_TOOLSET_INSTRUCTIONS = load_model_instructions("catalog_toolset.md")


class CompassCatalogToolOutput(BaseModel):
    """Model-facing advisory catalog search result."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    entity_types: list[RecallEntityType] = Field(default_factory=list)
    candidates: list[dict[str, object]] = Field(default_factory=list)
    advisory_only: bool = True
    authority_boundary: str = (
        "Candidate cards are planning aid only. Execution must re-resolve "
        "phrases through CatalogResolver; this tool never grants execution IDs, "
        "SQL, answer rows, citations, renderer writes, or final data authority."
    )


class CompassCatalogToolset(FunctionToolset[object]):
    """FunctionToolset wrapper around `CatalogRecallService`.

    The planner may use this only as candidate-finding help during a bounded
    finalization pass. Output deliberately uses `CandidateCard.to_model_context()`
    so internal refs and metadata never become model-facing execution authority.
    """

    def __init__(
        self,
        recall_service: CatalogRecallService,
        *,
        max_limit: int = 25,
    ) -> None:
        self._recall_service = recall_service
        self._max_limit = max(1, max_limit)
        super().__init__(
            id="compass_catalog",
            metadata={"authority": "advisory_catalog_recall"},
            instructions=CATALOG_TOOLSET_INSTRUCTIONS,
        )
        self.tool_plain(
            self.search_official_catalog_candidates,
            name="search_official_catalog_candidates",
            description=(
                "Search official Compass catalog candidate cards for a phrase. "
                "Returns advisory labels only; execution still re-resolves."
            ),
        )

    async def search_official_catalog_candidates(
        self,
        query: str,
        entity_types: list[RecallEntityType] | None = None,
        limit: int = 10,
        states: list[str] | None = None,
    ) -> CompassCatalogToolOutput:
        """Search official Compass catalog candidate cards for one phrase."""

        requested = tuple(entity_types or _DEFAULT_TOOL_ENTITY_TYPES)
        bounded_limit = min(max(limit, 1), self._max_limit)
        state_filter = {
            state.strip().upper()
            for state in states or []
            if state.strip()
        } or None
        report = await self._recall_service.recall(
            query,
            entity_types=requested,
            limit=bounded_limit,
            states=state_filter,
        )
        return CompassCatalogToolOutput(
            query=report.query,
            entity_types=list(requested),
            candidates=[
                candidate.to_model_context()
                for candidate in report.candidates[:bounded_limit]
            ],
        )


__all__ = ["CompassCatalogToolOutput", "CompassCatalogToolset"]
