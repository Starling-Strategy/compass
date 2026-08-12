"""FastAPI application factory for the fresh Compass backend."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import asyncpg
from fastapi import FastAPI

from compass_backend import __version__
from compass_backend.api.auth import ApiKeyAuthenticator
from compass_backend.config import Settings, settings
from compass_backend.db._pool import (
    ChatPoolHolder,
    close_chat_pool,
    create_chat_pool,
)
from compass_backend.db.health import database_health_check_from_settings
from compass_backend.execution import QueryExecutor
from compass_backend.planning import PlannerAgent
from compass_backend.session import SessionStore

from .case_reports import CaseReportRepositoryProtocol, create_case_reports_router
from .chat import create_chat_router
from .frontend_contracts import FrontendContractRepository, create_frontend_contract_router
from .health import DatabaseHealthCheck, create_health_router
from .lifespan import LifespanHooks, create_lifespan
from .middleware import install_transport_middleware
from compass_backend.observability import LOGFIRE_AVAILABLE, setup_logfire
from .scenarios import ScenarioRepository, create_scenario_router


@dataclass(frozen=True)
class CompassApiConfig:
    """Small transport config needed to construct the HTTP app.

    This deliberately starts narrower than the legacy settings object. Agent,
    writer, critic, export, and database settings should enter through their
    own layers as those files are admitted.
    """

    title: str = "NCTQ Compass API"
    description: str = "AI-powered access to teacher salary and policy data."
    version: str = __version__
    docs_url: str = "/docs"
    redoc_url: str = "/redoc"
    environment: str = "development"
    auth_enabled: bool = False
    logfire_enabled: bool = False
    # Single source of truth for the allowlist is Settings.cors_origins
    # (compass_backend/config.py); derive the default from it rather than
    # duplicating the literal, so adding an origin in one place can't lag here.
    cors_origins: Sequence[str] = field(
        default_factory=lambda: tuple(settings.cors_origins)
    )

    @classmethod
    def from_settings(cls, source: Settings = settings) -> "CompassApiConfig":
        """Build transport config from environment-backed settings."""

        return cls(
            environment=source.environment,
            auth_enabled=source.auth_enabled,
            logfire_enabled=source.logfire_enabled,
            cors_origins=tuple(source.cors_origins),
        )


def create_app(
    config: CompassApiConfig | None = None,
    *,
    lifespan_hooks: LifespanHooks | None = None,
    database_health_check: DatabaseHealthCheck | None = None,
    planner_agent: PlannerAgent | None = None,
    session_store: SessionStore | None = None,
    query_executor: QueryExecutor | None = None,
    scenario_repository: ScenarioRepository | None = None,
    frontend_contract_repository: FrontendContractRepository | None = None,
    case_report_repository: CaseReportRepositoryProtocol | None = None,
    api_key_auth_repository: ApiKeyAuthenticator | None = None,
    app_settings: Settings = settings,
    chat_pool: asyncpg.Pool | ChatPoolHolder | None = None,
) -> FastAPI:
    """Create the FastAPI application.

    ``chat_pool`` is the singleton asyncpg pool used by chat-path
    repositories. ``create_app_from_settings`` builds a real pool inside a
    ``LifespanHooks.startup`` callback (because pool creation is async) and
    threads it through. Tests that call ``create_app`` directly without a
    pool get an empty ``ChatPoolHolder()`` — construction still succeeds;
    ``resolve_pool`` raises if a repository tries to acquire a connection
    before the pool is populated.
    """

    if chat_pool is None:
        chat_pool = ChatPoolHolder()
    app_config = config or CompassApiConfig.from_settings(app_settings)
    app = FastAPI(
        title=app_config.title,
        description=app_config.description,
        version=app_config.version,
        lifespan=create_lifespan(lifespan_hooks),
        docs_url=app_config.docs_url,
        redoc_url=app_config.redoc_url,
    )

    install_transport_middleware(
        app,
        cors_origins=app_config.cors_origins,
        environment=app_config.environment,
    )
    app.include_router(
        create_health_router(
            version=app_config.version,
            auth_enabled=app_config.auth_enabled,
            database_health_check=database_health_check,
        )
    )
    app.include_router(
        create_chat_router(
            planner_agent=planner_agent,
            session_store=session_store,
            query_executor=query_executor,
            api_key_auth_repository=api_key_auth_repository,
            app_settings=app_settings,
            chat_pool=chat_pool,
        )
    )
    app.include_router(
        create_frontend_contract_router(
            frontend_contract_repository=frontend_contract_repository,
            api_key_auth_repository=api_key_auth_repository,
            app_settings=app_settings,
            chat_pool=chat_pool,
        )
    )
    app.include_router(
        create_scenario_router(
            scenario_repository=scenario_repository,
            api_key_auth_repository=api_key_auth_repository,
            app_settings=app_settings,
            chat_pool=chat_pool,
        )
    )
    app.include_router(
        create_case_reports_router(
            case_report_repository,
            app_settings=app_settings,
            chat_pool=chat_pool,
        )
    )
    return app


def create_app_from_settings(source: Settings = settings) -> FastAPI:
    """Create the app using environment-backed settings.

    Audit finding #1 (Critical): boots a singleton ``asyncpg.Pool`` for
    the chat hot path during ``LifespanHooks.startup`` and tears it down
    via ``LifespanHooks.shutdown``. Repository constructors receive a
    ``ChatPoolHolder`` reference at sync app-factory time; the startup
    hook fills in ``holder.pool`` once the event loop is running.
    """

    from compass_backend.policy_guidance.library import load_default_library
    from compass_backend.quality.startup_check import (
        warn_on_missing_m1_catalog_aliases,
        warn_on_nces_allowlist_drift,
        warn_on_profile_rank_fields_drift,
        warn_on_unknown_deterministic_validators,
    )

    config = CompassApiConfig.from_settings(source)

    # instrument_fastapi must run AFTER logfire.configure() so its hooks bind
    # to the real OTel TracerProvider rather than the default no-op one.
    pending_app: list[FastAPI] = []

    def _instrument_fastapi_after_configure() -> None:
        if not (LOGFIRE_AVAILABLE and config.logfire_enabled):
            return
        if not pending_app:
            return
        import logfire

        try:
            logfire.instrument_fastapi(pending_app[0])
        except Exception:  # pragma: no cover - defensive observability path
            pass

    # Mutable holder: repositories receive this reference at construction
    # time. The startup hook populates ``.pool`` once the event loop is
    # running. ``resolve_pool(holder)`` reads ``holder.pool`` lazily on
    # every query, so any request that arrives after startup sees the
    # live pool while keeping the sync app factory simple.
    pool_holder = ChatPoolHolder()

    async def _assert_auth_required_outside_dev() -> None:
        # Audit #18: refuse to boot staging/prod with auth off; the admin
        # routes' dev escape hatch must not apply outside development.
        if (
            source.environment != "development"
            and not source.api_key_auth_enabled
        ):
            raise RuntimeError(
                "Refusing to boot: api_key_auth_enabled=False is only "
                f"permitted when environment=='development' (got "
                f"environment={source.environment!r})."
            )

    async def _start_chat_pool() -> None:
        # Fail loudly on pool-creation errors so a broken deploy does not
        # silently degrade to per-call connects. ``asyncpg.create_pool``
        # raises whatever the underlying Postgres driver reports.
        pool_holder.pool = await create_chat_pool(source)

    async def _close_chat_pool_hook() -> None:
        if pool_holder.pool is not None:
            await close_chat_pool(pool_holder.pool, timeout=10.0)
            pool_holder.pool = None

    app = create_app(
        config,
        lifespan_hooks=LifespanHooks(
            startup=(
                _assert_auth_required_outside_dev,
                setup_logfire,
                _instrument_fastapi_after_configure,
                _start_chat_pool,
                load_default_library,
                warn_on_unknown_deterministic_validators,
                warn_on_missing_m1_catalog_aliases,
                warn_on_nces_allowlist_drift,
                warn_on_profile_rank_fields_drift,
            ),
            shutdown=(_close_chat_pool_hook,),
        ),
        database_health_check=database_health_check_from_settings(source),
        app_settings=source,
        chat_pool=pool_holder,
    )
    pending_app.append(app)
    return app
