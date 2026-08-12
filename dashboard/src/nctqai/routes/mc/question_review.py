"""Question review page -- /mc/districts/{district_id}/questions/{q_id}

Core analyst review page: question context, AI answer, citations, reasoning,
analyst inputs, and approve/reject actions via HTMX.
"""

import json

from fasthtml.common import (
    A,
    Div,
    Label,
    P,
    Small,
    Span,
    Textarea,
)
from starlette.requests import Request

from nctqai.components import HoldBadge, HoldButton, HoldStatus, INABadge, ModalDots, QIdBadge, ReviewActionButtons, ReviewStatus, q_label
from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.mc import get_question_review
from nctqai.services.mc.constants import SALARY_Q_IDS
from nctqai.utils import DEFAULT_AY, format_ay


def register(rt):

    @rt("/mc/districts/{district_id:int}/questions/{q_id:int}")
    def get_question_review_page(request: Request, district_id: int, q_id: int, ay: int = DEFAULT_AY):
        """Full question review page with AI answer, citations, and review actions."""
        user, deny = require_section(request, "mc")
        if deny:
            return deny

        review = get_question_review(district_id, ay, q_id)
        if not review:
            return Layout(
                "Not Found",
                "",
                P("Question not found for this district and academic year."),
                section="mc",
                sub_nav="/mc/districts",
                user=user,
            )

        # 1. Question header — hero question text with meta row
        meta_parts = [QIdBadge(review.q_id, review.q_num)]
        if review.policy_name:
            meta_parts.append(Span(f" · {review.policy_name}", cls="text-muted"))
        if review.subpolicy_name:
            meta_parts.append(Span(f" · {review.subpolicy_name}", cls="text-muted"))
        meta_parts.append(Span(f" · {review.district_name}", cls="text-muted"))
        if review.state:
            meta_parts.append(
                Span(
                    review.state,
                    cls="citation-meta-tag ml-sm",
                )
            )
        meta_parts.append(Span(f" · {format_ay(review.ay_id)}", cls="text-muted"))

        q_header = Div(
            Div(
                review.q_text,
                cls="question-text mb-sm",
            ),
            Div(*meta_parts, cls="review-meta-row"),
            cls="review-card",
        )

        # 2. AI Answer card with detail grid
        answer_display = INABadge() if review.is_ina else Span(
            review.suggested_answer or "--", cls="value bold",
        )

        # Build detail grid items
        grid_items = [
            # Row 1: AI Answer | Answer Type
            Div(
                Span("AI Answer", cls="label"),
                answer_display,
                cls="detail-item",
            ),
            Div(
                Span("Answer Type", cls="label"),
                Span(review.q_ans_type or "--", cls="value"),
                cls="detail-item",
            ),
        ]

        # Row 2: Modal Frequency (full width)
        if review.n_predictions and review.agreement_pct is not None:
            grid_items.append(
                Div(
                    Span("Modal Frequency", cls="label"),
                    Div(ModalDots(total=review.n_predictions, agreement_pct=review.agreement_pct), cls="mt-xs"),
                    cls="detail-item full",
                )
            )

        # Row 3: INA Flag | Predictions
        all_ina = review.is_ina and review.agreement_pct == 100
        grid_items.append(
            Div(
                Span("100% INA", cls="label"),
                Div(Span("⚠ Yes", cls="badge badge-ina") if all_ina else Span("—", cls="value")),
                cls="detail-item",
            ),
        )
        grid_items.append(
            Div(
                Span("Predictions", cls="label"),
                Span(f"{review.n_predictions}", cls="value"),
                cls="detail-item",
            ),
        )

        # Row 4: Date of AI Generation | Valid Options
        if review.predicted_at:
            grid_items.append(
                Div(
                    Span("Date of AI Generation", cls="label"),
                    Span(review.predicted_at[:10], cls="value"),
                    cls="detail-item",
                )
            )
        if review.valid_options:
            opts = ", ".join(str(o) for o in review.valid_options)
            grid_items.append(
                Div(
                    Span("Valid Options", cls="label"),
                    Span(opts, cls="value"),
                    cls="detail-item",
                )
            )

        # 3. Combined answer + citations card
        citations_section = _build_citations_section(
            review.citations_json,
            district_name=review.district_name,
            state=review.state,
            district_id=review.district_id,
            ay_id=review.ay_id,
            page_ref_json=review.page_ref,
            footnote=review.footnote,
        )

        answer_card_parts = []
        answer_card_cls = "review-card"
        if review.hold:
            answer_card_parts.append(
                Div(
                    HoldBadge(),
                    Span("This answer is undergoing a data quality review."),
                    cls="hold-banner-inline",
                )
            )
            answer_card_cls = "review-card review-card-held"
        # Data quality warnings
        warnings = []
        if review.q_id in SALARY_Q_IDS:
            warnings.append(Span("Salary — verify manually", cls="badge badge-warning"))
        if not review.is_ina and not review.citations_json:
            warnings.append(Span("No citation available", cls="badge badge-warning"))
        if warnings:
            answer_card_parts.append(Div(*warnings, cls="review-warnings mb-sm"))
        answer_card_parts.append(Div(*grid_items, cls="detail-grid"))
        answer_card_parts.append(citations_section)
        answer_card = Div(*answer_card_parts, cls=answer_card_cls)

        # 4. Reasoning card
        reasoning_card = None
        if review.reasoning:
            reasoning_card = Div(
                Div("AI Reasoning", cls="review-card-label"),
                P(review.reasoning, cls="reasoning-text"),
                cls="review-card reasoning-card",
            )

        # 5. Review actions
        can_review = user.can_review() if user else False
        can_hold = user.can_hold() if user else False
        review_status = _build_review_status(review, can_review=can_review, can_hold=can_hold)

        # 7. Navigation
        nav_parts = []
        if review.prev_q_id:
            nav_parts.append(
                A(
                    "← Previous",
                    href=f"/mc/districts/{district_id}/questions/{review.prev_q_id}?ay={ay}",
                    cls="q-nav-btn",
                )
            )
        else:
            nav_parts.append(Span())  # spacer

        if review.position and review.total_in_policy:
            policy_label = review.policy_name or "Policy"
            nav_parts.append(
                Span(
                    f"Question {review.position} of {review.total_in_policy} in {policy_label}",
                    cls="text-sm text-muted",
                )
            )

        if review.next_q_id:
            nav_parts.append(
                A(
                    "Next →",
                    href=f"/mc/districts/{district_id}/questions/{review.next_q_id}?ay={ay}",
                    cls="q-nav-btn",
                )
            )
        else:
            nav_parts.append(Span())  # spacer

        nav_bar = Div(
            *nav_parts,
            cls="review-nav",
        )

        # Back link to policy
        back_link = None
        if review.dpol_id:
            back_link = A(
                "← Back to policy",
                href=f"/mc/districts/{district_id}/policies/{review.dpol_id}?ay={ay}",
                cls="uk-link-text text-sm",
            )

        # Assemble content
        content_parts = [q_header]
        content_parts.append(answer_card)
        if reasoning_card:
            content_parts.append(reasoning_card)
        content_parts.append(review_status)
        content_parts.append(nav_bar)
        if back_link:
            content_parts.append(Div(back_link, cls="mt-md"))

        content = Div(*content_parts, cls="review-content")

        short_q = review.q_text[:60] + ("..." if len(review.q_text) > 60 else "")
        return Layout(
            f"{q_label(review.q_id, review.q_num)}: {short_q}",
            "",
            content,
            section="mc",
            sub_nav="/mc/districts",
            show_heading=False,
            breadcrumb=[
                ("Districts", "/mc/districts"),
                (review.district_name, f"/mc/districts/{district_id}?ay={ay}"),
                (review.policy_name or "Policy", f"/mc/districts/{district_id}/policies/{review.dpol_id}?ay={ay}" if review.dpol_id else None),
                (q_label(review.q_id, review.q_num), None),
            ],
            user=user,
        )




