"""Academic Year dropdown component — shared across all MC pages."""

from fasthtml.common import Div, Option, Select, Span

from nctqai.utils import AY_OPTIONS, format_ay


def AYSelect(current_ay: int, hx_get: str, hx_target: str, hx_include: str = ""):
    """Render an AY dropdown that triggers HTMX on change.

    Args:
        current_ay: Currently selected academic year ID (e.g. 25).
        hx_get: HTMX endpoint to call on change.
        hx_target: CSS selector for the HTMX swap target.
        hx_include: Additional inputs to include in the request.
    """
    return Div(
        Span("Academic Year", cls="filter-group-label"),
        Select(
            *[
                Option(format_ay(a), value=str(a), selected=("selected" if a == current_ay else None))
                for a in AY_OPTIONS
            ],
            name="ay",
            hx_get=hx_get,
            hx_target=hx_target,
            hx_include=hx_include if hx_include else None,
            hx_push_url="true",
            cls="uk-select uk-form-small select-sm",
        ),
        cls="filter-group",
    )
