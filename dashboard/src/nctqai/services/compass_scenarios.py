"""Compass B-spine case inventory for the dashboard.

This read path intentionally uses the dashboard database connection instead of
the Compass API. The Scenarios page is an admin inventory surface, so it should
render whenever the dashboard can read the canonical Compass schema.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from nctqai.db import COMPASS_SCHEMA, run_sql
from nctqai.models.compass import Scenario


def list_dashboard_scenarios(
    scenario_type: str | None = None,
    feature: str | None = None,
) -> list[Scenario]:
    """Return active B-spine case rows for the dashboard browser.

    ``feature`` is the legacy query-param name kept for URL compatibility.
    It filters by the canonical scorecard dimension or an explicit B-spine
    metadata feature/category group. The response still reports the scorecard
    dimension as ``feature`` for the existing dashboard components.
    """

    conditions = ["c.active IS TRUE", "s.active IS TRUE"]
    params: list[object] = []
    if scenario_type:
        params.append(scenario_type)
        conditions.append("COALESCE(c.metadata->>'type', 'golden') = %s")
    if feature:
        params.extend([feature, feature, feature])
        conditions.append(
            "("
            "s.accuracy_primary_dimension = %s "
            "OR c.metadata->>'feature' = %s "
            "OR c.metadata->>'category' = %s"
            ")"
        )

    where = f"WHERE {' AND '.join(conditions)}"
    rows = run_sql(
        f"""
        SELECT
            c.id AS case_id,
            c.case_code,
            c.scenario_id,
            s.scenario_code,
            s.expected_behaviour AS scenario_expected_behaviour,
            s.accuracy_primary_dimension,
            c.inputs,
            c.expected_output,
            c.metadata,
            c.active,
            c.created_at,
            c.updated_at
        FROM {COMPASS_SCHEMA}.cases c
        JOIN {COMPASS_SCHEMA}.scenarios s ON s.id = c.scenario_id
        {where}
        ORDER BY
            s.accuracy_primary_dimension NULLS LAST,
            c.id
        """,
        tuple(params),
    )
    if not rows:
        return []

    criteria_by_scenario = _load_active_criteria(
        sorted({int(row["scenario_id"]) for row in rows})
    )
    scenarios: list[Scenario] = []
    for row in rows:
        scenario_id = int(row["scenario_id"])
        payload = _normalize_case_row(row)
        payload["criteria"] = criteria_by_scenario.get(scenario_id, [])
        payload["example_conversations"] = []
        scenarios.append(Scenario.model_validate(payload))
    return scenarios


def _load_active_criteria(scenario_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not scenario_ids:
        return {}

    rows = run_sql(
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
        FROM {COMPASS_SCHEMA}.criteria
        WHERE scenario_ids && %s::int[]
          AND active IS TRUE
        ORDER BY priority DESC, id ASC
        """,
        (scenario_ids,),
    )

    criteria_by_scenario: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        criterion = dict(row)
        for scenario_id in criterion.get("scenario_ids") or []:
            if scenario_id in scenario_ids:
                criteria_by_scenario[int(scenario_id)].append(criterion)
    return dict(criteria_by_scenario)


def _normalize_case_row(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    expected_output = row.get("expected_output") or {}
    input_steps = _normalize_input_steps(row.get("inputs") or [])
    initial_user_message = input_steps[0]["prompt"] if input_steps else ""
    # The Scenario model still calls this field ``feature`` for back-compat
    # with the existing dashboard components. Populate it with the canonical
    # scorecard owner, not legacy copied metadata.
    feature = row.get("accuracy_primary_dimension")
    scenario_title = (
        metadata.get("origin_scenario_title")
        or metadata.get("scenario_title")
        or row.get("case_code")
        or row.get("scenario_code")
        or ""
    )
    expected_behaviour = (
        expected_output.get("expected_behaviour")
        or row.get("scenario_expected_behaviour")
        or ""
    )

    return {
        "id": int(row["case_id"]),
        "case_id": int(row["case_id"]),
        "case_code": row.get("case_code") or "",
        "scenario_id": int(row["scenario_id"]),
        "scenario_code": row.get("scenario_code") or "",
        "scenario_title": scenario_title,
        "initial_user_message": initial_user_message,
        "input_steps": input_steps,
        "what_we_are_testing": (
            metadata.get("origin_what_we_testing")
            or metadata.get("what_we_are_testing")
            or ""
        ),
        "expected_behaviour": expected_behaviour,
        "feature": feature,
        "type": metadata.get("type") or metadata.get("scenario_type") or "golden",
        "sort_order": _to_int(metadata.get("sort_order")),
        "active": bool(row.get("active", True)),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _normalize_input_steps(inputs: list[Any]) -> list[dict[str, str | None]]:
    steps: list[dict[str, str | None]] = []
    for step in inputs:
        if not isinstance(step, dict):
            continue
        prompt = str(step.get("prompt") or "")
        if not prompt:
            continue
        steps.append(
            {
                "prompt": prompt,
                "step_complete_when": step.get("step_complete_when"),
            }
        )
    return steps


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default
