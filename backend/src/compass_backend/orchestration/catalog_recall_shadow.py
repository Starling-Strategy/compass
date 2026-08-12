"""Catalog-recall shadow request planning for chat-turn orchestration.

Pure helpers that derive the advisory catalog-recall request list from a
planner turn (which phrases to look up, against which entity types, scoped to
which states) and de-duplicate it. Extracted verbatim from
`orchestration/chat.py` (#1130 decomposition); the recall entity-type and
skip-field constants these helpers consume travel with them. The async
``_run_catalog_recall_shadow_for_turn`` orchestrator stays in `chat` because it
sets span attributes that tests monkeypatch on the chat-module namespace.
Behaviour-preserving move — no logic changes.
"""

from compass_backend.catalog import RecallEntityType
from compass_backend.contracts.planning import PlannerTurn

_CATALOG_RECALL_ALL_TYPES: tuple[RecallEntityType, ...] = (
    "metric",
    "metric_bundle",
    "district",
    "profile_field",
    "topic",
    "glossary_term",
    "source_document",
    "unsupported_concept",
)
_CATALOG_RECALL_METRIC_TYPES: tuple[RecallEntityType, ...] = (
    "metric",
    "metric_bundle",
    "topic",
    "unsupported_concept",
)
_CATALOG_RECALL_PROFILE_FIELD_TYPES: tuple[RecallEntityType, ...] = (
    "profile_field",
)
_CATALOG_RECALL_DISTRICT_TYPES: tuple[RecallEntityType, ...] = ("district",)
_CATALOG_RECALL_FILTER_TYPES: tuple[RecallEntityType, ...] = (
    "metric",
    "metric_bundle",
    "profile_field",
    "unsupported_concept",
)
_CATALOG_RECALL_SKIP_FIELDS = {
    "district",
    "district_name",
    "state",
    "value",
    "enrollment",
}

def _catalog_recall_requests_for_turn(
    user_message: str,
    turn: PlannerTurn,
) -> list[tuple[str, tuple[RecallEntityType, ...]]]:
    requests: list[tuple[str, tuple[RecallEntityType, ...]]] = []
    _append_recall_request(requests, user_message, _CATALOG_RECALL_ALL_TYPES)
    if turn.route != "execute" or turn.query_plan is None:
        return requests

    plan = turn.query_plan
    for district in plan.selection.districts:
        _append_recall_request(
            requests,
            district,
            _CATALOG_RECALL_DISTRICT_TYPES,
        )
    if plan.similarity is not None:
        _append_recall_request(
            requests,
            plan.similarity.anchor_name,
            _CATALOG_RECALL_DISTRICT_TYPES,
        )
    for metric in plan.metrics:
        _append_recall_request(
            requests,
            metric.name,
            _CATALOG_RECALL_METRIC_TYPES,
        )
    for profile_field in plan.profile_fields:
        _append_recall_request(
            requests,
            profile_field.name,
            _CATALOG_RECALL_PROFILE_FIELD_TYPES,
        )
    for filter_spec in plan.filters:
        if filter_spec.field.casefold() not in _CATALOG_RECALL_SKIP_FIELDS:
            _append_recall_request(
                requests,
                filter_spec.field,
                _CATALOG_RECALL_FILTER_TYPES,
            )
        if filter_spec.threshold_hint is not None:
            _append_recall_request(
                requests,
                filter_spec.threshold_hint,
                ("glossary_term", "unsupported_concept"),
            )
        if filter_spec.anchor_value is not None:
            _append_recall_request(
                requests,
                filter_spec.anchor_value.district,
                _CATALOG_RECALL_DISTRICT_TYPES,
            )
            if filter_spec.anchor_value.metric is not None:
                _append_recall_request(
                    requests,
                    filter_spec.anchor_value.metric,
                    _CATALOG_RECALL_METRIC_TYPES,
                )
    if plan.sort is not None and plan.sort.field.casefold() not in (
        _CATALOG_RECALL_SKIP_FIELDS
    ):
        _append_recall_request(
            requests,
            plan.sort.field,
            _CATALOG_RECALL_FILTER_TYPES,
        )
    for step in plan.sort_steps:
        if step.field.casefold() in _CATALOG_RECALL_SKIP_FIELDS:
            continue
        entity_types = (
            _CATALOG_RECALL_PROFILE_FIELD_TYPES
            if step.key_type == "profile_field"
            else _CATALOG_RECALL_METRIC_TYPES
        )
        _append_recall_request(requests, step.field, entity_types)
    return requests


def _catalog_recall_states_for_turn(turn: PlannerTurn) -> set[str] | None:
    if turn.route != "execute" or turn.query_plan is None:
        return None
    states = {
        state.upper()
        for state in turn.query_plan.selection.states
        if isinstance(state, str) and state
    }
    return states or None


def _append_recall_request(
    requests: list[tuple[str, tuple[RecallEntityType, ...]]],
    phrase: str,
    entity_types: tuple[RecallEntityType, ...],
) -> None:
    normalized = phrase.strip()
    if not normalized:
        return
    key = (normalized.casefold(), entity_types)
    existing = {(item[0].casefold(), item[1]) for item in requests}
    if key in existing:
        return
    requests.append((normalized, entity_types))
