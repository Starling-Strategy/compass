"""B-spine scenario-case routes for debug-link compatibility."""

from __future__ import annotations

import logging
from typing import Protocol

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status

logger = logging.getLogger(__name__)

from compass_backend.api.auth import (
    ApiKeyAuthRepository,
    ApiKeyAuthenticator,
    authenticate_request,
)
from compass_backend.config import Settings, settings
from compass_backend.contracts import (
    ScenarioCaseDebugResponse,
    ScenarioCaseListResponse,
)
from compass_backend.db import ReadOnlyScenarioCaseRepository
from compass_backend.db._pool import ChatPoolHolder


class ScenarioRepository(Protocol):
    """Read-only scenario-case lookup boundary."""

    async def list_cases(
        self,
        *,
        active_only: bool = True,
        scenario_type: str | None = None,
        feature: str | None = None,
    ) -> list[ScenarioCaseDebugResponse]:
        """Return B-spine case payloads for the dashboard browser."""

    async def get_case(self, case_id: int) -> ScenarioCaseDebugResponse | None:
        """Return a B-spine case payload or None when it does not exist."""


def create_scenario_router(
    *,
    scenario_repository: ScenarioRepository | None = None,
    api_key_auth_repository: ApiKeyAuthenticator | None = None,
    app_settings: Settings = settings,
    chat_pool: asyncpg.Pool | ChatPoolHolder | None = None,
) -> APIRouter:
    """Create read-only B-spine case routes used by frontend debug links."""

    if chat_pool is None:
        chat_pool = ChatPoolHolder()
    router = APIRouter(prefix="/api/v1", tags=["Scenarios"])
    repository = scenario_repository or ReadOnlyScenarioCaseRepository(
        app_settings, pool=chat_pool
    )
    authenticator = api_key_auth_repository or ApiKeyAuthRepository(
        app_settings, pool=chat_pool
    )

    async def require_admin(raw_request: Request) -> None:
        # Audit #18: admin scope enforced unconditionally. Only explicit
        # dev escape hatch (environment=='development' + auth off) passes.
        auth_user = await authenticate_request(
            raw_request,
            source=app_settings,
            authenticator=authenticator,
        )
        if auth_user and auth_user.is_admin:
            return
        if (
            app_settings.environment == "development"
            and not app_settings.api_key_auth_enabled
        ):
            logger.warning(
                "scenario admin gate bypassed: development env with auth off"
            )
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    @router.get("/scenario-cases", response_model=ScenarioCaseListResponse)
    async def list_scenario_cases(
        raw_request: Request,
        type: str | None = None,
        feature: str | None = None,
    ) -> ScenarioCaseListResponse:
        await require_admin(raw_request)

        cases = await repository.list_cases(
            scenario_type=type or None,
            feature=feature or None,
        )
        return ScenarioCaseListResponse(cases=cases)

    @router.get("/scenario-cases/{case_id}", response_model=ScenarioCaseDebugResponse)
    async def get_scenario_case(
        raw_request: Request,
        case_id: int,
    ) -> ScenarioCaseDebugResponse:
        await require_admin(raw_request)

        case = await repository.get_case(case_id)
        if case is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Scenario case not found",
            )
        return case

    @router.get("/scenarios")
    async def list_legacy_scenarios(raw_request: Request) -> None:
        await require_admin(raw_request)
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="B-spine case_id required; use /api/v1/scenario-cases",
        )

    @router.get("/scenarios/{scenario_id}")
    async def get_legacy_scenario(raw_request: Request, scenario_id: int) -> None:
        await require_admin(raw_request)
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="B-spine case_id required; use /api/v1/scenario-cases/{case_id}",
        )

    return router
