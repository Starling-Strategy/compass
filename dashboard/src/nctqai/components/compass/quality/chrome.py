"""Page chrome components for the Compass Quality Scorecard.

breadcrumb_strip: linked breadcrumb trail (home › Scorecard or home › dim).
build_context_strip: muted metadata bar (build_id, sweep_id, n_cases, n_trials).
last_sweep_strip: "Last successful sweep: <X ago>" banner (issue #751).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fasthtml.common import A, Div, Span

from compass_backend.quality.scorecard_models import BuildContext, DimensionScore


def breadcrumb_strip(crumbs: list[tuple[str, str | None]]) -> Div:
    """Render a breadcrumb bar for the quality cluster pages.

    Args:
        crumbs: List of (label, href) tuples. Last item is rendered bold
                with no link; others are anchor links.
    """
    parts = []
    for i, (label, href) in enumerate(crumbs):
        is_last = i == len(crumbs) - 1
        if parts:
            parts.append(Span(" / ", cls="quality-breadcrumb-sep"))
        if is_last:
            parts.append(Span(label, cls="quality-breadcrumb-current"))
        else:
            parts.append(A(label, href=href, cls="quality-breadcrumb-link"))
    return Div(*parts, cls="quality-breadcrumb-strip")


def build_context_strip(build: BuildContext) -> Div:
    """Render the muted metadata bar showing build context.

    Shown at the top of the Scorecard hero and drill-down pages so admins
    know which sweep they're looking at.
    """
    items = [
        ("Build", build.build_id),
        ("Latest sweep", build.sweep_id),
        ("Criterion set", build.criterion_set_version),
        ("Cases", str(build.cases)),
        ("Trials", str(build.trials)),
    ]
    if build.threshold_version:
        items.append(("Thresholds", build.threshold_version))
    if build.threshold_review_date:
        items.append(("Threshold review", build.threshold_review_date))
    chips = []
    for label, value in items:
        chips.append(
            Span(
                Span(label, cls="quality-context-label"),
                Span(value, cls="quality-context-value"),
                cls="quality-context-chip",
            )
        )
    return Div(*chips, cls="quality-context-strip")


def _parse_timestamp(raw: str) -> datetime | None:
    """Best-effort parse of an ISO-8601 / Postgres timestamp string.

    Handles both the dashboard fixture form (``2026-05-23T12:00:00+00:00``)
    and Postgres ``finished_at::text`` (``2026-06-03 10:00:00+00`` — space
    separator, 2-digit offset). Returns a tz-aware datetime (assuming UTC
    when no offset is present), or None if the value cannot be parsed.
    """
    text = raw.strip().replace(" ", "T", 1)
    # Postgres renders a 2-digit offset (``+00``); fromisoformat needs ``+00:00``.
    if len(text) >= 3 and text[-3] in "+-" and text[-2:].isdigit():
        text = f"{text}:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def humanize_age(raw: str | None, now: datetime | None = None) -> str | None:
    """Render a coarse "<N units> ago" string for a timestamp.

    Returns None when ``raw`` is falsy or unparseable, so callers can fall
    back to the raw value. ``now`` is injectable for deterministic tests.
    """
    if not raw:
        return None
    parsed = _parse_timestamp(raw)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    delta_seconds = (reference - parsed).total_seconds()
    if delta_seconds < 60:
        return "just now"
    minutes = int(delta_seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def latest_finished_across(dimensions: list[DimensionScore]) -> str | None:
    """Return the max ``latest_finished_at`` across dimensions, or None.

    The scorecard scores each dimension off its own latest completed sweep,
    so the "last successful sweep" the reviewer cares about is the most
    recent finish across all dimensions. Unparseable values are ignored.
    """
    best_raw: str | None = None
    best_dt: datetime | None = None
    for dim in dimensions:
        raw = dim.latest_finished_at
        if not raw:
            continue
        parsed = _parse_timestamp(raw)
        if parsed is None:
            continue
        if best_dt is None or parsed > best_dt:
            best_dt = parsed
            best_raw = raw
    return best_raw


def last_sweep_strip(
    latest_finished_at: str | None,
    now: datetime | None = None,
) -> Div:
    """Render the "Last successful sweep: <X ago>" banner (issue #751).

    Reads the max ``latest_finished_at`` the caller resolved across
    dimensions. Shows a relative age with the absolute timestamp on hover.
    Falls back to the raw value if it cannot be parsed, and to an explicit
    "no successful sweep yet" message when there is no data.
    """
    if not latest_finished_at:
        return Div(
            Span("Last successful sweep:", cls="quality-last-sweep-label"),
            Span("no successful sweep yet", cls="quality-last-sweep-value"),
            cls="quality-last-sweep-strip",
        )
    age = humanize_age(latest_finished_at, now=now)
    value = age if age is not None else latest_finished_at
    return Div(
        Span("Last successful sweep:", cls="quality-last-sweep-label"),
        Span(
            value,
            title=latest_finished_at,
            cls="quality-last-sweep-value",
        ),
        cls="quality-last-sweep-strip",
    )
