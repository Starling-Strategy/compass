"""Postgres adapter for the debug-view review loop (``compass.case_reports``).

Reviewers record a pass/partial/fail verdict on a Compass turn straight from
the debug bar; this repository is the durable store for those reports. It
mirrors ``db/frontend_contracts.py`` in shape — a ``pool: asyncpg.Pool |
ChatPoolHolder`` constructor argument and an ``_acquire()`` helper that resolves
the shared chat pool lazily via ``resolve_pool`` — so it borrows the same
connection pool the rest of the chat hot path uses.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from compass_backend.config import Settings, settings
from compass_backend.db._base import RepositoryBase
from compass_backend.db._pool import ChatPoolHolder


class CaseReportRepository(RepositoryBase):
    """Read/write access to the single ``compass.case_reports`` table.

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

    async def session_exists(self, session_id: UUID | str) -> bool:
        """Return True when at least one chat message exists for the session.

        ``compass.chat_messages.session_id`` is TEXT (unlike
        ``compass.verdicts.session_id`` which is UUID), so the UUID handed in
        by the route is compared as text — otherwise the type mismatch would
        either error or silently miss the row.
        """

        async with self._acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT 1
                FROM {self._table("chat_messages")}
                WHERE session_id = $1
                LIMIT 1
                """,
                str(session_id),
            )
        return row is not None

    async def case_exists(self, case_id: int) -> bool:
        """Return True when an active B-spine case with this id exists."""

        async with self._acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT 1
                FROM {self._table("cases")}
                WHERE id = $1 AND active
                """,
                case_id,
            )
        return row is not None

    async def insert_report(
        self,
        *,
        case_id: int | None,
        session_id: UUID,
        turn_index: int | None,
        trace_id: str | None,
        dimension: str | None,
        outcome: str,
        comments: str | None,
        reviewer: str | None,
    ) -> UUID:
        """Insert one reviewer report and return its generated id."""

        async with self._acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._table("case_reports")}
                    (case_id, session_id, turn_index, trace_id,
                     dimension, outcome, comments, reviewer)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                case_id,
                session_id,
                turn_index,
                trace_id,
                dimension,
                outcome,
                comments,
                reviewer,
            )
        # RETURNING on a successful INSERT always yields a row.
        assert row is not None
        return row["id"]

    async def get_latest_report(
        self,
        session_id: UUID,
        case_id: int | None,
    ) -> dict[str, Any] | None:
        """Return the most recent report for a session (optionally a case).

        When ``case_id`` is None the latest report for the whole session is
        returned; when given, the result is narrowed to that case.
        """

        async with self._acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT id, case_id, session_id, turn_index, trace_id,
                       dimension, outcome, comments, status, reviewer,
                       linked_issue, created_at, updated_at
                FROM {self._table("case_reports")}
                WHERE session_id = $1
                  AND ($2::integer IS NULL OR case_id = $2)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                session_id,
                case_id,
            )
        return dict(row) if row is not None else None

    async def update_status(
        self,
        report_id: UUID,
        *,
        status: str,
        reviewer: str | None = None,
    ) -> bool:
        """Move a report to a new triage ``status`` and bump ``updated_at``.

        ``reviewer`` is COALESCEd so passing None leaves any existing reviewer
        in place rather than nulling it. ``updated_at`` is set explicitly here:
        the column carries a DB default but there is no trigger, so an UPDATE
        would otherwise leave it stale. Returns True when a row matched (the id
        existed), False otherwise — the route maps False to a 404.
        """

        async with self._acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE {self._table("case_reports")}
                SET status = $2,
                    reviewer = COALESCE($3, reviewer),
                    updated_at = now()
                WHERE id = $1
                RETURNING id
                """,
                report_id,
                status,
                reviewer,
            )
        return row is not None


__all__ = ["CaseReportRepository"]
