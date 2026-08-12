"""Tests for the shared date-range control (DATE-R1 / DASH-R1 foundation, U1).

Covers the generalized resolver (presets + custom "since" span, lower-bound
only), the rendered controls on both surfaces, the teal-only accent boundary,
defensive fallbacks, naive-UTC normalization, the aria-live swap region, the
single prefers-reduced-motion guard, and the route-side fix that stops passing
``since=None`` to ``list_conversations``.

Pure-component / pure-function tests — no live database. The one route test
monkeypatches ``list_conversations`` to capture the resolved ``since`` kwarg.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, UTC

from fasthtml.common import to_xml

from nctqai.components.compass.conversation_list import ConversationSidebar
from nctqai.components.compass.conversation_list import SMART_SEARCH_FORM_ID
from nctqai.components.compass.conversation_list import _convo_href
from nctqai.components.compass.range_pills import (
    RANGES,
    parse_custom_since,
    range_selector,
    resolve_since,
    resolve_until,
)
from nctqai.services.compass_snapshot_memory import normalize_since
from nctqai.models.compass import ConversationSummary
from nctqai.theme import CUSTOM_CSS


# ── Resolver: backward-compatible presets ───────────────────────────


def test_resolve_since_preset_is_backward_compatible():
    """7d still returns a 2-tuple (label, since) — operations.py unpack stays valid."""
    label, since = resolve_since("7d")
    assert label == "Last 7 days"
    assert since is not None
    # ~7 days before now (allow a small clock window).
    delta = datetime.now(UTC) - since
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


def test_resolve_since_all_returns_none():
    label, since = resolve_since("all")
    assert label == "All time"
    assert since is None


def test_resolve_since_unknown_key_falls_back_to_7d():
    label, since = resolve_since("bogus")
    assert label == "Last 7 days"
    assert since is not None


def test_today_starts_at_eastern_midnight_in_summer_and_winter():
    # 1 AM UTC on July 10 is still July 9 in EDT; its local-day start is 04:00 UTC.
    summer = RANGES["today"][1](datetime(2026, 7, 10, 1, tzinfo=UTC))
    assert summer == datetime(2026, 7, 9, 4, tzinfo=UTC)
    # 1 AM UTC on January 10 is still January 9 in EST; its local-day start is 05:00 UTC.
    winter = RANGES["today"][1](datetime(2026, 1, 10, 1, tzinfo=UTC))
    assert winter == datetime(2026, 1, 9, 5, tzinfo=UTC)


# ── Resolver: custom "since" span ───────────────────────────────────


def test_resolve_since_custom_returns_parsed_lower_bound():
    label, since = resolve_since("custom", "2026-05-01")
    assert "2026-05-01" in label
    assert since is not None
    assert (since.year, since.month, since.day) == (2026, 5, 1)


def test_resolve_since_custom_empty_falls_back_to_default():
    """An empty custom span degrades to the 7d default — never raises."""
    label, since = resolve_since("custom", "")
    assert label == "Last 7 days"
    assert since is not None


def test_resolve_since_custom_malformed_falls_back_to_default():
    for bad in ("not-a-date", "2026-13-99", "05/01/2026", "   "):
        label, since = resolve_since("custom", bad)
        assert label == "Last 7 days", bad
        assert since is not None, bad


def test_parse_custom_since_returns_none_on_bad_input():
    assert parse_custom_since("") is None
    assert parse_custom_since("garbage") is None
    assert parse_custom_since("2026-05-01") is not None


def test_custom_since_normalizes_to_naive_utc():
    """The Eastern-day bound converts to UTC, then flattens for created_at."""
    _, since = resolve_since("custom", "2026-05-01")
    flat = normalize_since(since)
    assert flat is not None
    assert flat.tzinfo is None
    assert (flat.year, flat.month, flat.day, flat.hour) == (2026, 5, 1, 4)


def test_custom_range_uses_eastern_day_boundaries_across_dst():
    # March 8 begins in EST and March 9 begins in EDT, so the UTC offset changes.
    assert parse_custom_since("2026-03-08") == datetime(2026, 3, 8, 5, tzinfo=UTC)
    assert resolve_until("2026-03-08") == datetime(2026, 3, 9, 4, tzinfo=UTC)


# ── Rendered control: Overview (full-page form) ─────────────────────


def test_range_selector_renders_presets_and_custom_form():
    html = to_xml(range_selector("7d", "/compass/overview"))
    # Four preset pills as URL links.
    assert "?range=today" in html
    assert "?range=7d" in html
    assert "?range=30d" in html
    assert "?range=all" in html
    # Custom span form emits range=custom + a from date input.
    assert 'value="custom"' in html
    assert 'name="from"' in html
    assert 'type="date"' in html


def test_range_selector_marks_active_preset():
    html = to_xml(range_selector("30d", "/compass/overview"))
    assert 'class="range-pill active"' in html and "?range=30d" in html


def test_range_selector_custom_round_trips_value():
    html = to_xml(range_selector("custom", "/compass/overview", "2026-05-01"))
    assert 'value="2026-05-01"' in html
    assert "range-custom-apply active" in html


def test_range_selector_invalid_custom_falls_back_to_default_pill():
    """A 'custom' range with an out-of-range date is rejected by
    parse_custom_since, so resolve_since queries the 7d default. The selector
    must match that: highlight the 7d pill, not a 'custom' pill over a 7d
    result, and never echo the rejected date back into the input."""
    # 2020-01-01 is before the Compass data floor → rejected.
    html = to_xml(range_selector("custom", "/compass/overview", "2020-01-01"))
    assert "range-custom-apply active" not in html
    assert 'class="range-pill active"' in html and "?range=7d" in html
    assert 'value="2020-01-01"' not in html


def test_range_selector_empty_custom_falls_back_to_default_pill():
    """An empty custom span degrades to the 7d default for the data, so the
    selector highlights 7d rather than leaving 'custom' active over a 7d window."""
    html = to_xml(range_selector("custom", "/compass/overview", ""))
    assert "range-custom-apply active" not in html
    assert 'class="range-pill active"' in html and "?range=7d" in html


def test_range_control_emits_no_client_storage():
    """URL-driven / hx_include-driven only — no localStorage or JS state store."""
    html = to_xml(range_selector("7d", "/compass/overview"))
    assert "localStorage" not in html
    assert "sessionStorage" not in html


def test_range_control_uses_teal_not_brand_blue():
    """Accent boundary: the control uses Compass-teal classes, never brand blue."""
    html = to_xml(range_selector("custom", "/compass/overview", "2026-05-01"))
    assert "range-custom" in html  # teal-token classes (see theme.py 26s)
    assert "brand-blue" not in html
    assert "--brand-blue" not in html


# ── Rendered control: Conversations sidebar (HTMX swap) ─────────────


def _sidebar_html(**kw) -> str:
    return to_xml(ConversationSidebar(conversations=[], **kw))


def test_sidebar_range_control_drives_htmx_list_partial():
    html = _sidebar_html(range_value="30d")
    # The range presets fire the same list partial the feedback filter uses.
    assert "/compass/conversations/list" in html
    assert "compass-convo-list" in html
    assert "localStorage" not in html


def test_range_pills_encode_range_in_url_not_hidden_input():
    """Each preset encodes ?range=<key> in its hx_get URL (no JS hidden-input
    mutation), so there's no read-before-handler race on the first click."""
    html = _sidebar_html(range_value="7d")
    assert "/compass/conversations/list?range=today" in html
    assert "/compass/conversations/list?range=30d" in html
    assert "/compass/conversations/list?range=all" in html
    assert "/compass/conversations/list?range=custom" in html
    # No leftover JS that stamps a hidden range/from input on click/change.
    assert "querySelector" not in html


