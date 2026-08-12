"""HTMX API endpoints for the Metric Calculator review actions.

POST /mc/api/approve — approve a suggested answer
POST /mc/api/reject  — reject a suggested answer
GET  /mc/api/review-actions — return action buttons (for "Change Decision")
"""

import json

from fasthtml.common import Div, Span
from starlette.requests import Request
from starlette.responses import Response

from nctqai.components import HoldStatus, ReviewActionButtons, ReviewStatus
from nctqai.config import Config
from nctqai.services.mc import (
    approve_answer,
    get_question_review,
    is_answer_held,
    place_hold,
    reject_answer,
    release_hold,
    reset_answer,
    update_note,
)
from nctqai.routes.compass._helpers import run_in_thread

# Singleton — config is immutable at runtime, no need to re-parse .env per request
_config = Config()


def _collect_page_ref(form_data) -> str | None:
    """Collect per-document citation notes from form fields named page_ref__<doc_id>.

    Returns JSON string like {"doc_id_1": "note", ...} or None if all empty.
    """
    notes = {}
    for key, value in form_data.items():
        if key.startswith("page_ref__") and value and value.strip():
            doc_id = key[len("page_ref__"):]
            notes[doc_id] = value.strip()
    return json.dumps(notes) if notes else None


def register(rt):

    @rt("/mc/api/approve", methods=["POST"])
    async def post_approve(
        request: Request,
        district_id: int,
        ay_id: int,
        q_id: int,
        decision_note: str = "",
        footnote: str = "",
        run_id: str = "",
    ):
        """Approve a suggested answer. Returns updated status HTML."""
        if not request.headers.get("hx-request"):
            return Response("Forbidden", status_code=403)
        user = request.state.user
        if not user or not user.can_review():
            return Div(
                Span("You don't have permission to review.", cls="text-error"),
                id="review-status",
                cls="review-card",
            )

        if await run_in_thread(is_answer_held, district_id, ay_id, q_id):
            return Div(
                Span("This answer is on hold and cannot be reviewed.", cls="text-warning"),
                id="review-status",
                cls="review-card",
            )

        reviewed_by = user.email

        # Collect per-document citation notes from form
        form_data = dict(await request.form())
        page_ref = _collect_page_ref(form_data)

        if _config.dry_run:
            return Div(
                Div("Review Decision", cls="review-card-label"),
                Span(
                    "Dry run mode -- no changes written. Set NCTQAI_DRY_RUN=false to enable writes.",
                    cls="review-meta text-warning",
                ),
                id="review-status",
                cls="review-card",
            )

        success = await run_in_thread(
            approve_answer,
            district_id=district_id,
            ay_id=ay_id,
            q_id=q_id,
            reviewed_by=reviewed_by,
            decision_note=decision_note or None,
            footnote=footnote or None,
            page_ref=page_ref,
            run_id=run_id or None,
        )

        if success:
            return ReviewStatus(
                status="approved",
                reviewed_by=reviewed_by,
                reviewed_at=None,
                rejection_reason=None,
                decision_note=decision_note or None,
                district_id=district_id,
                ay_id=ay_id,
                q_id=q_id,
                run_id=run_id or None,
            )

        return Div(
            Span("Error: Could not update record.", cls="text-error"),
            id="review-status",
            cls="review-card",
        )

    @rt("/mc/api/reject", methods=["POST"])
    async def post_reject(
        request: Request,
        district_id: int,
        ay_id: int,
        q_id: int,
        decision_note: str = "",
        footnote: str = "",
        run_id: str = "",
    ):
        """Reject a suggested answer. Returns updated status HTML."""
        if not request.headers.get("hx-request"):
            return Response("Forbidden", status_code=403)
        user = request.state.user
        if not user or not user.can_review():
            return Div(
                Span("You don't have permission to review.", cls="text-error"),
                id="review-status",
                cls="review-card",
            )

        if await run_in_thread(is_answer_held, district_id, ay_id, q_id):
            return Div(
                Span("This answer is on hold and cannot be reviewed.", cls="text-warning"),
                id="review-status",
                cls="review-card",
            )

        reviewed_by = user.email

        # Collect form data (must happen before dry-run check for rejection_reasons)
        form_data = await request.form()
        page_ref = _collect_page_ref(dict(form_data))

        # Multi-select rejection reasons → JSON array string
        rejection_reasons = form_data.getlist("rejection_reason")
        if not rejection_reasons:
            rejection_reasons = ["other"]
        rejection_reason = json.dumps(rejection_reasons)

        if _config.dry_run:
            return Div(
                Div("Review Decision", cls="review-card-label"),
                Span(
                    "Dry run mode -- no changes written. Set NCTQAI_DRY_RUN=false to enable writes.",
                    cls="review-meta text-warning",
                ),
                id="review-status",
                cls="review-card",
            )

        success = await run_in_thread(
            reject_answer,
            district_id=district_id,
            ay_id=ay_id,
            q_id=q_id,
            reviewed_by=reviewed_by,
            rejection_reason=rejection_reason,
            decision_note=decision_note or None,
            footnote=footnote or None,
            page_ref=page_ref,
            run_id=run_id or None,
        )

        if success:
            return ReviewStatus(
                status="rejected",
                reviewed_by=reviewed_by,
                reviewed_at=None,
                rejection_reason=rejection_reason,
                decision_note=decision_note or None,
                district_id=district_id,
                ay_id=ay_id,
                q_id=q_id,
                run_id=run_id or None,
            )

        return Div(
            Span("Error: Could not update record.", cls="text-error"),
            id="review-status",
            cls="review-card",
        )

    @rt("/mc/api/reset", methods=["POST"])
    def post_reset(
        request: Request,
        district_id: int,
        ay_id: int,
        q_id: int,
        run_id: str = "",
    ):
        """Reset a suggested answer to unreviewed. Returns action buttons."""
        if not request.headers.get("hx-request"):
            return Response("Forbidden", status_code=403)
        user = request.state.user
        if not user or not user.can_review():
            return Div(
                Span("You don't have permission to review.", cls="text-error"),
                id="review-status",
                cls="review-card",
            )

        if is_answer_held(district_id, ay_id, q_id):
            return Div(
                Span("This answer is on hold and cannot be reviewed.", cls="text-warning"),
                id="review-status",
                cls="review-card",
            )

        if _config.dry_run:
            return Div(
                Div("Review Decision", cls="review-card-label"),
                Span(
                    "Dry run mode -- no changes written. Set NCTQAI_DRY_RUN=false to enable writes.",
                    cls="review-meta text-warning",
                ),
                id="review-status",
                cls="review-card",
            )

        success = reset_answer(
            district_id=district_id,
            ay_id=ay_id,
            q_id=q_id,
            reviewed_by=user.email,
            run_id=run_id or None,
        )

        if success:
            return ReviewActionButtons(district_id, ay_id, q_id, run_id=run_id or None)

        return Div(
            Span("Error: Could not reset record.", cls="text-error"),
            id="review-status",
            cls="review-card",
        )

    @rt("/mc/api/hold", methods=["POST"])
    def post_hold(
        request: Request,
        district_id: int,
        ay_id: int,
        q_id: int,
        hold_reason: str = "",
    ):
        """Place a quality hold on a suggested answer."""
        if not request.headers.get("hx-request"):
            return Response("Forbidden", status_code=403)
        user = request.state.user
        if not user or not user.can_hold():
            return Div(
                Span("You don't have permission to place holds.", cls="text-error"),
                id="review-status",
                cls="review-card",
            )

        if not hold_reason.strip():
            return Div(
                Span("A reason is required to place a hold.", cls="text-warning"),
                id="review-status",
                cls="review-card",
            )

        if _config.dry_run:
            return Div(
                Div("Review Decision", cls="review-card-label"),
                Span(
                    "Dry run mode -- no changes written. Set NCTQAI_DRY_RUN=false to enable writes.",
                    cls="review-meta text-warning",
                ),
                id="review-status",
                cls="review-card",
            )

        success = place_hold(
            district_id=district_id,
            ay_id=ay_id,
            q_id=q_id,
            held_by=user.email,
            hold_reason=hold_reason.strip(),
        )

        if success:
            return HoldStatus(
                held_by=user.email,
                hold_reason=hold_reason.strip(),
                held_at=None,
                district_id=district_id,
                ay_id=ay_id,
                q_id=q_id,
                can_release=True,
            )

        return Div(
            Span("Hold already exists for this answer.", cls="text-warning"),
            id="review-status",
            cls="review-card",
        )

    @rt("/mc/api/release-hold", methods=["POST"])
    def post_release_hold(
        request: Request,
        district_id: int,
        ay_id: int,
        q_id: int,
    ):
        """Release a quality hold, returning the answer to the review queue."""
        if not request.headers.get("hx-request"):
            return Response("Forbidden", status_code=403)
        user = request.state.user
        if not user or not user.can_hold():
            return Div(
                Span("You don't have permission to release holds.", cls="text-error"),
                id="review-status",
                cls="review-card",
            )

        if _config.dry_run:
            return Div(
                Div("Review Decision", cls="review-card-label"),
                Span(
                    "Dry run mode -- no changes written. Set NCTQAI_DRY_RUN=false to enable writes.",
                    cls="review-meta text-warning",
                ),
                id="review-status",
                cls="review-card",
            )

        success = release_hold(
            district_id=district_id,
            ay_id=ay_id,
            q_id=q_id,
            released_by=user.email,
        )

        if success:
            # Reload the review to get run_id for the action buttons
            review = get_question_review(district_id, ay_id, q_id)
            run_id = review.run_id if review else None
            return ReviewActionButtons(district_id, ay_id, q_id, run_id=run_id)

        return Div(
            Span("No active hold found for this answer.", cls="text-warning"),
            id="review-status",
            cls="review-card",
        )

    @rt("/mc/api/update-note", methods=["POST"])
    async def post_update_note(
        request: Request,
        district_id: int,
        ay_id: int,
        q_id: int,
        decision_note: str = "",
        footnote: str = "",
        run_id: str = "",
    ):
        """Update note fields on an already-reviewed answer without changing status."""
        if not request.headers.get("hx-request"):
            return Response("Forbidden", status_code=403)
        user = request.state.user
        if not user or not user.can_review():
            return Div(
                Span("You don't have permission to review.", cls="text-error"),
                id="review-status",
                cls="review-card",
            )

        form_data = dict(await request.form())
        page_ref = _collect_page_ref(form_data)

        if _config.dry_run:
            return Div(
                Div("Review Decision", cls="review-card-label"),
                Span(
                    "Dry run mode -- no changes written. Set NCTQAI_DRY_RUN=false to enable writes.",
                    cls="review-meta text-warning",
                ),
                id="review-status",
                cls="review-card",
            )

        success = await run_in_thread(
            update_note,
            district_id=district_id,
            ay_id=ay_id,
            q_id=q_id,
            reviewed_by=user.email,
            decision_note=decision_note or None,
            footnote=footnote or None,
            page_ref=page_ref,
            run_id=run_id or None,
        )

        if success:
            review = get_question_review(district_id, ay_id, q_id)
            if review:
                return ReviewStatus(
                    status=review.status,
                    reviewed_by=review.reviewed_by,
                    reviewed_at=review.reviewed_at,
                    rejection_reason=review.rejection_reason,
                    decision_note=review.decision_note,
                    district_id=district_id,
                    ay_id=ay_id,
                    q_id=q_id,
                    run_id=run_id or None,
                )

        return Div(
            Span("Error: Could not update note.", cls="text-error"),
            id="review-status",
            cls="review-card",
        )

    @rt("/mc/api/review-actions")
    def get_review_actions(request: Request, district_id: int, ay_id: int, q_id: int):
        """Return action buttons HTML for 'Change Decision' link."""
        user = request.state.user
        if not user or not user.can_review():
            return Div(
                Span("You don't have permission to review.", cls="text-error"),
                id="review-status",
                cls="review-card",
            )

        review = get_question_review(district_id, ay_id, q_id)
        if not review:
            return Span("Error: question not found.", cls="text-error")

        # Return just the inner content (no outer review-status div since
        # this swaps innerHTML of the existing #review-status)
        buttons = ReviewActionButtons(review.district_id, review.ay_id, review.q_id, run_id=review.run_id)
        # ReviewActionButtons wraps in id="review-status", but for innerHTML
        # swap we need just the inner content. Extract children.
        return buttons
