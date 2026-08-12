"""Batches page -- /mc/batches

Batch prediction pipeline runs. Admin-only. Pick a district, click "Run Predictions",
and watch progress via HTMX polling.
"""

import logging

from fasthtml.common import (
    Button,
    Div,
    Form,
    Option,
    P,
    Select,
    Span,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
)
from starlette.requests import Request

from nctqai.components import AYSelect, TABLE_CLS
from nctqai.layout import Layout
from nctqai.services.mc.batches import (
    BatchStatus,
    get_available_districts,
    get_batch_manager,
    get_doc_count,
    get_high_priority_count,
)
from nctqai.utils import DEFAULT_AY, format_ay
from nctqai.routes.compass._helpers import run_in_thread

logger = logging.getLogger(__name__)

_STATUS_BADGE = {
    BatchStatus.pending: "badge badge-unreviewed",
    BatchStatus.running: "badge badge-running",
    BatchStatus.completed: "badge badge-accepted",
    BatchStatus.failed: "badge badge-rejected",
}


def register(rt):

    @rt("/mc/batches")
    def get_batches_page(request: Request):
        """Batch prediction management page (admin-only)."""
        user = request.state.user
        if not user.is_admin():
            return Layout(
                "Batches", "Access denied",
                P("Admin access required.", cls="uk-text-muted uk-text-center empty-state"),
                section="mc", sub_nav="/mc/batches", user=user, show_heading=False,
            )

        try:
            districts = get_available_districts()
        except Exception:
            logger.exception("Failed to load districts for batches page")
            districts = []

        manager = get_batch_manager()

        # District select
        district_select = Div(
            Span("District", cls="filter-group-label"),
            Select(
                Option("Select a district...", value="", selected="selected", disabled="disabled"),
                *[
                    Option(
                        f"{d['district_name']} ({d['state']})" if d.get("state") else d["district_name"],
                        value=str(d["district_id"]),
                    )
                    for d in districts
                ],
                name="district_id",
                id="batch-district",
                hx_get="/mc/batches/preview",
                hx_target="#batch-preview",
                hx_include="[name='ay']",
                cls="uk-select uk-form-small select-xl",
            ),
            cls="filter-group",
        )

        # AY select
        ay_select = AYSelect(DEFAULT_AY, "/mc/batches/preview", "#batch-preview", "[name='district_id']")

        # Run button
        run_btn = Div(
            Span("", cls="filter-group-label"),
            Button(
                "Run Predictions",
                type="submit",
                cls="uk-button uk-button-small btn-primary",
            ),
            cls="filter-group filter-bar-actions",
        )

        filter_bar = Form(
            Div(district_select, ay_select, run_btn, cls="filter-bar"),
            hx_post="/mc/batches/start",
            hx_target="#batch-jobs",
            hx_swap="outerHTML",
        )

        # Preview area
        preview = Div(id="batch-preview", cls="mb-md")

        # Jobs list
        jobs_div = _build_jobs_div(manager)

        content = Div(
            filter_bar,
            preview,
            jobs_div,
            id="batches-content",
        )

        if request.headers.get("hx-request"):
            return content

        return Layout(
            "Batches",
            "Prediction pipeline runs",
            content,
            section="mc",
            sub_nav="/mc/batches",
            user=user,
            show_heading=False,
        )

    @rt("/mc/batches/preview")
    def get_batch_preview(request: Request, district_id: str = "", ay: int = DEFAULT_AY):
        """HTMX partial: doc count + question count preview."""
        user = request.state.user
        if not user or not user.is_admin():
            return Div(id="batch-preview")
        if not district_id:
            return Div(id="batch-preview")

        try:
            did = int(district_id)
            doc_count = get_doc_count(did, ay)
            q_count = get_high_priority_count()
        except Exception:
            logger.exception("Failed to load preview for district=%s ay=%s", district_id, ay)
            return Div(
                P("Failed to load preview.", cls="uk-text-danger text-sm"),
                id="batch-preview",
            )

        if doc_count == 0:
            return Div(
                Div(
                    Span("No documents found for this district/year.", cls="text-error font-semibold"),
                    P(
                        "The pipeline needs documents in silver.district_documents to run predictions.",
                        cls="text-sm text-muted mt-xs",
                    ),
                    cls="review-card alert-error",
                ),
                id="batch-preview",
                cls="mb-md",
            )

        return Div(
            Div(
                Span(f"{doc_count} documents", cls="cell-status-green"),
                Span(f" available for {format_ay(ay)}", cls="text-secondary"),
                Span(" · ", cls="text-faint"),
                Span(f"{q_count} high-priority questions", cls="font-semibold"),
                Span(" will be processed", cls="text-secondary"),
                cls="review-card alert-success",
            ),
            id="batch-preview",
            cls="mb-md",
        )

    @rt("/mc/batches/start", methods=["POST"])
    async def post_start_batch(request: Request):
        """Start a new prediction batch."""
        user = request.state.user
        if not user or not user.is_admin():
            return Div(id="batch-jobs")

        form = await request.form()
        district_id = form.get("district_id", "")
        try:
            ay = int(form.get("ay", DEFAULT_AY))
            did = int(district_id) if district_id else 0
        except (TypeError, ValueError):
            logger.warning("post_start_batch: bad district_id=%r ay=%r", district_id, form.get("ay"))
            return _build_jobs_div(get_batch_manager())

        if not did:
            return _build_jobs_div(get_batch_manager())

        manager = get_batch_manager()

        if manager.is_district_running(did, ay):
            logger.warning("Batch already running for district=%s ay=%s", did, ay)
            return _build_jobs_div(manager)

        # Look up district name
        districts = await run_in_thread(get_available_districts)
        name = next((d["district_name"] for d in districts if d["district_id"] == did), f"District {did}")

        manager.start_batch(did, name, ay)
        logger.info("Started batch for %s (D%s) AY%s by %s", name, did, ay, user.email)

        return _build_jobs_div(manager)

    @rt("/mc/batches/jobs")
    def get_batch_jobs(request: Request):
        """HTMX polling partial: current job list."""
        user = request.state.user
        if not user or not user.is_admin():
            return Div(id="batch-jobs")
        return _build_jobs_div(get_batch_manager())


