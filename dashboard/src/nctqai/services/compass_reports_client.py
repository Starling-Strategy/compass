"""Thin HTTP client for the Compass backend writes the dashboard needs.

nctqai is read-only against ``compass.*`` (everything else here reads via
``run_sql``). Two flows are the exception, and both route through the backend's
purpose-built debug endpoints rather than writing ``compass.*`` directly:

    POST {compass_api_url}/api/v1/debug/report/{report_id}/status
    body: {"status": <one of the 5 triage values>, "reviewer": <str|null>}
        Changing a report's triage status (``update_report_status``).

    POST {compass_api_url}/api/v1/debug/report
    body: {"session_id", "outcome", "reviewer", "comments", ...}
        Filing a new flag from the dashboard (``create_report``, DASH-R6).

Both endpoints are intentionally UNAUTHENTICATED (they mirror the rest of the
P1 debug review-loop routes — possession of the debug link is the access
control), so no Authorization header is required; they ARE IP-rate-limited,
so a 429 is a real outcome both flows must handle. ``compass_api_url`` comes
from ``Config`` (default ``http://localhost:8000``); staging/prod set
``NCTQAI_COMPASS_API_URL`` to the backend's internal base URL.

This is the only place nctqai calls the Compass backend over HTTP; every other
Compass surface talks to the DB directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

import httpx

from nctqai.config import Config

logger = logging.getLogger(__name__)

_config = Config()


@dataclass(frozen=True)
class StatusUpdateResult:
    """Outcome of a status-update call, for the route to render a banner."""

    ok: bool
    status_code: int
    detail: str = ""


def update_report_status(
    report_id: UUID | str,
    *,
    status: str,
    reviewer: str | None = None,
) -> StatusUpdateResult:
    """Move a report to ``status`` via the backend, returning a render-ready
    result.

    Network and HTTP errors are caught and folded into a non-ok
    ``StatusUpdateResult`` so the queue page degrades to an inline error banner
    rather than a 500 — the read-only queue view is the must-have and should
    never be taken down by a backend hiccup on the secondary write path.
    """
    url = f"{_config.compass_api_url.rstrip('/')}/api/v1/debug/report/{report_id}/status"
    payload: dict[str, object] = {"status": status}
    if reviewer:
        payload["reviewer"] = reviewer

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("report status update failed (network): %s", exc)
        return StatusUpdateResult(
            ok=False,
            status_code=0,
            detail="Could not reach the Compass backend to save the status.",
        )

    if resp.status_code == 200:
        return StatusUpdateResult(ok=True, status_code=200)

    # 404 (no such report), 422 (bad status — shouldn't happen, the select is
    # constrained), or anything else: surface a short reason, don't crash.
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("detail", ""))
    except Exception:  # noqa: BLE001 — best-effort parse of an error body
        detail = ""
    logger.warning(
        "report status update non-200: %s %s", resp.status_code, detail
    )
    return StatusUpdateResult(
        ok=False,
        status_code=resp.status_code,
        detail=detail or f"Backend returned {resp.status_code}.",
    )


def update_report_dimension(
    report_id: UUID | str,
    *,
    dimension: str | None,
    reviewer: str | None = None,
) -> StatusUpdateResult:
    """Set or clear a report dimension through the Compass backend."""
    url = f"{_config.compass_api_url.rstrip('/')}/api/v1/debug/report/{report_id}/dimension"
    payload: dict[str, object] = {"dimension": dimension}
    if reviewer:
        payload["reviewer"] = reviewer

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("report dimension update failed (network): %s", exc)
        return StatusUpdateResult(
            ok=False,
            status_code=0,
            detail="Could not reach the Compass backend to save the dimension.",
        )

    if resp.status_code == 200:
        return StatusUpdateResult(ok=True, status_code=200)

    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("detail", ""))
    except Exception:  # noqa: BLE001 - best-effort parse of an error body
        pass
    logger.warning("report dimension update non-200: %s %s", resp.status_code, detail)
    return StatusUpdateResult(
        ok=False,
        status_code=resp.status_code,
        detail=detail or f"Backend returned {resp.status_code}.",
    )


@dataclass(frozen=True)
class CreateReportResult:
    """Outcome of a flag-create call, for the route to render a banner."""

    ok: bool
    status_code: int
    detail: str = ""


# Human-readable fallbacks per status code, so the banner reads sensibly even
# when the backend doesn't send a ``detail`` body (e.g. a bare 429 from the
# rate limiter, or a network error before any response).
_CREATE_REASONS: dict[int, str] = {
    404: "That conversation isn't in Compass, so it can't be flagged.",
    # The backend returns 422 both for a bad outcome AND for a malformed
    # session_id (UUID validation), so the fallback can't claim "outcome" alone.
    # The backend's own detail is preferred over this fallback (see below).
    422: "The flag was rejected (invalid session or outcome).",
    429: "Too many flags submitted just now — please wait a moment and retry.",
}


def create_report(
    *,
    session_id: UUID | str,
    outcome: str = "fail",
    reviewer: str | None = None,
    comments: str | None = None,
    case_id: int | None = None,
    turn_index: int | None = None,
    trace_id: str | None = None,
    dimension: str | None = None,
) -> CreateReportResult:
    """File a new flag against ``session_id`` via the backend, returning a
    render-ready result.

    Mirrors ``update_report_status``: network and HTTP errors are caught and
    folded into a non-ok result so the conversation detail surface degrades to
    an inline error banner rather than a 500. The backend ``submit_report``
    endpoint REQUIRES ``outcome ∈ {pass, partial, fail}`` (it has no dedicated
    "flag" verb), so a dashboard flag reuses that field; the default ``"fail"``
    puts the new row in the Flagged Issues open queue
    (``outcome IN ('fail','partial') AND status NOT IN ('resolved','wontfix')``)
    so the round-trip is visible. ``case_id`` is left None for a
    whole-conversation flag (the backend skips ``case_exists`` when it's None;
    ``session_exists`` still gates the insert → 404 for an unknown session).
    """
    url = f"{_config.compass_api_url.rstrip('/')}/api/v1/debug/report"
    payload: dict[str, object] = {
        "session_id": str(session_id),
        "outcome": outcome,
    }
    if reviewer:
        payload["reviewer"] = reviewer
    if comments:
        payload["comments"] = comments
    if case_id is not None:
        payload["case_id"] = case_id
    if turn_index is not None:
        payload["turn_index"] = turn_index
    if trace_id:
        payload["trace_id"] = trace_id
    if dimension:
        payload["dimension"] = dimension

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        logger.warning("flag create failed (network): %s", exc)
        return CreateReportResult(
            ok=False,
            status_code=0,
            detail="Could not reach the Compass backend to save the flag.",
        )

    if resp.status_code == 200:
        return CreateReportResult(ok=True, status_code=200)

    # 404 (no such session), 422 (bad outcome), 429 (rate-limited), or anything
    # else: surface a short reason, don't crash. Prefer the backend's own detail,
    # then a per-code fallback, then a generic message.
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("detail", ""))
    except Exception:  # noqa: BLE001 — best-effort parse of an error body
        detail = ""
    logger.warning("flag create non-200: %s %s", resp.status_code, detail)
    return CreateReportResult(
        ok=False,
        status_code=resp.status_code,
        detail=detail
        or _CREATE_REASONS.get(resp.status_code, f"Backend returned {resp.status_code}."),
    )
