"""District policy page -- /mc/districts/{district_id}/policies/{dpol_id}

Questions grouped by subpolicy with status tabs and question table.
Default view is the "Needs Review" queue (non-accepted questions).
"""

import logging

from fasthtml.common import (
    A,
    Div,
    P,
    Span,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
)

from starlette.requests import Request

from nctqai.components import AYSelect, INABadge, QIdBadge, StatusBadge, TABLE_CLS
from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.mc import (
    get_district_info,
    get_policy_name,
    get_policy_question_counts,
    get_policy_questions,
    get_subtopic_counts,
)
from nctqai.utils import DEFAULT_AY

logger = logging.getLogger(__name__)


def register(rt):

    @rt("/mc/districts/{district_id:int}/policies/{dpol_id:int}")
    def get_district_policy(request: Request, district_id: int, dpol_id: int, ay: int = DEFAULT_AY, status: str = "needs_review"):
        """Questions grouped by subpolicy for a district + policy."""
        user, deny = require_section(request, "mc")
        if deny:
            return deny
        try:
            info = get_district_info(district_id)
            district_name = info["district_name"]
            state = info["state"]
        except Exception:
            logger.exception("Failed to load district info for district_id=%s", district_id)
            district_name = f"District {district_id}"
            state = None
        try:
            policy_name = get_policy_name(dpol_id)
        except Exception:
            logger.exception("Failed to load policy name for dpol_id=%s", dpol_id)
            policy_name = f"Policy {dpol_id}"
        try:
            groups = get_policy_questions(district_id, ay, dpol_id, status_filter=status)
        except Exception:
            logger.exception("Failed to load policy questions for district_id=%s, dpol_id=%s", district_id, dpol_id)
            groups = []

        # Get counts for tab labels
        try:
            counts = get_policy_question_counts(district_id, ay, dpol_id)
        except Exception:
            logger.exception("Failed to load question counts for district_id=%s, dpol_id=%s", district_id, dpol_id)
            counts = {"total": 0, "accepted": 0, "rejected": 0, "unreviewed": 0, "needs_review": 0}

        # AY dropdown — same pattern as other MC pages
        _base = f"/mc/districts/{district_id}/policies/{dpol_id}"

        ay_select = AYSelect(ay, _base, "#policy-content", "[name='status']")

        # Status tabs with counts
        tabs = [
            ("Needs Review", "needs_review", counts["needs_review"]),
            ("Accepted", "accepted", counts["accepted"]),
            ("All", "all", counts["total"]),
        ]
        status_tabs = Div(
            *[
                A(
                    f"{label} ({n})",
                    href=f"{_base}?ay={ay}&status={val}",
                    cls="active" if status == val else "",
                )
                for label, val, n in tabs
            ],
            cls="filter-tabs",
        )

        view_docs_btn = A(
            "View Documents",
            href=f"/docs?district={district_id}",
            target="_blank",
            cls="uk-button uk-button-default uk-button-small",
        )

        filter_bar = Div(
            Div(ay_select, status_tabs),
            Div(view_docs_btn, cls="filter-bar-actions"),
            cls="filter-bar",
        )

        # Determine table view mode
        if status == "needs_review":
            view = "queue"
        elif status == "accepted":
            view = "accepted"
        else:
            view = "all"

        # Get per-subtopic review counts (unfiltered) for progress indicators
        try:
            subtopic_counts = get_subtopic_counts(district_id, ay, dpol_id)
        except Exception:
            logger.exception("Failed to load subtopic counts for district_id=%s, dpol_id=%s", district_id, dpol_id)
            subtopic_counts = {}

        # Merge filtered groups with all subtopics (so fully-reviewed ones still show)
        groups_by_name = {g.subpolicy_name: g for g in groups}
        ordered_names = list(subtopic_counts.keys()) if subtopic_counts else list(groups_by_name.keys())
        # Include any filtered groups not in subtopic_counts (shouldn't happen, but safe)
        for name in groups_by_name:
            if name not in ordered_names:
                ordered_names.append(name)

        # Build subpolicy sections
        sections = []
        total_q = 0
        for name in ordered_names:
            group = groups_by_name.get(name)
            sc = subtopic_counts.get(name, {})
            questions = group.questions if group else []
            total_q += len(questions)
            sections.append(_build_subpolicy_header(name, sc, status_filter=status, has_questions=bool(questions)))
            if questions:
                sections.append(Div(_build_question_table(questions, district_id, ay, view=view), cls="table-card"))

        if not ordered_names:
            sections.append(
                P(
                    "No questions found matching this filter.",
                    cls="uk-text-muted uk-text-center empty-state",
                )
            )

        state_part = f", {state}" if state else ""

        content = Div(
            P(
                A(
                    f"← {district_name}{state_part}",
                    href=f"/mc/districts/{district_id}?ay={ay}",
                    cls="uk-link-muted",
                ),
                Span(f" · {total_q} questions", cls="uk-text-muted"),
                cls="mb-xs",
            ),
            filter_bar,
            Div(*sections, id="policy-questions"),
            id="policy-content",
        )

        # HTMX partial — return just the content div on AY change
        if request.headers.get("hx-request"):
            return content

        return Layout(
            policy_name,
            "",
            content,
            section="mc",
            sub_nav="/mc/districts",
            user=user,
        )


