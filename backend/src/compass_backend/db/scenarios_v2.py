"""Read-only repo for compass.scenarios + compass.cases rows.

Spec B-spine PR 1 introduced this repository for the active scenario/case
ledger. Legacy source-row ids may appear in case metadata for provenance only.
"""

from __future__ import annotations

import json

import asyncpg
from pydantic import ValidationError

from compass_backend.config import settings
from compass_backend.db.rows import (
    CaseRecord,
    ScenarioExpectation,
    ScenarioRecord,
    StepExpectation,
    StepInput,
)
from compass_backend.observability import log_warn


def _scenario_from_row(row: asyncpg.Record) -> ScenarioRecord:
    return ScenarioRecord(
        id=row["id"],
        scenario_code=row["scenario_code"],
        expected_behaviour=row["expected_behaviour"],
        accuracy_primary_dimension=row["accuracy_primary_dimension"],
        topic_tags=list(row["topic_tags"] or []),
        intent_tags=list(row["intent_tags"] or []),
        version_hash=row["version_hash"],
        active=row["active"],
    )


def _case_from_row(row: asyncpg.Record) -> CaseRecord | None:
    """Build a CaseRecord from a compass.cases row, or None if the row is malformed.

    Returns None (with a structured warning) instead of raising when StepInput or
    StepExpectation validation fails — one bad row should not abort an entire
    dimension sweep. The contract on StepInput.prompt stays strict (str, not
    str | None); this loader just becomes resilient to bad data.
    """

    inputs_raw = row["inputs"]
    if isinstance(inputs_raw, str):
        inputs_raw = json.loads(inputs_raw)
    expected_raw = row["expected_output"]
    if isinstance(expected_raw, str):
        expected_raw = json.loads(expected_raw)
    metadata_raw = row["metadata"]
    if isinstance(metadata_raw, str):
        metadata_raw = json.loads(metadata_raw)
    try:
        inputs = [StepInput.model_validate(step) for step in inputs_raw]
        expected = ScenarioExpectation(
            expected_behaviour=expected_raw.get("expected_behaviour", ""),
            steps=[
                _step_expectation_from_raw(step, index)
                for index, step in enumerate(expected_raw.get("steps", []))
            ],
        )
    except ValidationError as exc:
        log_warn(
            "compass.cases.row_skipped_invalid_shape",
            case_code=row["case_code"],
            case_id=row["id"],
            scenario_id=row["scenario_id"],
            error=str(exc),
        )
        return None
    return CaseRecord(
        id=row["id"],
        scenario_id=row["scenario_id"],
        case_code=row["case_code"],
        inputs=inputs,
        expected_output=expected,
        metadata=metadata_raw or {},
        version_hash=row["version_hash"],
        active=row["active"],
    )


def _step_expectation_from_raw(raw: object, index: int) -> StepExpectation:
    """Accept current and legacy per-step expectation shapes.

    Some pre-scorecard migrations stored steps as
    ``{"prompt": ..., "expected_behaviour": ...}``. The runner only needs the
    step index and expected prose, so normalize those rows at the read boundary
    rather than excluding otherwise runnable cases from scorecard sweeps.
    """

    if not isinstance(raw, dict):
        return StepExpectation.model_validate(raw)
    if "step_index" in raw and "expected_output" in raw:
        return StepExpectation.model_validate(raw)
    if "expected_output" in raw:
        return StepExpectation(
            step_index=index,
            expected_output=str(raw.get("expected_output") or ""),
        )
    if "expected_behaviour" in raw:
        return StepExpectation(
            step_index=index,
            expected_output=str(raw.get("expected_behaviour") or ""),
        )
    return StepExpectation.model_validate(raw)


class ReadOnlyScenariosV2Repository:
    """Async read-only repo for the new compass.scenarios + compass.cases tables."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._schema = settings.pg_schema

    async def load_scenarios_for_dimension(self, dimension: str) -> list[ScenarioRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {self._schema}.scenarios "
                f"WHERE accuracy_primary_dimension = $1 AND active = TRUE "
                f"ORDER BY id",
                dimension,
            )
        return [_scenario_from_row(r) for r in rows]

    async def load_cases_for_scenario(self, scenario_id: int) -> list[CaseRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {self._schema}.cases "
                f"WHERE scenario_id = $1 AND active = TRUE "
                f"ORDER BY id",
                scenario_id,
            )
        return [case for r in rows if (case := _case_from_row(r)) is not None]

    async def load_scenario_by_code(self, scenario_code: str) -> ScenarioRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {self._schema}.scenarios "
                f"WHERE scenario_code = $1 AND active = TRUE",
                scenario_code,
            )
        return _scenario_from_row(row) if row else None

    async def load_case_metadata_by_id(self, case_id: int) -> dict:
        """Return compass.cases.metadata for one case id, or {} if absent.

        Used by the verdict pipeline's scenario_fit enrollment to decide
        applicability (structural-diagnostic FOUNDATION cases, #939) without
        hydrating a full CaseRecord. Reads metadata only and tolerates the
        JSONB-as-text shape some drivers return.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT metadata FROM {self._schema}.cases WHERE id = $1",
                case_id,
            )
        if row is None:
            return {}
        metadata_raw = row["metadata"]
        if isinstance(metadata_raw, str):
            metadata_raw = json.loads(metadata_raw)
        return metadata_raw or {}


__all__ = ["ReadOnlyScenariosV2Repository"]
