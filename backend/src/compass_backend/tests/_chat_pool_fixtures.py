"""Shared mock-pool fixtures for chat-path repository tests.

The underscore prefix prevents pytest from collecting this as a test module.

Chat-path repositories (``ReadOnlyCatalogRepository``,
``ReadOnlyPolicyAnswerRepository``, ``ReadOnlyScenarioCaseRepository``,
``PostgresFrontendContractRepository``, ``PostgresSessionStore``,
``ApiKeyAuthRepository``) now require an ``asyncpg.Pool | ChatPoolHolder``
constructor argument. Tests construct a ``FakeChatPool`` here and pass it
in directly — no more ``monkeypatch.setattr("...asyncpg.connect", ...)``.

The ``FakeConn`` is a minimal asyncpg connection stand-in that records the
SQL/params it sees and returns pre-configured rows. Tests can either pre-
configure the response queues at construction time or assign attributes
directly on the connection (``conn.fetchrow_response = {...}``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class FakeConn:
    """Minimal asyncpg connection stand-in for repository SQL mapping tests.

    Records every ``(sql, args)`` it sees on ``self.queries``. Responses
    can be supplied per-method:

      * ``fetch_response`` (callable taking ``(sql, args)``) — return value
        for ``fetch``. Defaults to a function that returns rows from
        ``rows_by_table`` when a key appears in the SQL, else ``[]``.
      * ``fetchrow_response`` / ``execute_response`` — return values for
        ``fetchrow`` / ``execute``. Can be set per-instance or supplied via
        constructor.
    """

    def __init__(
        self,
        rows_by_table: dict[str, list[dict[str, Any]]] | None = None,
        *,
        fetchrow_response: dict[str, Any] | None = None,
        execute_response: str = "OK",
    ) -> None:
        self.rows_by_table: dict[str, list[dict[str, Any]]] = rows_by_table or {}
        self.fetchrow_response: dict[str, Any] | None = fetchrow_response
        self.execute_response: str = execute_response
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        self.queries.append((sql, args))
        for table_name, rows in self.rows_by_table.items():
            if table_name in sql:
                return rows
        return []

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        self.queries.append((sql, args))
        return self.fetchrow_response

    async def execute(self, sql: str, *args: object) -> str:
        self.queries.append((sql, args))
        return self.execute_response


class FakeChatPool:
    """Minimal asyncpg.Pool stand-in that yields a :class:`FakeConn`.

    ``pool.acquire()`` returns an async context manager wrapping the
    single shared connection so tests can inspect ``conn.queries`` after
    the call. ``self.acquired`` counts each ``acquire()`` entry so tests
    can assert the repo borrowed from the pool.
    """

    def __init__(self, conn: FakeConn | None = None) -> None:
        self.conn = conn if conn is not None else FakeConn()
        self.acquired = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConn]:
        self.acquired += 1
        yield self.conn


__all__ = ["FakeConn", "FakeChatPool"]


# Re-exported as a convenience for tests that need a JSON sentinel.
_jsonb = json.dumps  # noqa: F401 — convenience constant for tests
