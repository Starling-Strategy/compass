"""Backend-only conversation memory helpers for Compass sessions."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from compass_backend.artifacts import ResultSet
from compass_backend.contracts import (
    ClarificationOption,
    ConversationMemory,
    ConversationSummary,
    CONTEXT_RESULT_REF_ITEM_LIMIT,
    MEMORY_TEXT_MAX_LENGTH,
    PendingQueryContext,
    PolicyGuidanceContext,
    PolicyGuidanceExemplarRef,
    QueryContext,
    QueryContextDistrictRef,
    QueryContextMetricRef,
    RESULT_MEMORY_REF_LIMIT,
    ResponseManifest,
    ResultMemoryRef,
    SUMMARY_TEXT_MAX_LENGTH,
    TurnSnapshot,
)

# W1-09 (#850): single source of truth for the prior-result district/metric
# ref cap (was duplicated as 25 in both this module and orchestration/chat.py
# while the chat.py docstring claimed 200). Reconciled to 200 — the value
# the M3 covered-universe follow-up cycle needs to round-trip ~133-district
# selections without silent truncation. chat.py imports this constant
# instead of defining its own.
_MAX_CONTEXT_RESULT_REFS = CONTEXT_RESULT_REF_ITEM_LIMIT
_MAX_SOURCE_SNAPSHOT_IDS = 10
_MAX_MEMORY_TEXT = MEMORY_TEXT_MAX_LENGTH
_MAX_SUMMARY_TEXT = SUMMARY_TEXT_MAX_LENGTH


def build_result_memory_ref(
    snapshot: TurnSnapshot,
    result: ResultSet,
    *,
    query_context: QueryContext | None = None,
) -> ResultMemoryRef:
    """Build a compact pointer to one validated result artifact."""

    context = query_context or snapshot.query_context
    question = _shorten(
        context.query_plan.question
        if context is not None
        else snapshot.user_message,
        _MAX_MEMORY_TEXT,
    )
    displayed = context.displayed_row_count if context is not None else None
    if displayed is None:
        displayed = len(_memory_rows(result))
    display_limit = context.display_limit if context is not None else None
    metrics = _result_metric_refs(result)
    districts = _result_district_refs(result)
    return ResultMemoryRef(
        snapshot_id=snapshot.snapshot_id,
        turn_index=snapshot.turn_index,
        question=question,
        result_type=result.result_type,
        row_count=len(_memory_rows(result)),
        displayed_row_count=displayed,
        display_limit=display_limit,
        metrics=metrics,
        districts=districts,
        has_chart=result.chart is not None,
        has_csv_export=result.csv_export is not None,
        digest=_result_digest(result, metrics=metrics, districts=districts),
    )


def append_result_memory_ref(
    existing: Sequence[ResultMemoryRef],
    new_ref: ResultMemoryRef | None,
    *,
    limit: int = RESULT_MEMORY_REF_LIMIT,
) -> list[ResultMemoryRef]:
    """Append one result ref and keep the newest unique refs."""

    refs = list(existing)
    if new_ref is not None:
        refs = [ref for ref in refs if ref.snapshot_id != new_ref.snapshot_id]
        refs.append(new_ref)
    refs = sorted(refs, key=lambda ref: ref.turn_index)
    return refs[-limit:]


def build_policy_guidance_context(
    snapshot: TurnSnapshot,
    manifest: ResponseManifest,
) -> PolicyGuidanceContext | None:
    """Build typed follow-up memory from a rendered policy-guidance response."""

    if snapshot.planner_turn.route != "policy_guidance":
        return None
    plan = snapshot.planner_turn.policy_guidance
    if plan is None:
        return None
    if manifest.status != "rendered" or manifest.result_type != "policy_guidance":
        return None

    raw_citations = manifest.metadata.get("citations", [])
    if not isinstance(raw_citations, list):
        return None

    exemplar_refs: list[PolicyGuidanceExemplarRef] = []
    for raw_citation in raw_citations:
        if not isinstance(raw_citation, dict):
            continue
        if raw_citation.get("citation_type") != "exemplar":
            continue

        exemplar_id = _metadata_str(raw_citation.get("stable_id"))
        topic_id = _metadata_str(raw_citation.get("topic_id"))
        source_url = _metadata_str(raw_citation.get("url"))
        citation_status = _metadata_str(raw_citation.get("citation_status"))
        title = _metadata_str(raw_citation.get("title"))
        district = _metadata_str(raw_citation.get("district"))
        subtopic = _metadata_str(raw_citation.get("subtopic"))
        if title is not None:
            title_district, title_subtopic = _split_exemplar_title(title)
            district = district or title_district
            subtopic = subtopic or title_subtopic
        if topic_id is None and len(plan.topic_ids) == 1:
            topic_id = plan.topic_ids[0]
        district_id = raw_citation.get("district_id")

        if (
            exemplar_id is None
            or topic_id is None
            or district is None
            or subtopic is None
            or source_url is None
            or citation_status is None
        ):
            continue

        exemplar_refs.append(
            PolicyGuidanceExemplarRef(
                exemplar_id=exemplar_id,
                topic_id=topic_id,
                district=district,
                district_id=district_id if isinstance(district_id, int) else None,
                subtopic=subtopic,
                source_url=source_url,
                citation_status=citation_status,
                citation_title=title,
            )
        )

    if not exemplar_refs:
        return None

    return PolicyGuidanceContext(
        topic_ids=list(plan.topic_ids),
        layers=list(plan.layers),
        focus_terms=list(plan.focus_terms),
        primary_topic_id=plan.primary_topic_id,
        intent_summary=plan.intent_summary,
        exemplars=exemplar_refs,
        turn_index=snapshot.turn_index,
        snapshot_id=snapshot.snapshot_id,
        user_message=_shorten(snapshot.user_message.strip(), _MAX_MEMORY_TEXT),
    )


def result_memory_refs_from_snapshots(
    snapshots: Sequence[TurnSnapshot],
    *,
    limit: int = RESULT_MEMORY_REF_LIMIT,
) -> list[ResultMemoryRef]:
    """Reconstruct recent result refs from persisted turn snapshots."""

    by_snapshot_id: dict[str, ResultMemoryRef] = {}
    for snapshot in sorted(snapshots, key=lambda item: item.turn_index):
        for ref in snapshot.result_refs:
            by_snapshot_id[ref.snapshot_id] = ref
        if snapshot.result is not None:
            by_snapshot_id[snapshot.snapshot_id] = build_result_memory_ref(
                snapshot,
                snapshot.result,
            )
    refs = sorted(by_snapshot_id.values(), key=lambda ref: ref.turn_index)
    return refs[-limit:]


def conversation_memory_from_snapshots(
    snapshots: Sequence[TurnSnapshot],
) -> ConversationMemory | None:
    """Reconstruct the latest unified memory envelope from persisted snapshots."""

    ordered = sorted(snapshots, key=lambda item: item.turn_index)
    if not ordered:
        return None
    latest_memory = next(
        (
            snapshot.memory
            for snapshot in reversed(ordered)
            if snapshot.memory is not None
        ),
        None,
    )
    latest_query_context = next(
        (
            snapshot.query_context
            for snapshot in reversed(ordered)
            if snapshot.query_context is not None
        ),
        None,
    )
    latest = ordered[-1]
    base = latest_memory or ConversationMemory()
    return base.model_copy(
        update={
            "latest_query_context": latest_query_context,
            "pending_query_context": latest.pending_query_context,
            "result_refs": result_memory_refs_from_snapshots(ordered),
            "recent_routes": recent_routes_from_snapshots(ordered),
            "latest_turn_index": latest.turn_index,
            "source_snapshot_ids": _source_snapshot_ids_from_snapshots(ordered),
            "message_history": message_history_from_snapshots(ordered),
        },
        deep=True,
    )


def conversation_memory_after_turn(
    previous: ConversationMemory | None,
    snapshot: TurnSnapshot,
    *,
    latest_query_context: QueryContext | None = None,
    latest_policy_guidance_context: PolicyGuidanceContext | None = None,
    pending_query_context: PendingQueryContext | None = None,
    result_refs: Sequence[ResultMemoryRef] | None = None,
) -> ConversationMemory:
    """Update the unified memory envelope from one persisted turn."""

    previous = previous or ConversationMemory()
    previous_summary = previous.summary
    open_questions = list(previous_summary.open_questions) if previous_summary else []
    accepted_choices = (
        list(previous_summary.accepted_choices) if previous_summary else []
    )
    rejected_choices = (
        list(previous_summary.rejected_choices) if previous_summary else []
    )
    user_preferences = (
        list(previous_summary.user_preferences) if previous_summary else []
    )
    last_options: list[str] = (
        list(previous_summary.last_clarification_options)
        if previous_summary
        else []
    )

    # #1348: structured clarification options persist ONLY for the turn that
    # offered them. Default-empty on every non-clarify turn clears them once
    # the user answers, so a later click can't resume a stale choice.
    pending_clarification_options: list[ClarificationOption] = []
    if snapshot.planner_turn.route == "clarify" and snapshot.planner_turn.clarification:
        clarification = snapshot.planner_turn.clarification
        open_questions = _append_unique(
            open_questions,
            _shorten(clarification.question, _MAX_MEMORY_TEXT),
        )[-10:]
        last_options = [
            _shorten(candidate, _MAX_MEMORY_TEXT)
            for candidate in clarification.candidates[:12]
        ]
        pending_clarification_options = list(clarification.candidate_options[:12])
    elif snapshot.planner_turn.route == "execute":
        open_questions = []

    user_message = _shorten(snapshot.user_message.strip(), _MAX_MEMORY_TEXT)
    lowered = user_message.lower()
    if lowered.startswith(("no", "not ", "actually", "instead")):
        rejected_choices = _append_unique(rejected_choices, user_message)[-12:]
    if "chart" in lowered or "graph" in lowered:
        user_preferences = _append_unique(
            user_preferences,
            "Prefers chartable results",
        )[-12:]

    source_ids = list(previous.source_snapshot_ids) if previous else []
    source_ids = _append_unique(source_ids, snapshot.snapshot_id)[
        -_MAX_SOURCE_SNAPSHOT_IDS:
    ]
    next_policy_guidance_context: PolicyGuidanceContext | None
    if latest_query_context is not None:
        next_policy_guidance_context = None
    elif snapshot.planner_turn.route == "policy_guidance":
        next_policy_guidance_context = latest_policy_guidance_context
    else:
        next_policy_guidance_context = previous.latest_policy_guidance_context

    return ConversationMemory(
        summary=ConversationSummary(
            summary=(
                previous_summary.summary
                if previous_summary and previous_summary.summary
                else _initial_summary(snapshot)
            ),
            active_user_goal=user_message,
            open_questions=open_questions,
            accepted_choices=accepted_choices,
            rejected_choices=rejected_choices,
            user_preferences=user_preferences,
            last_clarification_options=last_options,
        ),
        latest_query_context=latest_query_context or previous.latest_query_context,
        latest_policy_guidance_context=next_policy_guidance_context,
        pending_query_context=pending_query_context,
        pending_clarification_options=pending_clarification_options,
        result_refs=list(result_refs) if result_refs is not None else previous.result_refs,
        recent_routes=_append_recent_route(
            previous.recent_routes, snapshot.planner_turn.route
        ),
        latest_turn_index=snapshot.turn_index,
        source_snapshot_ids=source_ids,
    )


def _append_recent_route(
    existing: Sequence[str],
    route: str,
    *,
    limit: int = 6,
) -> list[str]:
    """Append a route to the recent_routes window; keep the newest ``limit``."""

    routes = list(existing)
    routes.append(route)
    return routes[-limit:]


def recent_routes_from_snapshots(
    snapshots: Sequence[TurnSnapshot],
    *,
    limit: int = 6,
) -> list[str]:
    """Rebuild the recent_routes window from persisted snapshots."""

    routes: list[str] = []
    for snapshot in sorted(snapshots, key=lambda item: item.turn_index):
        routes = _append_recent_route(routes, snapshot.planner_turn.route, limit=limit)
    return routes


def message_history_from_snapshots(
    snapshots: Sequence[TurnSnapshot],
) -> list[ModelMessage]:
    """Rebuild the pydantic-ai message_history from prior turn snapshots.

    Each turn snapshot persists the planner agent's
    ``result.new_messages_json()`` payload in
    ``snapshot.planner_evidence.new_messages_json``. Concatenated in
    turn-index order, these ``ModelMessage`` lists form the canonical
    ``message_history`` that the planner agent expects on the next
    ``agent.run(..., message_history=...)`` call (W0.5 / #832).

    Snapshots without planner_evidence (e.g., direct-route or pre-W0.5 rows)
    contribute no messages and are silently skipped.
    """

    history: list[ModelMessage] = []
    for snapshot in sorted(snapshots, key=lambda item: item.turn_index):
        evidence = snapshot.planner_evidence
        if evidence is None or not evidence.new_messages_json:
            continue
        try:
            decoded = ModelMessagesTypeAdapter.validate_json(
                evidence.new_messages_json
            )
        except Exception:
            # A corrupt or pre-canonical payload should not block the new
            # turn; the planner runs against an empty/partial history rather
            # than failing the user-facing turn.
            continue
        history.extend(decoded)
    return history


def _metadata_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _split_exemplar_title(title: str) -> tuple[str | None, str | None]:
    if " — " not in title:
        return title.strip() or None, None
    district, subtopic = title.split(" — ", 1)
    return district.strip() or None, subtopic.strip() or None


def _result_metric_refs(result: ResultSet) -> list[QueryContextMetricRef]:
    refs: list[QueryContextMetricRef] = []
    seen: set[int] = set()
    for row in _memory_rows(result):
        metric_id = getattr(row, "metric_id", None)
        metric_name = getattr(row, "metric_name", None)
        if metric_id is None or metric_id in seen or not metric_name:
            continue
        seen.add(metric_id)
        refs.append(QueryContextMetricRef(metric_id=metric_id, metric_name=metric_name))
        if len(refs) >= _MAX_CONTEXT_RESULT_REFS:
            break
    return refs


def _result_district_refs(result: ResultSet) -> list[QueryContextDistrictRef]:
    refs: list[QueryContextDistrictRef] = []
    seen: set[int] = set()
    qualifying_ids: list[int] = []
    for row in _memory_rows(result):
        for district_id in getattr(row, "qualifying_district_ids", []):
            if district_id not in qualifying_ids:
                qualifying_ids.append(district_id)
    if qualifying_ids and result.selection is not None:
        qualifying = set(qualifying_ids)
        for district in result.selection.districts:
            if district.district_id not in qualifying or district.district_id in seen:
                continue
            seen.add(district.district_id)
            refs.append(
                QueryContextDistrictRef(
                    district_id=district.district_id,
                    district_name=district.district_name,
                    state=district.state,
                )
            )
            if len(refs) >= _MAX_CONTEXT_RESULT_REFS:
                return refs
    for row in _memory_rows(result):
        district_id = getattr(row, "district_id", None)
        district_name = getattr(row, "district_name", None)
        if district_id is None or district_id in seen or not district_name:
            continue
        seen.add(district_id)
        refs.append(
            QueryContextDistrictRef(
                district_id=district_id,
                district_name=district_name,
                state=getattr(row, "state", None),
            )
        )
        if len(refs) >= _MAX_CONTEXT_RESULT_REFS:
            break
    return refs


def _result_digest(
    result: ResultSet,
    *,
    metrics: Sequence[QueryContextMetricRef],
    districts: Sequence[QueryContextDistrictRef],
) -> str:
    metric_text = ", ".join(metric.metric_name for metric in metrics[:3])
    district_text = ", ".join(district.district_name for district in districts[:3])
    parts = [f"{len(_memory_rows(result))} {result.result_type} rows"]
    if metric_text:
        parts.append(f"for {metric_text}")
    if district_text:
        parts.append(f"including {district_text}")
    return "; ".join(parts) + "."


def _memory_rows(result: ResultSet) -> list:
    if getattr(result, "result_type", None) != "composite_ranking":
        return list(result.rows)
    return [
        row
        for child in getattr(result, "children", [])
        for row in getattr(child, "rows", [])
    ]


def _initial_summary(snapshot: TurnSnapshot) -> str:
    if snapshot.planner_turn.route == "execute":
        return _shorten(
            f"User is exploring Compass data: {snapshot.user_message}",
            _MAX_SUMMARY_TEXT,
        )
    if snapshot.planner_turn.route == "clarify":
        return _shorten(
            f"Compass is clarifying the user's data request: {snapshot.user_message}",
            _MAX_SUMMARY_TEXT,
        )
    return _shorten(
        f"Recent Compass conversation started with: {snapshot.user_message}",
        _MAX_SUMMARY_TEXT,
    )


def _append_unique(values: Sequence[str], value: str) -> list[str]:
    result = list(values)
    if value and value not in result:
        result.append(value)
    return result


def _shorten(value: str, max_length: int) -> str:
    text = value.strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _source_snapshot_ids_from_snapshots(
    snapshots: Sequence[TurnSnapshot],
) -> list[str]:
    ids: list[str] = []
    for snapshot in sorted(snapshots, key=lambda item: item.turn_index):
        ids = _append_unique(ids, snapshot.snapshot_id)[-_MAX_SOURCE_SNAPSHOT_IDS:]
    return ids
