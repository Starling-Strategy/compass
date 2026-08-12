"""Extract typed planner phrases for advisory recognition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from compass_backend.contracts.planning import PlannerTurn, QueryPlan
from compass_backend.reference.profile_fields import profile_field_key

MentionEntityType = Literal["district", "metric", "profile_field", "peer_set"]


@dataclass(frozen=True)
class PlanningMention:
    """One typed planner phrase that can be recognized against the catalog."""

    phrase: str
    entity_type: MentionEntityType
    source_field: str


def extract_planning_mentions(turn: PlannerTurn) -> list[PlanningMention]:
    """Return typed phrases from an execute draft without reading raw user prose."""

    if turn.route != "execute" or turn.query_plan is None:
        return []
    return mentions_from_plan(turn.query_plan)


def mentions_from_plan(plan: QueryPlan) -> list[PlanningMention]:
    """Return typed phrases for a :class:`QueryPlan` (no turn wrapping needed).

    Exposed for the step-5 catalog pipeline orchestrator which has the
    plan directly and does not need the route gate that
    :func:`extract_planning_mentions` enforces.
    """

    return _mentions_from_plan(plan)


def _mentions_from_plan(plan: QueryPlan) -> list[PlanningMention]:
    mentions: list[PlanningMention] = []

    for district in plan.selection.districts:
        mentions.append(
            PlanningMention(
                phrase=district,
                entity_type="district",
                source_field="selection.districts",
            )
        )

    for metric in plan.metrics:
        mentions.append(
            PlanningMention(
                phrase=metric.name,
                entity_type="metric",
                source_field="metrics.name",
            )
        )

    for profile_field in plan.profile_fields:
        mentions.append(
            PlanningMention(
                phrase=profile_field.name,
                entity_type="profile_field",
                source_field="profile_fields.name",
            )
        )

    from compass_backend.execution.filters import filter_field_is_reserved

    for filter_spec in plan.filters:
        if not filter_field_is_reserved(filter_spec):
            mentions.append(
                PlanningMention(
                    phrase=filter_spec.field,
                    entity_type="metric",
                    source_field="filters.field",
                )
            )

    if plan.sort is not None and plan.sort.field not in {"state", "district"}:
        sort_profile_key = profile_field_key(plan.sort.field)
        mentions.append(
            PlanningMention(
                phrase=sort_profile_key or plan.sort.field,
                entity_type="profile_field" if sort_profile_key else "metric",
                source_field="sort.field",
            )
        )

    for sort_step in plan.sort_steps:
        entity_type: MentionEntityType = (
            "profile_field" if sort_step.key_type == "profile_field" else "metric"
        )
        if sort_step.key_type in {"profile_field", "policy_metric"}:
            mentions.append(
                PlanningMention(
                    phrase=sort_step.field,
                    entity_type=entity_type,
                    source_field="sort_steps.field",
                )
            )

    if plan.similarity is not None:
        mentions.append(
            PlanningMention(
                phrase=plan.similarity.anchor_name,
                entity_type="district",
                source_field="similarity.anchor_name",
            )
        )

    return _dedupe_mentions(mentions)


def _dedupe_mentions(mentions: list[PlanningMention]) -> list[PlanningMention]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[PlanningMention] = []
    for mention in mentions:
        key = (
            mention.entity_type,
            mention.phrase.casefold().strip(),
            mention.source_field,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(mention)
    return deduped