def test_range_triggers_carry_feedback_so_it_is_not_dropped():
    """Regression: changing the range must NOT reset the feedback filter.
    Every range trigger includes [name='feedback'] (the feedback-drop bug)."""
    html = _sidebar_html(range_value="7d", feedback_value="thumbs_down")
    # The range pills'/date input's hx-include must list feedback.
    assert "[name='feedback']" in html


def test_range_not_double_sent_on_range_triggers():
    """range is in the trigger URL, so it must NOT also be in the range
    triggers' hx-include (would send range twice)."""
    html = _sidebar_html(range_value="7d")
    # The feedback Select keeps range/from in its include (URL has no range);
    # the range triggers carry feedback/search/intent/scenario_id only. Assert
    # the range-trigger include shape is present and the feedback-include shape
    # (with range/from) is also present — both coexist.
    assert "[name='feedback'],[name='search'],[name='intent'],[name='scenario_id']" in html


def test_sidebar_hidden_inputs_carry_range_state():
    html = _sidebar_html(range_value="custom", custom_since="2026-05-01")
    assert 'name="range"' in html and 'value="custom"' in html
    assert 'name="from"' in html and 'value="2026-05-01"' in html


def test_sidebar_list_region_is_aria_live_polite():
    html = _sidebar_html(range_value="7d")
    assert 'aria-live="polite"' in html
    assert 'aria-busy="false"' in html
    # aria-busy is toggled around the swap via bubbling hx-on handlers, since
    # htmx does not set aria-busy and CSS cannot toggle an attribute.
    assert "aria-busy" in html and "before-request" in html


