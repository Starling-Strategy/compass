"""Dashboard helpers for reading fresh Compass turn-snapshot memory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nctqai.db import COMPASS_SCHEMA, run_sql, snapshot_summary_column_available

SNAPSHOT_KEY = "fresh_compass_turn_snapshot"


@dataclass(frozen=True)
class SnapshotDistrictRef:
    district_id: int
    district_name: str
    state: str | None = None


@dataclass(frozen=True)
class SnapshotMemory:
    topic_labels: tuple[str, ...] = ()
    districts: tuple[SnapshotDistrictRef, ...] = ()


def normalize_since(since: datetime | None) -> datetime | None:
    """chat_sessions.created_at is TIMESTAMP WITHOUT TIME ZONE (naive UTC).

    Convert tz-aware bounds to naive UTC so psycopg2 doesn't reinterpret them.
    Shared by every Compass service that filters by created_at.
    """
    if since is None:
        return None
    if since.tzinfo is not None:
        return since.astimezone(UTC).replace(tzinfo=None)
    return since


def fetch_latest_snapshot_rows(
    *,
    since: datetime | None = None,
    session_ids: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return the latest persisted fresh snapshot per Compass session."""

    since = normalize_since(since)
    binds: list[Any] = []
    where_clauses = [
        "m.role = 'assistant'",
        f"m.message_data ? '{SNAPSHOT_KEY}'",
    ]
    if since is not None:
        where_clauses.append("s.created_at >= %s")
        binds.append(since)
    if session_ids is not None:
        normalized_ids = [sid for sid in session_ids if sid]
        if not normalized_ids:
            return []
        where_clauses.append("s.session_id = ANY(%s)")
        binds.append(normalized_ids)

    where_sql = " AND ".join(where_clauses)
    # Read the tiny precomputed snapshot_summary (migration 163) instead of
    # detoasting the full ~123 KB fresh_compass_turn_snapshot blob. When the
    # column is present, the COALESCE-to-blob covers only the brief
    # post-migration, pre-backfill window (column exists, this row not yet
    # backfilled → NULL). When the column is ABSENT (163 unapplied on this
    # environment) fall back to reading the full blob directly so the query
    # degrades gracefully instead of hard-erroring with UndefinedColumn.
    if snapshot_summary_column_available():
        snapshot_expr = (
            f"COALESCE(payload.snapshot_summary, "
            f"payload.message_data->'{SNAPSHOT_KEY}')"
        )
    else:
        snapshot_expr = f"payload.message_data->'{SNAPSHOT_KEY}'"
    sql = f"""
        WITH ranked AS (
            SELECT DISTINCT ON (s.session_id)
                s.session_id,
                s.created_at,
                m.id
            FROM {COMPASS_SCHEMA}.chat_sessions s
            JOIN {COMPASS_SCHEMA}.chat_messages m ON m.session_id = s.session_id
            WHERE {where_sql}
            ORDER BY
                s.session_id,
                m.timestamp DESC NULLS LAST,
                m.id DESC
        )
        SELECT
            ranked.session_id,
            ranked.created_at,
            {snapshot_expr} AS snapshot
        FROM ranked
        JOIN {COMPASS_SCHEMA}.chat_messages payload ON payload.id = ranked.id
    """
    return run_sql(sql, tuple(binds))


def memory_by_session(rows: list[dict[str, Any]]) -> dict[str, SnapshotMemory]:
    memories: dict[str, SnapshotMemory] = {}
    for row in rows:
        session_id = str(row.get("session_id") or "")
        if not session_id:
            continue
        memories[session_id] = snapshot_memory_from_payload(row.get("snapshot"))
    return memories


def snapshot_memory_from_payload(payload: Any) -> SnapshotMemory:
    snapshot = _mapping(payload)
    memory = _mapping(snapshot.get("memory"))
    query_context = _mapping(memory.get("latest_query_context"))
    guidance_context = _mapping(memory.get("latest_policy_guidance_context"))

    metrics = _metric_labels(query_context.get("result_metrics"))
    districts = _district_refs(query_context.get("result_districts"))

    for ref_value in reversed(_sequence(memory.get("result_refs"))):
        ref = _mapping(ref_value)
        if not metrics:
            metrics = _metric_labels(ref.get("metrics"))
        if not districts:
            districts = _district_refs(ref.get("districts"))
        if metrics and districts:
            break

    topic_labels = _dedupe_strings(
        [*metrics, *_policy_topic_labels(guidance_context)]
    )
    return SnapshotMemory(
        topic_labels=tuple(topic_labels),
        districts=tuple(districts),
    )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _sequence(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _metric_labels(value: Any) -> list[str]:
    labels: list[str] = []
    for item in _sequence(value):
        ref = _mapping(item)
        label = _clean_text(ref.get("metric_name"))
        if label is not None:
            labels.append(label)
    return _dedupe_strings(labels)


def _district_refs(value: Any) -> list[SnapshotDistrictRef]:
    refs: list[SnapshotDistrictRef] = []
    seen: set[int] = set()
    for item in _sequence(value):
        ref = _mapping(item)
        district_id = _coerce_int(ref.get("district_id"))
        district_name = _clean_text(ref.get("district_name"))
        if district_id is None or district_name is None or district_id in seen:
            continue
        seen.add(district_id)
        refs.append(
            SnapshotDistrictRef(
                district_id=district_id,
                district_name=district_name,
                state=_clean_text(ref.get("state")),
            )
        )
    return refs


def _policy_topic_labels(guidance_context: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    primary_topic_id = _clean_text(guidance_context.get("primary_topic_id"))
    if primary_topic_id is not None:
        labels.append(_topic_label(primary_topic_id))
    for topic_id in _sequence(guidance_context.get("topic_ids")):
        cleaned = _clean_text(topic_id)
        if cleaned is not None:
            labels.append(_topic_label(cleaned))
    return _dedupe_strings(labels)


def _topic_label(topic_id: str) -> str:
    return topic_id.replace("_", " ").replace("-", " ").strip().title()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