def _build_subpolicy_header(name, counts, status_filter=None, has_questions=True):
    """Build subtopic header bar with progress indicator.

    has_questions: whether any question rows will follow this header. When False
    (e.g. all questions reviewed and filtered out in needs_review mode), the header
    gets fully-rounded corners instead of the open-bottom style.
    """
    total = counts.get("total", 0)
    reviewed = counts.get("reviewed", 0)
    all_done = total > 0 and reviewed == total

    if all_done:
        indicator = Span("\u2713 All reviewed", cls="subpolicy-progress-done")
    elif status_filter == "needs_review":
        remaining = total - reviewed
        indicator = Span(f"{remaining} remaining", cls="subpolicy-progress-text")
    else:
        indicator = Span(f"{reviewed} of {total} reviewed", cls="subpolicy-progress-text")

    pct = round(reviewed / total * 100) if total > 0 else 0
    if not all_done and total > 0:
        bar = Div(
            Div(cls="progress-fill", style=f"width:{pct}%"),
            cls="subpolicy-progress-bar",
        )
        right = Div(indicator, bar, cls="subpolicy-progress")
    else:
        right = Div(indicator, cls="subpolicy-progress")

    cls = "subpolicy-header"
    if all_done:
        cls += " subpolicy-complete"
    if not has_questions:
        cls += " subpolicy-header-standalone"

    return Div(Span(name), right, cls=cls)


def _build_question_table(questions, district_id, ay, view="queue"):
    """Build question table for a subpolicy group.

    view="queue"    — Needs Review: Q | Question | Status | Review button
    view="accepted" — Accepted:     Q | Question | Answer | Status
    view="all"      — All:          Q | Question | Answer | Status
    """
    if not questions:
        return P("No questions.", cls="uk-text-muted empty-state")

    rows = []
    for q in questions:
        q_text = q.q_text[:100] + ("..." if len(q.q_text) > 100 else "")

        # Determine status badge — override with "on_hold" if held
        status_display = StatusBadge("on_hold") if q.is_on_hold else StatusBadge(q.status)

        if view == "queue":
            # Work queue: no answer column, show Review button
            review_btn = A(
                "Review →",
                href=f"/mc/districts/{district_id}/questions/{q.q_id}?ay={ay}",
                cls="uk-button uk-button-primary uk-button-small",
            )
            rows.append(
                Tr(
                    Td(QIdBadge(q.q_id, q.q_num), cls="uk-table-shrink"),
                    Td(q_text, cls="uk-table-expand"),
                    Td(status_display, cls="uk-table-shrink uk-text-center"),
                    Td(review_btn, cls="uk-table-shrink cell-nowrap"),
                    cls="clickable-row",
                    onclick=f"window.location='/mc/districts/{district_id}/questions/{q.q_id}?ay={ay}'",
                )
            )
        else:
            # Accepted / All view: show answer only for accepted questions
            if q.status in ("approved", "accepted"):
                if q.is_ina:
                    answer_cell = INABadge()
                elif q.suggested_answer:
                    answer_cell = Span(q.suggested_answer[:60] + ("..." if len(q.suggested_answer) > 60 else ""))
                else:
                    answer_cell = Span("--", cls="uk-text-muted")
            else:
                answer_cell = A(
                    "Review",
                    href=f"/mc/districts/{district_id}/questions/{q.q_id}?ay={ay}",
                    cls="uk-button uk-button-default uk-button-small",
                )

            rows.append(
                Tr(
                    Td(QIdBadge(q.q_id, q.q_num), cls="uk-table-shrink"),
                    Td(q_text, cls="uk-table-expand"),
                    Td(answer_cell, cls="uk-table-shrink cell-nowrap"),
                    Td(status_display, cls="uk-table-shrink uk-text-center"),
                    cls="clickable-row",
                    onclick=f"window.location='/mc/districts/{district_id}/questions/{q.q_id}?ay={ay}'",
                )
            )

    if view == "queue":
        header = Tr(
            Th("QNum", cls="uk-table-shrink"),
            Th("Question", cls="uk-table-expand"),
            Th("Status", cls="uk-table-shrink uk-text-center cell-nowrap"),
            Th("", cls="uk-table-shrink"),
        )
    else:
        header = Tr(
            Th("QNum", cls="uk-table-shrink"),
            Th("Question", cls="uk-table-expand"),
            Th("Answer", cls="uk-table-shrink cell-nowrap"),
            Th("Status", cls="uk-table-shrink uk-text-center cell-nowrap"),
        )

    return Table(
        Thead(header),
        Tbody(*rows),
        cls=TABLE_CLS,
    )
