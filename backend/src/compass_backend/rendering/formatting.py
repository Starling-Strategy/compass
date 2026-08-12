"""Shared cell-formatting helpers for Compass deterministic renderers.

Lifted from `rendering/writer.py` so the markdown writer, the CSV builder
(`artifacts/results.py`), and the surface-consistency validator
(`quality/validators/surface.py`) all call the SAME function. When a
formatting rule changes, every surface changes atomically — no silent
divergence between table cells and exported rows.

Doctrine: surfaces apply surface-specific escaping ON TOP of a canonical
value (`row.display_value`, `row.state`, etc.). The escape is the
surface's responsibility, the value is the artifact's. Helpers in this
module live on the boundary: they accept the canonical value and return
the surface-ready form.
"""

from __future__ import annotations

from typing import Protocol


def escape_markdown_cell(value: str) -> str:
    """Escape pipe characters so a cell renders inside a markdown table.

    Markdown tables use `|` as the column separator. Cell values that
    contain `|` must escape it as `\\|` or the row gets parsed with the
    wrong column count. CSV cells do not apply this escape (CSV uses
    its own quoting rules), which is the canonical surface divergence
    the surface-consistency validator checks against.
    """
    return value.replace("|", "\\|")


def format_percent(value: float | None) -> str:
    """Render a percent value with one decimal, or `Unavailable` for None.

    Used by the categorical count renderer; lifted here so the validator
    can compute the expected cell without importing renderer internals.
    """
    if value is None:
        return "Unavailable"
    return f"{value:.1f}%"


def format_citation_markers(markers: list[int]) -> str:
    """Render citation markers as space-joined `[N]` tokens.

    Markdown writer uses this for the Sources column of every table type.
    Distinct from the CSV format (`" ".join(str(m) for m in markers)`)
    which omits the brackets. The renderer + validator import this to
    stay aligned.
    """
    return " ".join(f"[{marker}]" for marker in markers)


class _TrendRowLike(Protocol):
    """Minimal shape for `format_delta`. Matches `TrendRow` without importing it."""

    delta_value: float | None
    delta_from_academic_year: str | None


def format_delta(row: _TrendRowLike) -> str:
    """Render the trend delta cell.

    Empty when either `delta_value` or `delta_from_academic_year` is None
    (those rows have no comparable prior year). Integer deltas render as
    `$1,234`; fractional deltas render as `1,234.56` with two decimal
    places. The leading `+` only appears for positive deltas; negative
    deltas keep their native sign from the `,.0f` / `,.2f` formatter.
    """
    if row.delta_value is None or row.delta_from_academic_year is None:
        return ""
    prefix = "+" if row.delta_value > 0 else ""
    if float(row.delta_value).is_integer():
        value = f"{prefix}${row.delta_value:,.0f}"
    else:
        value = f"{prefix}{row.delta_value:,.2f}"
    return f"{value} since {row.delta_from_academic_year}"


# Display-policy thresholds shared by the renderers AND the fact-coverage
# validator (quality/validators/coverage.py), so the guard and the prose can't
# drift. Past this many matching names a count answer offers to list instead
# of dumping a table; below this denominator a share is more misleading than
# useful, so the match-denominator sentences state counts only (#744/#1415).
MATCHING_DISTRICTS_DISPLAY_LIMIT = 40
MIN_DENOMINATOR_FOR_PERCENT = 5


__all__ = [
    "MATCHING_DISTRICTS_DISPLAY_LIMIT",
    "MIN_DENOMINATOR_FOR_PERCENT",
    "escape_markdown_cell",
    "format_citation_markers",
    "format_delta",
    "format_percent",
]
