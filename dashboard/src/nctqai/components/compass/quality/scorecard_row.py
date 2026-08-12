"""DimensionRow — one row in the Scorecard hero table.

Branches on n_trials to distinguish "real score" from "no data yet":
- n_trials == 0  → shows "—" pill and "no data yet" note; no percentage color
- n_trials > 0   → shows score_pct% and n_trials count; no color on Scorecard hero
  (color is only on the K3Strip in the drill-down, per visual sobriety spec)

Click target links to /compass/quality/scorecard/<dim_slug>.
"""
from __future__ import annotations

from fasthtml.common import A, Div, Span, Td, Tr

from compass_backend.quality.scorecard_models import DimensionScore


def DimensionRow(
    dim: DimensionScore, *, show_link: bool = True, has_prior_sweep: bool = True
) -> Tr:
    """Render one row in the 9-dimension Scorecard table.

    Args:
        dim: DimensionScore with score_pct, n_trials, name, definition, dim_slug.
        show_link: When True, the dimension name links to the drill-down page.
        has_prior_sweep: When False, the Trend column is omitted from this row
            entirely (no Td emitted), so the whole column disappears as a unit
            when no dimension has a comparable prior sweep. The route computes
            this flag once across all dimensions and gates both the header Th
            and every row's Td with it. Defaults to True so a standalone row
            keeps its trend cell (matching the column when at least one
            dimension has a delta).
    """
    # Score cell — branches on n_trials
    if dim.n_trials == 0:
        score_cell = Td(
            Span("—", cls="quality-no-data-dash"),
            Span("no data yet", cls="quality-no-data-pill"),
            cls="quality-score-cell",
        )
    else:
        score_cell = Td(
            Span(f"{dim.score_pct}%", cls="quality-score-pct"),
            cls="quality-score-cell",
        )

    # n_trials cell
    if dim.n_trials == 0:
        n_cell = Td("—", cls="quality-n-cell quality-n-empty")
    else:
        n_cell = Td(f"n={dim.n_trials}", cls="quality-n-cell")

    if dim.threshold_pct is None:
        threshold_cell = Td("—", cls="quality-threshold-cell quality-threshold-no-data")
    else:
        threshold_cell = Td(
            Span(f">={dim.threshold_pct}%", cls="quality-threshold-value"),
            Span(dim.threshold_status.replace("_", " "), cls="quality-threshold-label"),
            cls=(
                "quality-threshold-cell "
                f"quality-threshold-{dim.threshold_status.replace('_', '-')}"
            ),
        )

    if dim.delta_pct is None:
        trend_cell = Td(
            "no comparable prior sweep",
            cls="quality-trend-cell quality-trend-empty",
        )
    else:
        if dim.delta_pct > 0:
            trend_text = f"+{dim.delta_pct}"
            trend_class = "quality-trend-positive"
        elif dim.delta_pct < 0:
            trend_text = str(dim.delta_pct)
            trend_class = "quality-trend-negative"
        else:
            trend_text = "0"
            trend_class = "quality-trend-flat"
        trend_cell = Td(
            Span(trend_text, cls=f"quality-trend-value {trend_class}"),
            cls="quality-trend-cell",
        )

    # When no dimension in the sweep has a comparable prior sweep, the route
    # passes has_prior_sweep=False and the Trend column is dropped entirely
    # (header Th + every row's Td together), rather than showing a column full
    # of "no comparable prior sweep" cells.
    trend_cells = (trend_cell,) if has_prior_sweep else ()

    cases_cell = Td(
        Span(str(dim.exemplar_case_count), cls="quality-exemplar-count"),
        Span(dim.exemplar_status, cls=f"quality-exemplar-status quality-exemplar-{dim.exemplar_status}"),
        cls="quality-exemplar-cell",
    )

    # Name cell — links when show_link=True (unconditionally; placeholder dims
    # link to a drill-down that shows "no data yet", which is acceptable UX)
    dim_url = f"/compass/quality/scorecard/{dim.dim_slug}"
    if show_link:
        name_el = A(dim.name, href=dim_url, cls="quality-dim-link")
    else:
        name_el = Span(dim.name, cls="quality-dim-name")

    name_cell = Td(
        name_el,
        Div(dim.definition, cls="quality-dim-definition"),
        cls="quality-name-cell",
    )

    details_cell = Td(
        A("Details", href=dim_url, cls="quality-detail-link") if show_link else "",
        cls="quality-detail-cell",
    )

    return Tr(
        name_cell,
        score_cell,
        threshold_cell,
        *trend_cells,
        cases_cell,
        n_cell,
        details_cell,
        cls="quality-dimension-row",
        data_dim=dim.dim_slug,
        data_has_data="true" if dim.n_trials > 0 else "false",
    )
