"""Read-only B-spine scenario-case repository for Compass debug/admin surfaces."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from compass_backend.config import Settings, settings
from compass_backend.contracts import ScenarioCaseDebugResponse
from compass_backend.db._base import RepositoryBase
from compass_backend.db._pool import ChatPoolHolder


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        return json.loads(value)
    return value


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value)


def _int(value: Any, fallback: int = 0) -> int:
    try:
        if value is None or value == "":
            return fallback
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _input_steps(inputs: Any) -> list[dict[str, str | None]]:
    steps: list[dict[str, str | None]] = []
    if not isinstance(inputs, list):
        return steps
    for step in inputs:
        if not isinstance(step, dict):
            continue
        prompt = _text(step.get("prompt"))
        if not prompt:
            continue
        steps.append(
            {
                "prompt": prompt,
                "step_complete_when": step.get("step_complete_when"),
            }
        )
    return steps


def _criterion_payload(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    payload = _json_value(row["payload"], {})
    return {
        "id": row["id"],
        "criterion_code": row["criterion_code"],
        "text": row["text"],
        "category": row["category"],
        "check_type": row["check_type"],
        "severity": row["severity"],
        "priority": row["priority"],
        "is_mandatory_global": row["is_mandatory_global"],
        "scenario_ids": list(row["scenario_ids"] or []),
        "topic_tags": list(row["topic_tags"] or []),
        "intent_tags": list(row["intent_tags"] or []),
        "payload": payload,
        "version_hash": row["version_hash"],
        "active": row["active"],
    }


def _case_payload(
    row: asyncpg.Record | dict[str, Any],
    *,
    criteria_by_scenario: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    inputs = _json_value(row["inputs"], [])
    expected_output = _json_value(row["expected_output"], {})
    metadata = _json_value(row["metadata"], {})
    steps = _input_steps(inputs)

    scenario_id = int(row["scenario_id"])
    criteria = (criteria_by_scenario or {}).get(scenario_id, [])
    # Back-compat response field name. For current B-spine inventory surfaces
    # this must mean the canonical scorecard owner, not legacy metadata copied
    # forward from the pre-B-spine scenarios table.
    feature = row["accuracy_primary_dimension"]
    scenario_title = (
        metadata.get("origin_scenario_title")
        or metadata.get("scenario_title")
        or row["case_code"]
        or row["scenario_code"]
        or ""
    )
    expected_behaviour = (
        expected_output.get("expected_behaviour")
        or row["scenario_expected_behaviour"]
        or ""
    )

    return {
        "id": row["case_id"],
        "case_id": row["case_id"],
        "case_code": _text(row["case_code"]),
        "scenario_id": scenario_id,
        "scenario_code": _text(row["scenario_code"]),
        "scenario_title": _text(scenario_title),
        "initial_user_message": steps[0]["prompt"] if steps else "",
        "input_steps": steps,
        "what_we_are_testing": (
            metadata.get("origin_what_we_testing")
            or metadata.get("what_we_are_testing")
            or ""
        ),
        "expected_behaviour": _text(expected_behaviour),
        "feature": _text(feature) if feature else None,
        "type": _text(metadata.get("type") or metadata.get("scenario_type"), "golden"),
        "sort_order": _int(metadata.get("sort_order")),
        "active": row["active"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "criteria": criteria,
        "example_conversations": [],
    }


class ReadOnlyScenarioCaseRepository(RepositoryBase):
    """Read B-spine runnable Cases with parent Scenario context.

    Borrows a connection from the shared chat pool wired by
    ``create_app_from_settings``. Tests inject a synthetic pool via
    ``FakeChatPool`` from ``compass_backend.tests._chat_pool_fixtures``.
    """

    def __init__(
        self,
        source: Settings = settings,
        *,
        pool: asyncpg.Pool | ChatPoolHolder,
    ) -> None:
        self._settings = source
        self._pool = pool

    async def list_cases(
        self,
        *,
        active_only: bool = True,
        scenario_type: str | None = None,
        feature: str | None = None,
    ) -> list[ScenarioCaseDebugResponse]:
        """Return runnable B-spine cases for dashboard/debug inventory."""

        conditions: list[str] = []
        params: list[object] = []
        if active_only:
            conditions.append("c.active = TRUE")
            conditions.append("s.active = TRUE")
        if scenario_type:
            params.append(scenario_type)
            conditions.append(
                f"COALESCE(c.metadata->>'type', 'golden') = ${len(params)}"
            )
        if feature:
            params.append(feature)
            conditions.append(
                f"(s.accuracy_primary_dimension = ${len(params)} "
                f"OR c.metadata->>'feature' = ${len(params)} "
                f"OR c.metadata->>'category' = ${len(params)})"
            )

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self._acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    c.id AS case_id,
                    c.case_code,
                    c.scenario_id,
                    s.scenario_code,
                    s.expected_behaviour AS scenario_expected_behaviour,
                    s.accuracy_primary_dimension,
                    s.topic_tags,
                    s.intent_tags,
                    c.inputs,
                    c.expected_output,
                    c.metadata,
                    c.active,
                    c.created_at,
                    c.updated_at
                FROM {self._table("cases")} c
                JOIN {self._table("scenarios")} s ON s.id = c.scenario_id
                {where}
                ORDER BY
                    s.accuracy_primary_dimension NULLS LAST,
                    c.id
                """,
                *params,
            )
            criteria_by_scenario = await self._criteria_by_scenario(
                conn,
                [int(row["scenario_id"]) for row in rows],
            )

        return [
            ScenarioCaseDebugResponse.model_validate(
                _case_payload(row, criteria_by_scenario=criteria_by_scenario)
            )
            for row in rows
        ]

    async def get_case(self, case_id: int) -> ScenarioCaseDebugResponse | None:
        """Return one runnable B-spine case payload for debug links."""

        async with self._acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT
                    c.id AS case_id,
                    c.case_code,
                    c.scenario_id,
                    s.scenario_code,
                    s.expected_behaviour AS scenario_expected_behaviour,
                    s.accuracy_primary_dimension,
                    s.topic_tags,
                    s.intent_tags,
                    c.inputs,
                    c.expected_output,
                    c.metadata,
                    c.active,
                    c.created_at,
                    c.updated_at
                FROM {self._table("cases")} c
                JOIN {self._table("scenarios")} s ON s.id = c.scenario_id
                WHERE c.id = $1
                """,
                case_id,
            )
            if row is None:
                return None
            criteria_by_scenario = await self._criteria_by_scenario(
                conn,
                [int(row["scenario_id"])],
            )

        return ScenarioCaseDebugResponse.model_validate(
            _case_payload(row, criteria_by_scenario=criteria_by_scenario)
        )

    async def _criteria_by_scenario(
        self,
        conn: asyncpg.Connection,
        scenario_ids: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        if not scenario_ids:
            return {}

        rows = await conn.fetch(
            f"""
            SELECT
                id,
                criterion_code,
                text,
                category,
                check_type,
                severity,
                priority,
                is_mandatory_global,
                scenario_ids,
                topic_tags,
                intent_tags,
                payload,
                version_hash,
                active
            FROM {self._table("criteria")}
            WHERE active = TRUE
              AND scenario_ids && $1::int[]
            ORDER BY priority DESC, id ASC
            """,
            scenario_ids,
        )

        criteria_by_scenario: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            criterion = _criterion_payload(row)
            for scenario_id in criterion["scenario_ids"]:
                if scenario_id in scenario_ids:
                    criteria_by_scenario.setdefault(scenario_id, []).append(criterion)
        return criteria_by_scenario


ReadOnlyScenarioRepository = ReadOnlyScenarioCaseRepository


__all__ = ["ReadOnlyScenarioCaseRepository", "ReadOnlyScenarioRepository"]
