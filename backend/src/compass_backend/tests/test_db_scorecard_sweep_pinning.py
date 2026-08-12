"""W0-02: scorecard rollup query must pin to ONE sweep_run_id per call.

Before W0-02 ``load_case_rollups_for_dimension`` resolved the latest
``status='completed'`` sweep via a correlated subquery embedded inside the
verdict rollup query. That worked, but it left a footgun: any future caller
that bypassed the subquery (or refactored it loose) could silently mix
verdicts across sweep runs and produce a meaningless per-dimension
reading.

W0-02 hardens the picker:
  * A dedicated helper ``_resolve_latest_completed_sweep_run_id`` returns
    exactly one id (or ``None``).
  * The rollup query binds that id as ``$2::uuid`` and reads from a single
    sweep.
  * Sweeps with ``status='partial'`` or ``'failed'`` MUST be excluded so
    dishonest sweeps never bubble into the dashboard.

These tests assert all three properties using a fake asyncpg connection
that records both queries — no live DB required.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any

import pytest

from compass_backend.db import scorecard as scorecard_db


class _FakeConn:
    """Captures fetchrow + fetch calls and replays canned responses."""

    def __init__(
        self,
        *,
        fetchrow_results: list[Any] | None = None,
        fetch_results: list[Any] | None = None,
    ) -> None:
        self._fetchrow_results = list(fetchrow_results or [])
        self._fetch_results = list(fetch_results or [])
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.fetchrow_calls.append((sql, args))
        return self._fetchrow_results.pop(0) if self._fetchrow_results else None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.fetch_calls.append((sql, args))
        return self._fetch_results.pop(0) if self._fetch_results else []


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


@pytest.mark.asyncio
async def test_resolve_latest_completed_sweep_run_id_filters_status_completed() -> None:
    """The resolver must hardcode ``status = 'completed'``."""
    conn = _FakeConn(fetchrow_results=[{"id": "11111111-1111-1111-1111-111111111111"}])

    resolved = await scorecard_db._resolve_latest_completed_sweep_run_id(
        conn, schema="compass", dimension="Sort Accuracy"
    )

    assert resolved == "11111111-1111-1111-1111-111111111111"
    assert len(conn.fetchrow_calls) == 1
    sql, args = conn.fetchrow_calls[0]
    assert "status = 'completed'" in sql, (
        "resolver MUST exclude partial/failed sweeps; the literal "
        "status='completed' filter is the W0-02 fence."
    )
    assert "LIMIT 1" in sql, "resolver must pick exactly one id"
    assert args == ("Sort Accuracy",)


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_completed_sweep_exists() -> None:
    """Empty DB → ``None``; caller must short-circuit, not fall back to mixing."""
    conn = _FakeConn(fetchrow_results=[None])

    resolved = await scorecard_db._resolve_latest_completed_sweep_run_id(
        conn, schema="compass", dimension="Filter Accuracy"
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_load_case_rollups_short_circuits_when_no_completed_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No completed sweep → returns ``[]`` without issuing the rollup query.

    This is the W0-02 honesty rule: when the only sweep is ``partial`` or
    ``failed`` (or none exist), the dashboard renders empty rather than
    falling back to mixing across runs.
    """
    monkeypatch.setattr(scorecard_db.settings, "pg_schema", "compass")
    conn = _FakeConn(fetchrow_results=[None])
    pool = _FakePool(conn)

    rollups = await scorecard_db.load_case_rollups_for_dimension(
        pool, "Sort Accuracy"
    )

    assert rollups == []
    assert len(conn.fetchrow_calls) == 1, (
        "expected the resolver round-trip but no rollup fetch"
    )
    assert len(conn.fetch_calls) == 0, (
        "rollup query MUST NOT run when no eligible sweep exists; "
        "running it would mix verdicts across runs"
    )


@pytest.mark.asyncio
async def test_load_case_rollups_binds_single_sweep_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rollup query must bind exactly one ``sweep_run_id`` parameter."""
    monkeypatch.setattr(scorecard_db.settings, "pg_schema", "compass")
    chosen_id = "22222222-2222-2222-2222-222222222222"
    conn = _FakeConn(
        fetchrow_results=[{"id": chosen_id}],
        fetch_results=[[]],  # rollup query returns no rows for this test
    )
    pool = _FakePool(conn)

    rollups = await scorecard_db.load_case_rollups_for_dimension(
        pool, "Sort Accuracy"
    )

    assert rollups == []
    assert len(conn.fetch_calls) == 1, "expected exactly one rollup fetch"
    sql, args = conn.fetch_calls[0]
    assert "v.sweep_run_id = $2::uuid" in sql, (
        "rollup query MUST bind the pinned sweep_run_id as $2 so the picker "
        "is auditable; got SQL:\n" + sql
    )
    assert args == ("Sort Accuracy", chosen_id), (
        f"rollup query bound unexpected args: {args!r}"
    )
    # No correlated subquery may remain — that was the pre-W0-02 footgun.
    assert "SELECT id" not in re.sub(r"\s+", " ", sql).split("WHERE")[-1], (
        "rollup query MUST NOT embed a sweep_runs sub-SELECT in its WHERE "
        "clause; the picker is now hoisted to a separate round-trip."
    )


@pytest.mark.asyncio
async def test_load_case_rollups_honours_caller_supplied_sweep_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``sweep_run_id`` must bypass the resolver entirely."""
    monkeypatch.setattr(scorecard_db.settings, "pg_schema", "compass")
    conn = _FakeConn(fetch_results=[[]])
    pool = _FakePool(conn)
    explicit_id = "33333333-3333-3333-3333-333333333333"

    await scorecard_db.load_case_rollups_for_dimension(
        pool, "Sort Accuracy", sweep_run_id=explicit_id
    )

    assert len(conn.fetchrow_calls) == 0, (
        "caller supplied a sweep_run_id; resolver fetchrow must NOT run"
    )
    assert len(conn.fetch_calls) == 1
    _sql, args = conn.fetch_calls[0]
    assert args == ("Sort Accuracy", explicit_id)
