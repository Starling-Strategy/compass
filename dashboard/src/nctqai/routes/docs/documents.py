"""Documents list page -- /docs

Pipeline health KPI cards + browsable document table with filters.
Ported from document_pipeline.dashboard.routes.documents.
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
from monsterui.all import Card
from starlette.requests import Request

from nctqai.components import (
    AYAlignmentBadge,
    ConfidenceScoreBadge,
    DocumentTypeBadge,
    KpiCard,
    KpiRow,
    ReadabilityBadge,
    TABLE_CLS,
    TypeAlignmentBadge,
)
from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.docs import get_ay_options, get_district_options, get_doc_type_options, get_document_stats, get_documents
from nctqai.utils import format_ay

logger = logging.getLogger(__name__)


def register_routes(rt):

    @rt("/docs")
    def get_documents_page(
        request: Request,
        district: int = 0,
        status: str = "all",
        search: str = "",
        sort: str = "src_id_desc",
        ay: int = 0,
        doc_type: str = "all",
    ):
        """Documents list with health cards and filterable table."""
        user, deny = require_section(request, "docs")
        if deny:
            return deny
        district_id = district if district > 0 else None
        ay_filter = ay if ay > 0 else None
        doc_type_filter = doc_type if doc_type != "all" else None

        try:
            stats = get_document_stats(district_id=district_id, ay=ay_filter, doc_type=doc_type_filter)
        except Exception:
            logger.exception("Failed to load document stats")
            from nctqai.models.docs import DocumentStats
            stats = DocumentStats()
        try:
            docs = get_documents(
                district_id=district_id,
                status=status if status != "all" else None,
                search=search or None,
                sort=sort,
                ay=ay_filter,
                doc_type=doc_type_filter,
            )
        except Exception:
            logger.exception("Failed to load documents list")
            docs = []
        try:
            district_options = get_district_options()
        except Exception:
            logger.exception("Failed to load district options")
            district_options = []
        try:
            ay_options = get_ay_options()
        except Exception:
            logger.exception("Failed to load academic year options")
            ay_options = []
        try:
            doc_type_options = get_doc_type_options()
        except Exception:
            logger.exception("Failed to load document type options")
            doc_type_options = []

        # KPI health cards
        kpis = KpiRow(
            KpiCard(str(stats.total), "Total Documents"),
            KpiCard(f"{stats.success_pct}%", "Extraction Success"),
            KpiCard(
                f"{stats.avg_confidence:.2f}" if stats.avg_confidence else "\u2014",
                "Avg Confidence",
            ),
            _readability_kpi(stats),
            KpiCard(f"{stats.ay_alignment_pct}%", "AY Alignment"),
            KpiCard(f"{stats.type_alignment_pct}%", "Type Alignment"),
        )

        # Filter bar
        base_url = "/docs"
        _hx_include_no_ay = "[name='district'],[name='status'],[name='search'],[name='sort'],[name='doc_type']"
        _hx_include_no_doc_type = "[name='district'],[name='ay'],[name='status'],[name='search'],[name='sort']"
        _hx_include_no_district = "[name='ay'],[name='status'],[name='search'],[name='sort'],[name='doc_type']"
        _hx_include_all = "[name='district'],[name='ay'],[name='status'],[name='search'],[name='sort'],[name='doc_type']"

        district_select = Select(
            Option("All Districts", value="0", selected=("selected" if (district_id is None) else None)),
            *[
                Option(name, value=str(did), selected=("selected" if (did == district_id) else None))
                for did, name in district_options
            ],
            name="district",
            hx_get=base_url,
            hx_target="#documents-content",
            hx_swap="outerHTML",
            hx_include=_hx_include_no_district,
            hx_push_url="true",
            cls="uk-select uk-form-small select-md",
        )

        ay_select = Select(
            Option("All Years", value="0", selected=("selected" if (ay_filter is None) else None)),
            *[
                Option(format_ay(a), value=str(a), selected=("selected" if (a == ay) else None))
                for a in ay_options
            ],
            name="ay",
            hx_get=base_url,
            hx_target="#documents-content",
            hx_swap="outerHTML",
            hx_include=_hx_include_no_ay,
            hx_push_url="true",
            cls="uk-select uk-form-small select-sm",
        )

        doc_type_select = Select(
            Option("All Types", value="all", selected=("selected" if (doc_type_filter is None) else None)),
            *[
                Option(dt.replace("_", " ").title(), value=dt, selected=("selected" if (dt == doc_type) else None))
                for dt in doc_type_options
            ],
            name="doc_type",
            hx_get=base_url,
            hx_target="#documents-content",
            hx_swap="outerHTML",
            hx_include=_hx_include_no_doc_type,
            hx_push_url="true",
            cls="uk-select uk-form-small select-md",
        )

        status_tabs = Div(
            *[
                A(
                    label,
                    hx_get=f"{base_url}?status={val}",
                    hx_target="#documents-content",
                    hx_swap="outerHTML",
                    hx_include=_hx_include_all.replace(",[name='status']", ""),
                    hx_push_url="true",
                    cls="active" if status == val else "",
                )
                for label, val in [
                    ("All", "all"),
                    ("Success", "success"),
                    ("Failed", "failed"),
                    ("Pending", "pending"),
                ]
            ],
            cls="filter-tabs",
        )

        search_input = Input(
            name="search",
            type="text",
            placeholder="Search by title or filename...",
            value=search,
            hx_get=base_url,
            hx_trigger="keyup changed delay:300ms",
            hx_target="#documents-content",
            hx_swap="outerHTML",
            hx_include=_hx_include_all.replace(",[name='search']", ""),
            hx_push_url="true",
            cls="uk-input search-field",
        )

        filter_bar = Div(
            district_select,
            ay_select,
            doc_type_select,
            status_tabs,
            search_input,
            cls="filter-bar",
        )

        # Hidden inputs for HTMX includes
        sort_input = Input(name="sort", type="hidden", value=sort)

        # Table
        table = _build_table(docs, district or 0, status, search, sort)

        content = Div(
            kpis,
            filter_bar,
            sort_input,
            Div(table, id="documents-table", cls="table-responsive"),
            id="documents-content",
        )

        # HTMX partial swap — return just the content div
        if request.headers.get("hx-request"):
            return content

        return Layout(
            "District Documents",
            f"{stats.total} documents indexed",
            content,
            section="docs",
            sub_nav="/docs",
            user=user,
            show_heading=False,
        )


def _readability_kpi(stats):
    """Readability KPI card with mini stacked bar."""
    total = stats.readability_good + stats.readability_fair + stats.readability_poor
    if total == 0:
        return KpiCard("\u2014", "Readability")

    good_pct = 100 * stats.readability_good / total
    fair_pct = 100 * stats.readability_fair / total

    bar = Div(
        Div(style=f"width: {good_pct}%; height: 100%;"),
        Div(style=f"width: {fair_pct}%; height: 100%;"),
        Div(style=f"width: {100 - good_pct - fair_pct}%; height: 100%;"),
        cls="readability-bar",
    )
    counts = Span(
        f"{stats.readability_good}G / {stats.readability_fair}F / {stats.readability_poor}P",
        cls="text-xs text-muted",
    )

    return Card(
        Div("Readability", cls="stat-card-label"),
        counts,
        bar,
        cls="flex-1",
        style="min-width: 160px; text-align: center;",
    )


def _build_table(docs, district, status, search, sort):
    """Build the document table."""
    if not docs:
        return P(
            "No documents found.",
            cls="uk-text-muted uk-text-center empty-state",
        )

    rows = []
    for d in docs:
        rows.append(
            Tr(
                Td(d.district_name or "", cls="cell-nowrap"),
                Td(
                    A(
                        d.display_title[:60]
                        + ("..." if len(d.display_title) > 60 else ""),
                        href=f"/docs/{d.src_id}",
                        target="_blank",
                        cls="uk-link-text font-semibold",
                    ),
                ),
                Td(DocumentTypeBadge(d.ai_document_type), cls="uk-text-center"),
                Td(d.ay_display, cls="uk-text-center cell-nowrap"),
                Td(
                    AYAlignmentBadge(d.human_ay_ids, d.ai_ay_ids),
                    cls="uk-text-center",
                ),
                Td(
                    TypeAlignmentBadge(d.src_type, d.ai_document_type),
                    cls="uk-text-center",
                ),
                Td(ReadabilityBadge(d.ai_readability), cls="uk-text-center"),
                Td(ConfidenceScoreBadge(d.ai_confidence), cls="uk-text-center"),
                Td(
                    f"{d.text_length:,}" if d.text_length else "\u2014",
                    cls="uk-text-right uk-text-muted",
                ),
                cls="clickable-row",
                onclick=f"window.location='/docs/{d.src_id}'",
            )
        )

    return Table(
        Thead(
            Tr(
                Th("District", cls="uk-table-shrink cell-nowrap"),
                Th("Title", cls="uk-table-expand"),
                Th(
                    "Type",
                    cls="uk-table-shrink uk-text-center cell-nowrap",
                ),
                Th(
                    "Academic Year",
                    cls="uk-table-shrink uk-text-center cell-nowrap",
                ),
                Th(
                    "AY Align",
                    cls="uk-table-shrink uk-text-center cell-nowrap",
                ),
                Th(
                    "Type Align",
                    cls="uk-table-shrink uk-text-center cell-nowrap",
                ),
                Th(
                    "Readability",
                    cls="uk-table-shrink uk-text-center cell-nowrap",
                ),
                Th(
                    "Confidence",
                    cls="uk-table-shrink uk-text-center cell-nowrap",
                ),
                Th(
                    "Length",
                    cls="uk-table-shrink uk-text-right cell-nowrap",
                ),
            )
        ),
        Tbody(*rows),
        cls=TABLE_CLS,
    )
