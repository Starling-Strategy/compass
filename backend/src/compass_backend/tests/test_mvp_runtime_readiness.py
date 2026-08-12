"""Runtime readiness tests for the fresh Compass service shell."""

from __future__ import annotations

from fastapi.testclient import TestClient

from compass_backend.api.app import create_app, create_app_from_settings
from compass_backend.config import Settings
from compass_backend.db.health import fresh_database_health_check


class SuccessfulConnection:
    """Fake asyncpg connection that records SQL and returns a table row."""

    def __init__(self) -> None:
        self.sql: str | None = None
        self.closed = False

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object]:
        self.sql = sql
        return {"ok": 1}

    async def close(self) -> None:
        self.closed = True


class MissingTableConnection(SuccessfulConnection):
    """Fake asyncpg connection that simulates a missing required table."""

    async def fetchrow(self, sql: str, *args: object) -> None:
        self.sql = sql
        return None


async def _successful_connect(**kwargs: object) -> SuccessfulConnection:
    return SuccessfulConnection()


async def _missing_table_connect(**kwargs: object) -> MissingTableConnection:
    return MissingTableConnection()


async def _failing_connect(**kwargs: object) -> SuccessfulConnection:
    raise OSError("database unavailable")


async def test_fresh_database_health_check_returns_connected_and_uses_quoted_schema() -> None:
    connection = SuccessfulConnection()

    async def connect(**kwargs: object) -> SuccessfulConnection:
        return connection

    status = await fresh_database_health_check(
        Settings(pg_schema='compass-test"schema'),
        connect=connect,
    )

    assert status == "connected"
    assert connection.closed is True
    assert '"compass-test""schema"."navigator_metrics"' in (connection.sql or "")
    assert "SELECT 1" in (connection.sql or "")


async def test_fresh_database_health_check_returns_unavailable_on_connection_failure() -> None:
    status = await fresh_database_health_check(
        Settings(pg_schema="compass"),
        connect=_failing_connect,
    )

    assert status == "unavailable"


async def test_fresh_database_health_check_returns_unavailable_when_table_missing() -> None:
    connection = MissingTableConnection()

    async def connect(**kwargs: object) -> MissingTableConnection:
        return connection

    status = await fresh_database_health_check(
        Settings(pg_schema="compass"),
        connect=connect,
    )

    assert status == "unavailable"
    assert connection.closed is True


def test_create_app_from_settings_wires_default_database_health_check(
    monkeypatch,
) -> None:
    # `pg_host="definitely.invalid"` would now cause the chat-pool
    # startup hook to fail loudly. This test only validates that the
    # default DB health check is wired; patch the pool factory.
    async def _no_pool(_source: object) -> None:
        return None

    monkeypatch.setattr("compass_backend.api.app.create_chat_pool", _no_pool)
    app = create_app_from_settings(Settings(pg_host="definitely.invalid"))

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["database"] == "unavailable"


def test_create_app_from_settings_health_does_not_require_gateway_key(
    monkeypatch,
) -> None:
    monkeypatch.delenv("PYDANTIC_AI_GATEWAY_API_KEY", raising=False)

    async def _no_pool(_source: object) -> None:
        return None

    monkeypatch.setattr("compass_backend.api.app.create_chat_pool", _no_pool)
    app = create_app_from_settings(Settings(pg_host="definitely.invalid"))

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["database"] == "unavailable"


def test_create_app_preserves_injected_database_health_check() -> None:
    app = create_app(database_health_check=lambda: "connected")

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["database"] == "connected"


def test_readiness_probe_is_not_rate_limited_by_global_ceiling() -> None:
    app = create_app(database_health_check=lambda: "connected")

    with TestClient(app) as client:
        responses = [client.get("/api/v1/ready") for _ in range(55)]

    assert {response.status_code for response in responses} == {200}


def test_create_app_from_settings_refuses_to_boot_when_auth_off_outside_dev(
    monkeypatch,
) -> None:
    """Audit #18: staging/prod with api_key_auth_enabled=False must not boot."""
    import pytest

    async def _no_pool(_source: object) -> None:
        return None

    monkeypatch.setattr("compass_backend.api.app.create_chat_pool", _no_pool)
    app = create_app_from_settings(
        Settings(
            pg_host="definitely.invalid",
            environment="staging",
            api_key_auth_enabled=False,
        )
    )

    with pytest.raises(RuntimeError, match="api_key_auth_enabled=False"):
        with TestClient(app):
            pass