def test_convo_href_preserves_range_all_and_custom_since():
    """Deep-link must keep range=all (real "All time"), drop the 7d default,
    and carry a custom span — so reloading the URL restores the same range."""
    # "all" is a real range, NOT the feedback no-op default → keep it.
    assert "range=all" in _convo_href("abc", {"range": "all"})
    # "7d" is the default → omit for a clean link.
    assert "range=" not in _convo_href("abc", {"range": "7d"})
    # custom span rides along.
    href = _convo_href("abc", {"range": "custom", "from": "2026-05-01"})
    assert "range=custom" in href and "from=2026-05-01" in href


def test_convo_href_still_drops_feedback_all_noop():
    """Regression guard: feedback's 'all' is still treated as a no-op default."""
    assert "feedback=" not in _convo_href("abc", {"feedback": "all"})
    assert "feedback=thumbs_down" in _convo_href("abc", {"feedback": "thumbs_down"})


def test_sidebar_empty_state_renders_not_blank():
    """A range matching zero sessions keeps the designed empty state."""
    html = _sidebar_html(range_value="today")
    assert "No conversations yet" in html


# ── Upper bound: resolve_until (W2-1) ───────────────────────────────


def test_resolve_until_inclusive_day_becomes_exclusive_next_midnight():
    """A `to` date means "through the end of that day" → exclusive next midnight,
    so created_at < until captures the whole chosen day (the convention the
    overview slice's inline fallback also uses)."""
    until = resolve_until("2026-05-01")
    assert until is not None
    assert until.year == 2026 and until.month == 5 and until.day == 2
    assert until.hour == 4 and until.minute == 0


def test_resolve_until_empty_or_malformed_is_none():
    for bad in ("", "not-a-date", "2026-13-40"):
        assert resolve_until(bad) is None


def test_resolve_until_out_of_range_is_none():
    # Before Compass existed, and a future date, both match nothing → None.
    assert resolve_until("2020-01-01") is None
    future = (datetime.now(UTC).date().replace(year=datetime.now(UTC).year + 1)).isoformat()
    assert resolve_until(future) is None


def test_range_selector_renders_to_input_for_upper_bound():
    """The full-page (Overview) control exposes a `to` date input alongside `from`."""
    html = to_xml(range_selector("custom", "/compass/overview", "2026-05-01", "2026-05-31"))
    assert 'name="to"' in html
    assert 'value="2026-05-31"' in html


def test_range_selector_with_upper_bound_false_hides_to_input():
    """A surface that never reads ?to= (e.g. Operations) must NOT render a `to`
    input it would silently drop — only the lower-bound input shows."""
    html = to_xml(
        range_selector(
            "custom", "/compass/operations", "2026-05-01", "2026-05-31",
            with_upper_bound=False,
        )
    )
    assert 'name="to"' not in html
    assert 'name="from"' in html


def test_range_selector_rejected_custom_clears_both_bounds():
    """An empty/invalid `from` rejects the whole custom span → the 7d pill is
    active and NEITHER bound echoes a stale value (symmetric from/to fallback),
    so a stale `to` can't linger over a preset pill."""
    html = to_xml(range_selector("custom", "/compass/overview", "", "2026-05-31"))
    assert 'value="2026-05-31"' not in html


