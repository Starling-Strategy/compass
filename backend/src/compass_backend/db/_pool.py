"""Shared asyncpg pool factory for the chat hot path.

Audit finding #1 (Critical): the chat hot path used to open a fresh
``asyncpg.connect(...)`` for every query. Over Tailscale to staging that
added ~0.5-1.5s of TCP/TLS handshake per turn, and at modest concurrency
it could exhaust Postgres ``max_connections``.

This module centralizes the singleton ``asyncpg.Pool`` the app boots in
``api.app.create_app_from_settings`` and the helper repositories use to
acquire connections. Every repository requires a ``pool`` constructor
argument — either an ``asyncpg.Pool`` directly or a ``ChatPoolHolder``
whose ``pool`` attribute resolves lazily.

The verdict-pipeline pool (`quality/verdict_pipeline._PIPELINE_POOL`)
intentionally stays separate. It is deliberately small (``max_size=3``)
to respect per-org RPM ceilings on the verdict-judge LLM; the chat hot
path has very different sizing needs.

The ``ChatPoolHolder`` exists so the synchronous app factory can hand a
stable reference to every repository at construction time, then have the
asynchronous startup hook populate ``.pool`` with the live pool. Repos
read ``holder.pool`` lazily inside ``_acquire()`` — before startup
completes ``holder.pool is None`` and ``resolve_pool`` raises so a
misconfigured deploy fails loudly rather than silently degrading.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import asyncpg

from compass_backend.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class ChatPoolHolder:
    """Mutable holder so the sync app factory can wire a pool reference
    before the async startup hook actually creates the pool.

    Repos accept ``asyncpg.Pool | ChatPoolHolder`` so production wiring
    can either pass a holder (the normal path) or hand a live pool
    directly when constructing a one-off repo in a script.
    """

    pool: asyncpg.Pool | None = None


async def create_chat_pool(source: Settings) -> asyncpg.Pool:
    """Create the singleton asyncpg pool used by the chat hot path.

    Sizing is settings-driven via ``pg_pool_min_size`` / ``pg_pool_max_size``
    so operators can tune against the deployed Postgres ``max_connections``
    ceiling without a code change.

    Raises whatever ``asyncpg.create_pool`` raises on failure — the app
    should fail to boot loudly rather than silently degrade to per-call
    connects.
    """

    pool = await asyncpg.create_pool(
        host=source.pg_host,
        port=source.pg_port,
        database=source.pg_database,
        user=source.pg_user,
        password=source.pg_password.get_secret_value(),
        min_size=source.pg_pool_min_size,
        max_size=source.pg_pool_max_size,
        command_timeout=source.pg_command_timeout,
        # Issue #1122: recycle idle connections before the Tailscale NAT
        # path reclaims them, so the pool never hands out a dead connection
        # (which surfaced as intermittent 500s after idle periods).
        max_inactive_connection_lifetime=source.pg_pool_max_inactive_lifetime,
        server_settings={"search_path": source.pg_schema},
    )
    logger.info(
        "compass_backend.db: chat pool created (min=%d max=%d max_inactive=%.1fs)",
        source.pg_pool_min_size,
        source.pg_pool_max_size,
        source.pg_pool_max_inactive_lifetime,
    )
    return pool


async def close_chat_pool(pool: asyncpg.Pool, *, timeout: float = 10.0) -> None:
    """Close the chat pool gracefully, falling back to terminate on timeout.

    ``pool.close()`` waits for in-flight checkouts to finish. Without a
    timeout, a hung connection during SIGTERM-driven shutdown would block
    the app from exiting. ``terminate()`` is the brute-force backstop.
    """

    try:
        await asyncio.wait_for(pool.close(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "compass_backend.db: chat pool close timed out after %.1fs — terminating",
            timeout,
        )
        pool.terminate()


def resolve_pool(
    pool_or_holder: "asyncpg.Pool | ChatPoolHolder",
) -> asyncpg.Pool:
    """Resolve a pool-or-holder into a live pool.

    Repos store ``pool_or_holder`` once at construction time and call
    this in ``_acquire()`` to read whatever the holder currently
    contains. This lets the sync app factory wire a stable holder
    reference before the async startup hook populates it.

    Raises ``RuntimeError`` when ``holder.pool`` is still ``None`` —
    that indicates the startup hook never ran, so the caller is about
    to silently degrade to a per-call connect path that no longer
    exists. Fail loud.
    """

    if isinstance(pool_or_holder, ChatPoolHolder):
        if pool_or_holder.pool is None:
            raise RuntimeError(
                "Chat pool not initialized; startup hook _start_chat_pool "
                "must run before any request"
            )
        return pool_or_holder.pool
    return pool_or_holder


__all__ = [
    "ChatPoolHolder",
    "create_chat_pool",
    "close_chat_pool",
    "resolve_pool",
]
