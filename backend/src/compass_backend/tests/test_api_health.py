"""Tests for the liveness/readiness split.

Spec: closure-plan item W0-03. ``/api/v1/health`` is liveness and MUST always
return 200 once the process is up. ``/api/v1/ready`` is readiness and MUST
return 503 when the database dependency is anything other than ``connected``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from compass_backend.api.app import create_app
from compass_backend.contracts.health import ReadinessCheck


def test_health_returns_200_always() -> None:
    """Liveness probe must stay 200 even when the database is unavailable."""

    app = create_app(database_health_check=lambda: "unavailable")

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["database"] == "unavailable"


def test_health_returns_200_when_db_connected() -> None:
    """Liveness probe stays 200 in the happy path too."""

    app = create_app(database_health_check=lambda: "connected")

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["database"] == "connected"


def test_ready_returns_503_when_db_unavailable() -> None:
    """Readiness probe must report 503 so HEALTHCHECK can drain the container."""

    app = create_app(database_health_check=lambda: "unavailable")

    with TestClient(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["database"] == "unavailable"


def test_ready_returns_503_when_db_connecting() -> None:
    """Cold-start ``connecting`` state is also not ready."""

    app = create_app(database_health_check=lambda: "connecting")

    with TestClient(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_ready_returns_200_when_db_connected() -> None:
    """Readiness probe returns 200 once the database is connected."""

    app = create_app(database_health_check=lambda: "connected")

    with TestClient(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "connected"


def test_ready_supports_async_database_health_check() -> None:
    """The DB-check callable may be async; readiness must await it."""

    async def async_check() -> str:
        return "connected"

    app = create_app(database_health_check=async_check)

    with TestClient(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json()["database"] == "connected"


def test_ready_defaults_to_503_when_no_database_health_check_wired() -> None:
    """No wired DB check means status defaults to ``connecting`` => not ready."""

    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json()["database"] == "connecting"


def test_health_response_schema_unchanged() -> None:
    """Existing /api/v1/health response fields must not break for callers."""

    app = create_app(database_health_check=lambda: "connected")

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    body = response.json()
    assert set(body.keys()) == {"status", "version", "database", "auth_enabled"}


def test_readiness_contract_is_a_pydantic_model() -> None:
    """ReadinessCheck must be a proper Pydantic model with typed fields."""

    model = ReadinessCheck(
        status="ready",
        version="x",
        database="connected",
        auth_enabled=False,
    )
    assert model.status == "ready"
    assert model.database == "connected"
    # Wrong literal should raise.
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReadinessCheck(
            status="ready",
            version="x",
            database="nope",  # type: ignore[arg-type]
            auth_enabled=False,
        )
