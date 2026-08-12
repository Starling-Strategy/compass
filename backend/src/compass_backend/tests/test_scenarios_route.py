"""Tests for B-spine case debug compatibility."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from compass_backend.api.app import create_app
from compass_backend.api.auth import AuthenticatedUser
from compass_backend.contracts import (
    ScenarioCaseDebugResponse,
    ScenarioCaseListResponse,
)
from compass_backend.config import Settings
from compass_backend.db.scenarios import ReadOnlyScenarioCaseRepository
from compass_backend.tests._chat_pool_fixtures import FakeChatPool


class StaticAuthRepository:
    """Fake auth repository for route-level scenario auth tests."""

    def __init__(self, user: AuthenticatedUser | None) -> None:
        self.user = user
        self.tokens: list[str] = []

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        self.tokens.append(token)
        return self.user


class FakeScenarioConnection:
    """Small asyncpg-like connection for scenario case repository tests."""

    def __init__(
        self,
        row: dict[str, Any] | None,
        *,
        rows: list[dict[str, Any]] | None = None,
        criteria_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.row = row
        self.rows = rows or []
        self.criteria_rows = criteria_rows or []
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.connect_kwargs: dict[str, object] = {}
        self.closed = False

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        self.queries.append((sql, args))
        return self.row

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        self.queries.append((sql, args))
        if "criteria" in sql:
            return self.criteria_rows
        return self.rows

    async def close(self) -> None:
        self.closed = True


def _settings() -> Settings:
    return Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
        api_key_auth_enabled=True,
        session_store_backend="memory",
    )


def _case_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "case_id": 501,
        "case_code": "SORT-MIGRATED-280-C00",
        "scenario_id": 280,
        "scenario_code": "SORT-MIGRATED-280",
        "scenario_expected_behaviour": "Chart and table use the same value order.",
        "accuracy_primary_dimension": "Sort Accuracy",
        "topic_tags": ["compensation"],
        "intent_tags": [],
        "inputs": [
            {"prompt": "Compare the performance pay bonus amounts across districts."},
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
        "updated_at": datetime(2026, 5, 7, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _criterion_row(**overrides: Any) -> dict[str, Any]:
    row = {
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
    }
    row.update(overrides)
    return row


def test_get_scenario_case_returns_b_spine_debug_link_payload(monkeypatch) -> None:
    connection = FakeScenarioConnection(
        _case_row(),
        criteria_rows=[_criterion_row()],
    )
    auth = StaticAuthRepository(
        AuthenticatedUser(
            email="admin@example.org",
            auth_method="api_key",
            api_key_id="pa_dev_admin",
            is_admin=True,
        )
    )
    repo = ReadOnlyScenarioCaseRepository(_settings(), pool=FakeChatPool(connection))

    with TestClient(
        create_app(
            app_settings=_settings(),
            scenario_repository=repo,
            api_key_auth_repository=auth,
        )
    ) as client:
        response = client.get(
            "/api/v1/scenario-cases/501",
            headers={"Authorization": "Bearer pa_dev_admin"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "id": 501,
        "case_id": 501,
        "case_code": "SORT-MIGRATED-280-C00",
        "scenario_id": 280,
        "scenario_code": "SORT-MIGRATED-280",
        "scenario_title": "SORT-NEW-01",
        "initial_user_message": (
            "Compare the performance pay bonus amounts across districts."
        ),
        "input_steps": [
            {
                "prompt": "Compare the performance pay bonus amounts across districts.",
                "step_complete_when": None,
            },
            {
                "prompt": "Now only show districts with a bonus.",
                "step_complete_when": None,
            },
        ],
        "what_we_are_testing": "chart/table sort parity",
        "expected_behaviour": "Chart and table use the same value order.",
        "feature": "Sort Accuracy",
        "type": "regression",
        "sort_order": 1,
        "active": True,
        "created_at": "2026-05-07T00:00:00Z",
        "updated_at": "2026-05-07T00:00:00Z",
        "criteria": [
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
            }
        ],
        "example_conversations": [],
    }
    assert auth.tokens == ["pa_dev_admin"]
    assert connection.queries[0][1] == (501,)
    assert '"compass"."cases"' in connection.queries[0][0]
    assert '"compass"."scenarios"' in connection.queries[0][0]
    assert "test_scenarios" not in connection.queries[0][0]
    assert "golden_criteria" not in connection.queries[1][0]


def test_list_scenario_cases_returns_dashboard_payload(monkeypatch) -> None:
    connection = FakeScenarioConnection(
        None,
        rows=[
            _case_row(),
        ],
        criteria_rows=[
            _criterion_row(),
        ],
    )
    auth = StaticAuthRepository(
        AuthenticatedUser(
            email="admin@example.org",
            auth_method="api_key",
            api_key_id="pa_dev_admin",
            is_admin=True,
        )
    )
    repo = ReadOnlyScenarioCaseRepository(_settings(), pool=FakeChatPool(connection))

    with TestClient(
        create_app(
            app_settings=_settings(),
            scenario_repository=repo,
            api_key_auth_repository=auth,
        )
    ) as client:
        response = client.get(
            "/api/v1/scenario-cases",
            headers={"Authorization": "Bearer pa_dev_admin"},
            params={"type": "regression", "feature": "Sort Accuracy"},
        )

    assert response.status_code == 200
    assert response.json()["cases"][0]["case_id"] == 501
    assert response.json()["cases"][0]["scenario_id"] == 280
    assert response.json()["cases"][0]["criteria"][0]["id"] == 10
    assert response.json()["cases"][0]["example_conversations"] == []
    assert auth.tokens == ["pa_dev_admin"]
    assert connection.queries[0][1] == ("regression", "Sort Accuracy")
    assert "c.metadata->>'feature' = $2" in connection.queries[0][0]
    assert "c.metadata->>'category' = $2" in connection.queries[0][0]
    assert '"compass"."cases"' in connection.queries[0][0]
    assert '"compass"."criteria"' in connection.queries[1][0]
    assert "test_scenarios" not in connection.queries[0][0]
    assert "golden_criteria" not in connection.queries[1][0]
    assert len(connection.queries) == 2


def test_scenario_admin_routes_reject_anonymous_when_not_development(
    monkeypatch,
) -> None:
    """Audit #18: staging/prod with auth off must NOT silently allow access."""
    connection = FakeScenarioConnection({"id": 280})
    auth = StaticAuthRepository(None)
    settings = Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
        api_key_auth_enabled=False,
        environment="staging",
        session_store_backend="memory",
    )
    repo = ReadOnlyScenarioCaseRepository(settings, pool=FakeChatPool(connection))

    with TestClient(
        create_app(
            app_settings=settings,
            scenario_repository=repo,
            api_key_auth_repository=auth,
        )
    ) as client:
        response = client.get("/api/v1/scenario-cases/501")

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"
    assert connection.queries == []


