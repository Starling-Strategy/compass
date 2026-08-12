"""Flagged Issues queue for the Compass Quality dashboard.

NOTE: this module is named ``reports.py`` and serves the ``/compass/quality/
reports`` URL, but the page is titled **"Flagged Issues"** (DASH-R6 relabel).
The label changed; the route and file names did not — renaming would break the
nav tab, ``_TAB_ROLES`` gate, ``_REPORTS_URL``, ``sub_nav=`` args, and tests
for no MVP gain. The mismatch is intentional and discoverable here.

GET /compass/quality/reports
    The reviewer triage list over ``compass.case_reports``. Default view is the
    *open queue*: ``outcome IN ('fail','partial')`` AND
    ``status NOT IN ('resolved','wontfix')``, newest first. Filterable by
    status / dimension / outcome via query params. Each row carries a debug
    link (the reviewer's "ticket") and a per-row status control.

POST /compass/quality/reports/{report_id}/status
    Per-row status change. nctqai is read-only on ``compass.*``, so this
    handler does NOT touch the DB directly — it calls the backend's
    unauthenticated status endpoint
    (``POST /api/v1/debug/report/{report_id}/status``) server-side, then
    re-renders the queue table. See ``services/compass_reports_client.py``.

Both routes require auth (inherited from the Compass section middleware) and
mirror the scorecard route's structure (raw FastHTML Table + Layout shell).
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlencode
from uuid import UUID

from fasthtml.common import (
    A,
    Button,
    Br,
    Details,
    Div,
    Form,
    Input,
    Option,
    P,
    Select,
    Span,
    Summary,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
)
from starlette.requests import Request

from nctqai.layout import Layout
from nctqai.components.compass.flag_dimensions import flag_dimension_options
from nctqai.components.compass.timestamps import format_eastern_timestamp
from nctqai.routes.compass._helpers import (
    require_compass,
    require_compass_partial,
    run_in_thread,
)
from nctqai.services.compass_quality.reports import (
    OUTCOMES,
    STATUSES,
    CaseReport,
    distinct_dimensions,
    list_reports,
    status_counts,
)
from nctqai.services.compass_reports_client import (
    update_report_dimension,
    update_report_status,
)

logger = logging.getLogger(__name__)

_REPORTS_URL = "/compass/quality/reports"

# In-dashboard deep-link base for the per-row "conversation" backlink — the
# existing /compass/conversations/{session_id} detail route, so a reviewer jumps
# from a flag straight to the conversation it's about.
_CONVERSATION_BASE = "/compass/conversations/"

# Staging debug link the row "ticket" points at. Kept to the durable shape from
# the repo guardrails (guardrail 10): ?debug=true&case_id=<case_id>. The
# ?session= param is a fossil that replays a stored turn, not a fresh run —
# durable reviewer/ticket links must use case_id only.
_STAGING_BASE = "https://staging-compass.nctq.ai/"

# Color class per outcome/status, reusing the scorecard verdict palette so the
# queue reads the same as the rest of the Quality cluster.
_OUTCOME_CLS = {
    "pass": "quality-verdict-pass",
    "partial": "quality-verdict-error",
    "fail": "quality-verdict-fail",
}
_STATUS_CLS = {
    "open": "quality-verdict-fail",
    "triaged": "quality-verdict-error",
    "promoted": "quality-verdict-error",
    "resolved": "quality-verdict-pass",
    "wontfix": "",
    # Terminal/closed, like wontfix — a benign "looked at it, nothing to do".
    "reviewed_no_followup": "",
}

# Human labels for the status <select>, summary pills, and filter dropdown.
# Statuses absent here render with their raw value (already human-readable);
# only the underscored ``reviewed_no_followup`` needs a friendly label. The
# option/pill VALUE always stays the raw status the DB CHECK + backend accept.
_STATUS_LABELS = {
    "reviewed_no_followup": "Reviewed – no follow-up",
}


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


_COMMENT_TRUNCATE = 80
_HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"]+")


def _debug_link(report: CaseReport) -> str | None:
    """Build the durable staging debug link for one report ('the ticket').

    The durable shape is ``?debug=true&case_id=<case_id>`` (guardrail 10).
    case_id is optional on a report; without it there is no durable ticket to
    point at (a bare ``?debug=true`` is a dead link), so we return None and the
    row omits the "Open ticket" link — the "View conversation" backlink remains
    the fallback. We do NOT fall back to ``?session=`` (a fossil that replays a
    stored turn rather than running the case fresh).
    """
    if report.case_id is None:
        return None
    params = {"debug": "true", "case_id": str(report.case_id)}
    return f"{_STAGING_BASE}?{urlencode(params)}"


def _truncate(text: str | None, length: int = _COMMENT_TRUNCATE) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= length else text[: length - 1] + "…"


def _link_url(raw_url: str) -> str:
    """Drop punctuation outside a URL while retaining balanced URL parentheses."""
    url = raw_url.rstrip(".,;:!?")
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    return url


def _comment_content(text: str) -> tuple:
    """Render text safely, preserving line breaks and linking only http(s) URLs."""
    children: list = []
    lines = text.splitlines() or [text]
    for line_index, line in enumerate(lines):
        cursor = 0
        for match in _HTTP_URL_RE.finditer(line):
            if match.start() > cursor:
                children.append(Span(line[cursor:match.start()]))
            raw_url = match.group(0)
            url = _link_url(raw_url)
            if url:
                children.append(
                    A(
                        url,
                        href=url,
                        target="_blank",
                        rel="noopener noreferrer",
                        cls="quality-reports-comment-link",
                    )
                )
                children.append(Span(raw_url[len(url):]))
            else:
                children.append(Span(raw_url))
            cursor = match.end()
        if cursor < len(line):
            children.append(Span(line[cursor:]))
        if line_index < len(lines) - 1:
            children.append(Br())
    return tuple(children)


def _comment_cell(text: str | None) -> Div | str:
    """Short preview plus a keyboard-accessible disclosure for full comments."""
    if not text or not text.strip():
        return "—"
    cleaned = text.strip()
    preview = _truncate(cleaned)
    if preview == cleaned:
        return Div(*_comment_content(cleaned), cls="quality-reports-comment-full")
    return Details(
        Summary(
            Span(preview, cls="quality-reports-comment-preview"),
            Span("View full comment", cls="quality-reports-comment-toggle"),
        ),
        Div(*_comment_content(cleaned), cls="quality-reports-comment-full"),
        cls="quality-reports-comment-details",
    )


def _filters_from(request: Request) -> dict[str, str]:
    """Pull the queue filter values off the query string."""
    qp = request.query_params
    return {
        "status": qp.get("status", ""),
        "dimension": qp.get("dimension", ""),
        "outcome": qp.get("outcome", ""),
    }


def _summary_strip(counts: dict[str, int]) -> Div:
    """Small counts-by-status summary row."""
    total = sum(counts.values())
    pills = [
        Span(f"Total {total}", cls="quality-summary-pill quality-summary-total")
    ]
    for status in STATUSES:
        pills.append(
            Span(
                f"{_status_label(status)} {counts.get(status, 0)}",
                cls=f"quality-summary-pill {_STATUS_CLS.get(status, '')}",
            )
        )
    return Div(*pills, cls="quality-reports-summary")


def _filter_bar(filters: dict[str, str], dimensions: list[str]) -> Form:
    """GET form with status / dimension / outcome selects.

    Submitting reloads the page with the chosen filters in the query string —
    the same pattern the conversations smart-search uses (plain GET form).
    """
    status_opts = [Option("All statuses", value="", selected=not filters["status"])]
    status_opts += [
        Option(_status_label(s), value=s, selected=filters["status"] == s)
        for s in STATUSES
    ]

    outcome_opts = [Option("All outcomes", value="", selected=not filters["outcome"])]
    outcome_opts += [
        Option(o, value=o, selected=filters["outcome"] == o) for o in OUTCOMES
    ]

    dim_opts = [Option("All dimensions", value="", selected=not filters["dimension"])]
    dim_opts += [
        Option(label, value=value, selected=filters["dimension"] == value)
        for value, label in flag_dimension_options(dimensions)
    ]

    return Form(
        Select(*status_opts, name="status", cls="quality-reports-filter"),
        Select(*outcome_opts, name="outcome", cls="quality-reports-filter"),
        Select(*dim_opts, name="dimension", cls="quality-reports-filter"),
        Button("Filter", type="submit", cls="quality-reports-filter-submit"),
        A("Reset", href=_REPORTS_URL, cls="quality-reports-filter-reset"),
        method="get",
        action=_REPORTS_URL,
        cls="quality-reports-filter-bar",
    )


def _carried_filters(filters: dict[str, str]) -> list:
    return [
        Input(type="hidden", name=f"f_{key}", value=value)
        for key, value in filters.items()
        if value
    ]


def _status_control(report: CaseReport, filters: dict[str, str]) -> Form:
    """Per-row status <select> that POSTs to the nctqai status handler.

    The handler proxies to the backend write endpoint, then re-renders the
    whole table. ``onchange`` auto-submits so the reviewer just picks a value.
    Current filters ride along as hidden inputs so the re-render keeps the same
    view. ``hx-*`` attributes swap the summary and table together when HTMX is
    present; without HTMX the form still works as a full-page POST→render.
    """
    opts = [
        Option(_status_label(s), value=s, selected=report.status == s)
        for s in STATUSES
    ]
    return Form(
        Select(
            *opts,
            name="status",
            cls="quality-reports-status-select",
            onchange="this.form.requestSubmit()",
        ),
        *_carried_filters(filters),
        method="post",
        action=f"{_REPORTS_URL}/{report.id}/status",
        hx_post=f"{_REPORTS_URL}/{report.id}/status",
        hx_target="#quality-reports-results",
        hx_swap="outerHTML",
        # Toggle aria-busy on the swap target around the request so a screen
        # reader hears the table is updating (htmx doesn't set aria-busy and CSS
        # can't toggle an attribute). The [aria-busy] fade hook is U1-owned.
        hx_on__before_request=(
            "document.getElementById('quality-reports-results')"
            "?.setAttribute('aria-busy','true')"
        ),
        hx_on__after_request=(
            "document.getElementById('quality-reports-results')"
            "?.setAttribute('aria-busy','false')"
        ),
        cls="quality-reports-status-form",
    )


def _dimension_control(report: CaseReport, filters: dict[str, str]) -> Form:
    """Per-row dimension selector, including an explicit unassigned state."""
    options = [
        Option("Not assigned", value="", selected=not report.dimension),
        *[
            Option(label, value=value, selected=report.dimension == value)
            for value, label in flag_dimension_options([report.dimension or ""])
        ],
    ]
    return Form(
        Select(
            *options,
            name="dimension",
            cls="quality-reports-status-select",
            onchange="this.form.requestSubmit()",
            aria_label="Issue dimension",
        ),
        *_carried_filters(filters),
        method="post",
        action=f"{_REPORTS_URL}/{report.id}/dimension",
        hx_post=f"{_REPORTS_URL}/{report.id}/dimension",
        hx_target="#quality-reports-results",
        hx_swap="outerHTML",
        hx_on__before_request=(
            "document.getElementById('quality-reports-results')"
            "?.setAttribute('aria-busy','true')"
        ),
        hx_on__after_request=(
            "document.getElementById('quality-reports-results')"
            "?.setAttribute('aria-busy','false')"
        ),
        cls="quality-reports-status-form",
    )


def _report_row(report: CaseReport, filters: dict[str, str]) -> Tr:
    outcome_cls = _OUTCOME_CLS.get(report.outcome, "")
    # "Flagged" = created_at (when the flag was filed — genuinely the flag time).
    flagged = format_eastern_timestamp(report.created_at)
    # "Updated" = updated_at, a single rolling timestamp. Migration 099 stores
    # only created_at + one updated_at, so we render ONE honest "last changed"
    # date for the current status — never three fabricated per-transition dates
    # (Seen/Fixed) the schema can't back (HM-R1).
    updated = format_eastern_timestamp(report.updated_at)
    case_label = str(report.case_id) if report.case_id is not None else "—"
    debug_link = _debug_link(report)

    return Tr(
        Td(flagged, cls="quality-reports-created"),
        Td(updated, cls="quality-reports-created"),
        Td(_dimension_control(report, filters), cls="quality-reports-dim"),
        Td(
            Span(report.outcome, cls=f"quality-reports-outcome {outcome_cls}"),
            cls="quality-reports-outcome-cell",
        ),
        Td(_status_control(report, filters), cls="quality-reports-status"),
        Td(case_label, cls="quality-reports-case"),
        Td(
            _comment_cell(report.comments),
            cls="quality-reports-comments",
        ),
        Td(
            A(
                "View conversation",
                href=f"{_CONVERSATION_BASE}{report.session_id}",
                cls="quality-reports-convo-link",
            ),
            cls="quality-reports-link",
        ),
        Td(
            A(
                "Open ticket",
                href=debug_link,
                target="_blank",
                rel="noopener",
                cls="quality-reports-debug-link",
            )
            if debug_link
            else "—",
            cls="quality-reports-link",
        ),
        cls="quality-reports-row",
    )


def _reports_table(reports: list[CaseReport], filters: dict[str, str]) -> Table | Div:
    # aria-live so a screen reader is told when the table content changes on a
    # status-change swap; aria-busy is toggled by the per-row status form's
    # bubbling hx-on handlers. The [aria-busy] fade hook is U1-owned in theme.py.
    if not reports:
        return Div(
            P(
                "No flagged issues match these filters.",
                cls="quality-no-data-pill",
            ),
            id="quality-reports-table",
            aria_live="polite",
            aria_busy="false",
        )
    rows = [_report_row(r, filters) for r in reports]
    return Table(
        Thead(Tr(
            Th("Flagged", cls="quality-scorecard-th"),
            Th("Updated", cls="quality-scorecard-th"),
            Th("Dimension", cls="quality-scorecard-th"),
            Th("Outcome", cls="quality-scorecard-th"),
            Th("Status", cls="quality-scorecard-th"),
            Th("Case", cls="quality-scorecard-th"),
            Th("Comments", cls="quality-scorecard-th"),
            Th("Conversation", cls="quality-scorecard-th"),
            Th("Ticket", cls="quality-scorecard-th"),
        )),
        Tbody(*rows),
        id="quality-reports-table",
        cls="quality-scorecard-table quality-reports-table",
        aria_live="polite",
        aria_busy="false",
    )


def _queue_results(
    reports: list[CaseReport],
    counts: dict[str, int],
    filters: dict[str, str],
    *,
    banner: Div | None = None,
) -> Div:
    """The HTMX swap region: summary counts and rows stay in sync."""
    return Div(
        banner or "",
        _summary_strip(counts),
        _reports_table(reports, filters),
        id="quality-reports-results",
        aria_live="polite",
        aria_busy="false",
    )


async def _load_queue(filters: dict[str, str]) -> tuple[list[CaseReport], dict[str, int], list[str]]:
    """Fetch the three read pieces the page needs, off the event loop."""
    reports, counts, dimensions = await asyncio.gather(
        run_in_thread(
            list_reports,
            status=filters["status"],
            dimension=filters["dimension"],
            outcome=filters["outcome"],
        ),
        # Count over the same population the list shows (outcome/dimension
        # scope) so the summary pills reconcile with the rows.
        run_in_thread(
            status_counts,
            dimension=filters["dimension"],
            outcome=filters["outcome"],
        ),
        run_in_thread(distinct_dimensions),
    )
    return reports, counts, dimensions


async def _reports_page(
    request: Request,
    *,
    banner: Div | None = None,
    filters: dict[str, str] | None = None,
):
    """Render the full reports-queue page.

    ``filters`` lets a caller supply the active view filters directly. The GET
    route reads them from the query string (the default), but a POST handler
    (e.g. the status change) carries them in the form body — its query string is
    empty, so it must pass the form-derived filters or the queue would reset to
    the default open view on a full-page (non-HTMX) re-render.
    """
    # #1806: Flagged Issues is a monitoring surface open to ALL Compass roles
    # (the section gate), not the admin-only eval-builder gate.
    user, deny = require_compass(request)
    if deny:
        return deny

    if filters is None:
        filters = _filters_from(request)

    try:
        reports, counts, dimensions = await _load_queue(filters)
    except Exception as exc:
        logger.exception("Reports queue: failed to load case_reports")
        content = Div(
            P("Could not load the reports queue. Please try again.", cls="text-red-600"),
            P(str(exc), cls="text-sm text-gray-500"),
        )
        return Layout(
            "Flagged Issues",
            "",
            content,
            section="compass",
            sub_nav=_REPORTS_URL,
            user=user,
            data_cluster="quality",
        )

    content = Div(
        _filter_bar(filters, dimensions),
        _queue_results(reports, counts, filters, banner=banner),
        cls="quality-reports-wrap",
    )

    return Layout(
        "Flagged Issues",
        "Conversations flagged for weekly review — open the conversation, or the debug ticket.",
        content,
        section="compass",
        sub_nav=_REPORTS_URL,
        user=user,
        show_heading=False,
        data_cluster="quality",
    )


def _form_filters(form) -> dict[str, str]:
    return {
        "status": str(form.get("f_status", "")),
        "dimension": str(form.get("f_dimension", "")),
        "outcome": str(form.get("f_outcome", "")),
    }


async def _updated_queue_results(
    filters: dict[str, str], *, ok: bool, success_message: str, error_message: str
) -> Div:
    """Reload the count strip and table after one dashboard write."""
    reports, counts = await asyncio.gather(
        run_in_thread(
            list_reports,
            status=filters["status"],
            dimension=filters["dimension"],
            outcome=filters["outcome"],
        ),
        run_in_thread(
            status_counts,
            dimension=filters["dimension"],
            outcome=filters["outcome"],
        ),
    )
    banner = Div(
        success_message if ok else error_message,
        cls=(
            "quality-reports-banner quality-reports-banner-ok"
            if ok
            else "quality-reports-banner quality-reports-banner-error"
        ),
    )
    return _queue_results(reports, counts, filters, banner=banner)


async def _invalid_report_id_response(request: Request, filters: dict[str, str]):
    """Keep an invalid report-id error inside the HTMX swap region."""
    if request.headers.get("HX-Request") == "true":
        return await _updated_queue_results(
            filters,
            ok=False,
            success_message="",
            error_message="Invalid report id.",
        )
    return await _reports_page(
        request,
        banner=Div(
            "Invalid report id.",
            cls="quality-reports-banner quality-reports-banner-error",
        ),
        filters=filters,
    )


def register(rt):
    """Register reports-queue routes with the FastHTML router."""

    @rt("/compass/quality/reports")
    async def reports_queue(request: Request):
        return await _reports_page(request)

    @rt("/compass/quality/reports/{report_id}/status")
    async def reports_status_update(request: Request, report_id: str):
        """Per-row status change: proxy to the backend write, then re-render.

        nctqai never writes ``compass.*`` directly — the actual UPDATE happens
        in the backend via ``update_report_status``. On an HTMX request we swap
        the summary and table together; otherwise we re-render the whole page so
        a no-JS reviewer still sees the result.
        """
        # HTMX-partial deny (bare Div, not a full page): this response swaps into
        # #quality-reports-results, so a denied non-Compass POST should drop a
        # small "Forbidden" into the panel, not a whole Layout that mangles it.
        # #1806: all Compass roles can change a flag's status, not just admins.
        user, deny = require_compass_partial(request)
        if deny:
            return deny

        form = await request.form()
        # The status <select> posts name="status" (the value to WRITE). The
        # carried view filters post under f_* names so they don't collide.
        new_status = str(form.get("status", ""))
        filters = _form_filters(form)

        reviewer = getattr(user, "email", None) or getattr(user, "name", None)

        try:
            UUID(report_id)
        except ValueError:
            return await _invalid_report_id_response(request, filters)

        result = await run_in_thread(
            update_report_status,
            report_id,
            status=new_status,
            reviewer=reviewer,
        )

        is_htmx = request.headers.get("HX-Request") == "true"
        if is_htmx:
            return await _updated_queue_results(
                filters,
                ok=result.ok,
                success_message=f"Status updated to {_status_label(new_status)}.",
                error_message=result.detail or "Could not update status.",
            )

        if result.ok:
            banner = Div(
                f"Status updated to {new_status}.",
                cls="quality-reports-banner quality-reports-banner-ok",
            )
        else:
            banner = Div(
                result.detail or "Could not update status.",
                cls="quality-reports-banner quality-reports-banner-error",
            )
        return await _reports_page(request, banner=banner, filters=filters)

    @rt("/compass/quality/reports/{report_id}/dimension")
    async def reports_dimension_update(request: Request, report_id: str):
        """Assign or clear a queue dimension through the backend write API."""
        user, deny = require_compass_partial(request)
        if deny:
            return deny

        form = await request.form()
        filters = _form_filters(form)
        dimension = str(form.get("dimension", "")).strip() or None
        reviewer = getattr(user, "email", None) or getattr(user, "name", None)

        try:
            UUID(report_id)
        except ValueError:
            return await _invalid_report_id_response(request, filters)

        result = await run_in_thread(
            update_report_dimension,
            report_id,
            dimension=dimension,
            reviewer=reviewer,
        )
        if request.headers.get("HX-Request") == "true":
            return await _updated_queue_results(
                filters,
                ok=result.ok,
                success_message="Dimension updated.",
                error_message=result.detail or "Could not update dimension.",
            )

        banner = Div(
            "Dimension updated." if result.ok else result.detail or "Could not update dimension.",
            cls=(
                "quality-reports-banner quality-reports-banner-ok"
                if result.ok
                else "quality-reports-banner quality-reports-banner-error"
            ),
        )
        return await _reports_page(request, banner=banner, filters=filters)
