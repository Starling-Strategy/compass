"""Tests for the per-turn pooled connection context (db/_turn_connection.py).

Three behaviors verified with FakeChatPool/FakeConn (no real Postgres):

(a) No turn connection set → borrows from pool.
(b) Turn connection set + free → reuses it, pool NOT touched for sequential
    acquires.
(c) Concurrency → while the turn conn is busy, a second acquire falls back
    to the pool.  After gather() completes, pool.acquired == 2 (one for the
    turn_connection() acquire, one for the fallback).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from compass_backend.db._turn_connection import (
    _turn_conn_var,
    acquire_turn_or_pool,
    turn_connection,
)


class FakeConnection:
    """Minimal asyncpg.Connection stand-in."""

    def __init__(self, name: str = "conn") -> None:
        self.name = name

    async def fetch(self, sql: str, *args: object) -> list[Any]:
        return []


class FakePool:
    """Minimal asyncpg.Pool stand-in.

    ``acquire()`` returns an async-context-manager that yields a
    FakeConnection.  Tracks every acquire so tests can assert the count.
    """

    def __init__(self, name: str = "pool") -> None:
        self._name = name
        self.acquired = 0
        self.connection = FakeConnection(name=f"{name}-conn")

    def acquire(self) -> Any:
        pool = self

        @asynccontextmanager
        async def _cm():
            pool.acquired += 1
            yield pool.connection

        return _cm()


# ─── (a) no turn connection → borrows from pool ──────────────────────────────


@pytest.mark.asyncio
async def test_acquire_without_turn_connection_uses_pool() -> None:
    """When no turn_connection is active, acquire_turn_or_pool checks out from
    the pool exactly once and yields that connection."""
    pool = FakePool()
    assert _turn_conn_var.get() is None  # baseline: no turn conn

    async with acquire_turn_or_pool(pool) as conn:
        assert conn is pool.connection

    assert pool.acquired == 1


# ─── (b) turn connection set + free → reuse ──────────────────────────────────


@pytest.mark.asyncio
async def test_turn_connection_sequential_reuse() -> None:
    """Once turn_connection() acquires, sequential acquire_turn_or_pool() calls
    must reuse the same connection.  pool.acquired must equal 1 after two
    sequential calls inside the turn.
    """
    pool = FakePool()

    async with turn_connection(pool):
        # First inner acquire — reuses the turn connection.
        async with acquire_turn_or_pool(pool) as c1:
            assert c1 is pool.connection

        # Second inner acquire — still reuses (turn conn is free again).
        async with acquire_turn_or_pool(pool) as c2:
            assert c2 is pool.connection

    # Only one acquire: the one turn_connection() itself made.
    assert pool.acquired == 1


# ─── (c) concurrency → busy guard falls back to pool ─────────────────────────


@pytest.mark.asyncio
async def test_turn_connection_concurrent_fallback() -> None:
    """While the turn connection is busy (held by one coroutine), a concurrent
    acquire must fall back to a second pool checkout rather than raising an
    asyncpg 'another operation in progress' error.

    After gather() completes, pool.acquired == 2:
      - 1 for the turn_connection() acquire
      - 1 for the concurrent fallback
    """
    pool = FakePool()

    async with turn_connection(pool):

        async def use() -> None:
            async with acquire_turn_or_pool(pool) as _c:
                # Hold the connection briefly so the concurrent task sees
                # it as busy.
                await asyncio.sleep(0.01)

        await asyncio.gather(use(), use())

    # turn_connection() = 1 acquire; one of the gather() branches hit the
    # busy-guard and fell back = 1 more acquire.  Total = 2.
    assert pool.acquired == 2


# ─── contextvar isolation: acquire after turn_connection exits ────────────────


@pytest.mark.asyncio
async def test_turn_connection_cleanup_on_exit() -> None:
    """After turn_connection() exits, _turn_conn_var must be reset to None so
    subsequent acquires fall through to the pool again."""
    pool = FakePool()

    async with turn_connection(pool):
        pass  # enter and exit immediately

    assert _turn_conn_var.get() is None

    # Acquire after exit must use the pool.
    async with acquire_turn_or_pool(pool) as conn:
        assert conn is pool.connection

    assert pool.acquired == 2  # turn_connection acquire + post-exit acquire


# ─── turn_connection: None pool raises loudly ────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_turn_or_pool_no_turn_conn_with_none_pool_raises() -> None:
    """resolve_pool raises when passed None (no ChatPoolHolder wrapping).

    This test documents the failure mode when pool is None and no turn
    connection is active — it should raise, not silently return.
    """
    # Passing None directly to resolve_pool causes an AttributeError or
    # TypeError — either way it fails loudly rather than silently.
    with pytest.raises((RuntimeError, AttributeError, TypeError)):
        async with acquire_turn_or_pool(None) as _:  # type: ignore[arg-type]
            pass
