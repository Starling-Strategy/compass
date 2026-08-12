"""Turn card component — compact conversation replay.

Each turn renders a user→assistant exchange as a card with progressive
disclosure for critic verdicts and direct trace links when available.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime

from fasthtml.common import A, Div, NotStr, Span, Table, Tbody, Td, Th, Thead, Tr

from nctqai.components.compass.timestamps import format_relative_eastern_timestamp
from markdown_it import MarkdownIt

from compass_backend.quality.scorecard_models import Verdict
from nctqai.components.compass.quality.verdict_list import VerdictList


INTENT_LABELS = {
    "DATA_LOOKUP": "Data",
    "COMPARISON": "Compare",
    "POLICY_RESEARCH": "Research",
    "NORMATIVE": "Normative",
    "GREETING": "Greeting",
    "CLARIFICATION": "Clarify",
    "OFF_TOPIC": "Off-topic",
}

_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")

COMPLEXITY_STYLES = {
    "QUICK": ("Quick", "compass-complexity-quick"),
    "STANDARD": ("Standard", "compass-complexity-standard"),
    "DEEP": ("Deep", "compass-complexity-deep"),
}


def _format_tokens(count: int) -> str:
    """Format token count compactly (e.g., 1.2k, 340)."""
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _format_cost(cost: float) -> str:
    """Format cost as compact dollar amount."""
    if cost < 0.001:
        return "<$0.001"
    if cost < 0.01:
        return f"${cost:.3f}"
    return f"${cost:.2f}"


def _format_duration(ms: int | None) -> str:
    """Format duration from ms to human-readable."""
    if ms is None:
        return ""
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


_CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")


def _citation_index(citations: list | None) -> dict[int, dict]:
    """Map integer citation marker → citation dict, for citations only.

    The saved snapshot ``result.citations`` entries carry a ``marker`` int plus
    a resolvable ``url``/``title`` (see the W2-3 snapshot shape). We key by the
    marker so the prose markers ``[1]``, ``[2]`` can be turned into links.
    """
    index: dict[int, dict] = {}
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        try:
            marker_int = int(citation.get("marker"))
        except (TypeError, ValueError):
            continue
        index[marker_int] = citation
    return index


def _linkify_citations(rendered_html: str, citations: list | None) -> str:
    """Turn ``[n]`` markers in rendered prose into links to their citation URL.

    Only markers that match a real citation in ``result.citations`` are linked;
    a coincidental ``[7]`` with no matching citation stays plain text. A citation
    with no URL also stays plain text (nothing resolvable to link to). The visible
    text is kept as the marker the user saw (``[1]``); an empty/missing title
    surfaces honestly as "Unknown Source" in the hover title — no invented prose.

    NOTE: a marker that happens to sit inside a fenced code block would also be
    linkified (the regex runs over the whole rendered HTML). That is a low-risk
    edge case for policy answers, which don't embed ``[n]`` inside code.
    """
    index = _citation_index(citations)
    if not index:
        return rendered_html

    def _replace(match: re.Match) -> str:
        marker_int = int(match.group(1))
        citation = index.get(marker_int)
        if citation is None:
            return match.group(0)
        url = (citation.get("url") or "").strip()
        if not url:
            return match.group(0)
        title = (citation.get("title") or "").strip() or "Unknown Source"
        href = _html.escape(url, quote=True)
        title_attr = _html.escape(title, quote=True)
        return (
            f'<a href="{href}" target="_blank" rel="noopener" '
            f'class="compass-citation-link" title="{title_attr}">[{marker_int}]</a>'
        )

    return _CITATION_MARKER_RE.sub(_replace, rendered_html)


def _render_markdown(text: str, citations: list | None = None) -> NotStr:
    """Render assistant markdown safely, linking citation markers when known."""
    rendered = _MARKDOWN.render(text or "")
    if citations:
        rendered = _linkify_citations(rendered, citations)
    return NotStr(rendered)


# Readable subset of the saved snapshot row fields, in display order. The full
# 35-column ``csv_export`` is for the download, not the inline table — these are
# the few columns a reviewer actually reads. Values are rendered VERBATIM from
# the saved row (``display_value`` etc.), never recomputed.
_SNAPSHOT_TABLE_COLUMNS: list[tuple[str, str]] = [
    ("district_name", "District"),
    ("state", "State"),
    ("display_value", "Value"),
    ("coverage_state", "Coverage"),
    ("citation_markers", "Sources"),
]


def _cell_text(value: object) -> str:
    """Stringify a saved row value verbatim (lists join with commas)."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _snapshot_table(rows: list | None) -> Div | None:
    """Render the saved snapshot ``result.rows`` as a readable HTML table.

    Returns None when there are no usable rows — the canonical "has a table"
    signal is a non-empty ``result.rows`` array (the same definition the
    has-table triage tab uses). Only the readable subset of columns that is
    actually present in the rows is shown.
    """
    norm_rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not norm_rows:
        return None

    columns: list[tuple[str, str]] = []
    for key, label in _SNAPSHOT_TABLE_COLUMNS:
        if key == "display_value":
            present = any(
                row.get("display_value") is not None or row.get("value") is not None
                for row in norm_rows
            )
        else:
            present = any(row.get(key) is not None for row in norm_rows)
        if present:
            columns.append((key, label))
    if not columns:
        return None

    head = Thead(Tr(*[Th(label) for _, label in columns]))
    body_rows = []
    for row in norm_rows:
        cells = []
        for key, _ in columns:
            if key == "display_value":
                value = row.get("display_value")
                if value is None:
                    value = row.get("value")
            else:
                value = row.get(key)
            cells.append(Td(_cell_text(value)))
        body_rows.append(Tr(*cells))

    count = len(norm_rows)
    label = f"Data table ({count} row{'s' if count != 1 else ''})"
    return Div(
        Div(label, cls="compass-snapshot-table-label"),
        Table(head, Tbody(*body_rows), cls="compass-snapshot-table"),
        cls="compass-snapshot",
    )


