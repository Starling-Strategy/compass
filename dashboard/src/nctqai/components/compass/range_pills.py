"""Shared range-pill helpers — Today / 7d / 30d / All time + a custom "since" span.

Used by the time-windowed Compass dashboards (Overview, Operations, the
Conversations list) so the selectors stay in lockstep. Each consumer passes
its own page path so the pills link back to the correct route.

The custom span supports a lower bound (``?from=…``, "since this date") and an
optional upper bound (``?to=…``, "through this date", inclusive of that whole
day). State lives entirely in the URL query string (``?range=…`` plus
``?from=…``/``?to=…`` for the custom case), so a chosen range is shareable and
server-driven; nothing is stored client-side.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, UTC
from zoneinfo import ZoneInfo

from fasthtml.common import A, Button, Div, Form, Input, Span

_EASTERN = ZoneInfo("America/New_York")


def _as_utc(timestamp: datetime) -> datetime:
    """Coerce a resolver input to an aware UTC timestamp."""
    return timestamp.astimezone(UTC) if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)


def _eastern_date_start_utc(day: date) -> datetime:
    """Return the UTC instant at midnight Eastern on ``day``."""
    return datetime(day.year, day.month, day.day, tzinfo=_EASTERN).astimezone(UTC)


def _today_since(now: datetime) -> datetime:
    """Start the Today window at Eastern midnight, expressed as UTC."""
    return _eastern_date_start_utc(_as_utc(now).astimezone(_EASTERN).date())


RANGES: dict[str, tuple[str, Callable[[datetime], datetime | None]]] = {
    "today": ("Today", _today_since),
    "7d": ("Last 7 days", lambda now: _as_utc(now) - timedelta(days=7)),
    "30d": ("Last 30 days", lambda now: _as_utc(now) - timedelta(days=30)),
    "all": ("All time", lambda now: None),
}

# Compass has no data before this; a custom "since" earlier than this (or in the
# future) is nonsensical, so we reject it and fall back to the default preset
# rather than spawning a huge empty trend series. The service layer also floors
# the effective start to MIN(created_at), so this is a UX/sanity guard.
_CUSTOM_SINCE_FLOOR = date(2025, 1, 1)

# The preset every fallback lands on (unknown key, or "custom" with a bad date).
# Single-sourced so resolve_since (the data window) and range_selector (the
# highlighted pill) agree on what an invalid custom span degrades to.
_DEFAULT_RANGE = "7d"


def parse_custom_since(custom_since: str) -> datetime | None:
    """Parse an ISO date to the UTC instant at its Eastern midnight.

    Returns None for empty, malformed, or out-of-range input so the caller can
    fall back to a preset without ever raising (the route's ``safe()`` wrappers
    must not 500 on a bad ``from=`` param). Out-of-range = before
    ``_CUSTOM_SINCE_FLOOR`` (pre-Compass) or after today in Eastern Time (a
    future lower bound matches nothing). The bound is normalized to naive UTC at
    the service layer via ``normalize_since`` before binding, matching the
    Compass ``created_at`` convention.
    """
    if not custom_since:
        return None
    try:
        parsed = date.fromisoformat(custom_since.strip())
    except (TypeError, ValueError):
        return None
    if parsed < _CUSTOM_SINCE_FLOOR or parsed > datetime.now(_EASTERN).date():
        return None
    return _eastern_date_start_utc(parsed)


def resolve_since(range_key: str, custom_since: str = "") -> tuple[str, datetime | None]:
    """Map a range key (+ optional custom ``from`` date) to (label, since|None).

    - A preset key returns its canonical (label, since) — unchanged contract, so
      existing callers like operations.py keep unpacking a 2-tuple.
    - ``range_key == "custom"`` with a valid ``custom_since`` returns a label like
      "Since 2026-05-01" and the parsed lower bound.
    - An unknown key, or "custom" with an empty/malformed date, falls back to the
      "7d" default and never raises.
    """
    if range_key == "custom":
        since = parse_custom_since(custom_since)
        if since is not None:
            return f"Since {custom_since.strip()}", since
        # Malformed/empty custom span → fall back to the default preset.
        range_key = _DEFAULT_RANGE
    key = range_key if range_key in RANGES else _DEFAULT_RANGE
    label, resolver = RANGES[key]
    return label, resolver(datetime.now(UTC))


def resolve_until(custom_until: str = "") -> datetime | None:
    """Parse an ISO ``YYYY-MM-DD`` INCLUSIVE upper bound to an EXCLUSIVE bound.

    A ``to`` date means "through the end of that day", so the query bound is the
    *following* midnight, used as ``created_at < until`` (half-open window). This
    is the single authority for the upper-bound convention — ``overview.py``
    imports it (with an inline fallback that mirrors the same +1-day rule), so the
    Overview KPIs and the Conversations list agree on what ``?to=`` means.

    Returns None for empty, malformed, or out-of-range input so callers fall back
    to "no upper bound" without ever raising (parallel to ``parse_custom_since``).
    """
    if not custom_until:
        return None
    try:
        parsed = date.fromisoformat(custom_until.strip())
    except (TypeError, ValueError):
        return None
    if parsed < _CUSTOM_SINCE_FLOOR or parsed > datetime.now(_EASTERN).date():
        return None
    # Inclusive Eastern day → exclusive start of the following Eastern day.
    return _eastern_date_start_utc(parsed + timedelta(days=1))


def range_selector(
    active_range: str,
    base_path: str,
    custom_since: str = "",
    custom_until: str = "",
    *,
    with_upper_bound: bool = True,
) -> Div:
    """Render the four preset pills plus a custom date form.

    Presets stay plain ``<A href>`` full-page navigation (already URL-driven).
    The custom form submits a GET to ``base_path`` with ``range=custom&from=…``
    (plus ``&to=…`` when ``with_upper_bound``), so its state round-trips in the
    URL too — no localStorage, no JS state. The control uses --compass-teal
    tokens only (Compass body accent).

    ``with_upper_bound`` gates the ``to`` (upper-bound) input: only surfaces that
    actually READ ``?to=`` and apply an exclusive upper bound (e.g. Overview)
    should show it. A surface that renders a ``to`` input it never reads would
    silently drop the value on submit, so it passes ``with_upper_bound=False``.
    """
    # Normalize so the highlighted control matches the window resolve_since
    # actually queries: a "custom" range with an invalid/empty date degrades to
    # the default preset for the data, so it must show the default pill active —
    # not a "custom" pill over a 7d result. Echoing the rejected date back into
    # either input would also misrepresent the window, so drop both bounds.
    if active_range == "custom" and parse_custom_since(custom_since) is None:
        active_range = _DEFAULT_RANGE
        custom_since = ""
        custom_until = ""

    pills = []
    for key, (label, _) in RANGES.items():
        active = " active" if key == active_range and active_range != "custom" else ""
        pills.append(A(label, href=f"{base_path}?range={key}", cls=f"range-pill{active}"))

    custom_active = " active" if active_range == "custom" else ""
    form_children = [
        Input(type="hidden", name="range", value="custom"),
        Input(
            type="date",
            name="from",
            value=custom_since,
            cls="range-custom-input",
            aria_label="Show data since this date",
        ),
    ]
    if with_upper_bound:
        form_children += [
            Span("to", cls="range-custom-sep"),
            Input(
                type="date",
                name="to",
                value=custom_until,
                cls="range-custom-input",
                aria_label="Show data through this date",
            ),
        ]
    form_children.append(
        Button("Apply", type="submit", cls=f"range-custom-apply{custom_active}")
    )
    custom_form = Form(
        *form_children,
        method="get",
        action=base_path,
        cls="range-custom-form",
    )
    return Div(
        Div(*pills, cls="range-pills"),
        custom_form,
        cls="range-selector",
    )
