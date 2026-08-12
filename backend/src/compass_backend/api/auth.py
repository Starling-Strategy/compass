"""Fresh API-key authentication boundary."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import asyncpg
from fastapi import HTTPException, Request, status

from compass_backend.contracts.auth import AuthenticatedUser
from compass_backend.db._pool import ChatPoolHolder
from compass_backend.db._turn_connection import acquire_turn_or_pool
from compass_backend.observability import compass_span, set_span_attributes
from compass_backend.config import Settings, settings
from compass_backend.session.postgres import quote_ident

# Re-export so existing `from compass_backend.api.auth import AuthenticatedUser`
# call sites (tests, frontend_contracts, etc.) keep working unchanged.
__all__ = [
    "AuthenticatedUser",
    "ApiKeyAuthRepository",
    "ApiKeyAuthenticator",
    "authenticate_request",
    "generate_api_key",
    "hash_api_key",
]


class ApiKeyAuthenticator(Protocol):
    """Small boundary for API-key validation."""

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        """Return an authenticated user for a bearer token, if valid."""


def hash_api_key(token: str) -> str:
    """Hash a full API key exactly as the legacy API does."""

    return hashlib.sha256(token.encode()).hexdigest()


def generate_api_key(environment: str = "prod") -> tuple[str, str, str]:
    """Generate a Compass API key and the database hash to store."""

    random_part = secrets.token_hex(16)
    full_key = f"pa_{environment}_{random_part}"
    key_id = f"pa_{environment}_{random_part[:8]}"
    return full_key, key_id, hash_api_key(full_key)


class ApiKeyAuthRepository:
    """Validate Compass API keys against the configured Postgres schema.

    Borrows a connection from the shared chat pool wired by
    ``create_app_from_settings``. Tests inject a synthetic pool via
    ``FakeChatPool`` from ``compass_backend.tests._chat_pool_fixtures``.
    """

    def __init__(
        self,
        source: Settings = settings,
        *,
        pool: asyncpg.Pool | ChatPoolHolder,
    ) -> None:
        self._settings = source
        self._pool = pool

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        """Validate a full bearer token and update usage on success."""

        with compass_span("compass.auth.api_key") as span:
            if not token.startswith("pa_"):
                set_span_attributes(span, outcome="not_pa_prefix")
                return None
            try:
                async with self._acquire() as conn:
                    row = await conn.fetchrow(
                        f"""
                        SELECT
                            api_key.key_id,
                            api_key.owner_email,
                            api_key.name,
                            COALESCE(app_user.is_admin, FALSE) AS is_admin
                        FROM {self._table("api_keys")} api_key
                        LEFT JOIN {self._table("users")} app_user
                            ON app_user.email = api_key.owner_email
                        WHERE api_key.key_hash = $1
                          AND api_key.revoked_at IS NULL
                          AND (
                            api_key.expires_at IS NULL
                            OR api_key.expires_at > NOW()
                          )
                        """,
                        hash_api_key(token),
                    )
                    if row is None:
                        set_span_attributes(span, outcome="not_found")
                        return None
                    user = AuthenticatedUser(
                        email=row["owner_email"],
                        name=row["name"],
                        auth_method="api_key",
                        api_key_id=row["key_id"],
                        is_admin=bool(row["is_admin"]),
                    )
                    await conn.execute(
                        f"""
                        UPDATE {self._table("api_keys")}
                        SET
                            last_used_at = NOW(),
                            request_count = COALESCE(request_count, 0) + 1
                        WHERE key_id = $1
                        """,
                        user.api_key_id,
                    )
                    set_span_attributes(
                        span,
                        outcome="success",
                        key_id=user.api_key_id,
                        owner_email=user.email,
                        is_admin=user.is_admin,
                    )
                    return user
            except Exception as exc:
                set_span_attributes(
                    span,
                    outcome="error",
                    error_type=type(exc).__name__,
                )
                raise

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Yield a connection: the per-turn connection if free, else pool."""

        async with acquire_turn_or_pool(self._pool) as conn:
            yield conn

    def _table(self, table_name: str) -> str:
        return f"{quote_ident(self._settings.pg_schema)}.{quote_ident(table_name)}"


def bearer_token_from_request(request: Request) -> str | None:
    """Extract a bearer token from the Authorization header."""

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.removeprefix("Bearer ").strip()
    return token or None


async def authenticate_request(
    request: Request,
    *,
    source: Settings,
    authenticator: ApiKeyAuthenticator,
) -> AuthenticatedUser | None:
    """Authenticate a request when API-key auth is enabled."""

    if not source.api_key_auth_enabled:
        return None

    token = bearer_token_from_request(request)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await authenticator.authenticate(token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.auth_user = user
    return user