# ── Chart rendering (#1809) ─────────────────────────────────────────────────
# The saved snapshot carries a ChartArtifact at ``result.chart`` (built in
# compass_backend/artifacts/results.py and serialized verbatim via
# ResultSet.model_dump). nctqai must not import the backend artifact type for
# runtime behavior, so we read the loaded dict STRUCTURALLY. We render a small
# self-contained SVG (no JS/Chart.js vendor needed server-side) that mirrors the
# chat front-end's bar/line shape, so a reviewer sees the same chart the user
# saw. The grounded data table renders alongside it, so the chart only needs an
# accessible summary label, not a duplicate data table for screen readers.

# Chart palette mirrors the chat front-end's secondary chart colors.
_CHART_PALETTE = ["#4A91D0", "#F68E1E", "#A0C620", "#DB4545", "#997C23", "#36682B"]

# SVG coordinate system (responsive via viewBox; these are internal units).
_CHART_W = 640
_CHART_H = 320
_CHART_PAD_L = 52
_CHART_PAD_R = 16
_CHART_PAD_T = 16
_CHART_PAD_B = 72


def _chart_points(points: object) -> list[tuple[str, float]]:
    """Normalize a list of ChartPoint dicts to ``(label, value)`` pairs.

    Non-dict entries and non-numeric values are skipped — the saved artifact is
    trusted but we render defensively so a malformed point never 500s the page.
    """
    out: list[tuple[str, float]] = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        value = point.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out.append((str(point.get("label") or ""), float(value)))
    return out


def _chart_series(chart: dict) -> list[tuple[str, list[tuple[str, float]]]]:
    """Normalize the ChartArtifact dict to ``[(series_label, points)]``.

    A multi-metric artifact carries ``series`` (each its own metric); a
    single-series artifact (ranking/count, or a single-metric lookup) carries
    top-level ``points``. We prefer ``series`` when present and fall back to the
    flat ``points``, mirroring how the chat front-end resolves the same shape.
    """
    raw_series = chart.get("series")
    if isinstance(raw_series, list) and raw_series:
        normalized: list[tuple[str, list[tuple[str, float]]]] = []
        for series in raw_series:
            if not isinstance(series, dict):
                continue
            points = _chart_points(series.get("points"))
            if points:
                normalized.append((str(series.get("label") or "Series"), points))
        if normalized:
            return normalized
    points = _chart_points(chart.get("points"))
    if points:
        return [(str(chart.get("title") or "Value"), points)]
    return []


