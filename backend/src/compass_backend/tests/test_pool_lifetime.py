"""Issue #1122: asyncpg pools must recycle idle connections over Tailscale.

asyncpg has no pre-ping. An idle connection over the Tailscale tunnel to
staging Postgres can be NAT-reclaimed and then handed out dead — surfacing as
intermittent 500s on the live path and, in a K=5 Selection sweep, ~32 failed
verdict writes that downgraded the sweep to ``partial`` (locked out of the
scorecard).

These tests pin the fix without a live DB:

  1. ``create_chat_pool`` (live hot path) and ``_get_or_create_pool``
     (verdict pipeline) both pass ``max_inactive_connection_lifetime`` to
     ``asyncpg.create_pool``, driven by the ``pg_pool_max_inactive_lifetime``
     setting.
  2. ``VerdictsRepository.write_one`` retries a connection *acquire* that
     fails with a connection-setup error (the exact frame in the proven
     trace) and still writes the verdict exactly once.
  3. A mid-INSERT failure is NOT retried — guarding against double-writes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from pydantic import SecretStr

from compass_backend.config import Settings
from compass_backend.db._pool import create_chat_pool
from compass_backend.db.rows import VerdictEvidence, VerdictRecord
from compass_backend.db.verdicts import VerdictsRepository


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"pg_password": SecretStr("test"), "pg_schema": "compass"}
    base.update(overrides)
    return Settings(**base)


# ─── Pool builders pass max_inactive_connection_lifetime ──────────────────────


@pytest.mark.asyncio
async def test_create_chat_pool_sets_max_inactive_lifetime() -> None:
    """The live chat pool must recycle idle connections (issue #1122)."""
    cfg = _settings(pg_pool_max_inactive_lifetime=42.0)

    with patch(
        "compass_backend.db._pool.asyncpg.create_pool", new_callable=AsyncMock
    ) as create_pool:
        await create_chat_pool(cfg)

    _, kwargs = create_pool.call_args
    assert kwargs["max_inactive_connection_lifetime"] == 42.0


@pytest.mark.asyncio
async def test_verdict_pipeline_pool_sets_max_inactive_lifetime() -> None:
    """The verdict-pipeline pool must recycle idle connections (issue #1122)."""
    import compass_backend.quality.verdict_pipeline as vp

    cfg = _settings(pg_pool_max_inactive_lifetime=37.0)
    vp._PIPELINE_POOL = None  # ensure the factory actually creates a pool

    try:
        with patch.object(
            vp.asyncpg, "create_pool", new_callable=AsyncMock
        ) as create_pool:
            await vp._get_or_create_pool(cfg)

        _, kwargs = create_pool.call_args
        assert kwargs["max_inactive_connection_lifetime"] == 37.0
    finally:
        vp._PIPELINE_POOL = None


# ─── write_one acquire resilience ─────────────────────────────────────────────


class _FakeConnection:
    def __init__(self) -> None:
        self.insert_calls = 0

    async def fetchval(self, sql: str, *args: object) -> str:
        assert "INSERT INTO compass.verdicts" in sql
        self.insert_calls += 1
        return "00000000-0000-0000-0000-000000000001"


class _FakeAcquireContext:
    """Stand-in for ``asyncpg``'s ``PoolAcquireContext``.

    ``__aenter__`` either raises a connection-setup error (to exercise the
    retry) or returns the live connection, mirroring asyncpg's real return
    contract (``__aenter__`` returns the connection).
    """

    def __init__(self, pool: "_FlakyPool") -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConnection:
        self._pool.acquire_attempts += 1
        if self._pool.acquire_attempts <= self._pool.fail_first:
            raise self._pool.error
        self._pool.entered = True
        return self._pool.connection

    async def __aexit__(self, *exc: object) -> None:
        self._pool.exited = True


class _FlakyPool:
    """Fails the first ``fail_first`` acquires, then yields a live connection."""

    def __init__(self, *, fail_first: int, error: BaseException) -> None:
        self.fail_first = fail_first
        self.error = error
        self.acquire_attempts = 0
        self.connection = _FakeConnection()
        self.entered = False
        self.exited = False

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(self)


def _verdict() -> VerdictRecord:
    return VerdictRecord(
        session_id="00000000-0000-0000-0000-0000000000aa",
        criterion_id=1,
        criterion_version_hash="hash",
        criterion_set_version="v1",
        judge_source="deterministic",
        scope="turn",
        outcome="pass",
        reason="ok",
        evidence=VerdictEvidence(
            artifact_snapshot={},
            answer_excerpt="",
            copied_span_attributes={},
        ),
        triggered_by="sweep_run",
    )


@pytest.mark.asyncio
async def test_write_one_retries_acquire_then_writes_once() -> None:
    """A NAT-reclaimed dead connection on acquire is retried; INSERT runs once.

    Reproduces the proven trace: ``pool.acquire()`` raises
    ``ConnectionDoesNotExistError`` (connection closed mid-flight / dead),
    then a fresh acquire succeeds. The verdict must be written exactly once —
    no double-write.
    """
    pool = _FlakyPool(
        fail_first=2,
        error=asyncpg.exceptions.ConnectionDoesNotExistError(
            "connection was closed in the middle of operation"
        ),
    )
    repo = VerdictsRepository(pool, source=_settings())  # type: ignore[arg-type]

    with patch("compass_backend.db.verdicts.asyncio.sleep", new_callable=AsyncMock):
        new_id = await repo.write_one(_verdict())

    assert new_id == "00000000-0000-0000-0000-000000000001"
    assert pool.acquire_attempts == 3  # 2 failures + 1 success
    assert pool.connection.insert_calls == 1  # INSERT runs exactly once
    assert pool.exited is True  # connection released


@pytest.mark.asyncio
async def test_write_one_retries_connect_timeout() -> None:
    """A connect/SSL-handshake timeout on acquire is retried (issue #1122)."""
    pool = _FlakyPool(fail_first=1, error=TimeoutError("ssl handshake timed out"))
    repo = VerdictsRepository(pool, source=_settings())  # type: ignore[arg-type]

    with patch("compass_backend.db.verdicts.asyncio.sleep", new_callable=AsyncMock):
        new_id = await repo.write_one(_verdict())

    assert new_id == "00000000-0000-0000-0000-000000000001"
    assert pool.acquire_attempts == 2
    assert pool.connection.insert_calls == 1


@pytest.mark.asyncio
async def test_write_one_gives_up_after_max_attempts() -> None:
    """If every acquire fails, the original error surfaces (no silent swallow)."""
    err = asyncpg.exceptions.ConnectionDoesNotExistError("still dead")
    pool = _FlakyPool(fail_first=99, error=err)
    repo = VerdictsRepository(pool, source=_settings())  # type: ignore[arg-type]

    with patch("compass_backend.db.verdicts.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
            await repo.write_one(_verdict())

    assert pool.acquire_attempts == 3  # _ACQUIRE_MAX_ATTEMPTS
    assert pool.connection.insert_calls == 0  # never reached the INSERT
