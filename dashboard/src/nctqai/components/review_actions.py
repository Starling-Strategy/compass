"""Shared review action components for approve/reject workflow.

Used by both question_review.py (initial render) and api.py (HTMX swap).
"""

import json

from fasthtml.common import (
    Button,
    Div,
    Form,
    Input,
    Label,
    Span,
    Textarea,
)

from nctqai.components.badges import HoldBadge, StatusBadge

REJECTION_REASONS = [
    ("source_doc_unavailable", "Source document unavailable"),
    ("wrong_doc_cited", "Wrong document cited"),
    ("incorrect_reasoning", "Incorrect reasoning"),
    ("answer_format", "Answer format"),
    ("not_sure", "I'm not sure"),
]


def _format_rejection_reasons(rejection_reason: str | None) -> str | None:
    """Format rejection_reason for display. Handles JSON arrays and bare strings."""
    if not rejection_reason:
        return None
    if rejection_reason.startswith("["):
        try:
            reasons = json.loads(rejection_reason)
            if isinstance(reasons, list) and reasons:
                return ", ".join(r.replace("_", " ").title() for r in reasons)
        except (json.JSONDecodeError, TypeError):
            # Not a JSON array after all — fall through and render the raw
            # rejection_reason as a single titled string.
            pass
    return rejection_reason.replace("_", " ").title()


def ReviewActionButtons(district_id: int, ay_id: int, q_id: int, run_id: str | None = None):
    """Approve/reject button forms for the review workflow.

    Returns a Div with id="review-status" containing Notes textarea
    above the action buttons. The decision card has a contrast background.
    """
    _hx_include = "[name='decision_note'], [name='footnote'], [name^='page_ref__'], [name='run_id']"

    # Notes textarea (decision-related, lives in this card)
    notes_section = Div(
        Label("Notes", htmlfor="decision_note", cls="form-label"),
        Textarea(
            name="decision_note",
            id="decision_note",
            placeholder="Optional note about this review decision...",
            cls="uk-textarea uk-form-small mt-xs",
            rows="3",
        ),
        cls="mb-md",
    )

    approve_form = Form(
        Input(type="hidden", name="district_id", value=str(district_id)),
        Input(type="hidden", name="ay_id", value=str(ay_id)),
        Input(type="hidden", name="q_id", value=str(q_id)),
        *([] if not run_id else [Input(type="hidden", name="run_id", value=run_id)]),
        Input(type="hidden", name="reviewed_by", value="analyst"),
        Button(
            "Accept",
            type="submit",
            cls="uk-button uk-button-small btn-approve",
        ),
        hx_post="/mc/api/approve",
        hx_target="#review-status",
        hx_swap="outerHTML",
        hx_include=_hx_include,
        style="display: inline;",
    )

    _validation_id = f"reject-validation-{district_id}-{ay_id}-{q_id}"

    reject_form = Form(
        Input(type="hidden", name="district_id", value=str(district_id)),
        Input(type="hidden", name="ay_id", value=str(ay_id)),
        Input(type="hidden", name="q_id", value=str(q_id)),
        *([] if not run_id else [Input(type="hidden", name="run_id", value=run_id)]),
        Input(type="hidden", name="reviewed_by", value="analyst"),
        Button(
            "Reject",
            type="submit",
            cls="uk-button uk-button-small btn-reject",
            onclick=f"if(!document.querySelector('[name=rejection_reason]:checked'))"
                   f"{{document.getElementById('{_validation_id}').style.display='block';"
                   f"event.preventDefault();return false;}}"
                   f"document.getElementById('{_validation_id}').style.display='none';",
        ),
        hx_post="/mc/api/reject",
        hx_target="#review-status",
        hx_swap="outerHTML",
        hx_include=_hx_include + ", [name='rejection_reason']",
        style="display: inline;",
    )

    checkbox_row = Div(
        Div(
            *[
                Label(
                    Input(type="checkbox", name="rejection_reason", value=val, cls="uk-checkbox"),
                    Span(label, cls="reject-checkbox-label"),
                    cls="reject-checkbox-item",
                )
                for val, label in REJECTION_REASONS
            ],
            cls="reject-checkbox-group",
        ),
        Span("Please select at least one reason.", cls="reject-validation-msg", id=_validation_id),
    )

    return Div(
        Div("Review Decision", cls="review-card-label"),
        notes_section,
        Div(
            approve_form,
            reject_form,
            cls="review-actions",
        ),
        checkbox_row,
        id="review-status",
        cls="review-card review-card-decision",
    )