def _chart_value_text(value: float) -> str:
    """Format a chart value for the bar/point tooltip and y-axis tick."""
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.1f}"


def _truncate_label(label: str, limit: int = 18) -> str:
    """Trim a long category label so the rotated x-axis text stays readable."""
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _chart_svg_body(
    series: list[tuple[str, list[tuple[str, float]]]], *, is_line: bool
) -> str:
    """Build the inner SVG markup (axes, bars/lines, labels) for the artifact.

    Categories come from the longest series and bars/points align by position.
    Multi-metric series that cover DIFFERENT district sets could therefore sit
    bars under the wrong label; in practice the lookup builder draws each
    metric's series from the same district selection, and the grounded data
    table alongside carries the source of truth — so this is a known, accepted
    limitation for the not-launch-critical chart, not a data-fidelity claim.
    """
    categories = max((points for _, points in series), key=len)
    labels = [label for label, _ in categories]
    n_cat = len(labels)
    max_val = max(
        (value for _, points in series for _, value in points), default=0.0
    )
    if max_val <= 0:
        max_val = 1.0

    plot_w = _CHART_W - _CHART_PAD_L - _CHART_PAD_R
    plot_h = _CHART_H - _CHART_PAD_T - _CHART_PAD_B
    baseline_y = _CHART_PAD_T + plot_h
    band = plot_w / n_cat

    def x_center(i: int) -> float:
        return _CHART_PAD_L + band * (i + 0.5)

    def y_for(value: float) -> float:
        return _CHART_PAD_T + plot_h * (1 - value / max_val)

    parts: list[str] = [
        f'<line x1="{_CHART_PAD_L}" y1="{baseline_y}" '
        f'x2="{_CHART_PAD_L + plot_w}" y2="{baseline_y}" stroke="#cbd5e1" />',
        f'<line x1="{_CHART_PAD_L}" y1="{_CHART_PAD_T}" '
        f'x2="{_CHART_PAD_L}" y2="{baseline_y}" stroke="#cbd5e1" />',
    ]

    if is_line:
        for s_idx, (s_label, points) in enumerate(series):
            color = _CHART_PALETTE[s_idx % len(_CHART_PALETTE)]
            coords = " ".join(
                f"{x_center(i):.1f},{y_for(value):.1f}"
                for i, (_, value) in enumerate(points)
            )
            parts.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="2" '
                f'points="{coords}" />'
            )
            for i, (p_label, value) in enumerate(points):
                title = _html.escape(f"{s_label} — {p_label}: {_chart_value_text(value)}")
                parts.append(
                    f'<circle cx="{x_center(i):.1f}" cy="{y_for(value):.1f}" '
                    f'r="3" fill="{color}"><title>{title}</title></circle>'
                )
    else:
        n_series = len(series)
        group_w = band * 0.85
        bar_w = group_w / n_series
        for s_idx, (s_label, points) in enumerate(series):
            color = _CHART_PALETTE[s_idx % len(_CHART_PALETTE)]
            for i, (p_label, value) in enumerate(points):
                x = _CHART_PAD_L + band * i + (band - group_w) / 2 + bar_w * s_idx
                y = y_for(value)
                height = baseline_y - y
                title = _html.escape(f"{s_label} — {p_label}: {_chart_value_text(value)}")
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                    f'height="{height:.1f}" fill="{color}" rx="2">'
                    f"<title>{title}</title></rect>"
                )

    for i, label in enumerate(labels):
        cx = x_center(i)
        ty = baseline_y + 12
        parts.append(
            f'<text x="{cx:.1f}" y="{ty:.1f}" font-size="10" fill="#64748b" '
            f'text-anchor="end" transform="rotate(-35 {cx:.1f} {ty:.1f})">'
            f"{_html.escape(_truncate_label(label))}</text>"
        )

    parts.append(
        f'<text x="{_CHART_PAD_L - 6}" y="{_CHART_PAD_T + 4}" font-size="10" '
        f'fill="#64748b" text-anchor="end">'
        f"{_html.escape(_chart_value_text(max_val))}</text>"
    )
    parts.append(
        f'<text x="{_CHART_PAD_L - 6}" y="{baseline_y}" font-size="10" '
        f'fill="#64748b" text-anchor="end">0</text>'
    )
    return "".join(parts)