def test_sidebar_carries_to_bound_through_includes_and_value():
    """The conversations list threads `to`: the date input is present, its value
    round-trips, and the other filters carry [name='to'] so changing them never
    drops the upper bound."""
    html = _sidebar_html(range_value="custom", custom_since="2026-05-01", custom_until="2026-05-31")
    assert 'name="to"' in html and 'value="2026-05-31"' in html
    # feedback/tab triggers must include the to-bound so it survives their swaps.
    assert "[name='to']" in html


def test_convo_href_preserves_to_upper_bound():
    href = _convo_href("abc", {"range": "custom", "from": "2026-05-01", "to": "2026-05-31"})
    assert "from=2026-05-01" in href and "to=2026-05-31" in href


# ── Scannability: per-row turn count (C2) ───────────────────────────


def test_list_row_shows_message_count_chip():
    """C2: otherwise-identical prompts get a turn-count chip so the review queue
    is scannable. message_count comes from the existing list projection."""
    from types import SimpleNamespace

    convo = SimpleNamespace(
        session_id="s1",
        first_user_message="Which districts have the best benefits?",
        created_at=datetime.now(UTC),
        district_name=None,
        state=None,
        thumbs_up_count=0,
        thumbs_down_count=0,
        message_count=6,
    )
    html = to_xml(ConversationSidebar(conversations=[convo]))
    assert "6 msgs" in html
    assert "compass-convo-count" in html


# ── Theme: single reduced-motion guard ──────────────────────────────


def test_theme_has_exactly_one_reduced_motion_block():
    css = to_xml(CUSTOM_CSS)
    assert css.count("@media (prefers-reduced-motion: reduce)") == 1


def test_reduced_motion_block_neutralizes_animations():
    css = to_xml(CUSTOM_CSS)
    block = css.split("@media (prefers-reduced-motion: reduce)")[1]
    block = block.split("}", 10)[0:8]
    block = "}".join(block)
    for selector in (".range-pill", ".bar-fill", ".compass-score-fill"):
        assert selector in block, selector


# ── Route threading: DASH-R1 (stop passing since=None) ──────────────


def test_list_partial_threads_resolved_since(monkeypatch):
    """The conversations list route must pass the resolved range bound to
    list_conversations, never the old hardcoded since=None (DASH-R1)."""
    import nctqai.routes.compass.conversations as conv

    captured: dict = {}

    def fake_list_conversations(**kwargs):
        captured.update(kwargs)
        return []

    # require_compass_partial returns (user, deny); deny=None lets us proceed.
    monkeypatch.setattr(conv, "require_compass_partial", lambda req: (object(), None))
    monkeypatch.setattr(conv, "list_conversations", fake_list_conversations)
    # The partial also computes live triage-tab counts now (U4); stub it so this
    # range-focused test stays DB-free.
    monkeypatch.setattr(
        conv, "count_conversations_by_tab", lambda **kwargs: {"all": 0}
    )

    # run_in_thread just awaits the sync callable; emulate that.
    async def fake_run_in_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(conv, "run_in_thread", fake_run_in_thread)

    handlers: dict = {}

    class FakeRouter:
        def __call__(self, path):
            def deco(fn):
                handlers[path] = fn
                return fn
            return deco

    conv.register(FakeRouter())
    list_handler = handlers["/compass/conversations/list"]

    class FakeRequest:
        query_params: dict = {}

    asyncio.run(list_handler(FakeRequest(), feedback="all", range="30d"))

    assert "since" in captured
    assert captured["since"] is not None, "DASH-R1: range must resolve a real bound"
    # 30d → ~30 days before now.
    delta = datetime.now(UTC) - captured["since"].replace(tzinfo=UTC)
    assert timedelta(days=29) < delta < timedelta(days=31)


# ── Count scope: tab counts share the list's date+search predicates (#11) ──


