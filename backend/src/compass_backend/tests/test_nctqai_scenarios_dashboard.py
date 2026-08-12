"""Tests for the DB-backed NCTQ.ai Compass B-spine cases dashboard."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fasthtml.common import to_xml
from pydantic import SecretStr

from nctqai.routes.compass import scenarios as scenario_routes
from nctqai.services import compass_scenarios


def _case_row(**overrides):
    row = {
        "case_id": 501,
        "case_code": "SORT-MIGRATED-280-C00",
        "scenario_id": 280,
        "scenario_code": "SORT-MIGRATED-280",
        "scenario_expected_behaviour": "Chart and table use the same value order.",
        "accuracy_primary_dimension": "Sort Accuracy",
        "inputs": [
            {"prompt": "Compare performance pay bonus amounts."},
            {"prompt": "Now only show districts with a bonus."},
        ],
        "expected_output": {
            "expected_behaviour": "Chart and table use the same value order.",
            "steps": [
                {"step_index": 0, "expected_output": "Sorted comparison appears."},
                {"step_index": 1, "expected_output": "Filter stays applied."},
            ],
        },
        "metadata": {
            "origin_scenario_title": "SORT-NEW-01",
            "origin_what_we_testing": "chart/table sort parity",
            "category": "Sort Accuracy",
            "type": "regression",
            "sort_order": 1,
        },
        "active": True,
        "created_at": datetime(2026, 5, 7, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 8, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def test_list_dashboard_scenarios_maps_rows_and_criteria(monkeypatch) -> None:
    calls: list[tuple[str, tuple]] = []

    def fake_run_sql(sql: str, params: tuple = ()) -> list[dict]:
        calls.append((sql, params))
        if "FROM compass.cases" in sql:
            return [
                _case_row(case_id=501, scenario_id=280, scenario_code="SORT-MIGRATED-280"),
                _case_row(
                    case_id=502,
                    scenario_id=281,
                    scenario_code="SELECT-MIGRATED-281",
                    case_code="SELECT-MIGRATED-281-C00",
                    metadata={
                        "origin_scenario_title": "SELECT-NEW-01",
                        "origin_what_we_testing": "selection parity",
                        "category": "Selection Accuracy",
                        "type": "regression",
                        "sort_order": 2,
                    },
                    accuracy_primary_dimension="Selection Accuracy",
                ),
            ]
        if "FROM compass.criteria" in sql:
            return [
                {
                    "id": 10,
                    "criterion_code": "SORT-001",
                    "text": "Sort table and chart by the same numeric value.",
                    "category": "sort_order",
                    "check_type": "deterministic",
                    "severity": "reject",
                    "priority": 100,
                    "is_mandatory_global": False,
                    "scenario_ids": [280],
                    "topic_tags": [],
                    "intent_tags": [],
                    "payload": {"validator": "sort_order"},
                    "version_hash": "criterion-hash",
                    "active": True,
                },
                {
                    "id": 11,
                    "criterion_code": "SELECT-001",
                    "text": "Select the requested district set.",
                    "category": "selection",
                    "check_type": "deterministic",
                    "severity": "reject",
                    "priority": 100,
                    "is_mandatory_global": False,
                    "scenario_ids": [281],
                    "topic_tags": [],
                    "intent_tags": [],
                    "payload": {"validator": "selection"},
                    "version_hash": "criterion-hash",
                    "active": True,
                },
            ]
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(compass_scenarios, "run_sql", fake_run_sql)

    scenarios = compass_scenarios.list_dashboard_scenarios()

    assert [scenario.id for scenario in scenarios] == [501, 502]
    assert [scenario.case_id for scenario in scenarios] == [501, 502]
    assert [scenario.scenario_id for scenario in scenarios] == [280, 281]
    assert scenarios[0].scenario_title == "SORT-NEW-01"
    assert scenarios[0].initial_user_message == "Compare performance pay bonus amounts."
    assert [step["prompt"] for step in scenarios[0].input_steps] == [
        "Compare performance pay bonus amounts.",
        "Now only show districts with a bonus.",
    ]
    assert scenarios[0].criteria[0]["id"] == 10
    assert scenarios[1].criteria[0]["id"] == 11
    assert scenarios[0].example_conversations == []
    assert calls[1][1] == ([280, 281],)
    assert "test_scenarios" not in calls[0][0]
    assert "golden_criteria" not in calls[1][0]


def test_list_dashboard_scenarios_applies_type_and_feature_filters(monkeypatch) -> None:
    calls: list[tuple[str, tuple]] = []

    def fake_run_sql(sql: str, params: tuple = ()) -> list[dict]:
        calls.append((sql, params))
        if "FROM compass.cases" in sql:
            return [_case_row()]
        if "FROM compass.criteria" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(compass_scenarios, "run_sql", fake_run_sql)

    scenarios = compass_scenarios.list_dashboard_scenarios(
        scenario_type="regression",
        feature="Sort Accuracy",
    )

    assert len(scenarios) == 1
    scenario_sql, scenario_params = calls[0]
    assert "COALESCE(c.metadata->>'type', 'golden') = %s" in scenario_sql
    assert "s.accuracy_primary_dimension = %s" in scenario_sql
    assert "c.metadata->>'feature' = %s" in scenario_sql
    assert "c.metadata->>'category' = %s" in scenario_sql
    assert scenario_params == (
        "regression",
        "Sort Accuracy",
        "Sort Accuracy",
        "Sort Accuracy",
    )


def test_list_dashboard_scenarios_normalizes_nullable_text_fields(monkeypatch) -> None:
    def fake_run_sql(sql: str, params: tuple = ()) -> list[dict]:
        if "FROM compass.cases" in sql:
            return [
                _case_row(
                    case_code=None,
                    scenario_code=None,
                    scenario_expected_behaviour=None,
                    accuracy_primary_dimension=None,
                    inputs=[],
                    expected_output={},
                    metadata={},
                )
            ]
        if "FROM compass.criteria" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(compass_scenarios, "run_sql", fake_run_sql)

    scenario = compass_scenarios.list_dashboard_scenarios()[0]

    assert scenario.scenario_title == ""
    assert scenario.initial_user_message == ""
    assert scenario.what_we_are_testing == ""
    assert scenario.expected_behaviour == ""
    assert scenario.type == "golden"
    assert scenario.sort_order == 0


def test_launch_url_uses_durable_staging_case_id() -> None:
    settings = type(
        "Settings",
        (),
        {
            "compass_frontend_url": "https://staging-compass.nctq.ai/",
            "compass_scenario_link_secret": SecretStr("secret"),
            "compass_scenario_link_ttl_seconds": 60,
        },
    )()

    launch = scenario_routes._launch_url(501, settings=settings, now=1000)

    assert launch.startswith("https://staging-compass.nctq.ai/?")
    assert "case_id=501" in launch
    assert "case_exp=" not in launch
    assert "case_sig=" not in launch
    assert "scenario_id=" not in launch


def test_launch_url_uses_signed_case_id_for_non_staging_hosts() -> None:
    settings = type(
        "Settings",
        (),
        {
            "compass_frontend_url": "https://compass.example.org/",
            "compass_scenario_link_secret": SecretStr("secret"),
            "compass_scenario_link_ttl_seconds": 60,
        },
    )()

    launch = scenario_routes._launch_url(501, settings=settings, now=1000)

    assert launch.startswith("https://compass.example.org/?")
    assert "case_id=501" in launch
    assert "case_exp=1060" in launch
    assert "case_sig=" in launch
    assert "scenario_id=" not in launch


@pytest.mark.asyncio
async def test_scenarios_page_load_error_is_not_false_empty_state(monkeypatch) -> None:
    def raise_load_error(*, scenario_type: str | None, feature: str | None):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(scenario_routes, "list_dashboard_scenarios", raise_load_error)

    content = await scenario_routes._build_scenarios_content("", "")
    html = to_xml(content)

    assert "Could not load scenario inventory." in html
    assert "compass.scenarios and compass.cases" in html
    assert "No scenarios found." not in html
    assert "0</" not in html


@pytest.mark.asyncio
async def test_scenarios_page_labels_cases_and_criteria(monkeypatch) -> None:
    monkeypatch.setattr(
        scenario_routes,
        "list_dashboard_scenarios",
        lambda scenario_type, feature: [
            compass_scenarios.Scenario.model_validate(
                {
                    "id": 501,
                    "case_id": 501,
                    "case_code": "SORT-MIGRATED-280-C00",
                    "scenario_id": 280,
                    "scenario_code": "SORT-MIGRATED-280",
                    "scenario_title": "SORT-NEW-01",
                    "initial_user_message": "Compare performance pay bonus amounts.",
                    "input_steps": [{"prompt": "Compare performance pay bonus amounts."}],
                    "what_we_are_testing": "chart/table sort parity",
                    "expected_behaviour": "Chart and table use the same value order.",
                    "feature": "Sort Accuracy",
                    "type": "regression",
                    "sort_order": 1,
                    "criteria": [{"id": 10}],
                }
            )
        ],
    )

    content = await scenario_routes._build_scenarios_content("", "")
    html = to_xml(content)

    assert "Cases" in html
    assert "Total Criteria" in html
    assert "Criteria" in html
    assert "Rules" not in html
    assert "table-responsive" in html
    assert "case_id=501" in html
    assert "#case-501" in html