def _chart_artifact(chart: object) -> Div | None:
    """Render the saved ChartArtifact dict as a self-contained SVG chart.

    Returns None when the artifact is missing or carries no plottable points, so
    a turn without a chart (or a pre-snapshot turn) shows nothing — never a
    broken element. ``artifact_type == "line_chart"`` renders a line chart;
    everything else renders bars (the only live shape today).
    """
    if not isinstance(chart, dict):
        return None
    series = _chart_series(chart)
    if not series:
        return None

    is_line = str(chart.get("artifact_type")) == "line_chart"
    title = str(chart.get("title") or "Chart")
    x_label = str(chart.get("x_axis_label") or "")
    y_label = str(chart.get("y_axis_label") or "")
    aria = " — ".join(part for part in [title, x_label, y_label] if part)

    body = _chart_svg_body(series, is_line=is_line)
    plot_w = _CHART_W - _CHART_PAD_L - _CHART_PAD_R
    x_axis_title = (
        f'<text x="{_CHART_PAD_L + plot_w / 2:.1f}" y="{_CHART_H - 6}" '
        f'font-size="11" fill="#0F223D" text-anchor="middle">'
        f"{_html.escape(x_label)}</text>"
        if x_label
        else ""
    )
    svg = (
        f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" '
        f'class="compass-snapshot-chart-svg" role="img" '
        f'aria-label="{_html.escape(aria)}" '
        f'preserveAspectRatio="xMidYMid meet">{body}{x_axis_title}</svg>'
    )

    legend = None
    if chart.get("show_legend") and len(series) > 1:
        chips = []
        for s_idx, (s_label, _) in enumerate(series):
            color = _CHART_PALETTE[s_idx % len(_CHART_PALETTE)]
            chips.append(
                Span(
                    Span(cls="compass-chart-legend-swatch", style=f"background: {color}"),
                    s_label,
                    cls="compass-chart-legend-item",
                )
            )
        legend = Div(*chips, cls="compass-chart-legend")

    return Div(
        Div(title, cls="compass-snapshot-table-label"),
        Div(NotStr(svg), cls="compass-snapshot-chart"),
        legend,
        cls="compass-snapshot",
    )


