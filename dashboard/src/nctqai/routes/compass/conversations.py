"""/compass/conversations — list + detail browser for NCTQ staff.

Two-pane page (sidebar + detail) over the canonical compass.* schema. Top of
page is a single smart-search input that handles two patterns:

- Paste anything containing a session UUID (a URL, a Slack thread, a raw ID)
  → if that session exists in compass.chat_sessions, 302 to the detail view.
- Type free-form text → keyword search across ALL message content (user OR
  assistant), multi-word AND.

Sidebar narrows the current list (feedback, date range). The list shows the
100 most recent sessions within the selected range. The shared date-range
control (Today / 7d / 30d / All + a custom "since this date" span) drives the
list via the same HTMX partial the feedback filter uses; its state lives in
the URL query string (?range=… / ?from=…), shared with the Overview page, so a
chosen range is shareable and server-driven. Each conversation has a sharable
deep-link at /compass/conversations/{id} with filter + range state in the
query string.

Companion HTMX partials at /compass/conversations/list and
/compass/conversations/detail keep the page snappy without a full reload.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fasthtml.common import Button, Div, Form, Input
from starlette.requests import Request
from starlette.responses import RedirectResponse

from nctqai.components.compass import ConversationSidebar
from nctqai.components.compass.conversation_detail import (
    build_detail,
    empty_state,
)
from nctqai.components.compass.conversation_list import (
    ConversationListItem,
    SMART_SEARCH_FORM_ID,
    triage_results,
)
from nctqai.components.compass.range_pills import resolve_since, resolve_until
from nctqai.layout import Layout
from nctqai.routes.compass._helpers import (
    require_compass,
    require_compass_partial,
    run_in_thread,
)
from nctqai.services.compass_conversations import (
    TRIAGE_TABS,
    count_conversations_by_tab,
    extract_session_id,
    get_conversation_summary,
    list_conversations,
    session_exists,
)
from nctqai.services.compass_reports_client import create_report

logger = logging.getLogger(__name__)


def _smart_search_bar(
    *,
    current_value: str,
    not_found_uuid: str | None,
) -> Div:
    """Big top-of-page search input. Submits a GET form to the same page.

    The live sidebar controls are associated to this form with HTML's ``form``
    attribute. That keeps search submits in sync after HTMX range/filter swaps
    without leaving stale hidden range fields in the top bar.
    """
    not_found_note = None
    if not_found_uuid:
        not_found_note = Div(
            f"No conversation found with ID {not_found_uuid[:8]}…{not_found_uuid[-4:]}.",
            cls="smart-search-error",
        )

    form = Form(
        Input(
            type="search",
            name="q",
            value=current_value,
            placeholder="Paste a session link or search conversations",
            cls="smart-search-input",
            autofocus=True,
            autocomplete="off",
        ),
        Button("Find", type="submit", cls="smart-search-submit"),
        method="get",
        action="/compass/conversations",
        id=SMART_SEARCH_FORM_ID,
        cls="smart-search-form",
    )
    return Div(
        form,
        not_found_note,
        cls="smart-search-bar",
    )


async def _build_sidebar(
    *,
    feedback: str,
    search: str,
    scenario_id: int | None,
    active_session_id: str | None,
    range_key: str,
    custom_since: str,
    custom_until: str,
    tab: str,
) -> tuple[Div, list]:
    """Run the list + tab-count queries and return ``(sidebar, convos)``.

    Returning the underlying convos list as well lets callers reuse the
    rows for the active-session summary instead of re-querying.
    """
    _, since = resolve_since(range_key, custom_since)
    until = resolve_until(custom_until)
    convos = await run_in_thread(
        list_conversations,
        since=since,
        until=until,
        feedback=feedback,
        intent="all",
        search=search,
        scenario_id=scenario_id,
        tab=tab,
        limit=100,
    )
    tab_counts = await run_in_thread(
        count_conversations_by_tab,
        since=since,
        until=until,
        search=search,
        feedback=feedback,
    )
    sidebar = ConversationSidebar(
        conversations=convos,
        active_session_id=active_session_id,
        feedback_value=feedback,
        intent_value="all",
        search_value=search,
        scenario_id=scenario_id,
        range_value=range_key,
        custom_since=custom_since,
        custom_until=custom_until,
        tab_value=tab,
        tab_counts=tab_counts,
    )
    return sidebar, convos


def _scenario_id_param(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tab_param(value: str | None) -> str:
    """Normalize the ``tab`` query param to a known triage tab; default 'all'."""
    return value if value in TRIAGE_TABS else "all"


# ── Routes ─────────────────────────────────────────────────────────────


def register(rt):

    @rt("/compass/conversations")
    async def get_conversations_page(
        request: Request,
        feedback: str = "all",
        intent: str = "all",
        search: str = "",
        scenario_id: str = "",
        session_id: str = "",
        q: str = "",
        range: str = "7d",
        tab: str = "all",
    ):
        # ``from``/``to`` are Python keywords → read them off the query string.
        custom_since = request.query_params.get("from", "")
        custom_until = request.query_params.get("to", "")
        # Smart search: a UUID anywhere in q with a matching session wins;
        # otherwise q falls through to keyword search across all messages.
        not_found_uuid: str | None = None
        if q:
            uuid = extract_session_id(q)
            if uuid:
                if await run_in_thread(session_exists, uuid):
                    params = {
                        k: v
                        for k, v in {
                            "feedback": feedback if feedback != "all" else "",
                            "scenario_id": scenario_id,
                            "range": range if range != "7d" else "",
                            "from": custom_since,
                            "to": custom_until,
                            "tab": tab if tab != "all" else "",
                        }.items()
                        if v
                    }
                    qs = f"?{urlencode(params)}" if params else ""
                    return RedirectResponse(
                        url=f"/compass/conversations/{uuid}{qs}",
                        status_code=302,
                    )
                not_found_uuid = uuid
            else:
                search = q

        return await _render_page(
            request,
            feedback=feedback,
            search=search,
            q=q,
            not_found_uuid=not_found_uuid,
            scenario_id_raw=scenario_id,
            session_id=session_id,
            range_key=range,
            custom_since=custom_since,
            custom_until=custom_until,
            tab=_tab_param(tab),
        )

    @rt("/compass/conversations/list")
    async def get_conversations_list_partial(
        request: Request,
        feedback: str = "all",
        intent: str = "all",
        search: str = "",
        scenario_id: str = "",
        range: str = "7d",
        tab: str = "all",
    ):
        """HTMX partial — re-render the triage strip + conversation list when a
        sidebar filter, the date range, or a triage tab changes. The strip and
        the list swap together (one #compass-convo-results target) so the live
        counts and the rows they describe stay atomic. The smart-search bar is a
        regular form GET, not an HTMX trigger, since UUID matches need a 302.
        """
        _, deny = require_compass_partial(request)
        if deny:
            return deny

        tab = _tab_param(tab)
        # ``from``/``to`` are Python keywords → read them off the query string.
        custom_since = request.query_params.get("from", "")
        custom_until = request.query_params.get("to", "")
        _, since = resolve_since(range, custom_since)
        until = resolve_until(custom_until)
        convos = await run_in_thread(
            list_conversations,
            since=since,
            until=until,
            feedback=feedback,
            intent="all",
            search=search,
            scenario_id=_scenario_id_param(scenario_id),
            tab=tab,
            limit=100,
        )
        tab_counts = await run_in_thread(
            count_conversations_by_tab,
            since=since,
            until=until,
            search=search,
            feedback=feedback,
        )

        filter_state = {
            "feedback": feedback,
            "intent": "all",
            "search": search,
            "scenario_id": scenario_id or "",
            "range": range or "",
            "from": custom_since,
            "to": custom_until,
            "tab": tab,
        }

        items = [
            ConversationListItem(
                session_id=c.session_id,
                preview=c.first_user_message,
                timestamp=c.created_at,
                district_name=c.district_name,
                state=c.state,
                district_count=c.district_count,
                thumbs_up_count=c.thumbs_up_count or 0,
                thumbs_down_count=c.thumbs_down_count or 0,
                message_count=c.message_count or 0,
                is_active=False,
                filter_state=filter_state,
            )
            for c in convos
        ]
        if not items:
            items = [
                Div(
                    Div("No conversations match these filters", cls="compass-empty-title"),
                    Div("Loosen the filters or pick a longer range.", cls="compass-empty-sub"),
                    cls="compass-empty-sidebar",
                )
            ]
        # innerHTML swap into #compass-convo-results (the aria-live region):
        # re-renders the triage strip (fresh counts) AND the list together.
        return triage_results(
            tab_strip_items=items,
            tab_counts=tab_counts,
            active_tab=tab,
            filter_state=filter_state,
        )

    @rt("/compass/conversations/detail")
    async def get_conversation_detail_partial(
        request: Request,
        session_id: str = "",
    ):
        """HTMX partial — re-render just the detail pane on row click."""
        _, deny = require_compass_partial(request)
        if deny:
            return deny

        if not session_id:
            return empty_state()
        convo = await run_in_thread(get_conversation_summary, session_id)
        return build_detail(session_id, convo)

    @rt("/compass/conversations/{session_id}/flag")
    async def flag_conversation(request: Request, session_id: str):
        """File a flag for weekly review against this conversation (DASH-R6).

        nctqai never writes ``compass.*`` directly — the row is inserted by the
        backend's ``submit_report`` endpoint via ``create_report``. Returns just
        the ok/error banner, which HTMX swaps into the detail pane's
        ``#compass-flag-status`` aria-live slot (the form never re-renders the
        verdict-bearing turn cards above it).

        Guarded by ``require_compass_partial`` (NOT admin): Conversations is
        power_user-accessible, and the deny response must be a bare partial so a
        denied POST drops a small message into the slot, not a full Layout.
        """
        user, deny = require_compass_partial(request)
        if deny:
            return deny

        form = await request.form()
        outcome = str(form.get("outcome", "fail")) or "fail"
        comments = str(form.get("comments", "")).strip() or None
        dimension = str(form.get("dimension", "")).strip() or None
        reviewer = getattr(user, "email", None) or getattr(user, "name", None)

        result = await run_in_thread(
            create_report,
            session_id=session_id,
            outcome=outcome,
            reviewer=reviewer,
            comments=comments,
            dimension=dimension,
        )

        if result.ok:
            ok_msg = "Flagged for review. It now appears in Flagged Issues."
            return Div(
                ok_msg,
                cls="quality-reports-banner quality-reports-banner-ok",
            )
        return Div(
            result.detail or "Could not file the flag.",
            cls="quality-reports-banner quality-reports-banner-error",
        )

    @rt("/compass/conversations/{session_id}")
    async def get_conversation_deeplink(
        request: Request,
        session_id: str,
        feedback: str = "all",
        intent: str = "all",
        search: str = "",
        scenario_id: str = "",
        range: str = "7d",
        tab: str = "all",
    ):
        # ``from``/``to`` are Python keywords → read them off the query string.
        custom_since = request.query_params.get("from", "")
        custom_until = request.query_params.get("to", "")
        return await _render_page(
            request,
            feedback=feedback,
            search=search,
            q="",
            not_found_uuid=None,
            scenario_id_raw=scenario_id,
            session_id=session_id,
            range_key=range,
            custom_since=custom_since,
            custom_until=custom_until,
            tab=_tab_param(tab),
        )


async def _render_page(
    request: Request,
    *,
    feedback: str,
    search: str,
    q: str,
    not_found_uuid: str | None,
    scenario_id_raw: str,
    session_id: str,
    range_key: str = "7d",
    custom_since: str = "",
    custom_until: str = "",
    tab: str = "all",
):
    """Single page renderer used by both /compass/conversations and the deep link."""
    user, deny = require_compass(request)
    if deny:
        return deny

    scenario_id_int = _scenario_id_param(scenario_id_raw)

    sidebar, convos = await _build_sidebar(
        feedback=feedback,
        search=search,
        scenario_id=scenario_id_int,
        active_session_id=session_id or None,
        range_key=range_key,
        custom_since=custom_since,
        custom_until=custom_until,
        tab=tab,
    )

    if session_id:
        # Prefer the row already loaded for the sidebar — saves one full
        # LATERAL-join roundtrip when the deep-linked session is in view.
        active_summary = next(
            (c for c in convos if c.session_id == session_id), None
        )
        if active_summary is None:
            active_summary = await run_in_thread(
                get_conversation_summary, session_id
            )
        detail = build_detail(session_id, active_summary)
    else:
        detail = empty_state()

    content = Div(
        Div(
            _smart_search_bar(
                current_value=q or search,
                not_found_uuid=not_found_uuid,
            ),
            cls="conversations-top-bar",
        ),
        Div(
            sidebar,
            Div(detail, id="compass-detail", cls="compass-detail"),
            cls="compass-layout",
        ),
        cls="compass-conversations-wrap",
    )

    return Layout(
        "Compass — Conversations",
        "",
        content,
        section="compass",
        sub_nav="/compass/conversations",
        user=user,
        show_heading=False,
    )
