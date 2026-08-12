"""KPI card components for the NCTQ.ai dashboard.

Stat cards showing large numbers with labels, arranged in a flex row.
"""

from fasthtml.common import A, Div


def KpiCard(
    value: str | int | float,
    label: str,
    subtitle: str = "",
    href: str | None = None,
    trend: str | None = None,
):
    """Single stat card with large number, label, and optional subtitle.

    Args:
        value: The primary number/text to display prominently.
        label: Short description below the value (uppercased via CSS).
        subtitle: Optional additional context below the label.
        href: If set, render the tile as a link — signals the number is
              clickable drill-down (e.g. a filter on a list).
        trend: Optional period-over-period delta (e.g. "+12% vs prior period").
               Rendered after the subtitle in muted ``.kpi-trend`` text — a
               neutral caption, not a pass/fail signal.
    """
    children = [
        Div(str(value), cls="kpi-value"),
        Div(label, cls="kpi-label"),
    ]
    if subtitle:
        children.append(Div(subtitle, cls="kpi-subtitle"))
    if trend:
        children.append(Div(trend, cls="kpi-trend"))
    if href:
        return A(*children, href=href, cls="kpi-card kpi-card--clickable")
    return Div(*children, cls="kpi-card")


def KpiRow(*cards, cls=""):
    """Flex row wrapping multiple KpiCard elements.

    Args:
        *cards: KpiCard components to arrange horizontally.
        cls: optional extra class(es) on the row container — e.g.
            "kpi-cards--grid4" to lay the cards out as a fixed 4-column grid that
            wraps to a second row instead of squishing many tiles into one row.
    """
    return Div(*cards, cls=f"kpi-cards {cls}".strip())
