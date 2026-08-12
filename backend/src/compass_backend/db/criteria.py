"""Read-only repo for compass.criteria rows.

Spec B-spine PR 1 — provides load_by_id, load_mandatory_globals, load_for_scenario,
load_active. Used by the criterion classifier (spec B-spine PR 5).

Note: this wraps the new compass.criteria table (B-spine CriterionRecord), not the
legacy compass.golden_criteria table (GoldenCriterion / criterion_loader.py).
"""

from __future__ import annotations

import json

import asyncpg

from compass_backend.config import Settings, settings
from compass_backend.db.rows import CriterionRecord


def _criterion_from_row(row: asyncpg.Record) -> CriterionRecord:
    """Build a CriterionRecord from a compass.criteria row."""
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    # target_spans (added by migration 018) is nullable; preserve None vs []
    # distinction so callers can tell "column unset" from "empty list".
    # Tolerate the column being absent on pre-018 schemas — asyncpg.Record's
    # `in` check works on column names.
    target_spans: list[str] | None = None
    if "target_spans" in row.keys():
        raw_target = row["target_spans"]
        target_spans = list(raw_target) if raw_target is not None else None
    return CriterionRecord(
        id=row["id"],
        criterion_code=row["criterion_code"],
        text=row["text"],
        category=row["category"],
        check_type=row["check_type"],
        severity=row["severity"],
        priority=row["priority"],
        is_mandatory_global=row["is_mandatory_global"],
        scenario_ids=list(row["scenario_ids"] or []),
        topic_tags=list(row["topic_tags"] or []),
        intent_tags=list(row["intent_tags"] or []),
        payload=payload,
        version_hash=row["version_hash"],
        active=row["active"],
        target_spans=target_spans,
    )


class ReadOnlyCriteriaRepository:
    """Async read-only repo for compass.criteria."""

    def __init__(self, pool: asyncpg.Pool, source: Settings = settings):
        self._pool = pool
        self._schema = source.pg_schema

    async def load_by_id(self, criterion_id: int) -> CriterionRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {self._schema}.criteria WHERE id = $1 AND active = TRUE",
                criterion_id,
            )
        return _criterion_from_row(row) if row else None

    async def load_mandatory_globals(self) -> list[CriterionRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {self._schema}.criteria "
                f"WHERE is_mandatory_global = TRUE AND active = TRUE "
                f"ORDER BY priority DESC, id ASC"
            )
        return [_criterion_from_row(r) for r in rows]

    async def load_for_scenario(self, scenario_id: int) -> list[CriterionRecord]:
        """Load criteria where this scenario_id appears in scenario_ids[]."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {self._schema}.criteria "
                f"WHERE $1 = ANY(scenario_ids) AND active = TRUE "
                f"ORDER BY priority DESC, id ASC",
                scenario_id,
            )
        return [_criterion_from_row(r) for r in rows]

    async def load_active(self) -> list[CriterionRecord]:
        """Load all active criteria — used by the AI classifier as the candidate set."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {self._schema}.criteria "
                f"WHERE active = TRUE "
                f"ORDER BY priority DESC, id ASC"
            )
        return [_criterion_from_row(r) for r in rows]


__all__ = ["ReadOnlyCriteriaRepository"]