def _build_citations_section(citations_json, district_name=None, state=None,
                             district_id=None, ay_id=None, page_ref_json=None,
                             footnote=None):
    """Build citations section (inline within the answer card).

    Returns a Div section, not a standalone card. If no citations, shows a message.
    Per-document citation notes are stored as JSON in page_ref_json:
    {"doc_id_1": "note text", ...}. Legacy plain-string values are shown
    as the note for the first document.
    """
    citations = citations_json
    if isinstance(citations, str):
        try:
            citations = json.loads(citations)
        except (json.JSONDecodeError, TypeError):
            citations = None

    # Parse per-document notes from page_ref
    doc_notes = {}
    if page_ref_json:
        if isinstance(page_ref_json, str):
            try:
                parsed = json.loads(page_ref_json)
                if isinstance(parsed, dict):
                    doc_notes = parsed
            except (json.JSONDecodeError, TypeError):
                # Legacy plain string — will assign to first doc below
                doc_notes = {"_legacy": page_ref_json}
        elif isinstance(page_ref_json, dict):
            doc_notes = page_ref_json

    # "Open all documents" link
    header_parts = [Div("Citations", cls="review-card-label")]
    if district_id:
        docs_url = f"/docs?district={district_id}"
        if ay_id:
            docs_url += f"&ay={ay_id}"
        header_parts.append(
            A("Open all documents",
              href=docs_url, target="_blank",
              cls="btn-ghost filter-bar-actions")
        )

    header_row = Div(
        *header_parts,
        cls="citation-header",
    )

    footnote_block = Div(
        Label("Footnote", htmlfor="footnote", cls="text-sm font-medium text-secondary"),
        Textarea(
            footnote or "",
            name="footnote",
            id="footnote",
            placeholder="Optional footnote for the published answer...",
            cls="uk-textarea uk-form-small mt-xs",
            rows="3",
        ),
        cls="citation-footnote",
    )

    if not citations:
        return Div(
            header_row,
            P("No documents were cited for this question.",
              cls="text-sm text-faint"),
            footnote_block,
            cls="citation-notes-section",
        )

    # Group citations by doc_id, preserving insertion order
    groups = {}
    for cite in (citations if isinstance(citations, list) else [citations]):
        if not isinstance(cite, dict):
            continue
        doc_id = cite.get("doc_id", "unknown")
        groups.setdefault(doc_id, []).append(cite)

    # Assign legacy note to first doc if needed
    if "_legacy" in doc_notes and groups:
        first_doc_id = next(iter(groups))
        doc_notes[str(first_doc_id)] = doc_notes.pop("_legacy")

    doc_elements = []
    for doc_idx, (doc_id, cites) in enumerate(groups.items()):
        first = cites[0]
        # _src_name = original NCTQ title from bronze.sources (preferred)
        # _doc_filename = raw PDF filename (fallback)
        raw_src_name = first.get("_src_name", "")
        ai_title = first.get("_ai_title", "")
        if raw_src_name and "Lgcy File" not in raw_src_name:
            src_name = raw_src_name
        elif ai_title:
            src_name = ai_title
        else:
            src_name = raw_src_name or first.get("_doc_filename") or first.get("doc_name", "")
        src_id = first.get("_src_id")
        src_type = first.get("_src_type") or first.get("_doc_type", "")

        # Collect page numbers from all citations for this doc
        # Section headings and AY years are intentionally excluded from the
        # citation display: section headings belong in the AI reasoning, and
        # AY year coverage is document metadata that doesn't belong in a
        # per-citation reference.
        pages = []
        for cite in cites:
            p = cite.get("page_number")
            if p and p not in pages:
                pages.append(p)

        # Build bibliographic entry
        parts = []

        # Line 1: original NCTQ title (src_name) linked to /docs/{src_id} + src_type badge
        title_parts = []
        if src_name and src_id:
            title_parts.append(
                A(src_name, href=f"/docs/{src_id}", cls="uk-link-text citation-title", target="_blank")
            )
        elif src_name:
            title_parts.append(
                Span(src_name, cls="font-semibold text-secondary")
            )
        else:
            fallback = f"Document {doc_idx + 1}"
            if src_id:
                title_parts.append(
                    A(fallback, href=f"/docs/{src_id}", cls="uk-link-text citation-title", target="_blank")
                )
            else:
                title_parts.append(
                    Span(fallback, cls="font-semibold text-secondary")
                )
        if src_type:
            title_parts.append(
                Span(src_type.replace("_", " ").title(), cls="citation-meta-tag ml-sm")
            )
        if title_parts:
            parts.append(Div(*title_parts, cls="mb-xs"))

        # Page numbers as inline tags (section headings and AY years removed)
        if pages:
            meta_tags = [Span(f"p. {p}", cls="citation-meta-tag") for p in pages]
            parts.append(Div(*meta_tags, cls="mb-xs"))

        # Bibliographic line (district, state)
        bib_parts = []
        if district_name:
            bib_parts.append(district_name)
        if state:
            bib_parts.append(state)
        if bib_parts:
            parts.append(
                Div(". ".join(bib_parts) + ".",
                    cls="citation-bib")
            )

        # Per-document citation note textarea (ticket #7)
        note_key = str(doc_id)
        existing_note = doc_notes.get(note_key, "")
        parts.append(
            Div(
                Small("Citation Note", cls="font-medium text-muted"),
                Textarea(
                    existing_note,
                    name=f"page_ref__{note_key}",
                    placeholder="Page numbers, sections, access dates...",
                    cls="uk-textarea uk-form-small citation-note-textarea",
                    rows="2",
                ),
                cls="mt-xs",
            )
        )

        if parts:
            doc_elements.append(
                Div(
                    *parts,
                    cls="citation-doc",
                )
            )

    if not doc_elements:
        return Div(
            header_row,
            P("No documents were cited for this question.",
              cls="text-sm text-faint"),
            footnote_block,
            cls="citation-notes-section",
        )

    return Div(
        header_row,
        *doc_elements,
        footnote_block,
        cls="citation-notes-section",
    )


