"""Compatibility routes kept for the active PHP frontend."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response, status

from compass_backend.api.auth import (
    ApiKeyAuthRepository,
    ApiKeyAuthenticator,
    AuthenticatedUser,
    authenticate_request,
)
from compass_backend.config import Settings, settings
from compass_backend.contracts.frontend import (
    ConversationExportRequest,
    ConversationExportTable,
    ConversationExportTurn,
    ConversationResponse,
    DebugBackfillResponse,
    FeedbackDeleteRequest,
    FeedbackDeleteResponse,
    FeedbackListResponse,
    FeedbackRequest,
    FeedbackResponse,
)
from compass_backend.db import PostgresFrontendContractRepository
from compass_backend.db._pool import ChatPoolHolder
from compass_backend.session.store import SessionAccessDenied

logger = logging.getLogger(__name__)


class FrontendContractRepository(Protocol):
    """Repository methods needed by the retained frontend proxy routes."""

    async def get_conversation(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> ConversationResponse:
        """Return one conversation or raise when it is missing/denied."""

    async def ensure_session_access(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> None:
        """Raise when a caller cannot access a session."""

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
        """Insert or update one feedback row."""

    async def list_feedback(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> list[FeedbackResponse]:
        """Return feedback rows for a session."""

    async def delete_feedback(
        self,
        *,
        session_id: str,
        message_id: int,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> bool:
        """Delete one feedback row."""

    async def get_debug_backfill(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> DebugBackfillResponse:
        """Return assembled admin-only debug data."""


def create_frontend_contract_router(
    *,
    frontend_contract_repository: FrontendContractRepository | None = None,
    api_key_auth_repository: ApiKeyAuthenticator | None = None,
    app_settings: Settings = settings,
    chat_pool: asyncpg.Pool | ChatPoolHolder | None = None,
) -> APIRouter:
    """Create active frontend compatibility routes.

    This intentionally does not recreate retired document text or lazy message
    export endpoints. The PHP proxies now fail those paths locally with 410.
    """

    if chat_pool is None:
        chat_pool = ChatPoolHolder()
    router = APIRouter(prefix="/api/v1", tags=["Frontend contracts"])
    repository = frontend_contract_repository or PostgresFrontendContractRepository(
        app_settings, pool=chat_pool
    )
    authenticator = api_key_auth_repository or ApiKeyAuthRepository(
        app_settings, pool=chat_pool
    )

    @router.get(
        "/conversations/{session_id}",
        response_model=ConversationResponse,
    )
    async def get_conversation(
        raw_request: Request,
        session_id: str,
    ) -> ConversationResponse:
        auth = await _auth_context(
            raw_request,
            app_settings=app_settings,
            authenticator=authenticator,
        )
        try:
            return await repository.get_conversation(session_id, **auth.access_kwargs)
        except SessionAccessDenied:
            raise _not_found("Conversation not found") from None

    @router.post("/feedback", response_model=FeedbackResponse)
    async def submit_feedback(
        raw_request: Request,
        body: FeedbackRequest,
    ) -> FeedbackResponse:
        auth = await _auth_context(
            raw_request,
            app_settings=app_settings,
            authenticator=authenticator,
        )
        try:
            return await repository.upsert_feedback(
                session_id=body.session_id,
                message_id=body.message_id,
                rating=body.rating,
                feedback_tags=body.feedback_tags,
                feedback_text=body.feedback_text,
                trace_id=body.trace_id,
                user_email=auth.user_email,
                **auth.access_kwargs,
            )
        except SessionAccessDenied:
            raise _not_found("Feedback target not found") from None

    @router.get("/feedback", response_model=FeedbackListResponse)
    async def list_feedback(
        raw_request: Request,
        session_id: str,
    ) -> FeedbackListResponse:
        return await _list_feedback_response(
            raw_request,
            session_id,
            repository=repository,
            authenticator=authenticator,
            app_settings=app_settings,
        )

    @router.get("/feedback/{session_id}", response_model=FeedbackListResponse)
    async def list_feedback_legacy_path(
        raw_request: Request,
        session_id: str,
    ) -> FeedbackListResponse:
        return await _list_feedback_response(
            raw_request,
            session_id,
            repository=repository,
            authenticator=authenticator,
            app_settings=app_settings,
        )

    @router.delete("/feedback", response_model=FeedbackDeleteResponse)
    async def delete_feedback(
        raw_request: Request,
        body: FeedbackDeleteRequest,
    ) -> FeedbackDeleteResponse:
        return await _delete_feedback_response(
            raw_request,
            body.session_id,
            body.message_id,
            repository=repository,
            authenticator=authenticator,
            app_settings=app_settings,
        )

    @router.delete(
        "/feedback/{session_id}/{message_id}",
        response_model=FeedbackDeleteResponse,
    )
    async def delete_feedback_legacy_path(
        raw_request: Request,
        session_id: str,
        message_id: int,
    ) -> FeedbackDeleteResponse:
        return await _delete_feedback_response(
            raw_request,
            session_id,
            message_id,
            repository=repository,
            authenticator=authenticator,
            app_settings=app_settings,
        )

    @router.post("/conversations/export")
    async def export_conversation(
        raw_request: Request,
        body: ConversationExportRequest,
    ) -> Response:
        auth = await _auth_context(
            raw_request,
            app_settings=app_settings,
            authenticator=authenticator,
        )
        try:
            await repository.ensure_session_access(body.session_id, **auth.access_kwargs)
        except SessionAccessDenied:
            raise _not_found("Conversation not found") from None

        archive_bytes = build_conversation_export_zip(body)
        filename = _export_filename(body)
        return Response(
            content=archive_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": _content_disposition(filename)},
        )

    @router.get(
        "/conversations/{session_id}/debug",
        response_model=DebugBackfillResponse,
    )
    async def get_debug_backfill(
        raw_request: Request,
        session_id: str,
    ) -> DebugBackfillResponse:
        auth = await _auth_context(
            raw_request,
            app_settings=app_settings,
            authenticator=authenticator,
        )
        # Audit #18: admin enforced unconditionally except for explicit
        # dev escape hatch (environment=='development' + auth off).
        if not auth.is_admin:
            if (
                app_settings.environment == "development"
                and not app_settings.api_key_auth_enabled
            ):
                logger.warning(
                    "debug backfill admin gate bypassed: dev env with auth off"
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin access required",
                )
        try:
            return await repository.get_debug_backfill(session_id, **auth.access_kwargs)
        except SessionAccessDenied:
            raise _not_found("Conversation not found") from None

    return router


async def _list_feedback_response(
    raw_request: Request,
    session_id: str,
    *,
    repository: FrontendContractRepository,
    authenticator: ApiKeyAuthenticator,
    app_settings: Settings,
) -> FeedbackListResponse:
    auth = await _auth_context(
        raw_request,
        app_settings=app_settings,
        authenticator=authenticator,
    )
    try:
        feedback = await repository.list_feedback(session_id, **auth.access_kwargs)
    except SessionAccessDenied:
        raise _not_found("Conversation not found") from None
    return FeedbackListResponse(session_id=session_id, feedback=feedback)


async def _delete_feedback_response(
    raw_request: Request,
    session_id: str,
    message_id: int,
    *,
    repository: FrontendContractRepository,
    authenticator: ApiKeyAuthenticator,
    app_settings: Settings,
) -> FeedbackDeleteResponse:
    auth = await _auth_context(
        raw_request,
        app_settings=app_settings,
        authenticator=authenticator,
    )
    try:
        deleted = await repository.delete_feedback(
            session_id=session_id,
            message_id=message_id,
            **auth.access_kwargs,
        )
    except SessionAccessDenied:
        raise _not_found("Conversation not found") from None
    return FeedbackDeleteResponse(deleted=deleted)


class _AuthContext:
    def __init__(self, user: AuthenticatedUser | None, auth_required: bool) -> None:
        self._user = user
        self._auth_required = auth_required

    @property
    def is_admin(self) -> bool:
        return bool(self._user and self._user.is_admin)

    @property
    def user_email(self) -> str | None:
        if self._user is None:
            return None
        return self._user.email or self._user.owner_email

    @property
    def access_kwargs(self) -> dict[str, object]:
        return {
            "owner_email": self._user.owner_email if self._user else None,
            "is_admin": self.is_admin,
            "auth_required": self._auth_required,
        }


async def _auth_context(
    raw_request: Request,
    *,
    app_settings: Settings,
    authenticator: ApiKeyAuthenticator,
) -> _AuthContext:
    user = await authenticate_request(
        raw_request,
        source=app_settings,
        authenticator=authenticator,
    )
    return _AuthContext(user, app_settings.api_key_auth_enabled)


def build_conversation_export_zip(body: ConversationExportRequest) -> bytes:
    """Build a ZIP bundle from validated client-side artifacts."""

    turns = _turns_from_export_request(body)
    if not _has_exportable_artifacts(turns):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Conversation has no exportable tables or charts",
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", _readme_text(body))
        archive.writestr(
            "conversation.json",
            json.dumps(body.model_dump(mode="json"), indent=2),
        )
        archive.writestr("conversation.md", _conversation_markdown(body, turns))
        _write_tables(archive, turns)
        _write_charts(archive, turns)
    return buffer.getvalue()


def _turns_from_export_request(
    body: ConversationExportRequest,
) -> list[ConversationExportTurn]:
    if body.turns:
        return body.turns

    turns: list[ConversationExportTurn] = []
    messages = body.messages
    for index, message in enumerate(messages):
        if message.role != "user":
            continue
        if index + 1 >= len(messages):
            continue
        assistant = messages[index + 1]
        if assistant.role != "assistant":
            continue
        turns.append(
            ConversationExportTurn(
                user_text=message.content,
                assistant_text=assistant.content,
                tables=assistant.tables,
                charts=assistant.charts,
                citations=assistant.citations,
            )
        )
    return turns


def _has_exportable_artifacts(turns: Sequence[ConversationExportTurn]) -> bool:
    for turn in turns:
        if turn.charts:
            return True
        if any(table.rows for table in turn.tables):
            return True
    return False


def _write_tables(
    archive: zipfile.ZipFile,
    turns: Sequence[ConversationExportTurn],
) -> None:
    used_names: set[str] = set()
    for turn_index, turn in enumerate(turns, start=1):
        for table_index, table in enumerate(turn.tables, start=1):
            if not table.rows:
                continue
            label = table.label or table.title or f"turn-{turn_index}-table-{table_index}"
            filename = _unique_archive_name(
                f"tables/{_slugify(label)}.csv",
                used_names,
            )
            archive.writestr(filename, _table_csv(table))


def _write_charts(
    archive: zipfile.ZipFile,
    turns: Sequence[ConversationExportTurn],
) -> None:
    used_names: set[str] = set()
    for turn_index, turn in enumerate(turns, start=1):
        for chart_index, chart in enumerate(turn.charts, start=1):
            label = chart.label or f"turn-{turn_index}-chart-{chart_index}"
            filename = _unique_archive_name(
                f"charts/{_slugify(label)}.json",
                used_names,
            )
            archive.writestr(filename, json.dumps(chart.data, indent=2, default=str))


def _table_csv(table: ConversationExportTable) -> str:
    stream = io.StringIO()
    writer = csv.writer(stream)
    # Prefer the pruned public projection (#1611) when the payload carries it;
    # fall back to today's columns/rows for older payloads and charts.
    if table.public_columns and table.public_rows:
        columns = table.public_columns
        rows: Sequence[dict[str, Any] | list[Any]] = table.public_rows
    else:
        columns = table.columns or _columns_from_rows(table.rows)
        rows = table.rows
    if columns:
        writer.writerow([_neutralize_formula(column) for column in columns])
    for row in rows:
        if isinstance(row, dict):
            values = [row.get(column, "") for column in columns]
        else:
            values = list(row)
        writer.writerow([_neutralize_formula(value) for value in values])
    return stream.getvalue()


# CSV formula-injection neutralization (#1611, links #1496). A spreadsheet
# treats a cell beginning with =, +, -, @, TAB, or CR as a formula; prefixing
# a single quote forces it to be read as text. We always neutralize =, @, TAB
# (0x09), and CR (0x0D). For leading - or +, we neutralize ONLY when the cell
# is not a well-formed bare number, so legitimate negatives (-5, -3.2) stay
# clean while payloads like "-2+3" or "+1+1" are caught. The number predicate
# is a strict regex (no inf/nan, no surrounding whitespace, no trailing % or
# units), erring toward neutralizing when ambiguous (security default).
# `_BARE_NUMBER` is the canonical shared shape — it MUST stay byte-identical to
# the JS `BARE_NUMBER` in csvExport.js so the ZIP (Python) and single-table (JS)
# CSV sinks neutralize every value the same way.
_NEUTRALIZE_PREFIXES = ("=", "@", "\t", "\r")
_BARE_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _neutralize_formula(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return text
    first = text[0]
    if first in _NEUTRALIZE_PREFIXES:
        return "'" + text
    if first in ("-", "+") and not _BARE_NUMBER.fullmatch(text):
        return "'" + text
    return text


def _columns_from_rows(rows: Sequence[dict[str, Any] | list[Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            if key not in columns:
                columns.append(str(key))
    return columns


def _conversation_markdown(
    body: ConversationExportRequest,
    turns: Sequence[ConversationExportTurn],
) -> str:
    lines = [
        f"# {body.conversation_title or 'Compass conversation'}",
        "",
        f"Session: `{body.session_id}`",
        f"Exported: {(body.exported_at or datetime.now(UTC)).isoformat()}",
        "",
    ]
    for index, turn in enumerate(turns, start=1):
        lines.extend(
            [
                f"## Turn {index}",
                "",
                "### User",
                "",
                turn.user_text.strip(),
                "",
                "### Compass",
                "",
                turn.assistant_text.strip(),
                "",
            ]
        )
        if turn.citations:
            lines.append("### Citations")
            lines.append("")
            for citation in turn.citations:
                title = citation.get("title") or citation.get("source_title") or "Source"
                marker = citation.get("marker") or citation.get("citation_number") or "-"
                lines.append(f"- [{marker}] {title}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _readme_text(body: ConversationExportRequest) -> str:
    return (
        "Compass conversation export\n\n"
        f"Session: {body.session_id}\n"
        "Files:\n"
        "- conversation.md: readable transcript\n"
        "- conversation.json: validated export payload\n"
        "- tables/*.csv: structured table artifacts with rows\n"
        "- charts/*.json: chart artifacts\n"
    )


def _unique_archive_name(filename: str, used_names: set[str]) -> str:
    if filename not in used_names:
        used_names.add(filename)
        return filename
    stem, suffix = filename.rsplit(".", 1)
    counter = 2
    while f"{stem}-{counter}.{suffix}" in used_names:
        counter += 1
    unique = f"{stem}-{counter}.{suffix}"
    used_names.add(unique)
    return unique


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:80] or "artifact"


def _sanitize_export_title(title: str) -> str:
    """Strip OS-illegal characters and control chars, collapse whitespace,
    trim to ~120 chars. Keeps spaces, commas, parens, and apostrophes (all
    legal on macOS/Windows/Linux) so the human title stays readable.
    """
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:120].strip()


def _export_filename(body: ConversationExportRequest) -> str:
    date_part = (body.exported_at or datetime.now(UTC)).strftime("%Y-%m-%d")
    title = _sanitize_export_title(body.conversation_title or "")
    if title:
        return f"NCTQ Compass - {title} - {date_part}.zip"
    # No human conversation title — fall back to the existing session-slug shape.
    session_part = _slugify(body.session_id)[:16]
    return f"compass-{date_part}-{session_part}.zip"


def _content_disposition(filename: str) -> str:
    """Build an RFC 6266 Content-Disposition value that never crashes the ASGI
    Latin-1 header encoder. The human title can carry Unicode (smart quotes,
    em-dashes from LLM-generated titles), so emit an ASCII-only fallback for the
    `filename` parameter plus a UTF-8 `filename*` so modern browsers still get
    the readable name.
    """
    ascii_fallback = filename.encode("ascii", "ignore").decode() or "compass-export.zip"
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
