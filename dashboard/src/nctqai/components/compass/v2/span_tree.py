"""Nested span-tree renderer for the /compass/v2/traces view."""

from __future__ import annotations

from fasthtml.common import Div, Span


def _fmt_duration(ms: float | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.1f}s"


def SpanNode(
    *,
    span_name: str,
    duration_ms: float | None,
    attributes: dict,
    children: list,
    depth: int = 0,
):
    """One node in the span tree.

    attributes is the flat OTel attribute dict; we show a few canonical keys.
    """
    highlights = []
    for key in ("tool_name", "rule_id", "model", "intent", "passed", "severity"):
        if key in attributes and attributes[key] not in (None, ""):
            highlights.append(Span(f"{key}={attributes[key]}", cls="v2-span-attr"))

    header = Div(
        Span(span_name, cls="v2-span-name"),
        Span(_fmt_duration(duration_ms), cls="v2-span-duration"),
        *highlights,
        cls="v2-span-header",
        style=f"padding-left: {depth * 18}px",
    )
    body = Div(*children, cls="v2-span-children") if children else None
    parts = [header]
    if body is not None:
        parts.append(body)
    return Div(*parts, cls="v2-span-node")
