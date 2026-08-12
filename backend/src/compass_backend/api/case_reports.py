"""Debug-view review loop routes (``/api/v1/debug/report``).

These endpoints let a reviewer save and re-read a pass/partial/fail report on a
Compass turn straight from the debug bar.

Auth posture: these routes are intentionally UNAUTHENTICATED. The debug link
(``?debug=true&case_id=...``) is itself the access control — possession of a
session's debug URL is the key, mirroring how the debug bar already exposes the
turn. We deliberately do NOT attach the admin/bearer gate the ``/conversations/
{session_id}/debug`` backfill route uses, so internal reviewers can file a
report without minting an API key. Abuse protection (rate limiting) is a P1
TODO — not built here to keep the change surgical.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from compass_backend.api.deps import client_ip
from compass_backend.api.rate_limit import (
    FixedWindowRateLimiter,
    rate_limit_identity,
)
from compass_backend.config import Settings, settings
from compass_backend.db._pool import ChatPoolHolder


class CaseReportRepositoryProtocol(Protocol):
    """Repository surface the report routes depend on."""

    async def session_exists(self, session_id: UUID) -> bool:
        ...

    async def case_exists(self, case_id: int) -> bool:
        ...

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
        ...

    async def get_latest_report(
        self,
        session_id: UUID,
        case_id: int | None,
    ) -> dict[str, Any] | None:
        ...

    async def update_status(
        self,
        report_id: UUID,
        *,
        status: str,
        reviewer: str | None = None,
    ) -> bool:
        ...


class CaseReportRequest(BaseModel):
    """Reviewer report payload posted from the debug bar.

    The endpoint is unauthenticated (the debug link is the access control), so
    every free-text field is length-bounded here: an unbounded ``comments`` or
    ``reviewer`` is a storage-abuse / DB-bloat vector, and a 422 from Pydantic
    is cheaper than persisting megabytes of attacker text. ``dimension`` is
    bounded but deliberately NOT enum-constrained — it is a display label the
    frontend derives from the case feature, and a new label should still be
    captured, not silently dropped.
    """

    session_id: UUID
    # Bounded to the INTEGER column range so an over-range id returns 422 here
    # rather than a Postgres numeric-overflow 500 in case_exists / the insert.
    case_id: int | None = Field(default=None, ge=1, le=2_147_483_647)
    turn_index: int | None = Field(default=None, ge=0, le=100_000)
    trace_id: str | None = Field(default=None, max_length=128)
    dimension: str | None = Field(default=None, max_length=64)
    outcome: Literal["pass", "partial", "fail"]
    comments: str | None = Field(default=None, max_length=4000)
    reviewer: str | None = Field(default=None, max_length=254)


class CaseReportCreated(BaseModel):
    """Response after a successful report insert."""

    id: UUID
    status: str


class CaseReportStatusUpdate(BaseModel):
    """Triage status change posted from the reports queue.

    ``status`` is constrained to the values the ``case_reports.status``
    CHECK allows (migrations 099 + 168), so an out-of-range value is rejected
    with 422 before it can reach the database. ``reviewed_no_followup``
    ("Reviewed – no follow-up", #1806) is a terminal/closed state.
    """

    status: Literal[
        "open", "triaged", "promoted", "resolved", "wontfix", "reviewed_no_followup"
    ]
    reviewer: str | None = Field(default=None, max_length=254)


def create_case_reports_router(
    repo: CaseReportRepositoryProtocol | None = None,
    *,
    app_settings: Settings = settings,
    chat_pool: asyncpg.Pool | ChatPoolHolder | None = None,
) -> APIRouter:
    """Create the unauthenticated debug-view review-loop routes."""

    if repo is None:
        # Imported lazily so the router module stays importable even if the
        # concrete repo's deps shift; mirrors the other create_*_router files.
        from compass_backend.db.case_reports import CaseReportRepository

        if chat_pool is None:
            chat_pool = ChatPoolHolder()
        repo = CaseReportRepository(app_settings, pool=chat_pool)

    router = APIRouter(prefix="/api/v1/debug", tags=["Debug review loop"])

    # Per-router limiter (one app → one limiter in prod; a fresh app per
    # route-level test, so counters never leak across tests). These routes are
    # unauthenticated, so the only identity is the client IP — limiting is only
    # as sharp as ``trusted_proxies`` makes the IP, exactly like the chat
    # limiter when auth is off. See issue #1349 (P1 abuse-protection follow-up).
    report_rate_limiter = (
        FixedWindowRateLimiter(
            max_requests=app_settings.debug_report_rate_limit_per_minute
        )
        if app_settings.debug_report_rate_limit_enabled
        else None
    )

    def _enforce_report_rate_limit(request: Request) -> None:
        """Raise 429 when this client IP has exceeded its per-minute window."""

        if report_rate_limiter is None:
            return
        decision = report_rate_limiter.check(
            rate_limit_identity(
                auth_user=None,
                client_ip_value=client_ip(request),
            )
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many report submissions. Please slow down and retry shortly.",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

    @router.post("/report", response_model=CaseReportCreated)
    async def submit_report(
        request: Request, body: CaseReportRequest
    ) -> CaseReportCreated:
        _enforce_report_rate_limit(request)
        if not await repo.session_exists(body.session_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found",
            )
        if body.case_id is not None and not await repo.case_exists(body.case_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Case not found",
            )
        report_id = await repo.insert_report(
            case_id=body.case_id,
            session_id=body.session_id,
            turn_index=body.turn_index,
            trace_id=body.trace_id,
            dimension=body.dimension,
            outcome=body.outcome,
            comments=body.comments,
            reviewer=body.reviewer,
        )
        return CaseReportCreated(id=report_id, status="open")

    @router.get("/report")
    async def latest_report(
        request: Request,
        session_id: UUID,
        case_id: int | None = None,
    ) -> dict[str, Any] | None:
        _enforce_report_rate_limit(request)
        return await repo.get_latest_report(session_id, case_id)

    @router.post("/report/{report_id}/status")
    async def update_report_status(
        request: Request,
        report_id: UUID,
        body: CaseReportStatusUpdate,
    ) -> dict[str, Any]:
        _enforce_report_rate_limit(request)
        # Same auth posture as the P1 routes above: intentionally
        # UNAUTHENTICATED. Possession of the debug link is the access control,
        # so this status mutation is rate-limited like the others; the only
        # residual is the accepted open posture itself (dashboard triagers
        # are deliberately not the session owner, so no ownership gate fits).
        updated = await repo.update_status(
            report_id,
            status=body.status,
            reviewer=body.reviewer,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Report not found",
            )
        return {"id": report_id, "status": body.status}

    return router


__all__ = [
    "create_case_reports_router",
    "CaseReportRequest",
    "CaseReportCreated",
    "CaseReportStatusUpdate",
]
