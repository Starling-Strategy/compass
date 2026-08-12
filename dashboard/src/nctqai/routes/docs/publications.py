"""Publications pages -- /docs/publications and /docs/publications/{pub_id}

NCTQ publications list and detail views.
"""

from fasthtml.common import (
    A,
    Div,
    Input,
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

from nctqai.components import TABLE_CLS
from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.docs import get_publication_detail, get_publications


def register_routes(rt):

    @rt("/docs/publications")
    def get_publications_page(request: Request, search: str = ""):
        """NCTQ publications list with search filter."""
        user, deny = require_section(request, "docs")
        if deny:
            return deny
        pubs = get_publications(search=search or None)

        # Search bar
        search_input = Input(
            name="search",
            type="text",
            placeholder="Search by title or author...",
            value=search,
            hx_get="/docs/publications",
            hx_trigger="keyup changed delay:300ms",
            hx_target="#publications-content",
            hx_push_url="true",
            cls="uk-input search-field mb-md",
        )

        # Table
        table = _build_table(pubs)
        count_label = f"{len(pubs)} publications"

        if request.headers.get("hx-request"):
            # Search targets #publications-content (the table). Return ONLY the table
            # for that swap, plus an out-of-band refresh of the sub-nav count — without
            # re-rendering the whole page (which previously nested a full nav + header
            # + second search box inside the content div).
            return table, P(
                count_label,
                id="sub-nav-annotation",
                cls="sub-nav-description uk-container uk-padding",
                hx_swap_oob="true",
            )

        content = Div(
            search_input,
            Div(table, id="publications-content"),
        )

        return Layout(
            "NCTQ Publications",
            count_label,
            content,
            section="docs",
            sub_nav="/docs/publications",
            show_heading=False,
            user=user,
        )

    @rt("/docs/publications/{pub_id}")
    def get_publication_detail_page(request: Request, pub_id: str):
        """Publication detail page."""
        user, deny = require_section(request, "docs")
        if deny:
            return deny
        pub = get_publication_detail(pub_id)
        if not pub:
            return Layout(
                "Not Found",
                "",
                P("Publication not found."),
                section="docs",
                sub_nav="/docs/publications",
                user=user,
            )

        # Back link
        back = Div(
            A("\u2190 Back to Publications", href="/docs/publications", cls="uk-link-text text-sm"),
            cls="uk-margin-bottom",
        )

        # Detail card
        from monsterui.all import Card

        detail_card = Card(
            _field_row("Title", pub.title),
            _field_row("Author", pub.author or "\u2014"),
            _field_row("Type", pub.publication_type or "\u2014"),
            _field_row("Published", pub.published_date or "\u2014"),
            _field_row(
                "Tags",
                ", ".join(pub.tags) if pub.tags else "\u2014",
            ),
            _field_row(
                "URL",
                A("Open source", href=pub.url, target="_blank", cls="uk-link-text")
                if pub.url
                else Span("\u2014", cls="uk-text-muted"),
            ),
            header=Div("Publication Details", cls="detail-card-header"),
            cls="uk-margin-bottom",
        )

        content = Div(back, detail_card)

        return Layout(
            pub.title[:60],
            f"ID: {pub.publication_id}",
            content,
            section="docs",
            sub_nav="/docs/publications",
            breadcrumb=[("Publications", "/docs/publications"), (pub.title[:60], None)],
            user=user,
        )


def _build_table(pubs):
    """Build the publications table."""
    if not pubs:
        return P(
            "No publications found.",
            cls="uk-text-muted uk-text-center empty-state",
        )

    rows = []
    for pub in pubs:
        rows.append(
            Tr(
                Td(
                    A(
                        pub.title[:80] + ("..." if len(pub.title) > 80 else ""),
                        href=f"/docs/publications/{pub.publication_id}",
                        cls="uk-link-text font-semibold",
                        target="_blank",
                    ),
                ),
                Td(pub.author or "\u2014"),
                Td(pub.publication_type or "\u2014", cls="cell-nowrap"),
                Td(pub.published_date or "\u2014", cls="cell-nowrap"),
                Td(
                    ", ".join(pub.tags[:3]) + ("..." if pub.tags and len(pub.tags) > 3 else "")
                    if pub.tags
                    else "\u2014",
                    cls="uk-text-muted",
                ),
                cls="clickable-row",
                onclick=f"window.location='/docs/publications/{pub.publication_id}'",
            )
        )

    return Table(
        Thead(
            Tr(
                Th("Title", cls="uk-table-expand"),
                Th("Author", cls="uk-table-shrink cell-nowrap"),
                Th("Type", cls="uk-table-shrink cell-nowrap"),
                Th("Published", cls="uk-table-shrink cell-nowrap"),
                Th("Tags", cls="uk-table-shrink cell-nowrap"),
            )
        ),
        Tbody(*rows),
        cls=TABLE_CLS,
    )


def _field_row(label, value):
    """Label: value row inside a card."""
    return Div(
        Span(label, cls="detail-field-label"),
        Span(value) if isinstance(value, str) else value,
        cls="detail-field-row",
    )
