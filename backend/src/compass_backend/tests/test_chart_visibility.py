"""Unit tests for the single chart-visibility authority (#1240).

`resolve_chart_visibility` decides whether a built chart is *shown*, from
user intent (`plan.output.format == "chart"`). Eligibility — "can this be
charted?" — is decided upstream in the result validators and is encoded by
whether `result.chart` was built. These tests cover all four quadrants of
the requested-vs-eligible matrix.
"""

from __future__ import annotations

from compass_backend.artifacts import (
    ChartArtifact,
    ChartPoint,
    MetricRankingResult,
    ProfileLookupResult,
    ProfileValueRow,
    RankingRow,
)
from compass_backend.contracts import (
    ChatResponse,
    DirectResponse,
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    SessionState,
    TurnSnapshot,
)
from compass_backend.rendering.chart_visibility import (
    ChartVisibilityDecision,
    resolve_chart_visibility,
)


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.98,
        direct_response=DirectResponse(message="hi", reason="greeting"),
    )


def _plan(*, output_format: str) -> QueryPlan:
    return QueryPlan(
        question="Rank districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format=output_format),
    )


def _ranking_rows() -> list[RankingRow]:
    return [
        RankingRow(
            district_id=index,
            district_name=f"District {index:02d}",
            state="CA",
            metric_id=1234,
            metric_name="Average teacher starting salary",
            value=100_000 - index * 1_000,
            display_value=f"${100_000 - index * 1_000:,}",
            academic_year="2024 - 2025",
            rank=index,
            coverage_state="covered",
            coverage_display=f"${100_000 - index * 1_000:,}",
            coverage_reason="answer_value",
        )
        for index in range(1, 5)
    ]


def _eligible_result() -> MetricRankingResult:
    """A ranking with ≥3 distinct numeric points → validator builds a chart."""

    result = MetricRankingResult(
        rows=_ranking_rows(),
        total_considered=4,
        excluded_count=0,
        order_statement="Ranked by starting salary, highest to lowest.",
    )
    # Sanity: the validator must have auto-built the chart so "eligible" is real.
    assert result.chart is not None
    return result


def _ineligible_result() -> MetricRankingResult:
    """A single-row ranking → fewer than 3 points → validator builds no chart."""

    result = MetricRankingResult(
        rows=_ranking_rows()[:1],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by starting salary, highest to lowest.",
    )
    assert result.chart is None
    return result


def test_requested_and_eligible_keeps_chart() -> None:
    plan = _plan(output_format="chart")
    result = _eligible_result()

    decision = resolve_chart_visibility(plan, result)

    assert isinstance(decision, ChartVisibilityDecision)
    assert decision.result.chart is not None
    assert decision.chart_unavailable is False
    assert decision.suggestion_eligible is False
    # The kept result is the same object — no copy needed when nothing changes.
    assert decision.result is result


def test_requested_but_not_eligible_flags_chart_unavailable() -> None:
    plan = _plan(output_format="chart")
    result = _ineligible_result()

    decision = resolve_chart_visibility(plan, result)

    assert decision.result.chart is None
    assert decision.chart_unavailable is True
    assert decision.suggestion_eligible is False


def test_not_requested_but_eligible_strips_chart_and_offers_suggestion() -> None:
    plan = _plan(output_format="table")
    result = _eligible_result()

    decision = resolve_chart_visibility(plan, result)

    assert decision.result.chart is None
    assert decision.suggestion_eligible is True
    assert decision.chart_unavailable is False
    # The strip is a copy; the original result is untouched (frozen, shared).
    assert result.chart is not None
    assert decision.result is not result


def test_not_requested_and_not_eligible_does_nothing() -> None:
    plan = _plan(output_format="table")
    result = _ineligible_result()

    decision = resolve_chart_visibility(plan, result)

    assert decision.result is result
    assert decision.result.chart is None
    assert decision.chart_unavailable is False
    assert decision.suggestion_eligible is False


def test_requested_chart_for_never_charts_type_is_not_chart_unavailable() -> None:
    """A profile lookup has no chart builder at all, so a chart request on it
    must NOT be flagged chart_unavailable — that would narrate a false
    "not enough comparable data" reason for a type that structurally never
    charts."""

    plan = _plan(output_format="chart")
    result = ProfileLookupResult(
        rows=[
            ProfileValueRow(
                district_id=index,
                district_name=f"District {index:02d}",
                field_key="enrollment",
                label="Enrollment",
                display_value=str(10_000 + index),
                academic_year="2024 - 2025",
                source_label="NCES district profile",
                provenance="nces",
                metric_name="Enrollment",
            )
            for index in range(1, 4)
        ],
        total_considered=3,
        excluded_count=0,
        order_statement="Profile values for selected districts.",
    )
    assert result.chart is None  # structural: no profile chart builder

    decision = resolve_chart_visibility(plan, result)

    assert decision.result.chart is None
    assert decision.chart_unavailable is False
    assert decision.suggestion_eligible is False


def test_decision_is_frozen() -> None:
    decision = ChartVisibilityDecision(result=_ineligible_result())
    try:
        decision.chart_unavailable = True  # type: ignore[misc]
    except Exception:  # noqa: BLE001
        return
    raise AssertionError("ChartVisibilityDecision must be frozen")


def test_chart_field_is_the_eligibility_signal() -> None:
    """Eligibility is read straight off result.chart, not recomputed."""

    plan = _plan(output_format="table")
    # Hand-build an eligible-looking result by attaching a chart artifact.
    base = _ineligible_result()
    charted = base.model_copy(
        update={
            "chart": ChartArtifact(
                title="x",
                x_axis_label="District",
                y_axis_label="Salary",
                points=[
                    ChartPoint(label="A", value=1.0, district_id=1),
                    ChartPoint(label="B", value=2.0, district_id=2),
                    ChartPoint(label="C", value=3.0, district_id=3),
                ],
            )
        }
    )

    decision = resolve_chart_visibility(plan, charted)

    assert decision.suggestion_eligible is True
    assert decision.result.chart is None


def test_stripped_chart_survives_pydantic_re_validation() -> None:
    """The strip must hold across re-validation at the real boundaries.

    Assigning a result into a ResultSet-typed field (ChatResponse,
    TurnSnapshot) re-runs the per-variant after-validator. Without the
    chart_suppressed marker the validator sees chart is None and rebuilds it —
    silently resurrecting the chart SSE serializes and the snapshot replays.
    This is the regression that proves the gate holds end-to-end (the isolated
    decision tests pass even without the marker because dataclasses don't
    re-validate)."""

    stripped = resolve_chart_visibility(
        _plan(output_format="table"), _eligible_result()
    ).result
    assert stripped.chart is None
    assert stripped.chart_suppressed is True

    response = ChatResponse(
        message="Ranked districts by starting salary.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=stripped,
    )
    assert response.result is not None
    assert response.result.chart is None

    snapshot = TurnSnapshot(
        session_id="session-1",
        turn_index=1,
        user_message="Rank districts by starting salary.",
        assistant_message="Ranked districts by starting salary.",
        planner_turn=_direct_turn(),
        result=stripped,
    )
    assert snapshot.result is not None
    assert snapshot.result.chart is None
