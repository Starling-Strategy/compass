"""Home page -- /

Live KPI cards linking to each dashboard section.
"""

import logging

from fasthtml.common import A, Div, Option, P, Select, Span
from starlette.requests import Request

from nctqai.layout import Layout
from nctqai.services.home import get_docs_stats, get_mc_stats, get_pa_stats
from nctqai.utils import AY_OPTIONS, DEFAULT_AY, format_ay

logger = logging.getLogger(__name__)


def _section_card(title, value, label, detail, href, accent_cls=""):
    """Clickable KPI card for the home page."""
    return A(
        Div(
            Div(Div(title, cls="home-card-title"), cls="home-card-header"),
            Div(
                Div(str(value), cls="home-card-value"),
                Div(label, cls="home-card-label"),
            ),
            Div(detail, cls="home-card-detail") if detail else "",
            cls=f"home-card {accent_cls}",
        ),
        href=href,
        cls="home-card-link",
    )


def register_home_routes(rt):

    @rt("/")
    def home(request: Request, ay: int = DEFAULT_AY):
        """Home page with live cross-section KPIs."""
        user = request.state.user
        first_name = user.name.split()[0] if user and user.name else "there"

        # Metric Calculator
        try:
            mc = get_mc_stats(ay)
        except Exception:
            logger.exception("Failed to load metric calculator stats for ay=%s", ay)
            mc = {"total": 0, "accepted": 0, "rejected": 0, "unreviewed": 0, "districts": 0}

        mc_total = mc.get("total", 0)
        mc_reviewed = mc.get("accepted", 0) + mc.get("rejected", 0)
        mc_pct = f"{round(mc_reviewed / mc_total * 100, 1)}%" if mc_total else "--"

        # Documents
        try:
            docs = get_docs_stats()
        except Exception:
            logger.exception("Failed to load document stats")
            docs = {"total": 0, "processed": 0, "failed": 0}

        docs_total = docs.get("total", 0)
        docs_pct = (
            f"{round(docs.get('processed', 0) / docs_total * 100, 1)}%"
            if docs_total
            else "--"
        )

        # Compass (only loaded if user has access)
        pa = None
        if user and user.can_access("compass"):
            try:
                pa = get_pa_stats()
            except Exception:
                logger.exception("Failed to load compass stats")
                pa = {"conversations": 0, "messages": 0}

        # AY selector
        ay_select = Select(
            *[
                Option(format_ay(a), value=str(a), selected=("selected" if (a == ay) else None))
                for a in AY_OPTIONS
            ],
            name="ay",
            hx_get="/",
            hx_target="#home-content",
            hx_push_url="true",
            cls="uk-select uk-form-small select-sm",
        )

        # Header with greeting + AY selector
        header = Div(
            Div(
                Div(f"Welcome back, {first_name}", cls="home-greeting"),
                P(f"Here's your {format_ay(ay)} overview.", cls="home-greeting-sub"),
                cls="home-greeting-block",
            ),
            Div(
                Span("Academic Year", cls="home-ay-label"),
                ay_select,
                cls="home-ay-group",
            ),
            cls="home-header",
        )

        # Section cards — only shown if the user has access to that section
        cards = []

        if user and user.can_access("mc"):
            cards.append(_section_card(
                "Metric Calculator", mc.get("districts", 0), "Districts Tracked",
                f"{mc_reviewed}/{mc_total} reviewed ({mc_pct})",
                "/mc/districts", "home-card-blue",
            ))

        if user and user.can_access("docs"):
            cards.append(_section_card(
                "Documents", f"{docs_total:,}", "Documents Indexed",
                f"Extraction: {docs_pct} success",
                "/docs", "home-card-green",
            ))

        if pa is not None:
            cards.append(_section_card(
                "Compass", f"{pa.get('conversations', 0):,}", "Conversations",
                f"{pa.get('messages', 0):,} total messages",
                "/compass/overview", "home-card-orange",
            ))

        content = Div(header, Div(*cards, cls="home-cards"), id="home-content")

        return Layout("Dashboard", "", content, user=user, show_heading=False)