def ReviewStatus(status: str, reviewed_by: str | None, reviewed_at: str | None,
                 rejection_reason: str | None, decision_note: str | None,
                 district_id: int, ay_id: int, q_id: int,
                 run_id: str | None = None,
                 show_change_link: bool = True):
    """Display current review status with optional 'Change Decision' link.

    Returns a Div with id="review-status" showing the review state.

    Args:
        show_change_link: If False, omit the "Change Decision" link (for viewers
            who lack review permissions).
    """
    parts = [StatusBadge(status)]

    if reviewed_by:
        info_text = f"Reviewed by {reviewed_by}"
        if reviewed_at:
            info_text += f" at {reviewed_at}"
        parts.append(
            Span(info_text, cls="review-meta ml-sm")
        )

    if rejection_reason:
        formatted = _format_rejection_reasons(rejection_reason)
        if formatted:
            parts.append(
                Div(
                    Span("Reason: ", cls="font-semibold text-sm"),
                    Span(formatted, cls="text-sm text-muted"),
                    cls="mt-sm",
                )
            )

    if decision_note:
        parts.append(
            Div(
                Span("Note: ", cls="font-semibold text-sm"),
                Span(decision_note, cls="text-sm text-muted"),
                cls="mt-xs",
            )
        )

    # Edit Note form (only for reviewed answers with edit permissions)
    if show_change_link and status in ("approved", "incorrect"):
        edit_btn_id = f"edit-note-btn-{district_id}-{ay_id}-{q_id}"
        edit_form_id = f"edit-note-form-{district_id}-{ay_id}-{q_id}"
        _hx_include = "[name^='page_ref__']"
        parts.append(
            Div(
                Button(
                    "Edit Note",
                    type="button",
                    cls="uk-button uk-button-small uk-button-default text-sm",
                    id=edit_btn_id,
                    onclick=(
                        f"document.getElementById('{edit_btn_id}').style.display='none';"
                        f"document.getElementById('{edit_form_id}').style.display='block';"
                    ),
                ),
                Form(
                    Input(type="hidden", name="district_id", value=str(district_id)),
                    Input(type="hidden", name="ay_id", value=str(ay_id)),
                    Input(type="hidden", name="q_id", value=str(q_id)),
                    *([] if not run_id else [Input(type="hidden", name="run_id", value=run_id)]),
                    Div(
                        Label("Note", htmlfor=f"edit-note-text-{district_id}-{ay_id}-{q_id}",
                              cls="form-label"),
                        Textarea(
                            decision_note or "",
                            name="decision_note",
                            id=f"edit-note-text-{district_id}-{ay_id}-{q_id}",
                            cls="uk-textarea uk-form-small mt-xs",
                            rows="3",
                        ),
                        cls="mb-sm",
                    ),
                    Div(
                        Button(
                            "Save",
                            type="submit",
                            cls="uk-button uk-button-small btn-approve",
                        ),
                        Button(
                            "Cancel",
                            type="button",
                            cls="uk-button uk-button-small uk-button-default text-sm ml-sm",
                            onclick=(
                                f"document.getElementById('{edit_form_id}').style.display='none';"
                                f"document.getElementById('{edit_btn_id}').style.display='inline-block';"
                            ),
                        ),
                        cls="review-actions",
                    ),
                    hx_post="/mc/api/update-note",
                    hx_target="#review-status",
                    hx_swap="outerHTML",
                    hx_include=_hx_include,
                    id=edit_form_id,
                    style="display:none;",
                ),
                cls="mt-sm",
            )
        )

    # Reset button (only for users with review permissions)
    if show_change_link:
        parts.append(
            Div(
                Button(
                    "Reset to Unreviewed",
                    cls="uk-button uk-button-small uk-button-default text-sm",
                    hx_post="/mc/api/reset",
                    hx_target="#review-status",
                    hx_swap="outerHTML",
                    hx_vals=json.dumps({"district_id": district_id, "ay_id": ay_id, "q_id": q_id, "run_id": run_id}),
                    hx_confirm="Reset this answer to unreviewed? This will clear the current review decision.",
                ),
                cls="mt-md",
            )
        )

    return Div(*parts, id="review-status", cls="review-card review-card-decision")


def HoldStatus(held_by: str, hold_reason: str, held_at: str | None,
               district_id: int, ay_id: int, q_id: int,
               can_release: bool = False):
    """Display active hold status with optional release button.

    Returns a Div with id="review-status" showing the hold state.
    """
    parts = [
        Div("Review Decision", cls="review-card-label"),
        Div(
            HoldBadge(),
            Span(f" held by {held_by}", cls="review-meta ml-sm"),
            *([] if not held_at else [Span(f" at {held_at}", cls="review-meta")]),
        ),
        Div(
            Span("Reason: ", cls="font-semibold text-sm"),
            Span(hold_reason, cls="text-sm text-muted"),
            cls="mt-sm",
        ),
    ]

    if can_release:
        parts.append(
            Div(
                Button(
                    "Release Hold",
                    cls="uk-button uk-button-small btn-release-hold",
                    hx_post="/mc/api/release-hold",
                    hx_target="#review-status",
                    hx_swap="outerHTML",
                    hx_vals=json.dumps({"district_id": district_id, "ay_id": ay_id, "q_id": q_id}),
                    hx_confirm="Release the quality hold on this answer? It will return to the review queue.",
                ),
                cls="mt-md",
            )
        )

    return Div(*parts, id="review-status", cls="review-card review-card-hold")


def HoldButton(district_id: int, ay_id: int, q_id: int):
    """Toggle button + inline reason form for placing a quality hold.

    Same UX pattern as the reject form: button reveals inline form.
    """
    hold_btn_id = f"hold-btn-{district_id}-{ay_id}-{q_id}"
    hold_form_id = f"hold-form-{district_id}-{ay_id}-{q_id}"

    return Div(
        Button(
            "Hold for Quality Review",
            type="button",
            cls="uk-button uk-button-small btn-hold",
            id=hold_btn_id,
            onclick=f"document.getElementById('{hold_btn_id}').style.display='none';"
                    f"document.getElementById('{hold_form_id}').style.display='flex';",
        ),
        Form(
            Input(type="hidden", name="district_id", value=str(district_id)),
            Input(type="hidden", name="ay_id", value=str(ay_id)),
            Input(type="hidden", name="q_id", value=str(q_id)),
            Input(
                type="text",
                name="hold_reason",
                placeholder="Reason for hold...",
                cls="uk-input uk-form-small",
                required="true",
            ),
            Button(
                "Confirm Hold",
                type="submit",
                cls="uk-button uk-button-small btn-hold",
            ),
            hx_post="/mc/api/hold",
            hx_target="#review-status",
            hx_swap="outerHTML",
            cls="hold-form-inline",
            id=hold_form_id,
            style="display:none;",
        ),
        cls="mt-md",
    )