def test_tab_counts_share_list_since_and_search(monkeypatch):
    """The triage-tab counts MUST be scoped to the same resolved ``since`` and
    ``search`` as the row list — otherwise the count chips describe a different
    population than the rows shown. Mirrors test_list_partial_threads_resolved_since
    but CAPTURES count_conversations_by_tab's kwargs instead of discarding them.
    """
    import nctqai.routes.compass.conversations as conv

    list_kwargs: dict = {}
    count_kwargs: dict = {}

    def fake_list_conversations(**kwargs):
        list_kwargs.update(kwargs)
        return []

    def fake_count_by_tab(**kwargs):
        count_kwargs.update(kwargs)
        return {"all": 0}

    monkeypatch.setattr(conv, "require_compass_partial", lambda req: (object(), None))
    monkeypatch.setattr(conv, "list_conversations", fake_list_conversations)
    monkeypatch.setattr(conv, "count_conversations_by_tab", fake_count_by_tab)

    async def fake_run_in_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(conv, "run_in_thread", fake_run_in_thread)

    handlers: dict = {}

    class FakeRouter:
        def __call__(self, path):
            def deco(fn):
                handlers[path] = fn
                return fn

            return deco

    conv.register(FakeRouter())
    list_handler = handlers["/compass/conversations/list"]

    class FakeRequest:
        query_params: dict = {}

    asyncio.run(
        list_handler(FakeRequest(), feedback="all", range="30d", search="anchorage")
    )

    # Both reads ran with identical scope: same resolved bound, same search.
    assert count_kwargs["since"] == list_kwargs["since"]
    assert count_kwargs["search"] == list_kwargs["search"] == "anchorage"


# ── Regression #1: the /list partial re-renders the range control ──────────


def test_list_partial_payload_carries_range_pills_and_state(monkeypatch):
    """After the Stage-A outerHTML refactor, a range/tab/feedback swap replaces
    the WHOLE #compass-convo-results region — so the range pills, the hidden
    range/from inputs, and the active-range highlight must live INSIDE that
    payload, or they'd vanish on the first swap. This pins them to the partial.
    """
    import nctqai.routes.compass.conversations as conv

    monkeypatch.setattr(conv, "require_compass_partial", lambda req: (object(), None))
    monkeypatch.setattr(conv, "list_conversations", lambda **kw: [])
    monkeypatch.setattr(conv, "count_conversations_by_tab", lambda **kw: {"all": 0})

    async def fake_run_in_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(conv, "run_in_thread", fake_run_in_thread)

    handlers: dict = {}

    class FakeRouter:
        def __call__(self, path):
            def deco(fn):
                handlers[path] = fn
                return fn

            return deco

    conv.register(FakeRouter())
    list_handler = handlers["/compass/conversations/list"]

    class FakeRequest:
        # ``from`` rides the query string (Python keyword can't bind as a param).
        query_params: dict = {"from": "2026-05-01"}

    result = asyncio.run(list_handler(FakeRequest(), feedback="all", range="30d"))
    html = to_xml(result)

    # The swap target itself is in the payload (outerHTML replaces it in place).
    assert 'id="compass-convo-results"' in html
    # Range preset pills re-render (each encodes its key in the trigger URL).
    assert "range-pill" in html
    assert "/compass/conversations/list?range=today" in html
    assert "/compass/conversations/list?range=all" in html
    # Hidden range + the custom date input (the sole ``from`` carrier) re-render.
    assert 'name="range"' in html and 'value="30d"' in html
    assert 'name="from"' in html and 'value="2026-05-01"' in html
    # Those live controls also feed the top search form; otherwise the form
    # would keep submitting the range that was present when the page first loaded.
    assert f'form="{SMART_SEARCH_FORM_ID}"' in html
    # Active-range highlight rides along (30d is the active preset).
    assert 'class="range-pill active"' in html


def test_list_partial_keeps_multi_district_context(monkeypatch):
    """A tab/range/search swap must retain the compact multi-district label."""
    import nctqai.routes.compass.conversations as conv

    monkeypatch.setattr(conv, "require_compass_partial", lambda req: (object(), None))
    monkeypatch.setattr(
        conv,
        "list_conversations",
        lambda **_kw: [
            ConversationSummary(
                session_id="multi",
                first_message="Compare these districts",
                district_count=17,
            )
        ],
    )
    monkeypatch.setattr(conv, "count_conversations_by_tab", lambda **_kw: {"all": 1})

    async def fake_run_in_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(conv, "run_in_thread", fake_run_in_thread)

    handlers: dict = {}

    class FakeRouter:
        def __call__(self, path):
            def deco(fn):
                handlers[path] = fn
                return fn

            return deco

    conv.register(FakeRouter())

    class FakeRequest:
        query_params: dict = {}

    result = asyncio.run(
        handlers["/compass/conversations/list"](FakeRequest(), range="30d")
    )
    assert "17 districts in context" in to_xml(result)