def test_scenario_admin_routes_allow_anonymous_in_development(monkeypatch) -> None:
    """Audit #18: dev with auth off keeps the explicit escape hatch."""
    connection = FakeScenarioConnection(
        _case_row(),
        criteria_rows=[_criterion_row()],
    )
    auth = StaticAuthRepository(None)
    settings = Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
        api_key_auth_enabled=False,
        environment="development",
        session_store_backend="memory",
    )
    repo = ReadOnlyScenarioCaseRepository(settings, pool=FakeChatPool(connection))

    with TestClient(
        create_app(
            app_settings=settings,
            scenario_repository=repo,
            api_key_auth_repository=auth,
        )
    ) as client:
        response = client.get("/api/v1/scenario-cases/501")

    assert response.status_code == 200


def test_get_scenario_case_requires_admin_when_auth_enabled(monkeypatch) -> None:
    connection = FakeScenarioConnection({"id": 280})
    auth = StaticAuthRepository(
        AuthenticatedUser(
            email="user@example.org",
            auth_method="api_key",
            api_key_id="pa_dev_user",
            is_admin=False,
        )
    )
    repo = ReadOnlyScenarioCaseRepository(_settings(), pool=FakeChatPool(connection))

    with TestClient(
        create_app(
            app_settings=_settings(),
            scenario_repository=repo,
            api_key_auth_repository=auth,
        )
    ) as client:
        response = client.get(
            "/api/v1/scenario-cases/501",
            headers={"Authorization": "Bearer pa_dev_user"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"
    assert connection.queries == []


def test_legacy_scenario_routes_fail_after_admin_auth() -> None:
    auth = StaticAuthRepository(
        AuthenticatedUser(
            email="admin@example.org",
            auth_method="api_key",
            api_key_id="pa_dev_admin",
            is_admin=True,
        )
    )

    with TestClient(
        create_app(
            app_settings=_settings(),
            api_key_auth_repository=auth,
        )
    ) as client:
        list_response = client.get(
            "/api/v1/scenarios",
            headers={"Authorization": "Bearer pa_dev_admin"},
        )
        get_response = client.get(
            "/api/v1/scenarios/280",
            headers={"Authorization": "Bearer pa_dev_admin"},
        )

    assert list_response.status_code == 410
    assert get_response.status_code == 410
    assert list_response.json()["detail"] == (
        "B-spine case_id required; use /api/v1/scenario-cases"
    )
    assert get_response.json()["detail"] == (
        "B-spine case_id required; use /api/v1/scenario-cases/{case_id}"
    )


def test_scenario_case_contract_list_alias() -> None:
    case = ScenarioCaseDebugResponse.model_validate(
        {
            "case_id": 501,
            "case_code": "SORT-MIGRATED-280-C00",
            "scenario_id": 280,
            "scenario_code": "SORT-MIGRATED-280",
            "scenario_title": "SORT-NEW-01",
            "initial_user_message": "Compare performance pay bonus amounts.",
            "input_steps": [{"prompt": "Compare performance pay bonus amounts."}],
            "expected_behaviour": "Chart and table use the same value order.",
        }
    )

    payload = ScenarioCaseListResponse(cases=[case]).model_dump()

    assert payload["cases"][0]["id"] == 501
    assert payload["cases"][0]["case_id"] == 501
