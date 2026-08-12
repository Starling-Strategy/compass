"""Candidate-only catalog adjudication contracts for Compass."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from compass_backend.instructions.loader import load_model_instructions
from compass_backend.agents.hooks import build_diagnostic_hooks
from compass_backend.agents.model_settings import AgentProfile, agent_model_settings
from compass_backend.agents.model_settings import model_settings_for_profile

AdjudicationAction = Literal[
    "select_one",
    "select_bundle",
    "select_with_alternates",
    "clarify",
    "no_match",
]

# Step 2 of the catalog-resolution-unification plan widens the adjudicator
# union to match
# :data:`compass_backend.contracts.catalog_resolution.ResolvedEntityType`.
# The two literals are intentionally kept structurally identical (and pinned
# by a sync test) rather than aliased — the adjudicator contract is older
# and downstream consumers may import it without pulling in the new
# reconciliation contracts. A drift between the two unions is a bug.
#
# Previously this only admitted ``{"district", "metric", "metric_bundle"}``,
# which meant the adjudicator could not represent picking
# ``profile_rank_field:enrollment`` over ``metric:enrollment`` even when the
# reconciler offered both as candidates. The SORT-NEW-02 / case 8 north-star
# regression depends on the adjudicator being able to choose the profile_rank
# drawer.
CatalogAdjudicationEntityType = Literal[
    "metric",
    "metric_bundle",
    "profile_rank_field",
    "profile_field",
    "district",
    "unsupported_concept",
    "peer_set",
    "topic",
]

CATALOG_ADJUDICATOR_INSTRUCTIONS = load_model_instructions("catalog_adjudicator.md")


class CatalogAdjudicationCandidate(BaseModel):
    """A bounded catalog candidate the adjudicator may choose."""

    model_config = ConfigDict(extra="forbid")

    entity_type: CatalogAdjudicationEntityType
    candidate_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str | None = None
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional decision-relevant attributes the adjudicator may read "
            "(e.g. degree_lane, coverage, academic year). Serialized into the "
            "adjudicator prompt, so it is part of the contract the model sees — "
            "keep keys stable and meaningful rather than dumping arbitrary state."
        ),
    )


class CatalogAdjudicationDecision(BaseModel):
    """Candidate-only catalog adjudication output."""

    model_config = ConfigDict(extra="forbid")

    action: AdjudicationAction
    selected_ids: list[str] = Field(default_factory=list)
    alternate_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of adjacent-variant candidates paired with the primary selection. "
            "These are named as alternatives in the response. Only populated for "
            "action='select_with_alternates'."
        ),
    )
    clarification_question: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)


@dataclass(frozen=True)
class CatalogAdjudicationDeps:
    """Runtime context supplied to the catalog adjudication agent."""

    query: str
    candidates: tuple[CatalogAdjudicationCandidate, ...]


def create_catalog_adjudicator_agent(
    model: Model | str | None = None,
) -> Agent[CatalogAdjudicationDeps, CatalogAdjudicationDecision]:
    """Create the bounded catalog adjudicator agent."""

    agent = Agent(
        model or agent_model_settings.catalog_adjudicator_model,
        deps_type=CatalogAdjudicationDeps,
        output_type=CatalogAdjudicationDecision,
        instructions=CATALOG_ADJUDICATOR_INSTRUCTIONS,
        model_settings=model_settings_for_profile(AgentProfile.CATALOG_ADJUDICATOR),
        retries={"output": 1},
        capabilities=[build_diagnostic_hooks("catalog_adjudicator")],
    )

    @agent.instructions
    def add_catalog_adjudication_context(ctx: RunContext[CatalogAdjudicationDeps]) -> str:
        return catalog_adjudication_context(ctx.deps)

    @agent.output_validator
    def validate_catalog_adjudicator_output(
        ctx: RunContext[CatalogAdjudicationDeps],
        decision: CatalogAdjudicationDecision,
    ) -> CatalogAdjudicationDecision:
        return validate_catalog_adjudication_decision(decision, ctx.deps.candidates)

    return agent


def catalog_adjudication_context(deps: CatalogAdjudicationDeps) -> str:
    """Return run-specific candidate context for the adjudicator prompt."""

    candidate_payload = [
        candidate.model_dump(mode="json") for candidate in deps.candidates
    ]
    return "\n".join(
        [
            f"User phrase: {deps.query}",
            "Provided candidates JSON:",
            json.dumps(candidate_payload, ensure_ascii=True, indent=2),
        ]
    )


def validate_catalog_adjudication_decision(
    decision: CatalogAdjudicationDecision,
    candidates: tuple[CatalogAdjudicationCandidate, ...]
    | list[CatalogAdjudicationCandidate],
) -> CatalogAdjudicationDecision:
    """Reject adjudication outputs that choose IDs outside the supplied candidates."""

    allowed_ids = {candidate.candidate_id for candidate in candidates}
    invalid_ids = [
        selected_id
        for selected_id in decision.selected_ids
        if selected_id not in allowed_ids
    ]
    if invalid_ids:
        raise ModelRetry(
            "Catalog adjudication selected IDs outside the provided candidates: "
            f"{', '.join(invalid_ids)}. Allowed candidate IDs: "
            f"{', '.join(sorted(allowed_ids)) or '(none)'}."
        )

    invalid_alternate_ids = [
        alt_id
        for alt_id in decision.alternate_ids
        if alt_id not in allowed_ids
    ]
    if invalid_alternate_ids:
        raise ModelRetry(
            "Catalog adjudication alternate_ids include IDs outside the provided "
            f"candidates: {', '.join(invalid_alternate_ids)}. Allowed candidate IDs: "
            f"{', '.join(sorted(allowed_ids)) or '(none)'}."
        )

    if decision.action == "select_with_alternates":
        if len(decision.selected_ids) != 1:
            raise ModelRetry(
                "Catalog adjudication select_with_alternates must choose exactly "
                "one primary ID in selected_ids."
            )
        if not decision.alternate_ids:
            raise ModelRetry(
                "Catalog adjudication select_with_alternates must populate "
                "alternate_ids with at least one ID."
            )
        if set(decision.alternate_ids) & set(decision.selected_ids):
            raise ModelRetry(
                "Catalog adjudication alternate_ids must be disjoint from "
                "selected_ids (no overlap with the primary)."
            )
    elif decision.alternate_ids:
        raise ModelRetry(
            "Catalog adjudication alternate_ids may only be populated when "
            "action='select_with_alternates'."
        )

    if decision.action == "select_one" and len(decision.selected_ids) != 1:
        raise ModelRetry("Catalog adjudication select_one must choose exactly one ID.")
    if decision.action == "select_bundle" and not decision.selected_ids:
        raise ModelRetry("Catalog adjudication select_bundle must choose candidate IDs.")
    if decision.action in {"clarify", "no_match"} and decision.selected_ids:
        raise ModelRetry(
            "Catalog adjudication clarify/no_match decisions cannot select IDs."
        )
    if decision.action == "clarify" and not decision.clarification_question:
        raise ModelRetry(
            "Catalog adjudication clarify decisions need a clarification question."
        )

    return decision
