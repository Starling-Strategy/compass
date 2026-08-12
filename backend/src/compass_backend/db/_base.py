"""Shared base for Postgres-backed repositories.

The ``db/*`` repositories each carried a byte-identical pair of private
helpers — ``_acquire()`` (yield the per-turn connection if free, else the
pool) and ``_table()`` (schema-qualify a table name). ``RepositoryBase``
holds the single copy; the repositories mix it in and keep only their own
queries.

A repository that mixes this in must set ``self._settings`` (a ``Settings``)
and ``self._pool`` (an ``asyncpg.Pool`` or ``ChatPoolHolder``) in its
``__init__`` — the same two attributes the inlined helpers already used.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import asyncpg

from compass_backend.db._turn_connection import acquire_turn_or_pool
from compass_backend.session.postgres import quote_ident

if TYPE_CHECKING:
    from compass_backend.config import Settings
    from compass_backend.db._pool import ChatPoolHolder


class RepositoryBase:
    """Connection-acquire + table-name helpers shared by db repositories."""

    _settings: "Settings"
    _pool: "asyncpg.Pool | ChatPoolHolder"

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Yield a connection: the per-turn connection if free, else pool."""

        async with acquire_turn_or_pool(self._pool) as conn:
            yield conn

    def _table(self, table_name: str) -> str:
        return f"{quote_ident(self._settings.pg_schema)}.{quote_ident(table_name)}"
