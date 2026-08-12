"""Wiring tests for the singleton chat asyncpg pool.

Audit finding #1: the chat hot path used to open a fresh
``asyncpg.connect(...)`` per query. ``create_app_from_settings`` now wires
a singleton pool through a ``ChatPoolHolder`` so every repository the
chat router builds reads from the same shared pool.

These tests use a fake pool (no real Postgres) to assert:
  1. Repositories share the same ``ChatPoolHolder`` reference.
  2. ``_acquire()`` reads ``holder.pool`` lazily, so the pool can be
     populated after the repos are constructed (the sync app factory /
     async startup hook split this PR adopts).
  3. ``resolve_pool`` raises if the holder is still empty when a repo
     tries to acquire a connection — guarding against silent regression
     to per-call connects (the legacy fallback is gone).
  4. ``search_catalog_aliases`` / ``fetch_recall_aliases`` use a
     process-lifetime cache: the pool is acquired once for the full alias
     set and subsequent calls filter in-memory (C1).
  5. ``search_nces_fields`` caches the ``information_schema.columns`` query
     per ``(schema, table)``; a second call must not acquire from the pool
     again (C2).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import SecretStr

from compass_backend.api.auth import ApiKeyAuthRepository
from compass_backend.config import Settings
from compass_backend.db._pool import ChatPoolHolder, resolve_pool
from compass_backend.db._turn_connection import turn_connection
from compass_backend.db.catalog import (
    ReadOnlyCatalogRepository,
    reset_alias_cache,
    reset_nces_fields_cache,
)
from compass_backend.db.frontend_contracts import (
    PostgresFrontendContractRepository,
)
from compass_backend.db.policy_answers import ReadOnlyPolicyAnswerRepository
from compass_backend.db.scenarios import ReadOnlyScenarioCaseRepository
from compass_backend.session.postgres import PostgresSessionStore


class FakeConnection:
    """Records the fetch SQL so we can assert the pool branch ran."""

    def __init__(self) -> None:
        self.fetched: list[str] = []

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        self.fetched.append(sql)
        # ReadOnlyCatalogRepository.fetch_all_metrics_for_resolver expects
        # rows shaped like {"metric_id": ..., "name": ..., ...}. Returning
        # [] is a valid no-rows response and avoids re-implementing the
        # mapping logic in the test.
        return []

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        self.fetched.append(sql)
        return None

    async def execute(self, sql: str, *args: object) -> str:
        self.fetched.append(sql)
        return "OK"


class FakePool:
    """Minimal asyncpg.Pool stand-in.

    ``acquire()`` returns an async-context-manager that yields a
    ``FakeConnection``. Tracks every acquire so the test can assert the
    pool was actually used.
    """

    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.acquired = 0

    def acquire(self) -> Any:
        pool = self

        @asynccontextmanager
        async def _cm():
            pool.acquired += 1
            yield pool.connection

        return _cm()


def _settings() -> Settings:
    return Settings(pg_schema="compass", pg_password=SecretStr("test"))


def test_resolve_pool_handles_holder_and_direct_pool() -> None:
    """``resolve_pool`` reads ``holder.pool`` lazily and passes pools through."""

    holder = ChatPoolHolder()
    # Unpopulated holder raises — fail loud rather than silently degrade.
    with pytest.raises(RuntimeError, match="Chat pool not initialized"):
        resolve_pool(holder)

    pool = FakePool()
    holder.pool = pool  # type: ignore[assignment]
    assert resolve_pool(holder) is pool

    # Direct pool passes through.
    assert resolve_pool(pool) is pool  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_repositories_share_holder_and_route_through_pool() -> None:
    """Every chat-path repo accepts the same ChatPoolHolder and uses its pool.

    Mirrors the production wiring in ``create_app_from_settings``:
    the holder is built once, every repository constructor receives it,
    then the startup hook populates ``holder.pool``. Any subsequent
    ``_acquire()`` call must route through that shared pool — a regression
    would re-introduce per-call ``asyncpg.connect(...)``.
    """

    holder = ChatPoolHolder()
    cfg = _settings()

    catalog = ReadOnlyCatalogRepository(cfg, pool=holder)
    policy = ReadOnlyPolicyAnswerRepository(cfg, pool=holder)
    scenarios = ReadOnlyScenarioCaseRepository(cfg, pool=holder)
    frontend = PostgresFrontendContractRepository(cfg, pool=holder)
    session_store = PostgresSessionStore(cfg, pool=holder)
    auth = ApiKeyAuthRepository(cfg, pool=holder)

    # All six repositories hold the same holder reference.
    assert catalog._pool is holder
    assert policy._pool is holder
    assert scenarios._pool is holder
    assert frontend._pool is holder
    assert session_store._pool is holder
    assert auth._pool is holder

    # Populate the holder AFTER construction — emulates the lifespan
    # startup hook landing the live pool while routers are already built.
    pool = FakePool()
    holder.pool = pool  # type: ignore[assignment]

    # Exercise the pool branch through one representative repo. Any
    # query method that calls ``_acquire()`` will do; the catalog repo's
    # search-metrics path is concise and routes through ``conn.fetch``.
    await catalog.search_metrics("teacher salary", limit=5)
    assert pool.acquired == 1, "search_metrics must borrow from the pool"
    assert any(
        "navigator_metrics" in sql for sql in pool.connection.fetched
    ), "the fake connection should have seen the catalog SQL"


@pytest.mark.asyncio
async def test_acquire_on_empty_holder_raises_loud_error() -> None:
    """If a request fires before the startup hook populates the holder,
    ``_acquire()`` must raise immediately rather than silently fall back
    to per-call ``asyncpg.connect``. The legacy fallback is gone — this
    test pins the loud-failure contract that replaces it.
    """

    holder = ChatPoolHolder()
    catalog = ReadOnlyCatalogRepository(_settings(), pool=holder)

    with pytest.raises(RuntimeError, match="Chat pool not initialized"):
        await catalog.search_metrics("anything", limit=1)


# ─── C1: process-lifetime alias cache ────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_catalog_aliases_uses_cache_after_first_load() -> None:
    """``search_catalog_aliases`` loads alias rows once; a second call with the
    same or different filters must NOT acquire from the pool again.

    The cache is populated on the first acquire. Both calls must return the
    same filtered records for the same input, and ``pool.acquired`` must equal
    1 after two calls (no second round-trip to the DB).
    """
    reset_alias_cache()

    # Pre-build rows that simulate what the DB would return for the full
    # ``active IS TRUE AND review_status='approved'`` fetch.
    alias_rows = [
        {
            "alias": "LA Unified",
            "normalized_alias": "la unified",
            "entity_type": "district",
            "resolution_status": "approved",
            "canonical_id": "D001",
            "canonical_ids": [],
            "candidate_refs": [],
            "metadata": {},
            "source": "manual",
            "provenance": "",
            "scenario_ids": None,
            "review_status": "approved",
            "active": True,
        },
        {
            "alias": "NYC Schools",
            "normalized_alias": "nyc schools",
            "entity_type": "district",
            "resolution_status": "ambiguous",
            "canonical_id": None,
            "canonical_ids": [],
            "candidate_refs": [],
            "metadata": {},
            "source": "manual",
            "provenance": "",
            "scenario_ids": None,
            "review_status": "approved",
            "active": True,
        },
    ]

    pool = FakePool()
    # Prime the connection to return our alias rows for the catalog_aliases query.
    pool.connection.rows_for_table = alias_rows

    # Patch FakeConnection.fetch to return the alias rows when catalog_aliases appears.
    original_fetch = pool.connection.fetch

    async def _patched_fetch(sql: str, *args: object) -> list[dict[str, Any]]:
        if "catalog_aliases" in sql:
            return alias_rows
        return await original_fetch(sql, *args)

    pool.connection.fetch = _patched_fetch  # type: ignore[method-assign]

    cfg = _settings()
    catalog = ReadOnlyCatalogRepository(cfg, pool=pool)

    # First call — must acquire from pool to populate cache.
    results1 = await catalog.search_catalog_aliases(
        "la unified", entity_types={"district"}
    )
    assert pool.acquired == 1, "first call must acquire once to populate cache"

    # Second call with identical args — must NOT acquire again (cache hit).
    results2 = await catalog.search_catalog_aliases(
        "la unified", entity_types={"district"}
    )
    assert pool.acquired == 1, "second call must not acquire; cache should be warm"
    assert results1 == results2, "both calls must return identical records"

    # Clean up so other tests are not affected.
    reset_alias_cache()


@pytest.mark.asyncio
async def test_fetch_recall_aliases_uses_same_cache() -> None:
    """``fetch_recall_aliases`` reuses the same process cache so it does not
    acquire a second time if ``search_catalog_aliases`` already warmed it.
    """
    reset_alias_cache()

    alias_rows = [
        {
            "alias": "Denver Schools",
            "normalized_alias": "denver schools",
            "entity_type": "district",
            "resolution_status": "approved",
            "canonical_id": "D002",
            "canonical_ids": [],
            "candidate_refs": [],
            "metadata": {},
            "source": "manual",
            "provenance": "",
            "scenario_ids": None,
            "review_status": "approved",
            "active": True,
        },
    ]

    pool = FakePool()

    async def _patched_fetch(sql: str, *args: object) -> list[dict[str, Any]]:
        if "catalog_aliases" in sql:
            return alias_rows
        return []

    pool.connection.fetch = _patched_fetch  # type: ignore[method-assign]

    cfg = _settings()
    catalog = ReadOnlyCatalogRepository(cfg, pool=pool)

    # Warm the cache via search_catalog_aliases.
    await catalog.search_catalog_aliases("denver", entity_types={"district"})
    assert pool.acquired == 1

    # fetch_recall_aliases must not acquire again — same cache.
    results = await catalog.fetch_recall_aliases(entity_types={"district"})
    assert pool.acquired == 1, "fetch_recall_aliases must reuse the warm cache"
    assert len(results) == 1
    assert results[0].alias == "Denver Schools"

    reset_alias_cache()


# ─── C2: nces_districts column introspection cache ───────────────────────────


@pytest.mark.asyncio
async def test_search_nces_fields_caches_information_schema_query() -> None:
    """``search_nces_fields`` introspects ``information_schema.columns`` once
    per ``(schema, table)`` key; a second call must NOT acquire from the pool
    again (cache hit).

    The pool is acquired once to populate the column-name set; subsequent
    calls for the same ``(schema, table)`` pair filter against the in-memory
    set.  ``frpl_pct`` is always included regardless of the column list
    (special-case preserved).
    """
    reset_nces_fields_cache()

    # Simulate the DB returning one physical column from information_schema.
    info_schema_rows: list[dict[str, Any]] = [{"column_name": "total_students"}]

    pool = FakePool()
    original_fetch = pool.connection.fetch

    async def _patched_fetch(sql: str, *args: object) -> list[dict[str, Any]]:
        if "information_schema" in sql:
            return info_schema_rows
        return await original_fetch(sql, *args)

    pool.connection.fetch = _patched_fetch  # type: ignore[method-assign]

    cfg = _settings()
    catalog = ReadOnlyCatalogRepository(cfg, pool=pool)

    # First call — must acquire from pool to populate column cache.
    results1 = await catalog.search_nces_fields("total students", limit=10)
    assert pool.acquired == 1, "first call must acquire once to populate column cache"

    # Second call — must NOT acquire again (cache hit).
    results2 = await catalog.search_nces_fields("total students", limit=10)
    assert pool.acquired == 1, "second call must not acquire; column cache is warm"

    # Both calls must return the same set of field keys.
    keys1 = {r.field_key for r in results1}
    keys2 = {r.field_key for r in results2}
    assert keys1 == keys2, "both calls must return identical records"

    reset_nces_fields_cache()


# ─── C3: per-turn pooled connection via contextvar ────────────────────────────


@pytest.mark.asyncio
async def test_catalog_repo_sequential_reuse_under_turn_connection() -> None:
    """ReadOnlyCatalogRepository reuses the turn connection for sequential queries.

    Under an active turn_connection(), two sequential _acquire() calls inside
    ReadOnlyCatalogRepository must both receive the same connection object and
    pool.acquired must remain 1 (only the turn_connection() acquire counts).

    Without a turn connection, each _acquire() falls through to the pool as
    today.
    """
    reset_alias_cache()
    reset_nces_fields_cache()

    pool = FakePool()
    cfg = _settings()
    catalog = ReadOnlyCatalogRepository(cfg, pool=pool)

    # Without a turn connection — each _acquire() uses the pool.
    await catalog.search_metrics("teacher salary", limit=5)
    assert pool.acquired == 1

    # Reset counter.
    pool.acquired = 0

    # With a turn connection — sequential _acquire() calls reuse it.
    async with turn_connection(pool):
        # turn_connection() itself acquires once.
        assert pool.acquired == 1

        # A second query reuses the same connection (busy-guard is idle between
        # sequential calls).
        await catalog.search_metrics("compensation", limit=5)
        # pool.acquired stays at 1.
        assert pool.acquired == 1, (
            "second sequential query must reuse the turn connection, not acquire again"
        )


@pytest.mark.asyncio
async def test_catalog_repo_no_turn_connection_uses_pool_per_call() -> None:
    """Without a turn_connection active, _acquire() falls through to the pool
    on every call — identical to behaviour before C3.
    """
    reset_alias_cache()
    reset_nces_fields_cache()

    pool = FakePool()
    cfg = _settings()
    catalog = ReadOnlyCatalogRepository(cfg, pool=pool)

    await catalog.search_metrics("salary", limit=5)
    await catalog.search_metrics("salary", limit=5)
    # Two independent acquires — no turn connection set.
    assert pool.acquired == 2
