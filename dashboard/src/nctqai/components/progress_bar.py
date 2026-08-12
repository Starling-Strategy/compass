"""Stacked horizontal progress bar component.

Shows accepted (green), rejected (red), and unreviewed (gray) segments.
"""

from fasthtml.common import Div


def ProgressBar(accepted: int, rejected: int, unreviewed: int, total: int):
    """Render a stacked progress bar with accepted/rejected/unreviewed segments.

    Args:
        accepted: Count of accepted items.
        rejected: Count of rejected items.
        unreviewed: Count of unreviewed items.
        total: Total count (used for percentage calculation).
    """
    if total == 0:
        return Div(cls="progress-stacked")
    segments = []
    for count, cls in [
        (accepted, "progress-accepted"),
        (rejected, "progress-rejected"),
        (unreviewed, "progress-unreviewed"),
    ]:
        pct = count / total * 100
        if pct > 0:
            segments.append(Div(style=f"width:{pct}%", cls=cls))
    return Div(*segments, cls="progress-stacked")
