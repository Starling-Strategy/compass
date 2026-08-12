"""Shared Compass dashboard bar components."""

from __future__ import annotations

from collections.abc import Sequence

from fasthtml.common import Div, Span


def bar_row(
    label: str,
    value: int,
    max_value: int,
    suffix: str = "",
    *,
    value_label: str | None = None,
) -> Div:
    """Render one horizontal bar row."""
    pct = round(value / max_value * 100) if max_value else 0
    display_value = value_label if value_label is not None else f"{value:,}{suffix}"
    return Div(
        Div(label, cls="bar-label"),
        Div(Div(cls="bar-fill", style=f"width: {pct}%;"), cls="bar-track"),
        Div(display_value, cls="bar-value"),
        cls="bar-row",
    )


def trend_chart(points: Sequence[dict]) -> Div:
    """Render an over-time bar chart, one ``bar_row`` per day/week bucket.

    Each point is a ``{"label": str, "value": int}`` dict (plain dicts, not a
    service type, so this component carries no import dependency on
    compass_stats). The bars share ``bar_row``'s ".bar-fill"; teal in the
    Compass body comes from the ``.compass-trend .bar-fill`` scope set by the
    caller's wrapper, not from this component. Empty input renders an empty
    ".bar-chart" — callers own the "No activity in this range yet." state.
    """
    max_val = max((p["value"] for p in points), default=0) or 1
    return Div(
        *[bar_row(str(p["label"]), int(p["value"]), int(max_val)) for p in points],
        cls="bar-chart",
    )


def layered_trend_chart(points: Sequence[dict]) -> Div:
    """Render the activity trend as ONE layered bar per bucket: a lighter
    "questions" bar with the darker "sessions" bar overlaid on top.

    Questions are always >= sessions in a bucket, so the shorter sessions bar
    sits within the longer questions bar — the visible result per row is a dark
    teal segment (sessions) that fades into a pale teal extension (the extra
    questions). Each row also shows the bucket's session/question counts and the
    average questions per session.

    Each point is a ``{"label", "sessions", "questions", "avg"}`` dict (plain
    dicts — no import dependency on compass_stats). ``avg`` is a preformatted
    string (e.g. "1.6") or None when there were no sessions that bucket. Both
    bars share one scale (max questions across the range) so bar lengths are
    comparable across rows.
    """
    max_val = max((int(p["questions"]) for p in points), default=0) or 1
    rows = []
    for p in points:
        sessions = int(p["sessions"])
        questions = int(p["questions"])
        q_pct = round(questions / max_val * 100)
        s_pct = round(sessions / max_val * 100)
        avg = p.get("avg")
        rows.append(
            Div(
                Div(str(p["label"]), cls="bar-label"),
                Div(
                    # Questions fill (lighter) is drawn FIRST so the darker
                    # sessions fill paints on top of its left portion.
                    Div(cls="bar-fill bar-fill-questions", style=f"width: {q_pct}%;"),
                    Div(cls="bar-fill bar-fill-sessions", style=f"width: {s_pct}%;"),
                    cls="bar-track bar-track-layered",
                ),
                Div(
                    Span(f"{sessions:,}", cls="trend-val-sessions"),
                    Span(f"{questions:,}", cls="trend-val-questions"),
                    Span(
                        f"{avg}× avg" if avg is not None else "—",
                        cls="trend-val-avg",
                    ),
                    cls="bar-value bar-value-layered",
                ),
                cls="bar-row",
            )
        )
    return Div(*rows, cls="bar-chart")
