"""Health response contracts."""

from typing import Literal

from pydantic import BaseModel


class HealthCheck(BaseModel):
    """Liveness response returned by ``GET /api/v1/health``.

    Liveness reports that the process is up and able to answer HTTP. It MUST
    always return 200 so orchestrators distinguish "process dead" from
    "process up but dependencies not ready" (see :class:`ReadinessCheck`).
    """

    status: str = "healthy"
    version: str
    database: Literal["connected", "connecting", "unavailable"] = "connecting"
    auth_enabled: bool = False


class ReadinessCheck(BaseModel):
    """Readiness response returned by ``GET /api/v1/ready``.

    Readiness reports whether the service can take traffic right now. The
    endpoint returns HTTP 503 when ``database != "connected"`` so load
    balancers and HEALTHCHECK probes can drain or restart unhealthy
    containers. The body keeps the same shape as :class:`HealthCheck` so
    callers can parse either uniformly.
    """

    status: Literal["ready", "not_ready"] = "ready"
    version: str
    database: Literal["connected", "connecting", "unavailable"] = "connecting"
    auth_enabled: bool = False
