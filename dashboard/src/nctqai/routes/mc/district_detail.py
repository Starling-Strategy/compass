"""District detail page -- /mc/districts/{district_id}

Single district's policy progress with review KPIs and progress bars.
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

from nctqai.components import AYSelect, KpiCard, KpiRow, ProgressBar, TABLE_CLS
from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.mc import get_district_info, get_policy_progress
from nctqai.utils import DEFAULT_AY, format_ay

logger = logging.getLogger(__name__)


def register(rt):

    @rt("/mc/districts/{district_id:int}")
    def get_district_detail(request: Request, district_id: int, ay: int = DEFAULT_AY):
        """District detail with policy progress table."""
        user, deny = require_section(request, "mc")
        if deny:
            return deny
        try:
            info = get_district_info(district_id)
            name = info["district_name"]
            state = info["state"]
        except Exception:
            logger.exception("Failed to load district info for district_id=%s", district_id)
            name = f"District {district_id}"
            state = None
        try:
            policies = get_policy_progress(district_id, ay)
        except Exception:
            logger.exception("Failed to load policy progress for district_id=%s, ay=%s", district_id, ay)
            policies = []

        # Aggregate KPIs
        total_accepted = sum(p.accepted for p in policies)
        total_rejected = sum(p.rejected for p in policies)
        total_unreviewed = sum(p.unreviewed for p in policies)
        total_q = total_accepted + total_rejected + total_unreviewed

        accepted_pct = f"{round(total_accepted / total_q * 100, 1)}%" if total_q else "--"
        rejected_pct = f"{round(total_rejected / total_q * 100, 1)}%" if total_q else "--"
        unreviewed_pct = f"{round(total_unreviewed / total_q * 100, 1)}%" if total_q else "--"

        kpis = KpiRow(
            KpiCard(
                str(total_accepted),
                "Accepted",
                accepted_pct,
            ),
            KpiCard(
                str(total_rejected),
                "Rejected",
                rejected_pct,
            ),
            KpiCard(
                str(total_unreviewed),
                "Unreviewed",
                unreviewed_pct,
            ),
        )

        # AY dropdown
        ay_select = AYSelect(ay, f"/mc/districts/{district_id}", "#policy-content")

        view_docs_btn = A(
            "View Documents",
            href=f"/docs?district={district_id}",
            target="_blank",
            cls="uk-button uk-button-default uk-button-small",
        )

        filter_bar = Div(ay_select, view_docs_btn, cls="filter-bar")

        # Policy table
        table = _build_policy_table(policies, district_id, ay)

        content = Div(
            kpis,
            filter_bar,
            Div(Div(table, cls="table-card"), id="policy-content"),
        )

        # HTMX partial swap — return just the content div
        if request.headers.get("hx-request"):
            return content

        state_part = f"{state} — " if state else ""
        return Layout(
            name,
            f"{state_part}{format_ay(ay)}",
            content,
            section="mc",
            sub_nav="/mc/districts",
            breadcrumb=[
                ("Districts", "/mc/districts"),
                (name, None),
            ],
            user=user,
        )


def _build_policy_table(policies, district_id, ay):
    """Build the policy progress table."""
    if not policies:
        return P(
            "No predictions found for this district.",
            cls="uk-text-muted uk-text-center empty-state",
        )

    rows = []
    for p in policies:
        total = p.accepted + p.rejected + p.unreviewed
        rows.append(
            Tr(
                Td(
                    A(
                        p.policy_name,
                        href=f"/mc/districts/{district_id}/policies/{p.dpol_id}?ay={ay}",
                        cls="uk-link-text font-semibold",
                    ),
                ),
                Td(str(total), cls="uk-text-center"),
                Td(
                    Span(str(p.accepted), cls="cell-status-green"),
                    cls="uk-text-center",
                ),
                Td(
                    Span(str(p.rejected), cls="cell-status-red"),
                    cls="uk-text-center",
                ),
                Td(
                    Span(str(p.unreviewed), cls="cell-status-muted"),
                    cls="uk-text-center",
                ),
                Td(
                    ProgressBar(p.accepted, p.rejected, p.unreviewed, total),
                    style="min-width: 120px;",
                ),
                cls="clickable-row",
                onclick=f"window.location='/mc/districts/{district_id}/policies/{p.dpol_id}?ay={ay}'",
            )
        )

    return Table(
        Thead(
            Tr(
                Th("Policy", cls="uk-table-expand"),
                Th("Total", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Accepted", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Rejected", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Unreviewed", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Progress", cls="uk-table-shrink cell-nowrap", style="min-width: 120px;"),
            )
        ),
        Tbody(*rows),
        cls=TABLE_CLS,
    )
