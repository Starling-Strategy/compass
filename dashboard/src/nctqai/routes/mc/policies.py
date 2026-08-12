"""Policies reference pages -- /mc/policies, /mc/policies/{dpol_id}

Policy areas with question counts and cross-district stats.
"""

import logging

from fasthtml.common import (
    A,
    Div,
    P,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
)

from starlette.requests import Request

from nctqai.components import KpiCard, KpiRow, TABLE_CLS
from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.mc import get_policies_with_counts, get_policy_detail, get_subpolicies

logger = logging.getLogger(__name__)


def register(rt):

    @rt("/mc/policies")
    def get_policies_page(request: Request):
        """Policy areas with subpolicy and question counts."""
        user, deny = require_section(request, "mc")
        if deny:
            return deny
        try:
            policies = get_policies_with_counts()
        except Exception:
            logger.exception("Failed to load policies with counts")
            policies = []

        total_policies = len(policies)
        total_subpolicies = sum(p.get("subpolicy_count", 0) for p in policies)
        total_questions = sum(p.get("question_count", 0) for p in policies)

        kpis = KpiRow(
            KpiCard(str(total_policies), "Policies"),
            KpiCard(str(total_subpolicies), "Subpolicies"),
            KpiCard(str(total_questions), "Questions"),
        )

        table = _build_policies_table(policies)

        content = Div(kpis, table)

        return Layout(
            "Policies",
            f"{total_policies} policy areas",
            content,
            section="mc",
            sub_nav="/mc/policies",
            user=user,
            show_heading=False,
        )

    @rt("/mc/policies/{dpol_id:int}")
    def get_policy_detail_page(request: Request, dpol_id: int):
        """Policy detail with subpolicies and question counts."""
        user, deny = require_section(request, "mc")
        if deny:
            return deny
        try:
            policy = get_policy_detail(dpol_id)
        except Exception:
            logger.exception("Failed to load policy detail for dpol_id=%s", dpol_id)
            policy = None

        if not policy:
            return Layout(
                "Not Found", "",
                P("Policy not found."),
                section="mc", sub_nav="/mc/policies",
                user=user,
            )

        try:
            subpolicies = get_subpolicies(dpol_id)
        except Exception:
            logger.exception("Failed to load subpolicies for dpol_id=%s", dpol_id)
            subpolicies = []

        total_questions = sum(s.get("question_count", 0) for s in subpolicies)

        kpis = KpiRow(
            KpiCard(str(len(subpolicies)), "Subpolicies"),
            KpiCard(str(total_questions), "Questions"),
        )

        table = _build_subpolicies_table(subpolicies)

        content = Div(kpis, table)

        return Layout(
            policy.get("dpolicy_name", f"Policy {dpol_id}"),
            f"{len(subpolicies)} subpolicies, {total_questions} questions",
            content,
            section="mc",
            sub_nav="/mc/policies",
            breadcrumb=[
                ("Policies", "/mc/policies"),
                (policy.get("dpolicy_name", f"Policy {dpol_id}"), None),
            ],
            user=user,
        )


def _build_policies_table(policies):
    """Build the policies list table."""
    if not policies:
        return P("No policies found.", cls="uk-text-muted uk-text-center empty-state")

    rows = []
    for p in policies:
        rows.append(
            Tr(
                Td(
                    A(
                        p["dpolicy_name"],
                        href=f"/mc/policies/{p['dpol_id']}",
                        cls="uk-link-text font-semibold",
                    ),
                    cls="uk-table-expand",
                ),
                Td(str(p.get("subpolicy_count", 0)), cls="uk-text-center"),
                Td(str(p.get("question_count", 0)), cls="uk-text-center"),
                cls="clickable-row",
                onclick=f"window.location='/mc/policies/{p['dpol_id']}'",
            )
        )

    return Table(
        Thead(
            Tr(
                Th("Policy", cls="uk-table-expand"),
                Th("Subpolicies", cls="uk-table-shrink uk-text-center cell-nowrap"),
                Th("Questions", cls="uk-table-shrink uk-text-center cell-nowrap"),
            )
        ),
        Tbody(*rows),
        cls=TABLE_CLS,
    )


def _build_subpolicies_table(subpolicies):
    """Build the subpolicies table for a policy detail page."""
    if not subpolicies:
        return P("No subpolicies found.", cls="uk-text-muted uk-text-center empty-state")

    rows = []
    for sp in subpolicies:
        rows.append(
            Tr(
                Td(sp.get("dsubpolicy_name", ""), cls="uk-table-expand"),
                Td(str(sp.get("question_count", 0)), cls="uk-text-center"),
            )
        )

    return Table(
        Thead(
            Tr(
                Th("Subpolicy", cls="uk-table-expand"),
                Th("Questions", cls="uk-table-shrink uk-text-center"),
            )
        ),
        Tbody(*rows),
        cls=TABLE_CLS,
    )
