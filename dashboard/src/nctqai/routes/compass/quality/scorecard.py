"""Scorecard routes for the Compass Quality dashboard.

GET /compass/quality/scorecard
    Hero page showing 9 DimensionRows. 8 non-Sort dimensions show "no data yet".
    Includes build context strip and print-friendly styling.

GET /compass/quality/scorecard/<dim_slug>
    Drill-down for one dimension. Shows Scenarios × Cases × K3 strips.
    Links each failed case's session_id to the conversation detail page.

Both routes are admin-only (``require_compass_admin``) — Scorecard sits in the
Starling-only Compass admin set, not the power_user tabs.
"""
from __future__ import annotations

import logging
import uuid as _uuid_mod

from fasthtml.common import (
    A,
    Div,
    P,
    Span,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
)
from starlette.requests import Request

from nctqai.components.compass.quality import (
    DimensionRow,
    K3Strip,
    breadcrumb_strip,
    build_context_strip,
    last_sweep_strip,
    latest_finished_across,
)
from nctqai.layout import Layout
from nctqai.routes.compass._helpers import require_compass_admin
from nctqai.services.compass_quality.loaders import load_dimension, load_scorecard

logger = logging.getLogger(__name__)

_SCORECARD_URL = "/compass/quality/scorecard"


def _parse_sweep_run_id(request: Request) -> str | None:
    """Extract and validate the ?sweep_run_id=<UUID> query parameter.

    Returns the UUID string if valid, None if absent or empty.
    On a malformed (non-UUID) value, logs a warning and returns None so
    the route degrades gracefully to the latest sweep rather than crashing.
    """
    raw = request.query_params.get("sweep_run_id") or ""
    if not raw:
        return None
    try:
        _uuid_mod.UUID(raw)
        return raw
    except ValueError:
        logger.warning(
            "scorecard route: ignoring malformed sweep_run_id=%r (not a valid UUID)",
            raw,
        )
        return None


def _scorecard_hero(request: Request) -> tuple:
    """Render the 9-dimension Scorecard hero page."""
    user = getattr(request.state, "user", None)
    sweep_run_id = _parse_sweep_run_id(request)

    try:
        snapshot = load_scorecard(sweep_run_id=sweep_run_id)
    except Exception as exc:
        logger.exception("Scorecard hero: failed to load scorecard")
        content = Div(
            P("Could not load Scorecard data. Please try again.", cls="text-red-600"),
            P(str(exc), cls="text-sm text-gray-500"),
        )
        return Layout(
            "Compass Quality Scorecard",
            "",
            content,
            section="compass",
            sub_nav=_SCORECARD_URL,
            user=user,
            data_cluster="quality",
        )

    crumbs = breadcrumb_strip([
        ("Compass", "/compass/overview"),
        ("Quality Scorecard", None),
    ])

    context_strip = build_context_strip(snapshot.build)
    sweep_strip = last_sweep_strip(latest_finished_across(snapshot.dimensions))

    # Show the Trend column only when at least one dimension has a comparable
    # prior sweep (delta_pct is not None). Otherwise drop it entirely — header
    # Th and every row's Td together — so reviewers don't see a column full of
    # "no comparable prior sweep" placeholders. Computed once across all dims so
    # the header and the rows stay in sync (no orphan header, no empty column).
    has_prior_sweep = any(d.delta_pct is not None for d in snapshot.dimensions)

    # Build the 9-row table
    rows = [
        DimensionRow(dim, has_prior_sweep=has_prior_sweep)
        for dim in snapshot.dimensions
    ]

    header_cells = [
        Th("Dimension", cls="quality-scorecard-th"),
        Th("Score", cls="quality-scorecard-th quality-th-right"),
        Th("Threshold", cls="quality-scorecard-th quality-th-right"),
    ]
    if has_prior_sweep:
        header_cells.append(Th("Trend", cls="quality-scorecard-th quality-th-right"))
    header_cells += [
        Th("Cases", cls="quality-scorecard-th quality-th-right"),
        Th("n", cls="quality-scorecard-th quality-th-right"),
        Th("Details", cls="quality-scorecard-th"),
    ]

    table = Table(
        Thead(Tr(*header_cells)),
        Tbody(*rows),
        cls="quality-scorecard-table",
    )

    content = Div(crumbs, context_strip, sweep_strip, table)

    return Layout(
        "Compass Quality Scorecard",
        "Reviewer readout across 7 scorecard dimensions.",
        content,
        section="compass",
        sub_nav=_SCORECARD_URL,
        user=user,
        data_cluster="quality",
    )


