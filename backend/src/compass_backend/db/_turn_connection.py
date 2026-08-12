"""Per-turn pooled-connection context for the chat hot path.

Each chat turn fires many sequential DB queries (catalog recall, alias
resolution, session load/save, auth checks).  Every ``_acquire()`` call
today does a full ``pool.acquire()`` / ``pool.release()`` round-trip — the
wall-clock cost is small but it generates N ``RESET ALL`` calls and holds
pool slots open longer than necessary.

This module provides:

``turn_connection(pool)``
    An async context-manager that acquires **one** pooled connection at the
    start of a turn and returns it to the pool on exit (including on
    ``CancelledError``).  Set around ``build_chat_response`` in
    ``api/chat_stream.py`` so the contextvar is inherited by all
    coroutines — including those launched via ``asyncio.gather()`` — within
    the same task.

``acquire_turn_or_pool(pool)``
    The per-query async context-manager that repos call from ``_acquire()``.
    When a turn connection is set **and idle**, it is reused without touching
    the pool.  When no turn connection is set, or when the connection is
    already in use (concurrent within-turn ``gather()``), a fresh pool
    checkout falls back transparently — no asyncpg *"another operation is in
    progress"* error.

**Safety invariant:** there is NO ``await`` between the ``_turn_conn_var.get()``
check and the ``tc.busy = True`` assignment, so the busy-flag flip is
effectively atomic within the event loop's single-threaded model.  ``finally``
always clears ``busy`` so a mid-query exception never leaves the connection
stranded.

Non-turn callers (verdict pipeline, scripts, tests that never set the
contextvar) are wholly unaffected — they hit the pool-fallback branch, which
is byte-identical to the previous ``pool.acquire()`` body.

Note: catalog aliases (C1) and NCES column introspection (C2) caches are
loaded first, so the ``gather()`` sites in recall/reconciliation mostly hit
memory.  The busy-guard handles any remaining concurrent pool path correctly.
"""

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import asyncpg

from compass_backend.db._pool import resolve_pool


@dataclass
class _TurnConn:
    """Holds the single pooled connection for a turn plus a busy flag."""

    conn: asyncpg.Connection
    busy: bool = field(default=False)


_turn_conn_var: contextvars.ContextVar[_TurnConn | None] = contextvars.ContextVar(
    "compass_turn_conn", default=None
)


@asynccontextmanager
async def turn_connection(pool):  # type: ignore[type-arg]
    """Hold ONE pooled connection for the life of a turn.

    Repos reuse it for sequential queries via ``acquire_turn_or_pool()``;
    concurrent queries (from ``asyncio.gather()``) fall back to the pool.
    Set inside the orchestrator task so ``gather()``'d coroutines inherit
    the contextvar.

    The connection is released on any exit — normal return, exception, or
    ``asyncio.CancelledError``.
    """
    async with resolve_pool(pool).acquire() as conn:
        token = _turn_conn_var.set(_TurnConn(conn=conn))
        try:
            yield
        finally:
            _turn_conn_var.reset(token)


@asynccontextmanager
async def acquire_turn_or_pool(pool):  # type: ignore[type-arg]
    """Yield the turn connection when it is free; otherwise borrow from the pool.

    Covers three cases:
    1. No turn connection set (non-turn callers, tests) → pool checkout.
    2. Turn connection set and idle → reuse it; no pool acquire.
    3. Turn connection set but busy (concurrent ``gather()``) → pool fallback.
    """
    tc = _turn_conn_var.get()
    if tc is not None and not tc.busy:
        # No await between get() and busy=True — atomic within the event loop.
        tc.busy = True
        try:
            yield tc.conn
        finally:
            tc.busy = False
        return
    # Fall back to a normal pool checkout.
    async with resolve_pool(pool).acquire() as conn:
        yield conn


__all__ = [
    "_TurnConn",
    "_turn_conn_var",
    "acquire_turn_or_pool",
    "turn_connection",
]
