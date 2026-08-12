"""Deterministic gate for charts-on-request-only (#1240, EXPORT-R4).

`chart_visibility_matches_intent` is the structural check the answer-text judge
cannot be: chart presence is the artifact `result.chart`, which is absent from
the judge's evidence envelope. So a deterministic validator reads the served
result against `plan.output.format` and fails the one unambiguous bug
direction — an unrequested chart on a non-chart-intent turn. The requested but
thin-data case (no chart drawn) is left to the `chart_unavailable_explained`
judge, so this validator passes it.
"""

from __future__ import annotations

import pytest

from compass_backend.artifacts import MetricRankingResult, RankingRow
from compass_backend.contracts import MetricSpec, OutputSpec, QueryPlan, SelectionSpec
from compass_backend.quality.validators import registry
from compass_backend.rendering.chart_visibility import resolve_chart_visibility

VALIDATOR = registry.lookup("chart_visibility_matches_intent")


def _plan(*, output_format: str) -> QueryPlan:
    return QueryPlan(
        question="Rank districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format=output_format),
    )


def _ranking_rows(n: int) -> list[RankingRow]:
    return [
        RankingRow(
            district_id=i,
            district_name=f"District {i:02d}",
            state="CA",
            metric_id=1234,
            metric_name="Average teacher starting salary",
            value=100_000 - i * 1_000,
            display_value=f"${100_000 - i * 1_000:,}",
            academic_year="2024 - 2025",
            rank=i,
            coverage_state="covered",
            coverage_display=f"${100_000 - i * 1_000:,}",
            coverage_reason="answer_value",
        )
        for i in range(1, n + 1)
    ]


def _result_with_chart() -> MetricRankingResult:
    """≥3 distinct points → the result validator auto-builds a chart."""
    result = MetricRankingResult(
        rows=_ranking_rows(4),
        total_considered=4,
        excluded_count=0,
        order_statement="Ranked by salary, highest to lowest.",
    )
    assert result.chart is not None
    return result


def _result_thin() -> MetricRankingResult:
    """A single row → fewer than 3 points → no chart is built (not eligible)."""
    result = MetricRankingResult(
        rows=_ranking_rows(1),
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by salary, highest to lowest.",
    )
    assert result.chart is None
    return result


@pytest.mark.anyio
async def test_unrequested_chart_on_table_intent_fails(evaluator_context) -> None:
    """The #1240 bug: a chart attached to a table-intent answer must fail."""
    ctx = evaluator_context(
        route="execute",
        plan=_plan(output_format="table"),
        result=_result_with_chart(),
    )
    outcome = await VALIDATOR(ctx, {})
    assert outcome.outcome == "fail"
    assert "EXPORT-R4" in outcome.reason


@pytest.mark.anyio
async def test_gated_table_intent_passes(evaluator_context) -> None:
    """After the orchestration resolver strips the chart, the same turn passes."""
    decision = resolve_chart_visibility(
        _plan(output_format="table"), _result_with_chart()
    )
    assert decision.result.chart is None  # the resolver stripped it
    ctx = evaluator_context(
        route="execute",
        plan=_plan(output_format="table"),
        result=decision.result,
    )
    outcome = await VALIDATOR(ctx, {})
    assert outcome.outcome == "pass"


@pytest.mark.anyio
async def test_requested_chart_present_passes(evaluator_context) -> None:
    ctx = evaluator_context(
        route="execute",
        plan=_plan(output_format="chart"),
        result=_result_with_chart(),
    )
    outcome = await VALIDATOR(ctx, {})
    assert outcome.outcome == "pass"


@pytest.mark.anyio
async def test_requested_chart_thin_data_passes(evaluator_context) -> None:
    """Requested but too few points to plot: this validator passes; the graceful
    "couldn't draw it" note is graded by the judge, not here."""
    ctx = evaluator_context(
        route="execute",
        plan=_plan(output_format="chart"),
        result=_result_thin(),
    )
    outcome = await VALIDATOR(ctx, {})
    assert outcome.outcome == "pass"


@pytest.mark.anyio
async def test_no_result_is_not_applicable(evaluator_context) -> None:
    """A non-execute turn produces no result — not applicable, never a failure."""
    ctx = evaluator_context(
        route="clarify",
        plan=_plan(output_format="chart"),
        result=None,
    )
    outcome = await VALIDATOR(ctx, {})
    assert outcome.outcome != "fail"
    assert "not applicable" in outcome.reason
