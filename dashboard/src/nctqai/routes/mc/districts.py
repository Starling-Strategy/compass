"""Districts list page -- /mc/districts

All districts with review progress stats, state filter, unreviewed toggle, AY dropdown.
"""

import logging

from fasthtml.common import (
    A,
    Div,
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

from nctqai.components import AYSelect, KpiCard, KpiRow, ProgressBar, TABLE_CLS
from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.mc import get_all_districts, get_state_options
from nctqai.utils import DEFAULT_AY, format_ay

logger = logging.getLogger(__name__)

# Shared hx_include selector — all filter inputs reference each other
_HX_INCLUDE = "[name='ay'],[name='state'],[name='review_status']"


def register(rt):

    @rt("/mc/districts")
    def get_districts_page(
        request: Request,
        ay: int = DEFAULT_AY,
        state: str = "all",
        review_status: str = "all",
        unreviewed_only: str = "",  # kept for backward compat
    ):
        """Districts list with KPI cards, state filter, review status filter, and AY filter."""
        user, deny = require_section(request, "mc")
        if deny:
            return deny
        try:
            all_districts = get_all_districts(ay)
        except Exception:
            logger.exception("Failed to load districts for ay=%s", ay)
            all_districts = []

        # Backward compat: old unreviewed_only param maps to review_status
        if unreviewed_only and review_status == "all":
            review_status = "has_unreviewed"

        # Client-side filtering
        districts = all_districts
        if state != "all":
            districts = [d for d in districts if d.state == state]
        if review_status == "has_unreviewed":
            districts = [d for d in districts if d.unreviewed > 0]
        elif review_status == "fully_reviewed":
            districts = [d for d in districts if d.unreviewed == 0 and d.total > 0]

        # Aggregate KPIs (always based on full dataset)
        total_districts = len(all_districts)
        total_reviewed = sum(d.accepted + d.rejected for d in all_districts)
        total_questions = sum(d.total for d in all_districts)
        total_accepted = sum(d.accepted for d in all_districts)
        acceptance_rate = (
            f"{round(total_accepted / total_reviewed * 100, 1)}%"
            if total_reviewed > 0
            else "--"
        )

        kpis = KpiRow(
            KpiCard(str(total_districts), "Districts"),
            KpiCard(
                f"{total_reviewed}/{total_questions}",
                "Reviewed",
                f"{round(total_reviewed / total_questions * 100, 1)}% complete" if total_questions else "",
            ),
            KpiCard(acceptance_rate, "Acceptance Rate"),
        )

        # AY dropdown
        ay_select = AYSelect(ay, "/mc/districts", "#districts-content", _HX_INCLUDE)

        # State dropdown
        try:
            states = get_state_options(ay)
        except Exception:
            logger.exception("Failed to load state options for ay=%s", ay)
            states = []

        state_select = Div(
            Span("State", cls="filter-group-label"),
            Select(
                Option("All States", value="all", selected=("selected" if (state == "all") else None)),
                *[
                    Option(s, value=s, selected=("selected" if (s == state) else None))
                    for s in states
                ],
                name="state",
                hx_get="/mc/districts",
                hx_target="#districts-content",
                hx_include=_HX_INCLUDE,
                hx_push_url="true",
                cls="uk-select uk-form-small select-sm",
            ),
            cls="filter-group",
        )

        # Review status filter
        status_select = Div(
            Span("Review Status", cls="filter-group-label"),
            Select(
                Option("All Districts", value="all", selected=("selected" if (review_status == "all") else None)),
                Option("Has Unreviewed", value="has_unreviewed", selected=("selected" if (review_status == "has_unreviewed") else None)),
                Option("Fully Reviewed", value="fully_reviewed", selected=("selected" if (review_status == "fully_reviewed") else None)),
                name="review_status",
                hx_get="/mc/districts",
                hx_target="#districts-content",
                hx_include=_HX_INCLUDE,
                hx_push_url="true",
                cls="uk-select uk-form-small select-sm",
            ),
            cls="filter-group",
        )

        filter_bar = Div(ay_select, state_select, status_select, cls="filter-bar")

        # Table
        table = _build_districts_table(districts, ay)

        content = Div(
            kpis,
            filter_bar,
            Div(table, cls="table-card"),
            id="districts-content",
        )

        # HTMX partial swap — return just the content div
        if request.headers.get("hx-request"):
            return content

        return Layout(
            "Metric Calculator",
            f"District review progress -- {format_ay(ay)}",
            content,
            section="mc",
            sub_nav="/mc/districts",
            user=user,
            show_heading=False,
        )


def _build_districts_table(districts, ay):
    """Build the districts table."""
    if not districts:
        return P(
            "No districts found with predictions.",
            cls="uk-text-muted uk-text-center uk-padding",
        )

    rows = []
    for d in districts:
        rows.append(
            Tr(
                Td(
                    A(
                        d.district_name,
                        href=f"/mc/districts/{d.district_id}?ay={ay}",
                        cls="uk-link-text font-semibold",
                    ),
                    cls="cell-nowrap",
                ),
                Td(Span(d.state or "--", cls="state-badge"), cls="uk-text-center cell-nowrap"),
                Td(
                    Span(str(d.accepted), cls="cell-status-green"),
                    cls="uk-text-center",
                ),
                Td(
                    Span(str(d.rejected), cls="cell-status-red"),
                    cls="uk-text-center",
                ),
                Td(
                    Span(str(d.unreviewed), cls="cell-status-muted"),
                    cls="uk-text-center",
                ),
                Td(
                    Div(
                        Span(f"{d.review_pct}%", cls="review-pct"),
                        ProgressBar(d.accepted, d.rejected, d.unreviewed, d.total),
                        cls="review-cell",
                    ),
                    style="min-width: 132px;",
                ),
                cls="clickable-row",
                onclick=f"window.location='/mc/districts/{d.district_id}?ay={ay}'",
            )
        )

    return Table(
        Thead(
            Tr(
                Th("District", cls="uk-table-expand"),
                Th("State", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Accepted", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Rejected", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Unreviewed", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Review %", cls="uk-table-shrink cell-nowrap", style="min-width: 120px;"),
            )
        ),
        Tbody(*rows),
        cls=TABLE_CLS,
    )
