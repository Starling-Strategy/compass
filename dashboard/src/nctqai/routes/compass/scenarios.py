"""Scenarios page — /compass/scenarios

Read-only B-spine case browser with type/primary-dimension filters and launch links.
"""

import hmac
import logging
from hashlib import sha256
from time import time
from urllib.parse import urlencode, urlsplit

from fasthtml.common import (
    A,
    Div,
    HtmxResponseHeaders,
    Option,
    P,
    Script,
    Select,
    Span,
    Style,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
)
from starlette.requests import Request

from nctqai.components import KpiCard, KpiRow, TABLE_CLS
from nctqai.config import Config
from nctqai.layout import Layout
from nctqai.routes.compass._helpers import (
    require_compass_admin,
    require_compass_admin_partial,
    run_in_thread,
)
from nctqai.services.compass_scenarios import list_dashboard_scenarios

logger = logging.getLogger(__name__)

# Type badge colors
TYPE_BADGE_CLS = {
    "golden": "badge badge-accepted",
    "dangerous": "badge badge-rejected",
    "boundary": "badge badge-unreviewed",
    "regression": "badge badge-on-hold",
}


def _launch_url(
    case_id: int,
    *,
    settings: Config | None = None,
    now: int | None = None,
) -> str:
    """Build a Compass chat launch URL in debug mode for a B-spine case.

    The frontend fetches the case and runs every input step in one session, so
    editing the case updates every debug link that references it.
    """
    try:
        resolved_settings = settings or Config()
        base = resolved_settings.compass_frontend_url.rstrip("/")
    except Exception:
        resolved_settings = None
        base = "http://localhost:3000"

    params = {"debug": "true", "case_id": str(case_id)}
    if resolved_settings is not None and not _uses_durable_unsigned_case_links(base):
        params.update(_signed_case_params(case_id, resolved_settings, now=now))
    return f"{base}/?{urlencode(params)}"