def _format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s"


def _build_jobs_div(manager) -> Div:
    """Build the jobs table with optional HTMX polling."""
    jobs = manager.get_all_jobs()

    if not jobs:
        return Div(
            P("No batch jobs yet. Select a district and click Run Predictions.",
              cls="uk-text-muted uk-text-center empty-state"),
            id="batch-jobs",
        )

    rows = []
    for job in jobs:
        progress_text = ""
        if job.total > 0:
            progress_text = f"{job.completed}/{job.total} ({job.progress_pct}%)"
        elif job.status == BatchStatus.running:
            progress_text = "Starting..."

        status_text = job.status.value.capitalize()
        if job.status == BatchStatus.failed and job.error:
            status_text = "Failed"

        rows.append(
            Tr(
                Td(job.district_name, cls="cell-nowrap font-semibold"),
                Td(format_ay(job.ay_id), cls="uk-text-center cell-nowrap"),
                Td(Span(status_text, cls=_STATUS_BADGE.get(job.status, "badge")), cls="uk-text-center"),
                Td(progress_text, cls="uk-text-center cell-nowrap"),
                Td(_format_elapsed(job.elapsed_seconds), cls="uk-text-center cell-nowrap"),
            )
        )

    # Poll every 3s only while jobs are running
    poll_attrs = {}
    if manager.has_running_jobs():
        poll_attrs = {
            "hx_get": "/mc/batches/jobs",
            "hx_trigger": "every 3s",
            "hx_swap": "outerHTML",
        }

    return Div(
        Div(
            Table(
                Thead(
                    Tr(
                        Th("District", cls="uk-table-expand"),
                        Th("AY", cls="uk-table-shrink uk-text-center"),
                        Th("Status", cls="uk-table-shrink uk-text-center"),
                        Th("Progress", cls="uk-table-shrink uk-text-center"),
                        Th("Elapsed", cls="uk-table-shrink uk-text-center"),
                    )
                ),
                Tbody(*rows),
                cls=TABLE_CLS,
            ),
            cls="table-card",
        ),
        id="batch-jobs",
        **poll_attrs,
    )