def VerdictPanel(
    verdict: str | None = None,
    score: float | None = None,
    dimensions: dict | None = None,
    feedback: str | None = None,
    turn_id: str = "0",
) -> Div:
    """Render a critic verdict panel with score bars and feedback.

    Args:
        verdict: "approved", "revision_needed", or "override"
        score: Overall score (0-5)
        dimensions: Dict of dimension_name → score (e.g., {"accuracy": 4, "completeness": 3})
        feedback: Critic feedback text
        turn_id: Unique ID for expand/collapse targeting
    """
    if not verdict:
        return Div()

    verdict_cls_map = {
        "approved": "compass-verdict-approved",
        "revision_needed": "compass-verdict-revision",
        "override": "compass-verdict-override",
    }
    verdict_label_map = {
        "approved": "pass",
        "revision_needed": "fail",
        "override": "override",
    }

    verdict_cls = verdict_cls_map.get(verdict, "")
    verdict_label = verdict_label_map.get(verdict, verdict.replace("_", " ").lower())

    # Score bars for dimensions
    score_bars = []
    if dimensions:
        for dim_name, dim_score in dimensions.items():
            if isinstance(dim_score, (int, float)):
                pct = min(dim_score / 5 * 100, 100)
                display_name = dim_name.replace("_", " ").title()
                score_bars.append(
                    Div(
                        Div(
                            Span(display_name, cls="compass-score-label"),
                            Span(f"{dim_score:.1f}", cls="compass-score-value"),
                            cls="compass-score-header",
                        ),
                        Div(
                            Div(cls="compass-score-fill", style=f"width: {pct}%"),
                            cls="compass-score-track",
                        ),
                        cls="compass-score-bar",
                    )
                )

    verdict_detail_id = f"verdict-detail-{turn_id}"

    parts = [
        # Verdict badge (always visible, click to expand)
        Div(
            Span(verdict_label, cls=f"compass-verdict-badge {verdict_cls}"),
            Span(f"{score:.1f}" if score else "", cls="compass-verdict-score") if score else None,
            Span("Details", cls="compass-verdict-toggle"),
            cls="compass-verdict-header",
            onclick=f"document.getElementById('{verdict_detail_id}').classList.toggle('expanded')",
        ),
    ]

    # Expandable detail
    detail_parts = []
    if score_bars:
        detail_parts.append(Div(*score_bars, cls="compass-score-bars"))
    if feedback:
        detail_parts.append(
            Div(
                Div("Feedback", cls="compass-verdict-feedback-label"),
                Div(feedback, cls="compass-verdict-feedback-text"),
                cls="compass-verdict-feedback",
            )
        )

    if detail_parts:
        parts.append(Div(*detail_parts, id=verdict_detail_id, cls="compass-verdict-detail"))

    return Div(*parts, cls="compass-verdict-panel")