def _uses_durable_unsigned_case_links(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host in {"staging-compass.nctq.ai", "localhost", "127.0.0.1"}


def _signed_case_params(
    case_id: int,
    settings: Config,
    *,
    now: int | None = None,
) -> dict[str, str]:
    secret = settings.compass_scenario_link_secret.get_secret_value().strip()
    if not secret:
        return {}

    issued_at = int(now if now is not None else time())
    exp = issued_at + settings.compass_scenario_link_ttl_seconds
    payload = f"{case_id}.{exp}"
    signature = hmac.new(secret.encode(), payload.encode(), sha256).hexdigest()
    return {"case_exp": str(exp), "case_sig": signature}


def _type_badge(scenario_type: str) -> Span:
    """Render a colored type badge."""
    cls = TYPE_BADGE_CLS.get(scenario_type, "badge badge-unreviewed")
    return Span(scenario_type, cls=cls)


async def _build_scenarios_content(type: str, feature: str) -> Div:
    """Build the inner #scenarios-content Div used by both the page and the
    HTMX /list partial. Returns ready-to-render content with no Layout shell.
    """
    try:
        scenarios = await run_in_thread(
            list_dashboard_scenarios,
            scenario_type=type or None,
            feature=feature or None,
        )
    except Exception:
        logger.exception("Failed to load scenarios")
        return Div(
            P("Could not load scenario inventory.", cls="text-red-600 uk-text-center"),
            P(
                "The dashboard could not read compass.scenarios and compass.cases from the Compass database.",
                cls="uk-text-muted uk-text-center text-sm",
            ),
            id="scenarios-content",
        )

    total = len(scenarios)
    type_counts: dict[str, int] = {}
    dimensions: set[str] = set()
    total_criteria = 0
    for s in scenarios:
        type_counts[s.type] = type_counts.get(s.type, 0) + 1
        if s.feature:
            dimensions.add(s.feature)
        total_criteria += len(s.criteria)

    type_summary = ", ".join(f"{count} {t}" for t, count in sorted(type_counts.items()))
    kpis = KpiRow(
        KpiCard(str(total), "Cases", subtitle=type_summary or "None"),
        KpiCard(str(len(dimensions)), "Primary Dimensions"),
        KpiCard(str(total_criteria), "Total Criteria"),
    )

    type_options = [Option("All Types", value="")]
    for t in sorted(type_counts.keys()):
        type_options.append(Option(t.title(), value=t, selected=("selected" if t == type else None)))

    feature_options = [Option("All Primary Dimensions", value="")]
    for f in sorted(dimensions):
        feature_options.append(Option(f, value=f, selected=("selected" if f == feature else None)))

    filters = Div(
        Select(
            *type_options,
            name="type",
            hx_get="/compass/scenarios/list",
            hx_target="#scenarios-content",
            hx_swap="outerHTML",
            hx_push_url="true",
            hx_include="[name='feature']",
            cls="uk-select uk-form-small select-sm",
        ),
        Select(
            *feature_options,
            name="feature",
            hx_get="/compass/scenarios/list",
            hx_target="#scenarios-content",
            hx_swap="outerHTML",
            hx_push_url="true",
            hx_include="[name='type']",
            cls="uk-select uk-form-small select-sm",
        ),
        cls="filter-bar",
    )

    grouped: dict[str | None, list] = {}
    for s in scenarios:
        grouped.setdefault(s.feature, []).append(s)

    rows = []
    seq = 0
    for feat, group in grouped.items():
        if feat:
            rows.append(
                Tr(
                    Td(
                        Span(feat, cls="font-semibold"),
                        Span(f" ({len(group)})", cls="uk-text-muted text-sm"),
                        colspan="8",
                        cls="feature-group-header",
                    ),
                    cls="feature-group-row",
                )
            )
        for s in group:
            seq += 1
            msg_preview = s.initial_user_message[:80] + "..." if len(s.initial_user_message) > 80 else s.initial_user_message
            criteria_count = len(s.criteria)
            launch = _launch_url(s.case_id or s.id)

            rows.append(
                Tr(
                    Td(str(seq), cls="uk-text-muted uk-text-center"),
                    Td(
                        Div(s.scenario_title, cls="font-medium"),
                        cls="cell-nowrap",
                    ),
                    Td(
                        Span(msg_preview, cls="uk-text-muted text-sm font-italic"),
                    ),
                    Td(s.what_we_are_testing or "--", cls="text-sm"),
                    Td(_type_badge(s.type)),
                    Td(
                        Span(str(criteria_count), cls="badge badge-unreviewed") if criteria_count else Span("--", cls="uk-text-muted"),
                        cls="uk-text-center",
                    ),
                    Td(
                        A("Launch →", href=launch, target="_blank",
                          cls="uk-link-text text-sm"),
                        cls="uk-text-center cell-nowrap",
                    ),
                    Td(
                        A("\U0001f517", href=f"#case-{s.case_id or s.id}",
                          onclick=f"navigator.clipboard.writeText(location.origin+'/compass/scenarios#case-{s.case_id or s.id}');this.textContent='\\u2713';setTimeout(()=>this.textContent='\\ud83d\\udd17',1500);return false;",
                          title="Copy link to this case",
                          cls="uk-link-text text-sm copy-link"),
                        cls="uk-text-center cell-nowrap",
                    ),
                    id=f"case-{s.case_id or s.id}",
                )
            )

    table = (
        Div(
            Table(
                Thead(
                    Tr(
                        Th("#", cls="uk-table-shrink uk-text-center"),
                        Th("Case", cls="uk-table-expand"),
                        Th("Message", cls="uk-table-expand"),
                        Th("Testing", cls="uk-table-expand"),
                        Th("Type", cls="uk-table-shrink"),
                        Th("Criteria", cls="uk-table-shrink uk-text-center"),
                        Th("", cls="uk-table-shrink"),
                        Th("", cls="uk-table-shrink"),
                    ),
                ),
                Tbody(*rows),
                cls=TABLE_CLS,
            ),
            cls="table-responsive",
        )
        if rows
        else P("No scenarios found.", cls="uk-text-muted uk-text-center empty-state")
    )

    highlight_css = Style("""
        .highlight { animation: flash 2.5s ease-out; }
        @keyframes flash {
            0%, 20% { background-color: rgba(59,130,246,0.15); }
            100% { background-color: transparent; }
        }
        .copy-link { cursor:pointer; text-decoration:none; }
    """)

    # Run on initial page load *and* after every HTMX swap, so a deep-link
    # like /compass/scenarios#case-47 still flashes the row when the
    # filter selects re-render the table.
    scroll_js = Script("""
        function scenarioScrollToHash() {
            const h = location.hash;
            if (!h || !h.startsWith('#case-')) return;
            const el = document.getElementById(h.slice(1));
            if (!el) return;
            el.scrollIntoView({behavior:'smooth', block:'center'});
            el.classList.add('highlight');
            setTimeout(() => el.classList.remove('highlight'), 2500);
        }
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', scenarioScrollToHash);
        } else {
            scenarioScrollToHash();
        }
        document.body.addEventListener('htmx:afterSwap', scenarioScrollToHash);
    """)

    return Div(highlight_css, scroll_js, kpis, filters, table, id="scenarios-content")


def register(rt):

    @rt("/compass/scenarios")
    async def get_scenarios_page(request: Request, type: str = "", feature: str = ""):
        """Scenarios browser with type/primary-dimension filters."""
        user, deny = require_compass_admin(request)
        if deny:
            return deny

        content = await _build_scenarios_content(type, feature)

        return Layout(
            "Scenarios",
            "",
            content,
            section="compass",
            sub_nav="/compass/scenarios",
            user=user,
            show_heading=False,
        )

    @rt("/compass/scenarios/list")
    async def get_scenarios_list_partial(request: Request, type: str = "", feature: str = ""):
        """HTMX partial — re-render the scenarios content (kpis + filters + table)
        without the Layout shell when filters change.

        The Selects on the page set ``hx-push-url="true"``, which would normally
        push *this* request URL (`/compass/scenarios/list?...`) into the
        address bar. That URL only returns the bare partial — refresh = broken
        page. Override with HX-Push-Url so the URL bar gets the equivalent
        full-page URL (`/compass/scenarios?...`) instead.
        """
        _, deny = require_compass_admin_partial(request)
        if deny:
            return deny

        content = await _build_scenarios_content(type, feature)

        params = {k: v for k, v in (("type", type), ("feature", feature)) if v}
        push_url = "/compass/scenarios" + (f"?{urlencode(params)}" if params else "")
        return content, HtmxResponseHeaders(push_url=push_url)