def _build_review_status(review, can_review=False, can_hold=False):
    """Build the review status section with action buttons or status display."""
    # Check for active hold first
    if review.hold:
        return HoldStatus(
            held_by=review.hold.held_by,
            hold_reason=review.hold.hold_reason,
            held_at=review.hold.held_at,
            district_id=review.district_id,
            ay_id=review.ay_id,
            q_id=review.q_id,
            can_release=can_hold,
        )

    if review.status in ("approved", "accepted", "incorrect", "rejected"):
        return ReviewStatus(
            status=review.status,
            reviewed_by=review.reviewed_by,
            reviewed_at=review.reviewed_at,
            rejection_reason=review.rejection_reason,
            decision_note=review.decision_note,
            district_id=review.district_id,
            ay_id=review.ay_id,
            q_id=review.q_id,
            run_id=review.run_id,
            show_change_link=can_review,
        )

    if not can_review:
        # Viewer -- show status but no action buttons
        return Div(
            Div("Review Decision", cls="review-card-label"),
            Span("Unreviewed", cls="badge badge-unreviewed"),
            Span(" -- View only", cls="review-meta ml-sm"),
            id="review-status",
            cls="review-card review-card-decision",
        )

    # Unreviewed + can review -- show action buttons
    buttons = ReviewActionButtons(review.district_id, review.ay_id, review.q_id, run_id=review.run_id)

    # Add hold button for power users
    if can_hold:
        from fasthtml.common import Div as _Div
        # Wrap the buttons content with the hold button appended
        # ReviewActionButtons returns a Div with id="review-status" — we need to
        # inject the HoldButton inside it before the closing tag.
        hold_btn = HoldButton(review.district_id, review.ay_id, review.q_id)
        return _Div(
            Div("Review Decision", cls="review-card-label"),
            # Re-build the notes + action buttons + hold button inline
            *buttons.children[1:],  # skip the label (we re-added it above)
            hold_btn,
            id="review-status",
            cls="review-card review-card-decision",
        )

    return buttons
