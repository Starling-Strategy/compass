"""Health and lightweight config routes.

This module exposes two distinct probes:

* ``GET /api/v1/health`` — **liveness**. Always returns 200 as long as the
  FastAPI process can answer requests. Used by orchestrators to decide
  "is this container alive at all?".
* ``GET /api/v1/ready`` — **readiness**. Returns 503 when the database
  dependency is anything other than ``"connected"``. Used by HEALTHCHECK
  probes and load balancers to decide "should I send traffic here?".

Splitting the two lets Docker / Coolify restart truly dead containers
without flapping on healthy processes that are still warming up or have
a transient DB hiccup.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Response, status

from compass_backend.contracts.health import HealthCheck, ReadinessCheck

DatabaseStatus = Literal["connected", "connecting", "unavailable"]
DatabaseHealthCheck = Callable[[], Awaitable[DatabaseStatus] | DatabaseStatus]


async def _resolve_database_status(
    database_health_check: DatabaseHealthCheck | None,
) -> DatabaseStatus:
    """Run the (sync-or-async) DB health check, defaulting to ``connecting``."""

    if database_health_check is None:
        return "connecting"
    result = database_health_check()
    if hasattr(result, "__await__"):
        return await result  # type: ignore[no-any-return]
    return result  # type: ignore[return-value]


def create_health_router(
    *,
    version: str,
    auth_enabled: bool,
    database_health_check: DatabaseHealthCheck | None = None,
) -> APIRouter:
    """Create public health/config routes for the API shell."""

    router = APIRouter()

    @router.get("/api/v1/health", response_model=HealthCheck, tags=["Health"])
    async def health_check() -> HealthCheck:
        """Liveness probe — always returns 200 when the process is up."""

        database = await _resolve_database_status(database_health_check)
        return HealthCheck(
            status="healthy",
            version=version,
            database=database,
            auth_enabled=auth_enabled,
        )

    @router.get(
        "/api/v1/ready",
        response_model=ReadinessCheck,
        tags=["Health"],
        responses={
            200: {"description": "Service is ready to accept traffic."},
            503: {
                "description": (
                    "Service is not ready: a required dependency (e.g. the "
                    "database) is unavailable."
                ),
                "model": ReadinessCheck,
            },
        },
    )
    async def readiness_check(response: Response) -> ReadinessCheck:
        """Readiness probe — returns 503 when the database is not connected."""

        database = await _resolve_database_status(database_health_check)
        ready = database == "connected"
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessCheck(
            status="ready" if ready else "not_ready",
            version=version,
            database=database,
            auth_enabled=auth_enabled,
        )

    @router.get("/", include_in_schema=False)
    async def api_info() -> dict[str, str]:
        return {
            "service": "NCTQ Compass API",
            "version": version,
            "docs": "/docs",
        }

    return router
