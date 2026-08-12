"""Questions reference pages -- /mc/questions, /mc/questions/{q_id}

Cross-district question performance and per-question detail.
"""

import logging

from fasthtml.common import (
    A,
    Div,
    Input,
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

from nctqai.components import INABadge, KpiCard, KpiRow, ModalDots, QIdBadge, StatusBadge, TABLE_CLS, q_label
from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.mc.questions import get_question_cross_district, get_question_text, get_questions_list
from nctqai.utils import AY_OPTIONS, DEFAULT_AY, format_ay

logger = logging.getLogger(__name__)


def register(rt):

    @rt("/mc/questions")
    def get_questions_page(request: Request, ay: int = DEFAULT_AY, search: str = "", ans_type: str = ""):
        """Questions list with cross-district stats."""
        user, deny = require_section(request, "mc")
        if deny:
            return deny
        try:
            questions = get_questions_list(
                ay_id=ay,
                search=search or None,
                ans_type=ans_type or None,
            )
        except Exception:
            logger.exception("Failed to load questions list for ay=%s", ay)
            questions = []

        # KPIs
        total_q = len(questions)
        total_districts = sum(q.get("districts_answered", 0) for q in questions)
        avg_acceptance = None
        rates = [q["acceptance_rate"] for q in questions if q.get("acceptance_rate") is not None]
        if rates:
            avg_acceptance = round(sum(rates) / len(rates), 1)

        kpis = KpiRow(
            KpiCard(str(total_q), "Questions"),
            KpiCard(str(total_districts), "Total Answers"),
            KpiCard(f"{avg_acceptance}%" if avg_acceptance is not None else "--", "Avg Acceptance"),
        )

        # Filters
        ay_select = Select(
            *[
                Option(format_ay(a), value=str(a), selected=("selected" if (a == ay) else None))
                for a in AY_OPTIONS
            ],
            name="ay",
            hx_get="/mc/questions",
            hx_target="#questions-content",
            hx_include="[name='search'],[name='ans_type']",
            hx_push_url="true",
            cls="uk-select uk-form-small select-sm",
        )

        search_input = Input(
            name="search",
            type="text",
            placeholder="Search questions...",
            value=search,
            hx_get="/mc/questions",
            hx_trigger="keyup changed delay:300ms",
            hx_target="#questions-content",
            hx_include="[name='ay'],[name='ans_type']",
            hx_push_url="true",
            cls="uk-input search-field",
        )

        filter_bar = Div(ay_select, search_input, cls="filter-bar")

        # Table
        table = _build_questions_table(questions, ay)

        if request.headers.get("hx-request"):
            # The search / AY triggers target #questions-content (the table area).
            # Return ONLY the table for that swap, plus an out-of-band refresh of the
            # KPI row so the headline counts track the filter — without re-rendering
            # the filter bar (which would recreate and unfocus the search box).
            return table, Div(kpis, id="questions-kpis", hx_swap_oob="true")

        content = Div(
            Div(kpis, id="questions-kpis"),
            filter_bar,
            Div(table, id="questions-content"),
        )

        return Layout(
            "Questions",
            f"{total_q} questions -- {format_ay(ay)}",
            content,
            section="mc",
            sub_nav="/mc/questions",
            user=user,
            show_heading=False,
        )

    @rt("/mc/questions/{q_id:int}")
    def get_question_detail(request: Request, q_id: int, ay: int = DEFAULT_AY):
        """Single question across all districts."""
        user, deny = require_section(request, "mc")
        if deny:
            return deny
        try:
            question = get_question_text(q_id)
        except Exception:
            logger.exception("Failed to load question text for q_id=%s", q_id)
            question = None

        if not question:
            return Layout(
                "Not Found", "",
                P("Question not found."),
                section="mc", sub_nav="/mc/questions",
                user=user,
            )

        try:
            districts = get_question_cross_district(q_id, ay)
        except Exception:
            logger.exception("Failed to load cross-district data for q_id=%s, ay=%s", q_id, ay)
            districts = []

        # Stats
        total = len(districts)
        accepted = sum(1 for d in districts if d.get("status") in ("approved", "accepted"))
        rejected = sum(1 for d in districts if d.get("status") in ("incorrect", "rejected"))
        unreviewed = total - accepted - rejected

        kpis = KpiRow(
            KpiCard(str(total), "Districts"),
            KpiCard(str(accepted), "Accepted"),
            KpiCard(str(rejected), "Rejected"),
            KpiCard(str(unreviewed), "Unreviewed"),
        )

        # AY selector
        ay_select = Select(
            *[Option(format_ay(a), value=str(a), selected=("selected" if (a == ay) else None)) for a in AY_OPTIONS],
            name="ay",
            hx_get=f"/mc/questions/{q_id}",
            hx_target="#question-detail-content",
            hx_push_url="true",
            cls="uk-select uk-form-small select-sm",
        )

        # Table
        table = _build_cross_district_table(districts, q_id, ay)

        content = Div(
            Div(
                QIdBadge(q_id, question.get("q_num")),
                Span(" "),
                Span(question["q_text"], cls="question-text"),
                cls="mb-sm",
            ),
            Span(
                f"Answer type: {question.get('q_ans_type', '--')}",
                cls="review-meta",
            ),
            kpis,
            Div(ay_select, cls="filter-bar"),
            Div(table, id="question-detail-content"),
        )

        return Layout(
            q_label(q_id, question.get("q_num")),
            question["q_text"][:80],
            content,
            section="mc",
            sub_nav="/mc/questions",
            breadcrumb=[
                ("Questions", "/mc/questions"),
                (q_label(q_id, question.get("q_num")), None),
            ],
            user=user,
        )


def _build_questions_table(questions, ay):
    """Build the questions list table."""
    if not questions:
        return P("No questions found.", cls="uk-text-muted uk-text-center uk-padding")

    rows = []
    for q in questions:
        q_text = q.get("q_text", "")
        q_text_display = q_text[:100] + ("..." if len(q_text) > 100 else "")
        rate = q.get("acceptance_rate")
        rate_display = f"{rate}%" if rate is not None else "--"

        rows.append(
            Tr(
                Td(QIdBadge(q["q_id"], q.get("q_num")), cls="uk-table-shrink"),
                Td(q_text_display, cls="uk-table-expand"),
                Td(q.get("q_ans_type", "--"), cls="uk-text-center cell-nowrap"),
                Td(str(q.get("districts_answered", 0)), cls="uk-text-center"),
                Td(rate_display, cls="uk-text-center"),
                cls="clickable-row",
                onclick=f"window.location='/mc/questions/{q['q_id']}?ay={ay}'",
            )
        )

    return Table(
        Thead(
            Tr(
                Th("QNum", cls="uk-table-shrink"),
                Th("Question", cls="uk-table-expand"),
                Th("Type", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Districts", cls="uk-table-shrink uk-text-center"),
                Th("Acceptance", cls="uk-table-shrink uk-text-center"),
            )
        ),
        Tbody(*rows),
        cls=TABLE_CLS,
    )


def _build_cross_district_table(districts, q_id, ay):
    """Build per-district table for a single question."""
    if not districts:
        return P("No district answers found.", cls="uk-text-muted uk-text-center uk-padding")

    rows = []
    for d in districts:
        # Agreement dots
        agreement_cell = Span("--", cls="uk-text-muted")
        if d.get("agreement_pct") is not None and d.get("n_predictions"):
            agreement_cell = ModalDots(total=d["n_predictions"], agreement_pct=d["agreement_pct"])

        status = d.get("status")
        if status in ("approved", "accepted"):
            answer_parts = [Span(d.get("suggested_answer", "--")[:40])]
            if d.get("is_ina"):
                answer_parts.append(Span(" "))
                answer_parts.append(INABadge())
        else:
            answer_parts = [A(
                "Review",
                href=f"/mc/districts/{d['district_id']}/questions/{q_id}?ay={ay}",
                cls="uk-button uk-button-default uk-button-small",
            )]

        rows.append(
            Tr(
                Td(d.get("district_name", ""), cls="cell-nowrap"),
                Td(d.get("state", ""), cls="uk-text-center cell-nowrap"),
                Td(*answer_parts, cls="cell-nowrap"),
                Td(agreement_cell, cls="uk-table-shrink cell-nowrap"),
                Td(StatusBadge(d.get("status")), cls="uk-text-center"),
                cls="clickable-row",
                onclick=f"window.location='/mc/districts/{d['district_id']}/questions/{q_id}?ay={ay}'",
            )
        )

    return Table(
        Thead(
            Tr(
                Th("District", cls="uk-table-expand"),
                Th("State", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Answer", cls="uk-table-shrink cell-nowrap"),
                Th("Agreement", cls="uk-table-shrink cell-nowrap"),
                Th("Status", cls="uk-table-shrink uk-text-center cell-nowrap"),
            )
        ),
        Tbody(*rows),
        cls=TABLE_CLS,
    )