def _dimension_detail(request: Request, dim_slug: str) -> tuple:
    """Render the drill-down for one dimension (Scenarios × Cases × K3 strips)."""
    user = getattr(request.state, "user", None)
    sweep_run_id = _parse_sweep_run_id(request)

    try:
        detail = load_dimension(dim_slug=dim_slug, sweep_run_id=sweep_run_id)
    except KeyError:
        content = Div(
            P(f"Dimension not found: {dim_slug!r}", cls="text-red-600"),
            A("Back to Scorecard", href=_SCORECARD_URL, cls="quality-breadcrumb-link"),
        )
        return Layout(
            f"Dimension: {dim_slug}",
            "",
            content,
            section="compass",
            sub_nav=_SCORECARD_URL,
            user=user,
            data_cluster="quality",
        )
    except Exception as exc:
        logger.exception("Scorecard drill-down: failed for %s", dim_slug)
        content = Div(
            P("Could not load dimension data.", cls="text-red-600"),
            P(str(exc), cls="text-sm text-gray-500"),
        )
        return Layout(
            f"Dimension: {dim_slug}",
            "",
            content,
            section="compass",
            sub_nav=_SCORECARD_URL,
            user=user,
            data_cluster="quality",
        )

    dim = detail.dimension

    crumbs = breadcrumb_strip([
        ("Compass", "/compass/overview"),
        ("Quality Scorecard", _SCORECARD_URL),
        (dim.name, None),
    ])

    context_strip = build_context_strip(detail.build)

    # Score summary
    if dim.n_trials == 0:
        score_summary = Div(
            Span("—", cls="quality-no-data-dash"),
            Span("No verdicts recorded yet for this dimension.", cls="quality-no-data-pill"),
            cls="quality-dim-score-summary",
        )
    else:
        score_summary = Div(
            Span(f"{dim.score_pct}%", cls="quality-score-pct"),
            Span(f" across {dim.n_trials} trials", cls="quality-score-label"),
            cls="quality-dim-score-summary",
        )

    # Cases table
    if not detail.cases:
        cases_section = P(
            "No cases with verdicts for this dimension yet. Run pa-eval to seed data.",
            cls="quality-no-data-pill",
        )
    else:
        case_rows = []
        for case in detail.cases:
            # Collect session links for this case
            session_links = []
            for trial in case.trials:
                session_url = f"/compass/quality/conversations/{trial.session_id}"
                outcome_cls = {
                    "pass": "quality-verdict-pass",
                    "fail": "quality-verdict-fail",
                    "error": "quality-verdict-error",
                }.get(trial.outcome, "")
                session_links.append(
                    A(
                        trial.session_id[:8] + "…",
                        href=session_url,
                        cls=f"quality-session-link {outcome_cls}",
                        title=f"{trial.outcome}: {trial.reason or ''}",
                    )
                )

            k3 = K3Strip(case.trials)
            rate_cls = ""
            if case.pass_rate_pct == 100:
                rate_cls = "quality-verdict-pass"
            elif case.pass_rate_pct == 0:
                rate_cls = "quality-verdict-fail"

            case_rows.append(Tr(
                Td(
                    Div(case.scenario_id, cls="quality-case-scenario"),
                    Div(case.name or case.case_id, cls="quality-case-name-text"),
                    cls="quality-case-name",
                ),
                Td(f"{case.pass_rate_pct}%", cls=f"quality-case-rate {rate_cls}"),
                Td(k3, cls="quality-case-k3"),
                Td(*session_links, cls="quality-case-sessions"),
                cls="quality-case-row",
            ))

        cases_section = Table(
            Thead(Tr(
                Th("Case", cls="quality-scorecard-th"),
                Th("Pass rate", cls="quality-scorecard-th quality-th-right"),
                Th("K=3 trials", cls="quality-scorecard-th"),
                Th("Sessions", cls="quality-scorecard-th"),
            )),
            Tbody(*case_rows),
            cls="quality-scorecard-table",
        )

    content = Div(
        crumbs,
        context_strip,
        Div(
            P(dim.definition, cls="quality-dim-definition"),
            score_summary,
        ),
        cases_section,
    )

    return Layout(
        f"Scorecard: {dim.name}",
        dim.definition,
        content,
        section="compass",
        sub_nav=_SCORECARD_URL,
        user=user,
        show_heading=False,
        data_cluster="quality",
    )


def register(rt):
    """Register scorecard routes with the FastHTML router."""

    @rt("/compass/quality/scorecard")
    def scorecard_hero(request: Request):
        _, deny = require_compass_admin(request)
        if deny:
            return deny
        return _scorecard_hero(request)

    @rt("/compass/quality/scorecard/{dim_slug}")
    def scorecard_drill_down(request: Request, dim_slug: str):
        _, deny = require_compass_admin(request)
        if deny:
            return deny
        return _dimension_detail(request, dim_slug)
