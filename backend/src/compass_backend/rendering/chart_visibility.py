"""Single authority for whether a chart is *shown* (#1240).

Two questions, kept distinct:

- **Eligibility — "can this be charted?"** A property of the result, decided
  upstream in the per-variant `populate_artifact_surfaces` validators
  (`artifacts/results.py`). They build `result.chart` only when the data is
  chartable (``_should_render_chart``: ≥3 numeric points, ≥2 distinct). So a
  built chart *is* the eligibility signal — we never recompute it here.
- **Visibility — "should a chart be shown?"** A render-boundary decision that
  needs user intent (``plan.output.format == "chart"``). The validators cannot
  see the plan, so this gate lives here and runs once, before rendering.

Mirrors the existing intent gate in ``execution/trend.py`` (`MetricTrendResult`
already gates on ``plan.output.format``), generalized to one seam so the other
four chart-building variants obey intent without four edited executors.

The decision is a frozen dataclass, not a Pydantic model, on purpose: storing
a stripped ``ResultSet`` in a Pydantic field would re-validate it and the
per-variant ``populate_artifact_surfaces`` after-validator would *rebuild* the
chart we just removed. A dataclass holds the already-stripped object as-is.
"""

from __future__ import annotations

from dataclasses import dataclass

from compass_backend.artifacts import (
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricTrendResult,
    PeerComparisonResult,
    ResultSet,
)
from compass_backend.contracts import QueryPlan

# Result types that *can* carry a chart: the four that auto-build one in their
# validators, plus trend (built on intent by its executor). For these, a missing
# chart on a chart request means the data was too thin to plot — a true
# "chart_unavailable". ProfileLookupResult (no chart builder) and
# CompositeRankingResult (charts live on children, top-level .chart is always
# None) never build a top-level chart structurally, so a missing chart there is
# not a thin-data signal — flagging chart_unavailable would narrate a false
# "not enough comparable data" reason.
_CHART_CAPABLE_RESULT_TYPES: tuple[type, ...] = (
    MetricRankingResult,
    MetricLookupResult,
    MetricCountResult,
    PeerComparisonResult,
    MetricTrendResult,
)


@dataclass(frozen=True)
class ChartVisibilityDecision:
    """Outcome of the chart-visibility gate.

    ``result`` is the result the rest of the pipeline must use (SSE, snapshot,
    CSV/table) — chart stripped when intent did not ask for it. The two flags
    are structured signals for downstream layers:

    - ``chart_unavailable`` — the user asked for a chart but the data could not
      support one. Threaded into the answer-layer input so the stylist can
      mention it naturally (and a deterministic fallback line when the layer is
      off), never silently dropping the fact.
    - ``suggestion_eligible`` — the data *could* be charted but the user did not
      ask. Drives the "want a chart?" follow-up suggestion (#1555).
    """

    result: ResultSet
    chart_unavailable: bool = False
    suggestion_eligible: bool = False


def resolve_chart_visibility(
    plan: QueryPlan, result: ResultSet
) -> ChartVisibilityDecision:
    """Decide whether ``result``'s built chart is shown, from user intent.

    Eligibility is read straight off ``result.chart`` (the validator built it
    iff chartable); visibility is ``plan.output.format == "chart"``.
    """

    requested = plan.output.format == "chart"
    eligible = result.chart is not None
    if requested and eligible:
        return ChartVisibilityDecision(result=result)
    if requested and not eligible:
        # Only narrate "couldn't draw it, too little comparable data" for types
        # that can actually carry a chart — otherwise the message states a false
        # thin-data reason (e.g. profile lookups have no chart builder at all).
        chart_unavailable = isinstance(result, _CHART_CAPABLE_RESULT_TYPES)
        return ChartVisibilityDecision(
            result=result, chart_unavailable=chart_unavailable
        )
    if not requested and eligible:
        # Set chart_suppressed so the strip survives Pydantic re-validation:
        # assigning this result into a ResultSet-typed field (ChatResponse,
        # TurnSnapshot) re-runs the after-validator, which would otherwise see
        # chart is None and rebuild it. The flag tells the builder to skip.
        return ChartVisibilityDecision(
            result=result.model_copy(
                update={"chart": None, "chart_suppressed": True}
            ),
            suggestion_eligible=True,
        )
    return ChartVisibilityDecision(result=result)


__all__ = ["ChartVisibilityDecision", "resolve_chart_visibility"]
