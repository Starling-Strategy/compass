"""Postgres-backed session persistence for the fresh Compass API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from compass_backend.observability import compass_span
from compass_backend.config import Settings, settings
from compass_backend.contracts import ConversationMemory, SessionState, TurnSnapshot

if TYPE_CHECKING:  # pragma: no cover - typing only
    from compass_backend.db._pool import ChatPoolHolder

from .memory import conversation_memory_from_snapshots
from .store import (
    SavedMessageRef,
    SavedTurnResult,
    SessionAccessDenied,
    TurnErrorEnvelope,
    _authorize_owner,
)

_SNAPSHOT_KEY = "fresh_compass_turn_snapshot"


def quote_ident(identifier: str) -> str:
    """Quote a PostgreSQL identifier."""

    return '"' + identifier.replace('"', '""') + '"'


class PostgresSessionStore:
    """Persist fresh Compass sessions into existing Compass chat tables.

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

    async def load(
        self,
        session_id: str | None = None,
        *,
        owner_email: str | None = None,
        is_admin: bool = False,
        create_if_missing: bool = True,
        visitor_id: str | None = None,
    ) -> SessionState:
        """Load existing fresh session state, creating the session row if needed."""

        state = SessionState(session_id=session_id) if session_id else SessionState()
        async with self._acquire() as conn:
            session_row = await self._ensure_session(
                conn,
                state.session_id,
                owner_email=owner_email,
                create_if_missing=create_if_missing,
                visitor_id=visitor_id,
            )
            _authorize_owner(
                session_row.get("owner_email") if session_row else None,
                owner_email=owner_email,
                is_admin=is_admin,
            )
            snapshots = await self._fetch_snapshots(conn, state.session_id)

        return _state_from_snapshots(
            session_id=state.session_id,
            created_at=session_row.get("created_at") if session_row else state.created_at,
            snapshots=snapshots,
        )

    async def save_turn(self, snapshot: TurnSnapshot) -> SavedTurnResult:
        """Persist one user/assistant turn and return the updated session state."""

        with compass_span(
            "compass.persistence.save_turn",
            trace_id=snapshot.trace_id,
            session_id=snapshot.session_id,
            turn_index=snapshot.turn_index,
            snapshot_present=True,
        ):
            async with self._acquire() as conn:
                async with conn.transaction():
                    session_row = await self._ensure_session(conn, snapshot.session_id)
                    user_ref = await self._insert_message(
                        conn,
                        session_id=snapshot.session_id,
                        role="user",
                        content=snapshot.user_message,
                        message_data=None,
                        trace_id=snapshot.trace_id,
                    )
                    assistant_ref = await self._insert_message(
                        conn,
                        session_id=snapshot.session_id,
                        role="assistant",
                        content=snapshot.assistant_message,
                        message_data={_SNAPSHOT_KEY: snapshot.model_dump(mode="json")},
                        trace_id=snapshot.trace_id,
                    )
                snapshots = await self._fetch_snapshots(conn, snapshot.session_id)

            return SavedTurnResult(
                session=_state_from_snapshots(
                    session_id=snapshot.session_id,
                    created_at=(
                        session_row.get("created_at") if session_row else snapshot.created_at
                    ),
                    snapshots=snapshots,
                ),
                messages=(user_ref, assistant_ref),
            )

    async def save_error_envelope(
        self, envelope: TurnErrorEnvelope
    ) -> SavedTurnResult:
        """Persist a failed-turn envelope (user + error-assistant rows).

        Written under the same span shape as `save_turn` so observability
        binds error-path persistence the same way as success-path
        persistence. The envelope's `error_type`/`error_message`/`stage`
        are stored in the assistant row's `message_data` JSONB under a
        dedicated `error` key (distinct from the `fresh_compass_turn_snapshot`
        key used by successful turns) so downstream readers can tell
        the two apart on a single query.

        The DB write is wrapped in a nested ``compass.persistence.save_turn``
        span (carrying ``persistence_path="error_envelope"``) in addition to
        the outer ``compass.persistence.save_error_envelope`` span. The user
        turn *is* persisted on this path, so the trace must surface the same
        ``compass.persistence.save_turn`` span the success path emits;
        otherwise the ``persistence_succeeded_for_user_turn`` span-assertion
        false-fails every error-path turn even though persistence succeeded
        (#940). The outer error span stays for error diagnostics; the
        ``persistence_path`` attribute keeps the two paths distinguishable.
        """

        with compass_span(
            "compass.persistence.save_error_envelope",
            trace_id=envelope.trace_id,
            session_id=envelope.session_id,
            error_type=envelope.error_type,
            stage=envelope.stage,
        ), compass_span(
            "compass.persistence.save_turn",
            trace_id=envelope.trace_id,
            session_id=envelope.session_id,
            persistence_path="error_envelope",
            snapshot_present=False,
        ):
            async with self._acquire() as conn:
                async with conn.transaction():
                    session_row = await self._ensure_session(
                        conn, envelope.session_id
                    )
                    user_ref = await self._insert_message(
                        conn,
                        session_id=envelope.session_id,
                        role="user",
                        content=envelope.user_message,
                        message_data=None,
                        trace_id=envelope.trace_id,
                    )
                    assistant_ref = await self._insert_message(
                        conn,
                        session_id=envelope.session_id,
                        role="assistant",
                        content=envelope.assistant_message,
                        message_data={
                            "error": {
                                "type": envelope.error_type,
                                "message": envelope.error_message,
                                "stage": envelope.stage,
                            },
                        },
                        trace_id=envelope.trace_id,
                    )
                snapshots = await self._fetch_snapshots(
                    conn, envelope.session_id
                )

            return SavedTurnResult(
                session=_state_from_snapshots(
                    session_id=envelope.session_id,
                    created_at=(
                        session_row.get("created_at")
                        if session_row
                        else datetime.now()
                    ),
                    snapshots=snapshots,
                ),
                messages=(user_ref, assistant_ref),
            )

    async def snapshots_for_session(self, session_id: str) -> list[TurnSnapshot]:
        """Return fresh turn snapshots for a session in turn order."""

        async with self._acquire() as conn:
            return await self._fetch_snapshots(conn, session_id)

    async def _ensure_session(
        self,
        conn: asyncpg.Connection,
        session_id: str,
        *,
        owner_email: str | None = None,
        create_if_missing: bool = True,
        visitor_id: str | None = None,
    ) -> dict[str, Any]:
        if create_if_missing:
            # WS-5: visitor_id uses DO UPDATE (not DO NOTHING) so it backfills on
            # an existing row and reconciles fallback→parent across turns. The
            # WHERE guard updates only when this turn carries a non-null id, so a
            # later turn with no id never wipes a stored one, while a non-null
            # incoming id wins (last-non-null write) — that's how a parent-issued
            # id supersedes an earlier iframe-local fallback. owner_email keeps its
            # original insert-only semantics.
            await conn.execute(
                f"""
                INSERT INTO {self._table("chat_sessions")}
                    (session_id, owner_email, visitor_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (session_id) DO UPDATE
                    SET visitor_id = EXCLUDED.visitor_id
                    WHERE EXCLUDED.visitor_id IS NOT NULL
                """,
                session_id,
                owner_email,
                visitor_id,
            )
        row = await conn.fetchrow(
            f"""
            SELECT session_id, owner_email, created_at
            FROM {self._table("chat_sessions")}
            WHERE session_id = $1
            """,
            session_id,
        )
        if row is None and not create_if_missing:
            raise SessionAccessDenied()
        return dict(row) if row is not None else {"session_id": session_id}

    async def _insert_message(
        self,
        conn: asyncpg.Connection,
        *,
        session_id: str,
        role: str,
        content: str,
        message_data: dict[str, object] | None,
        trace_id: str | None,
    ) -> SavedMessageRef:
        row = await conn.fetchrow(
            f"""
            INSERT INTO {self._table("chat_messages")}
                (session_id, role, content, message_data, trace_id)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            RETURNING id
            """,
            session_id,
            role,
            content,
            json.dumps(message_data) if message_data is not None else None,
            trace_id,
        )
        return SavedMessageRef(
            message_id=int(row["id"]) if row is not None else 0,
            role="assistant" if role == "assistant" else "user",
        )

    async def _fetch_snapshots(
        self,
        conn: asyncpg.Connection,
        session_id: str,
    ) -> list[TurnSnapshot]:
        rows = await conn.fetch(
            f"""
            SELECT message_data->'{_SNAPSHOT_KEY}' AS snapshot
            FROM {self._table("chat_messages")}
            WHERE session_id = $1
              AND role = 'assistant'
              AND message_data ? '{_SNAPSHOT_KEY}'
            ORDER BY
                ((message_data->'{_SNAPSHOT_KEY}'->>'turn_index')::int),
                timestamp,
                id
            """,
            session_id,
        )
        return [
            TurnSnapshot.model_validate(_snapshot_payload(row["snapshot"]))
            for row in rows
        ]

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Yield a connection: the per-turn connection if free, else pool.

        Local imports keep the module-load graph acyclic: db/__init__
        would otherwise pull frontend_contracts -> session.postgres in
        a loop. See docs note in db/_pool.py.
        """

        from compass_backend.db._turn_connection import acquire_turn_or_pool

        async with acquire_turn_or_pool(self._pool) as conn:
            yield conn

    def _table(self, table_name: str) -> str:
        return f"{quote_ident(self._settings.pg_schema)}.{quote_ident(table_name)}"


def _snapshot_payload(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _state_from_snapshots(
    *,
    session_id: str,
    created_at: object,
    snapshots: list[TurnSnapshot],
) -> SessionState:
    created = created_at if isinstance(created_at, datetime) else SessionState().created_at
    if not snapshots:
        return SessionState(
            session_id=session_id,
            created_at=created,
            updated_at=created,
            turn_count=0,
            latest_snapshot_id=None,
            memory=ConversationMemory(),
        )
    latest = max(snapshots, key=lambda snapshot: snapshot.turn_index)
    return SessionState(
        session_id=session_id,
        created_at=created,
        updated_at=latest.created_at,
        turn_count=latest.turn_index,
        latest_snapshot_id=latest.snapshot_id,
        memory=conversation_memory_from_snapshots(snapshots) or ConversationMemory(),
    )
