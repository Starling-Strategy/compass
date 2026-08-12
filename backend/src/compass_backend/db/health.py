"""Read-only database health checks for the fresh Compass API."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

import asyncpg

from compass_backend.config import Settings

DatabaseStatus = Literal["connected", "connecting", "unavailable"]


class HealthConnection(Protocol):
    """Small subset of asyncpg connection behavior needed for health checks."""

    async def fetchrow(self, sql: str, *args: object) -> object | None: ...

    async def close(self) -> None: ...


ConnectFn = Callable[..., Awaitable[HealthConnection]]


async def fresh_database_health_check(
    settings: Settings,
    *,
    connect: ConnectFn = asyncpg.connect,
) -> DatabaseStatus:
    """Return whether the configured Compass schema is reachable.

    This check is intentionally read-only and narrow. It proves the service can
    connect and can see the navigator metrics table that fresh catalog and
    execution paths depend on.
    """

    connection: HealthConnection | None = None
    try:
        connection = await connect(
            host=settings.pg_host,
            port=settings.pg_port,
            database=settings.pg_database,
            user=settings.pg_user,
            password=settings.pg_password.get_secret_value(),
            command_timeout=settings.pg_command_timeout,
        )
        row = await connection.fetchrow(
            f"SELECT 1 AS ok FROM {_table(settings.pg_schema, 'navigator_metrics')} LIMIT 1"
        )
    except Exception:
        return "unavailable"
    finally:
        if connection is not None:
            await connection.close()
    return "connected" if row is not None else "unavailable"


def database_health_check_from_settings(settings: Settings):
    """Build a FastAPI health-check callback from environment-backed settings."""

    async def check() -> DatabaseStatus:
        return await fresh_database_health_check(settings)

    return check


def _table(schema: str, table_name: str) -> str:
    return f"{_quote_ident(schema)}.{_quote_ident(table_name)}"


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
