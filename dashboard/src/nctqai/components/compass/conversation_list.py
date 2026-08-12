"""Conversation list sidebar — compact browsing for conversation review.

The sidebar is the primary navigation for the Compass conversations view.
Each item keeps the default state quiet and calls out only review signals.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from fasthtml.common import A, Button, Div, Input, NotStr, Option, Select, Span

from nctqai.components.compass.timestamps import format_relative_eastern_timestamp


SMART_SEARCH_FORM_ID = "conversation-smart-search-form"


def _format_time(timestamp: datetime | None) -> str:
    """Format a timestamp with increasing detail based on age.

    Today:      "2:14 PM"
    This week:  "Tue 10:30 AM"
    Older:      "Mar 22, 3:15 PM"
    """
    if not timestamp:
        return ""
    return format_relative_eastern_timestamp(timestamp)


def _convo_href(session_id: str, filter_state: dict[str, str] | None) -> str:
    """Build a conversation deep-link URL that preserves sidebar filter state.

    The deep-link form (/compass/conversations/{session_id}) is what the user
    will copy/paste; the filter state rides along as query params so a recipient
    lands on the same filtered list with the chosen conversation pre-loaded.
    """
    params: dict[str, str] = {}
    if filter_state:
        # "all" is the no-op default for feedback/intent only — NOT for range,
        # where "all" means "All time" and must survive in the deep-link.
        for key in ("feedback", "intent", "search", "scenario_id"):
            value = filter_state.get(key)
            if value and value != "all":
                params[key] = value
        # range: keep unless empty or the "7d" default; "from": keep when set.
        range_value = filter_state.get("range")
        if range_value and range_value != "7d":
            params["range"] = range_value
        custom_since = filter_state.get("from")
        if custom_since:
            params["from"] = custom_since
        custom_until = filter_state.get("to")
        if custom_until:
            params["to"] = custom_until
        # tab: keep unless the "all" no-op default so the chosen triage tab
        # survives in the shareable deep-link.
        tab_value = filter_state.get("tab")
        if tab_value and tab_value != "all":
            params["tab"] = tab_value
    qs = f"?{urlencode(params)}" if params else ""
    return f"/compass/conversations/{session_id}{qs}"


def ConversationListItem(
    session_id: str,
    title: str | None = None,
    preview: str | None = None,
    timestamp: datetime | None = None,
    district_name: str | None = None,
    state: str | None = None,
    district_count: int = 0,
    thumbs_up_count: int = 0,
    thumbs_down_count: int = 0,
    message_count: int = 0,
    is_eval: bool = False,
    eval_category: str | None = None,
    is_active: bool = False,
    filter_state: dict[str, str] | None = None,
) -> A:
    """A single conversation item in the sidebar list."""
    display_text = title or (preview or "New conversation")[:96]
    if not title and preview and len(preview) > 96:
        display_text += "..."

    time_str = _format_time(timestamp)

    meta_parts = []
    if time_str:
        meta_parts.append(Span(time_str, cls="compass-convo-time"))
    if district_count > 1:
        meta_parts.append(
            Span(
                f"{district_count} districts in context",
                cls="compass-convo-district",
                title="Districts saved in Compass's conversation context. This is not the user's identity or location.",
            )
        )
    elif district_name:
        district_str = f"District context: {district_name}"
        if state:
            district_str += f", {state}"
        meta_parts.append(
            Span(
                district_str,
                cls="compass-convo-district",
                title="District saved in Compass's conversation context. This is not the user's identity or location.",
            )
        )
    # C2 (scannability): a turn-count chip so otherwise-identical prompts
    # ("Which districts have the best benefits?" ×N) are distinguishable at a
    # glance. message_count is already in the list projection — no extra query.
    if message_count:
        meta_parts.append(
            Span(f"{message_count} msgs", cls="compass-convo-count")
        )

    badges = []
    if thumbs_down_count:
        badges.append(Span("flagged", cls="compass-badge compass-badge-thumbs-down"))
    if is_eval:
        badges.append(Span(eval_category or "Scenario", cls="compass-eval-pill"))

    active_cls = " active" if is_active else ""
    # Tier-1 fail signal — driven by user feedback (the real signal). Inline
    # per-turn verdicts now live in the detail view (TurnCard / DASH-R5), so
    # the list row carries no verdict status of its own. The class is ALWAYS
    # applied (CSS makes `.active` win) so the client-side active toggle below
    # can move the selection without losing a row's flagged state.
    has_fail = thumbs_down_count > 0
    fail_cls = " has-fail" if has_fail else ""
    href = _convo_href(session_id, filter_state)

    return A(
        Div(
            Div(
                Span(display_text, cls="compass-convo-preview"),
                cls="compass-convo-header",
            ),
            Div(*meta_parts, cls="compass-convo-meta") if meta_parts else None,
            Div(*badges, cls="compass-convo-badges") if badges else None,
            cls="compass-convo-inner",
        ),
        href=href,
        cls=f"compass-convo-item{active_cls}{fail_cls}",
        hx_get=f"/compass/conversations/detail?session_id={session_id}",
        hx_target="#compass-detail",
        hx_swap="innerHTML",
        # Loading cue while the detail fetches: htmx adds `htmx-request` to the
        # indicated element for the request's duration, activating the existing
        # `.compass-detail.htmx-request` fade (theme.py §26q, the single U1-owned
        # loading hook). Without this the pane just sits unchanged until the swap
        # lands, which read as frozen on slower loads.
        hx_indicator="#compass-detail",
        hx_push_url=href,
        # Clicking swaps only #compass-detail, so the sidebar's server-rendered
        # active state would go stale. Move the "currently viewing" highlight
        # immediately on the client (full sidebar re-renders on filter changes
        # still set it server-side from active_session_id).
        onclick=(
            "document.querySelectorAll('.compass-convo-item.active')"
            ".forEach(function(el){el.classList.remove('active');});"
            "this.classList.add('active');"
        ),
    )


_FEEDBACK_OPTIONS: list[tuple[str, str]] = [
    ("all", "All feedback"),
    ("thumbs_up", "Thumbs up"),
    ("thumbs_down", "Thumbs down"),
    ("unrated", "Not rated"),
]


_RANGE_PRESETS: list[tuple[str, str]] = [
    ("today", "Today"),
    ("7d", "7d"),
    ("30d", "30d"),
    ("all", "All"),
]


def _range_control(
    list_url: str,
    range_include: str,
    range_value: str,
    custom_since: str,
    custom_until: str = "",
) -> Div:
    """Date-range control for the HTMX list swap (presets + custom "since").

    Unlike the Overview range_selector (full-page <A> navigation), the
    Conversations list swaps server-side via HTMX. The selected ``range`` is
    encoded directly in each trigger's hx_get URL (``/list?range=30d``) rather
    than mutated into a hidden input via JS — that removes both the
    feedback-drop and the read-before-handler race a hidden-input approach has.
    The OTHER filters (feedback/search/intent/scenario_id) ride ``range_include``
    so changing the range never resets them. ``range`` must NOT also appear in
    ``range_include`` or it would be sent twice. No localStorage — state is the
    URL + the included form fields. Uses --compass-teal tokens only.
    """
    pills = []
    for key, label in _RANGE_PRESETS:
        active = " active" if key == range_value and range_value != "custom" else ""
        pills.append(
            Button(
                label,
                type="button",
                cls=f"range-pill{active}",
                hx_get=f"{list_url}?range={key}",
                hx_target="#compass-convo-results",
                hx_swap="outerHTML",
                hx_include=range_include,
            )
        )

    # Two date inputs (from / to) both encode range=custom in their URL. Each is
    # the triggering element for its own value, and carries the SIBLING bound in
    # its include so a half-open window [from, to) round-trips on either change.
    # from/to stay OUT of range_include (the pills' include) so they aren't
    # double-sent; each input adds only the sibling it needs.
    custom_active = " active" if range_value == "custom" else ""
    from_include = f"{range_include},[name='to']"
    to_include = f"{range_include},[name='from']"
    custom_form = Div(
        Input(
            type="date",
            name="from",
            value=custom_since,
            form=SMART_SEARCH_FORM_ID,
            cls="range-custom-input",
            aria_label="Show conversations since this date",
            hx_get=f"{list_url}?range=custom",
            hx_target="#compass-convo-results",
            hx_swap="outerHTML",
            hx_trigger="change",
            hx_include=from_include,
        ),
        Span("to", cls="range-custom-sep"),
        Input(
            type="date",
            name="to",
            value=custom_until,
            form=SMART_SEARCH_FORM_ID,
            cls="range-custom-input",
            aria_label="Show conversations through this date",
            hx_get=f"{list_url}?range=custom",
            hx_target="#compass-convo-results",
            hx_swap="outerHTML",
            hx_trigger="change",
            hx_include=to_include,
        ),
        cls=f"range-custom-form{custom_active}",
    )
    return Div(Div(*pills, cls="range-pills"), custom_form, cls="range-selector")


# Triage quick-filter tabs (TRIAGE-R1). Order + labels; the keys mirror
# services.compass_conversations.TRIAGE_TABS. "all" is the no-op default.
_TRIAGE_TABS: list[tuple[str, str]] = [
    ("all", "All"),
    ("thumbs-down", "Thumbs-down"),
    ("has-table", "Has data table"),
    ("has-csv-export", "Has CSV export"),
    ("has-chart", "Has chart"),
    ("unreviewed", "Unreviewed"),
]

# Hover definitions for tabs whose meaning isn't self-evident from the label.
# "Has data table" is the one reviewers asked us to pin down: it is the same
# ``snapshot_summary->>'has_table'`` signal the Overview "Returned a data table"
# tile counts, so this filter's count matches that tile when both use the same
# range. A data table is the structured output shown inline; its answer rows are
# also what the CSV downloads and what a chart is drawn from (coverage-only rows
# such as "not reviewed" render in the table but carry no CSV/chart), so this
# filter is the practical "produced structured output" view. Keys without an
# entry render no tooltip.
_TRIAGE_TITLES: dict[str, str] = {
    "has-table": (
        "Conversations with a saved non-empty data table."
    ),
    "has-csv-export": "Conversations with a saved CSV export available to download.",
    "has-chart": "Conversations with a saved chart shown in the answer.",
}

# Inline outline icons (currentColor) for each triage filter — real SVG, no
# emoji. 14px, stroked, inherit the tab's text color.
def _ti(path: str) -> str:
    return (
        "<svg class='triage-tab-icon' width='14' height='14' viewBox='0 0 24 24' "
        "fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' "
        f"stroke-linejoin='round' aria-hidden='true'>{path}</svg>"
    )


_TRIAGE_ICONS: dict[str, str] = {
    "all": _ti("<line x1='8' y1='6' x2='21' y2='6'/><line x1='8' y1='12' x2='21' y2='12'/>"
               "<line x1='8' y1='18' x2='21' y2='18'/><line x1='3' y1='6' x2='3.01' y2='6'/>"
               "<line x1='3' y1='12' x2='3.01' y2='12'/><line x1='3' y1='18' x2='3.01' y2='18'/>"),
    "thumbs-down": _ti("<path d='M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 "
                       "2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17'/>"),
    "has-table": _ti("<rect x='3' y='3' width='18' height='18' rx='2'/><line x1='3' y1='9' x2='21' y2='9'/>"
                     "<line x1='3' y1='15' x2='21' y2='15'/><line x1='9' y1='3' x2='9' y2='21'/>"),
    "has-csv-export": _ti("<path d='M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'/>"
                          "<polyline points='14 2 14 8 20 8'/><path d='M8 13h8M8 17h8'/>"),
    "has-chart": _ti("<line x1='18' y1='20' x2='18' y2='10'/><line x1='12' y1='20' x2='12' y2='4'/>"
                     "<line x1='6' y1='20' x2='6' y2='14'/>"),
    "unreviewed": _ti("<circle cx='12' cy='12' r='10'/><polyline points='12 6 12 12 16 14'/>"),
}


def _triage_tabs(
    list_url: str,
    tab_include: str,
    active_tab: str,
    tab_counts: dict[str, int | None] | None,
) -> Div:
    """Quick-filter tab strip with live count chips (TRIAGE-R1).

    Each tab is a keyboard-accessible <button> that swaps the
    #compass-convo-results region via the same list partial the other filters
    use, encoding ``tab`` in its hx_get URL and carrying every other filter via
    ``tab_include``. A count of None (e.g. has-table over "All time", which is
    too costly to compute live) renders the tab with no chip rather than a
    fabricated number. Teal accent only — chrome stays brand blue.
    """
    counts = tab_counts or {}
    tabs = []
    for key, label in _TRIAGE_TABS:
        active = " active" if key == active_tab else ""
        count = counts.get(key)
        children: list = []
        icon = _TRIAGE_ICONS.get(key)
        if icon:
            children.append(NotStr(icon))
        children.append(Span(label, cls="triage-tab-label"))
        if count is not None:
            children.append(Span(f"{count:,}", cls="triage-tab-count"))
        tabs.append(
            Button(
                *children,
                type="button",
                cls=f"triage-tab{active}",
                aria_pressed="true" if active else "false",
                title=_TRIAGE_TITLES.get(key),
                hx_get=f"{list_url}?tab={key}",
                hx_target="#compass-convo-results",
                hx_swap="outerHTML",
                hx_include=tab_include,
            )
        )
    # role="group" (not "tablist") — the children are aria-pressed toggle
    # <button> chips, not ARIA tabs (no tabpanel/arrow-key model), so a tablist
    # role would mislead assistive tech (WCAG name/role/value).
    return Div(*tabs, cls="triage-tabs", role="group", aria_label="Triage filters")


# What every range trigger (pills + custom date inputs) carries so changing the
# range never resets another filter; ``range`` rides the trigger URL (not the
# include) to avoid sending it twice. ``from``/``to`` are NOT here either — each
# custom date input adds the OTHER bound to its own include (see _range_control),
# and is its own trigger value, so neither is ever double-sent.
_RANGE_INCLUDE = (
    "[name='feedback'],[name='search'],[name='intent'],"
    "[name='scenario_id'],[name='tab']"
)


def triage_results(
    *,
    tab_strip_items: list,
    tab_counts: dict[str, int | None] | None,
    active_tab: str,
    filter_state: dict[str, str],
) -> Div:
    """The swap payload: range control + triage strip (fresh counts) + the list.

    Returned by both the full-page sidebar and the /list HTMX partial so the
    range control, counts, and the rows they describe always swap together
    (atomic). The outer #compass-convo-results is the single aria-live region.

    The range pills + the hidden ``range`` input live HERE (not in the sibling
    filter bar) so a range change re-renders them in the returned same-id div —
    an ``outerHTML`` swap replaces the whole region, keeping the pills' active
    state and the hidden ``range`` value in lock-step with the rows. ``from``
    lives on the date input below (its sole carrier), so there is exactly one
    element named ``from`` in the document.
    """
    list_url = "/compass/conversations/list"
    tab_include = (
        "[name='feedback'],[name='search'],[name='intent'],"
        "[name='scenario_id'],[name='range'],[name='from'],[name='to']"
    )
    range_value = filter_state.get("range") or "7d"
    custom_since = filter_state.get("from") or ""
    custom_until = filter_state.get("to") or ""
    return Div(
        _range_control(list_url, _RANGE_INCLUDE, range_value, custom_since, custom_until),
        # The hidden ``range`` lives INSIDE the swapped region so an outerHTML
        # swap re-renders it with the new value; feedback_include/tab_include
        # reference [name='range'] (a whole-document selector), so it's found
        # here. ``from`` is carried by the date input in _range_control above.
        Input(
            type="hidden",
            name="range",
            value=range_value,
            form=SMART_SEARCH_FORM_ID,
        ),
        _triage_tabs(list_url, tab_include, active_tab, tab_counts),
        # The active tab lives INSIDE the swapped region so a tab change updates
        # this hidden input too — otherwise a later feedback/range change would
        # send the stale tab. feedback_include/range_include reference
        # [name='tab'] (a whole-document selector), so it's found here.
        Input(type="hidden", name="tab", value=active_tab, form=SMART_SEARCH_FORM_ID),
        Div(*tab_strip_items, id="compass-convo-list", cls="compass-convo-list"),
        id="compass-convo-results",
        cls="compass-convo-results",
        aria_live="polite",
        aria_busy="false",
        # WCAG 2.4.3 (focus order): an outerHTML swap replaces the focused
        # control, dropping focus to <body>. tabindex=-1 makes the fresh region
        # programmatically focusable; the sidebar wrapper's after-swap handler
        # moves focus back into it (see ConversationSidebar). The refocus lives
        # on the wrapper, not here, because htmx does NOT fire after-swap on a
        # node removed by an outerHTML swap (htmx 1.2.0) — it fires on the parent
        # instead, which is exactly why a wrapper handler catches every list swap.
        tabindex="-1",
    )


def ConversationSidebar(
    conversations: list,
    active_session_id: str | None = None,
    feedback_value: str = "all",
    intent_value: str = "all",
    search_value: str = "",
    scenario_id: int | None = None,
    range_value: str = "7d",
    custom_since: str = "",
    custom_until: str = "",
    tab_value: str = "all",
    tab_counts: dict[str, int | None] | None = None,
) -> Div:
    """Full sidebar with filter bar and conversation list.

    Args:
        conversations: List of ConversationSummary objects.
        active_session_id: Currently selected session_id for highlighting.
        feedback_value: Current user-feedback filter ("all", "thumbs_up", "thumbs_down", "unrated").
        intent_value: Current intent filter ("all", "DATA_LOOKUP", ...).
        search_value: Current free-text search string.
        scenario_id: Optional scenario filter (carried through HTMX as a hidden input).
        range_value: Current date-range key ("today"/"7d"/"30d"/"all"/"custom").
        custom_since: Current custom "since" ISO date (only when range_value == "custom").
        tab_value: Current triage tab ("all"/"thumbs-down"/"has-table"/"unreviewed").
        tab_counts: Live per-tab counts for the active range; None per tab = no chip.
    """
    list_url = "/compass/conversations/list"
    # Sidebar filters narrow the current list by user feedback and date range.
    # Intent remains a supported query parameter for older links, but not a
    # default control. The free-text search lives at the top of the page (see
    # routes/compass/conversations.py::_smart_search_bar) because it's the
    # primary way users find a specific session, not a sidebar refinement.
    #
    # feedback_include: what the feedback Select sends — everything except its
    # own value (automatic), i.e. search/intent/scenario_id + the hidden
    # range/from/tab so the chosen range + tab survive a feedback change. The
    # range pills, hidden ``range``, and date-input ``from`` now live inside the
    # swapped #compass-convo-results region (see triage_results); these are
    # whole-document selectors, so the feedback Select still finds them there.
    # The range triggers' own include lives in triage_results (_RANGE_INCLUDE).
    feedback_include = (
        "[name='search'],[name='intent'],[name='scenario_id'],"
        "[name='range'],[name='from'],[name='to'],[name='tab']"
    )
    filter_bar = Div(
        Select(
            *[
                Option(
                    label,
                    value=value,
                    selected="selected" if feedback_value == value else None,
                )
                for value, label in _FEEDBACK_OPTIONS
            ],
            name="feedback",
            form=SMART_SEARCH_FORM_ID,
            cls="compass-filter-select",
            hx_get=list_url,
            hx_target="#compass-convo-results",
            hx_swap="outerHTML",
            hx_include=feedback_include,
        ),
        Input(type="hidden", name="search", value=search_value),
        Input(
            type="hidden",
            name="intent",
            value=intent_value,
            form=SMART_SEARCH_FORM_ID,
        ),
        Input(
            type="hidden",
            name="scenario_id",
            value=str(scenario_id) if scenario_id else "",
            form=SMART_SEARCH_FORM_ID,
        ),
        # NB: the range control + hidden ``range``/``from``/``tab`` inputs live
        # inside #compass-convo-results (see triage_results) so an outerHTML swap
        # keeps them in sync with the rows they filter.
        cls="compass-filter-bar",
    )

    filter_state = {
        "feedback": feedback_value,
        "intent": intent_value,
        "search": search_value,
        "scenario_id": str(scenario_id) if scenario_id else "",
        "range": range_value,
        "from": custom_since,
        "to": custom_until,
        "tab": tab_value,
    }

    items = []
    for convo in conversations:
        items.append(
            ConversationListItem(
                session_id=convo.session_id,
                title=getattr(convo, "title", None),
                preview=convo.first_user_message,
                timestamp=convo.created_at,
                district_name=getattr(convo, "district_name", None),
                state=getattr(convo, "state", None),
                district_count=getattr(convo, "district_count", 0) or 0,
                thumbs_up_count=getattr(convo, "thumbs_up_count", 0) or 0,
                thumbs_down_count=getattr(convo, "thumbs_down_count", 0) or 0,
                message_count=getattr(convo, "message_count", 0) or 0,
                is_active=(convo.session_id == active_session_id),
                filter_state=filter_state,
            )
        )

    if not items:
        items.append(
            Div(
                Div("No conversations yet", cls="compass-empty-title"),
                Div("Run a test scenario or wait for user sessions.", cls="compass-empty-sub"),
                cls="compass-empty-sidebar",
            )
        )

    # triage_results wraps the range control + triage strip + the list in
    # #compass-convo-results, the single aria-live region for every
    # filter/range/tab swap. htmx does NOT set aria-busy and CSS can't toggle an
    # attribute, so the sidebar wrapper carries hx-on handlers around the swap.
    #
    # aria-busy: before-request marks the region busy, after-request clears it.
    # after-request bubbles from the feedback Select / detail items (not removed
    # by their swap); for the region-internal range/tab triggers it never bubbles
    # (the trigger is detached by the outerHTML swap), but the fresh region
    # renders aria-busy="false" baked in, so it lands correct anyway.
    #
    # Focus (WCAG 2.4.3) is restored in after-swap, NOT after-request, on
    # purpose: the range pills / date input / triage tabs now live INSIDE the
    # swapped region, so an outerHTML swap detaches the triggering control before
    # after-request would bubble — that event never reaches this wrapper. But
    # htmx fires after-swap on the PARENT of an outerHTML-swapped node (htmx
    # 1.2.0), i.e. this wrapper, for all four list triggers. Detail clicks swap
    # #compass-detail (outside this wrapper), so their after-swap fires there and
    # does not steal focus back to the list.
    results = triage_results(
        tab_strip_items=items,
        tab_counts=tab_counts,
        active_tab=tab_value,
        filter_state=filter_state,
    )

    return Div(
        filter_bar,
        results,
        cls="compass-sidebar",
        hx_on__before_request=(
            "document.getElementById('compass-convo-results')"
            "?.setAttribute('aria-busy','true')"
        ),
        hx_on__after_request=(
            "document.getElementById('compass-convo-results')"
            "?.setAttribute('aria-busy','false')"
        ),
        hx_on__after_swap=(
            "document.getElementById('compass-convo-results')"
            "?.focus({preventScroll:true})"
        ),
    )