def TurnCard(
    turn_number: int,
    user_message: str,
    assistant_response: str,
    timestamp: datetime | None = None,
    intent: str | None = None,
    complexity: str | None = None,
    verdict: dict | None = None,
    total_cost: float | None = None,
    total_duration_ms: int | None = None,
    trace_id: str | None = None,
    feedback_rating: int | None = None,
    feedback_tags: list[str] | None = None,
    feedback_text: str | None = None,
    l1_verdicts: list[Verdict] | None = None,
    snapshot_rows: list | None = None,
    citations: list | None = None,
    csv_download: object | None = None,
    chart: object | None = None,
) -> Div:
    """Render a complete turn card — one user→assistant exchange.

    This is the core component of the conversation detail view. It keeps the
    answer readable first and moves instrumentation into the footer.

    ``l1_verdicts`` are the real per-turn L1 verdicts from compass.verdicts
    (DASH-R5 — unify the quality detail into the main detail). ``None`` means
    "this card has no matched backend turn" (an orphan/short turn the verdict
    reader dropped) → no verdict block. An empty list means "this is a paired
    turn that simply has zero verdicts recorded" → VerdictList shows its honest
    "No verdicts recorded" state so coverage gaps stay visible, never faked.
    """
    turn_id = str(turn_number)

    # ── Turn header ──
    header_parts = [
        Span(f"Turn {turn_number}", cls="compass-turn-number"),
    ]
    if timestamp:
        time_str = format_relative_eastern_timestamp(timestamp)
        header_parts.append(Span(time_str, cls="compass-turn-time"))
    if intent:
        label = INTENT_LABELS.get(intent, intent.replace("_", " ").title())
        header_parts.append(Span(label, cls="compass-intent-badge"))
    if complexity:
        label, cls = COMPLEXITY_STYLES.get(complexity, (complexity, "compass-complexity-standard"))
        header_parts.append(Span(label, cls=cls))

    # Right-aligned verdict pill — summarizes the turn at a glance. Tier-1
    # data source is the user feedback rating; the Tier-2 verdict-tally
    # join will replace this with a true per-turn pass/fail/skip count.
    if feedback_rating is not None and feedback_rating > 0:
        header_parts.append(
            Span(
                "helpful",
                cls="compass-turn-verdict-pill compass-turn-verdict-pass",
                title="User marked helpful",
            )
        )
    elif feedback_rating is not None and feedback_rating < 0:
        tag_suffix = f" · {', '.join(feedback_tags)}" if feedback_tags else ""
        header_parts.append(
            Span(
                f"flagged{tag_suffix}",
                cls="compass-turn-verdict-pill compass-turn-verdict-fail",
                title=feedback_text or "User marked not helpful",
            )
        )

    header = Div(*header_parts, cls="compass-turn-header")

    # ── User message ──
    user_msg = Div(
        Div("Q", cls="compass-msg-avatar compass-msg-avatar-user"),
        Div(user_message, cls="compass-msg-text"),
        cls="compass-msg compass-msg-user",
    )

    # ── Assistant response ──
    assistant_text_children = [
        Div(
            _render_markdown(assistant_response, citations),
            cls="compass-msg-text compass-msg-response",
        )
    ]

    # Structured snapshot table — ADDITIVE to the prose. The prose may already
    # carry an LLM-written markdown table, but this renders the grounded
    # source-of-truth rows the answer was built from, so a reviewer can compare
    # what the user saw against the saved data. The per-turn CSV download (built
    # by the detail layer from the verbatim ``csv_export``) sits alongside it.
    snapshot_table = _snapshot_table(snapshot_rows)
    chart_el = _chart_artifact(chart)
    if snapshot_table is not None or chart_el is not None or csv_download is not None:
        snapshot_children = []
        if chart_el is not None:
            snapshot_children.append(chart_el)
        if snapshot_table is not None:
            snapshot_children.append(snapshot_table)
        if csv_download is not None:
            snapshot_children.append(
                Div(csv_download, cls="compass-snapshot-actions")
            )
        assistant_text_children.append(
            Div(*snapshot_children, cls="compass-snapshot-block")
        )

    if feedback_text and feedback_rating is not None and feedback_rating < 0:
        assistant_text_children.append(
            Div(
                Span("User feedback:", cls="compass-feedback-note-label"),
                Span(feedback_text, cls="compass-feedback-note-text"),
                cls="compass-feedback-note",
            )
        )
    assistant_msg = Div(
        Div("A", cls="compass-msg-avatar compass-msg-avatar-assistant"),
        Div(*assistant_text_children, cls="compass-msg-text-wrap"),
        cls="compass-msg compass-msg-assistant",
    )

    # ── Inline L1 verdicts (real, from compass.verdicts) ──
    # Rendered for every paired turn (l1_verdicts is not None), even when the
    # list is empty — VerdictList's own empty state shows the coverage gap
    # honestly. ``None`` (an unmatched orphan card) renders nothing.
    l1_verdict_block = None
    if l1_verdicts is not None:
        l1_verdict_block = Div(
            VerdictList(l1_verdicts, auto_expand_on_failure=True),
            cls="compass-turn-verdicts",
        )

    # ── Legacy critic verdict panel (dead: ``verdict`` dict is never populated
    # by the live detail path; left intact per the surgical-diff rule) ──
    verdict_component = None
    if verdict:
        verdict_component = VerdictPanel(
            verdict=verdict.get("verdict"),
            score=verdict.get("score"),
            dimensions=verdict.get("dimensions"),
            feedback=verdict.get("feedback"),
            turn_id=turn_id,
        )

    # ── Turn footer ──
    footer_parts = []
    if total_cost is not None:
        footer_parts.append(Span(_format_cost(total_cost), cls="compass-turn-footer-stat"))
    if total_duration_ms is not None:
        footer_parts.append(
            Span(_format_duration(total_duration_ms), cls="compass-turn-footer-stat")
        )
    if trace_id:
        footer_parts.append(
            A(
                "Trace",
                href=f"https://logfire-us.pydantic.dev/murmuration/nctqai?tid={trace_id}",
                target="_blank",
                cls="compass-turn-footer-link",
            )
        )

    footer = Div(*footer_parts, cls="compass-turn-footer") if footer_parts else None

    return Div(
        header,
        user_msg,
        assistant_msg,
        l1_verdict_block,
        verdict_component,
        footer,
        id=f"turn-{turn_number}",
        cls="compass-turn-card",
    )
