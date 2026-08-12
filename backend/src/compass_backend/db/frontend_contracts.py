"""Active Postgres adapters for the PHP frontend compatibility routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from compass_backend.config import Settings, settings
from compass_backend.contracts.frontend import (
    ConversationMessage,
    ConversationResponse,
    DebugBackfillResponse,
    FeedbackResponse,
    UsageTotals,
)
from compass_backend.db._base import RepositoryBase
from compass_backend.db._pool import ChatPoolHolder
from compass_backend.db._turn_connection import acquire_turn_or_pool
from compass_backend.session.store import SessionAccessDenied

_SNAPSHOT_KEY = "fresh_compass_turn_snapshot"


class PostgresFrontendContractRepository(RepositoryBase):
    """Repository limited to the PG_SCHEMA tables needed by active proxies.

    Borrows a connection from the shared chat pool wired by
    ``create_app_from_settings``. Tests inject a synthetic pool via
    ``FakeChatPool`` from ``compass_backend.tests._chat_pool_fixtures``.

    Note on ``readonly``: pool-shared connections do not toggle the
    session-level ``default_transaction_read_only`` flag, so the
    ``readonly`` argument on ``_acquire`` is informational only. The
    pool defaults to read-write and the read paths are safe under
    either.
    """

    def __init__(
        self,
        source: Settings = settings,
        *,
        pool: asyncpg.Pool | ChatPoolHolder,
    ) -> None:
        self._settings = source
        self._pool = pool

    async def get_conversation(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> ConversationResponse:
        async with self._acquire(readonly=True) as conn:
            meta = await self._load_session_meta(conn, session_id)
            self._authorize_meta(
                meta,
                owner_email=owner_email,
                is_admin=is_admin,
                auth_required=auth_required,
            )
            return await self._load_conversation(conn, meta)

    async def ensure_session_access(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> None:
        async with self._acquire(readonly=True) as conn:
            meta = await self._load_session_meta(conn, session_id)
            self._authorize_meta(
                meta,
                owner_email=owner_email,
                is_admin=is_admin,
                auth_required=auth_required,
            )

    async def upsert_feedback(
        self,
        *,
        session_id: str,
        message_id: int,
        rating: int,
        feedback_tags: list[str],
        feedback_text: str | None,
        trace_id: str | None,
        user_email: str | None,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> FeedbackResponse:
        async with self._acquire(readonly=False) as conn:
            meta = await self._load_session_meta(conn, session_id)
            self._authorize_meta(
                meta,
                owner_email=owner_email,
                is_admin=is_admin,
                auth_required=auth_required,
            )
            await self._ensure_message_belongs_to_session(conn, session_id, message_id)
            row = await conn.fetchrow(
                f"""
                INSERT INTO {self._table("message_feedback")}
                    (session_id, message_id, rating, feedback_tags, feedback_text, user_email, trace_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (session_id, message_id) DO UPDATE SET
                    rating = EXCLUDED.rating,
                    feedback_tags = EXCLUDED.feedback_tags,
                    feedback_text = EXCLUDED.feedback_text,
                    user_email = COALESCE(
                        EXCLUDED.user_email,
                        {self._table("message_feedback")}.user_email
                    ),
                    trace_id = COALESCE(
                        EXCLUDED.trace_id,
                        {self._table("message_feedback")}.trace_id
                    ),
                    updated_at = NOW()
                RETURNING id, session_id, message_id, rating, feedback_tags,
                          feedback_text, trace_id, created_at, updated_at
                """,
                session_id,
                message_id,
                rating,
                feedback_tags,
                feedback_text,
                user_email,
                trace_id,
            )

        if row is None:
            raise SessionAccessDenied()
        return FeedbackResponse.model_validate(dict(row))

    async def list_feedback(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> list[FeedbackResponse]:
        async with self._acquire(readonly=True) as conn:
            meta = await self._load_session_meta(conn, session_id)
            self._authorize_meta(
                meta,
                owner_email=owner_email,
                is_admin=is_admin,
                auth_required=auth_required,
            )
            rows = await conn.fetch(
                f"""
                SELECT id, session_id, message_id, rating, feedback_tags,
                       feedback_text, trace_id, created_at, updated_at
                FROM {self._table("message_feedback")}
                WHERE session_id = $1
                ORDER BY message_id, id
                """,
                session_id,
            )

        return [FeedbackResponse.model_validate(dict(row)) for row in rows]

    async def delete_feedback(
        self,
        *,
        session_id: str,
        message_id: int,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> bool:
        async with self._acquire(readonly=False) as conn:
            meta = await self._load_session_meta(conn, session_id)
            self._authorize_meta(
                meta,
                owner_email=owner_email,
                is_admin=is_admin,
                auth_required=auth_required,
            )
            result = await conn.execute(
                f"""
                DELETE FROM {self._table("message_feedback")}
                WHERE session_id = $1 AND message_id = $2
                """,
                session_id,
                message_id,
            )

        return result == "DELETE 1"

    async def get_debug_backfill(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> DebugBackfillResponse:
        async with self._acquire(readonly=True) as conn:
            meta = await self._load_session_meta(conn, session_id)
            self._authorize_meta(
                meta,
                owner_email=owner_email,
                is_admin=is_admin,
                auth_required=auth_required,
            )
            conversation = await self._load_conversation(conn, meta)
            verdicts: list[dict[str, Any]] = []
            usage = await self._fetch_debug_records(
                conn,
                f"""
                SELECT phase, model, input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens,
                       requests, cost_usd, trace_id, created_at
                FROM {self._table("llm_usage")}
                WHERE session_id = $1
                ORDER BY created_at, phase
                """,
                session_id,
            )
            linked_scenarios: list[dict[str, Any]] = []

        trace_ids = sorted(
            {
                str(trace_id)
                for trace_id in [
                    *(row.get("trace_id") for row in usage),
                    *(row.get("trace_id") for row in verdicts),
                    *(message.trace_id for message in conversation.messages),
                ]
                if trace_id
            }
        )
        return DebugBackfillResponse(
            session_id=session_id,
            conversation=conversation,
            verdicts=verdicts,
            usage=usage,
            usage_totals=_usage_totals(usage),
            linked_scenarios=linked_scenarios,
            trace_ids=trace_ids,
        )

    async def _load_session_meta(
        self,
        conn: asyncpg.Connection,
        session_id: str,
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            f"""
            SELECT session_id, created_at, owner_email
            FROM {self._table("chat_sessions")}
            WHERE session_id = $1
            """,
            session_id,
        )
        if row is None:
            raise SessionAccessDenied()
        return dict(row)

    async def _load_conversation(
        self,
        conn: asyncpg.Connection,
        meta: dict[str, Any],
    ) -> ConversationResponse:
        session_id = str(meta["session_id"])
        rows = await conn.fetch(
            f"""
            SELECT id, role, content, "timestamp" AS created_at,
                   message_data::text AS message_json, trace_id
            FROM {self._table("chat_messages")}
            WHERE session_id = $1
              AND role IN ('user', 'assistant')
            ORDER BY "timestamp", id
            """,
            session_id,
        )
        messages = [_conversation_message_from_row(row) for row in rows]
        return ConversationResponse(
            session_id=session_id,
            created_at=meta.get("created_at"),
            title=_conversation_title(messages),
            messages=messages,
        )

    async def _ensure_message_belongs_to_session(
        self,
        conn: asyncpg.Connection,
        session_id: str,
        message_id: int,
    ) -> None:
        row = await conn.fetchrow(
            f"""
            SELECT id
            FROM {self._table("chat_messages")}
            WHERE session_id = $1
              AND id = $2
              AND role = 'assistant'
            """,
            session_id,
            message_id,
        )
        if row is None:
            raise SessionAccessDenied()

    async def _fetch_debug_records(
        self,
        conn: asyncpg.Connection,
        sql: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        try:
            rows = await conn.fetch(sql, session_id)
        except asyncpg.PostgresError:
            return []
        return [_jsonable(dict(row)) for row in rows]

    def _authorize_meta(
        self,
        meta: dict[str, Any],
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> None:
        if not auth_required or is_admin:
            return
        existing_owner = meta.get("owner_email")
        if not owner_email or not existing_owner or existing_owner != owner_email:
            raise SessionAccessDenied()

    @asynccontextmanager
    async def _acquire(
        self, *, readonly: bool
    ) -> AsyncIterator[asyncpg.Connection]:
        """Yield a connection: the per-turn connection if free, else pool.

        ``readonly`` is informational; pool-shared connections do not
        toggle a per-call read-only flag (see class docstring).
        """

        del readonly  # informational only — pool connections are shared
        async with acquire_turn_or_pool(self._pool) as conn:
            yield conn


def _conversation_message_from_row(row: asyncpg.Record) -> ConversationMessage:
    raw = dict(row)
    message_json = raw.get("message_json")
    parsed = _parse_json(message_json)
    data = None
    if isinstance(parsed, dict):
        data = parsed.get(_SNAPSHOT_KEY, parsed)
    return ConversationMessage(
        id=raw.get("id"),
        role=raw["role"],
        content=raw.get("content") or "",
        created_at=raw.get("created_at"),
        message_json=message_json,
        trace_id=raw.get("trace_id"),
        data=data if isinstance(data, dict) else None,
    )


def _conversation_title(messages: list[ConversationMessage]) -> str | None:
    for message in messages:
        if message.role == "user" and message.content.strip():
            return message.content.strip()[:120]
    return None


def _parse_json(value: object) -> object:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _usage_totals(usage: list[dict[str, Any]]) -> UsageTotals:
    return UsageTotals(
        input_tokens=sum(_int_value(row.get("input_tokens")) for row in usage),
        output_tokens=sum(_int_value(row.get("output_tokens")) for row in usage),
        cache_read_tokens=sum(_int_value(row.get("cache_read_tokens")) for row in usage),
        cache_write_tokens=sum(_int_value(row.get("cache_write_tokens")) for row in usage),
        requests=sum(_int_value(row.get("requests")) for row in usage),
        cost_usd=round(sum(_float_value(row.get("cost_usd")) for row in usage), 6),
    )


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return 0


def _float_value(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _jsonable(value: object) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, asyncpg.Record):
        return {key: _jsonable(val) for key, val in dict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
