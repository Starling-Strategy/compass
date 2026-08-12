"""Compass quality loaders — bridge from sync dashboard to async backend.

The dashboard (nctqai, FastHTML/psycopg2) is synchronous; the backend
scorecard functions are async (asyncpg). This module bridges the two with
a single process-lifetime event loop + asyncpg pool running in a daemon
thread. The sync caller submits coroutines via
`asyncio.run_coroutine_threadsafe` and gets results back.

Public API:
    load_scorecard(build_id=None, sweep_run_id=None) -> ScorecardSnapshot
    load_dimension(dim_slug, build_id=None, sweep_run_id=None) -> DimensionDetail
    load_conversation_with_verdicts(session_id) -> ConversationWithVerdicts

Why the dedicated thread (Phase 3e / audit finding F4):
    The previous implementation created a fresh asyncpg pool per request
    via `asyncio.run()`. Per request that's a Postgres handshake (~30–100ms
    over TLS) plus event-loop construction, plus connection churn against
    Postgres `max_connections`. At dashboard scale the cumulative cost was
    measurable and avoidable. The persistent loop + pool eliminates that
    overhead — first call pays the startup cost (~150ms), every later call
    reuses the warm pool.

    Tests bypass this via monkeypatching `_load` (see
    test_compass_quality_service.py); the runtime starts lazily on first
    real call, so unit tests don't spin up an event-loop thread.

The fixture (_fixture.py) is kept for tests — see test_compass_quality_service.py.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable

import asyncpg

from compass_backend.quality.scorecard import (
    compute_conversation_with_verdicts,
    compute_dimension_detail,
    compute_scorecard_snapshot,
)
from compass_backend.quality.scorecard_models import (
    ConversationWithVerdicts,
    DimensionDetail,
    ScorecardSnapshot,
)
from nctqai.config import Config

logger = logging.getLogger(__name__)

_PG_COMMAND_TIMEOUT_SECONDS = 60.0
_RUNTIME_STARTUP_TIMEOUT_SECONDS = 10.0
_CALL_DEADLINE_PADDING_SECONDS = 5.0


# ── Persistent loop + pool runtime ────────────────────────────────────────


_RUNTIME_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_POOL: asyncpg.Pool | None = None
_LOOP_THREAD: threading.Thread | None = None


def _ensure_runtime() -> tuple[asyncio.AbstractEventLoop, asyncpg.Pool]:
    """Lazily start a background event loop + asyncpg pool. Idempotent.

    Called from sync code. The first call constructs the loop on a daemon
    thread, creates an asyncpg pool against the loop, and caches both.
    Subsequent calls return the cached pair without touching the lock-protected
    construction path (fast path = two reads).
    """
    global _LOOP, _POOL, _LOOP_THREAD

    loop = _LOOP
    pool = _POOL
    if loop is not None and pool is not None and not loop.is_closed():
        return loop, pool

    with _RUNTIME_LOCK:
        # Double-checked: another thread may have constructed it between
        # the unlocked read and our acquire.
        loop = _LOOP
        pool = _POOL
        if loop is not None and pool is not None and not loop.is_closed():
            return loop, pool

        new_loop = asyncio.new_event_loop()
        loop_ready = threading.Event()

        def _run_loop_forever() -> None:
            asyncio.set_event_loop(new_loop)
            loop_ready.set()
            try:
                new_loop.run_forever()
            finally:
                # Best-effort cleanup if the loop ever stops. In normal
                # operation it never does — the daemon thread dies with the
                # process.
                pending = asyncio.all_tasks(new_loop)
                for task in pending:
                    task.cancel()
                new_loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
                new_loop.close()

        thread = threading.Thread(
            target=_run_loop_forever,
            daemon=True,
            name="compass-quality-loop",
        )
        thread.start()
        if not loop_ready.wait(timeout=_RUNTIME_STARTUP_TIMEOUT_SECONDS):
            raise RuntimeError(
                "compass-quality-loop thread did not start within "
                f"{_RUNTIME_STARTUP_TIMEOUT_SECONDS}s"
            )

        cfg = Config()

        async def _create_pool_on_loop() -> asyncpg.Pool:
            return await asyncpg.create_pool(
                host=cfg.pg_host,
                port=cfg.pg_port,
                database=cfg.pg_database,
                user=cfg.pg_user,
                password=cfg.pg_password.get_secret_value(),
                # Sized for dashboard concurrency. Scorecard reads are
                # short-lived and read-only; 2–10 covers typical fan-out
                # without crowding Postgres `max_connections`.
                min_size=2,
                max_size=10,
                command_timeout=_PG_COMMAND_TIMEOUT_SECONDS,
                loop=new_loop,
            )

        pool_future = asyncio.run_coroutine_threadsafe(
            _create_pool_on_loop(),
            new_loop,
        )
        try:
            new_pool = pool_future.result(timeout=_RUNTIME_STARTUP_TIMEOUT_SECONDS)
        except Exception:
            # Pool init failed; tear the loop back down so a retry can
            # construct a fresh one rather than reusing a half-initialized state.
            new_loop.call_soon_threadsafe(new_loop.stop)
            raise

        _LOOP = new_loop
        _POOL = new_pool
        _LOOP_THREAD = thread
        logger.info(
            "compass-quality: persistent loop + asyncpg pool started "
            "(min=2, max=10, timeout=%ss)",
            _PG_COMMAND_TIMEOUT_SECONDS,
        )
        return _LOOP, _POOL


def _load(coro_factory: Callable[[asyncpg.Pool], Awaitable[Any]]) -> Any:
    """Submit a coroutine to the background loop and block for its result.

    Tests monkeypatch this function directly to bypass the runtime
    entirely — that's the seam.
    """
    loop, pool = _ensure_runtime()
    future = asyncio.run_coroutine_threadsafe(coro_factory(pool), loop)
    return future.result(
        timeout=_PG_COMMAND_TIMEOUT_SECONDS + _CALL_DEADLINE_PADDING_SECONDS
    )


# ── Public API ────────────────────────────────────────────────────────────


def load_scorecard(
    build_id: str | None = None,
    sweep_run_id: str | None = None,
) -> ScorecardSnapshot:
    """Return the current ScorecardSnapshot, reading from compass.verdicts.

    Args:
        build_id: Optional sentinel build identifier. Currently unused
                  (the backend uses a fixed sentinel); reserved for future
                  sweep-run disambiguation.
        sweep_run_id: Optional sweep run UUID to restrict verdicts to a
                      specific sweep. If None, the backend defaults to the
                      most-comprehensive completed sweep per dimension
                      (audit finding 3d).
    """
    return _load(
        lambda pool: compute_scorecard_snapshot(
            pool,
            build_id=build_id,
            sweep_run_id=sweep_run_id,
        )
    )


def load_dimension(
    dim_slug: str,
    build_id: str | None = None,
    sweep_run_id: str | None = None,
) -> DimensionDetail:
    """Return a DimensionDetail for one dimension, reading from compass.verdicts.

    Args:
        dim_slug: Kebab-case dimension slug, e.g. "sort-accuracy".
        build_id: Optional sentinel build identifier. See load_scorecard().
                  Note: compute_dimension_detail does not accept build_id —
                  the param is retained in this signature for API symmetry
                  with load_scorecard and future extension.
        sweep_run_id: Optional sweep run UUID to restrict verdicts to a
                      specific sweep. If None, the backend defaults to the
                      most-comprehensive completed sweep per dimension.
    """
    return _load(
        lambda pool: compute_dimension_detail(
            pool,
            dim_slug,
            sweep_run_id=sweep_run_id,
        )
    )


def load_conversation_with_verdicts(session_id: str) -> ConversationWithVerdicts:
    """Return a ConversationWithVerdicts for one session.

    Args:
        session_id: UUID string of the chat session.

    Raises:
        KeyError: If the session_id is not found in chat_sessions.
    """
    return _load(lambda pool: compute_conversation_with_verdicts(pool, session_id))


# ── Shutdown helper (test cleanup; production daemon thread dies on exit) ──


def _shutdown_runtime() -> None:
    """Tear down the background loop + pool.

    Production never calls this — the daemon thread dies with the process.
    Test fixtures may call it to release the pool between integration runs.
    """
    global _LOOP, _POOL, _LOOP_THREAD

    with _RUNTIME_LOCK:
        loop = _LOOP
        pool = _POOL
        thread = _LOOP_THREAD
        if loop is None or pool is None:
            return

        try:
            asyncio.run_coroutine_threadsafe(pool.close(), loop).result(
                timeout=_RUNTIME_STARTUP_TIMEOUT_SECONDS
            )
        except Exception:
            logger.warning("compass-quality: pool close raised", exc_info=True)

        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=_RUNTIME_STARTUP_TIMEOUT_SECONDS)

        _LOOP = None
        _POOL = None
        _LOOP_THREAD = None
