"""W2-M3-01 (#806): CompositeRankingResult typed artifact.

The composite envelope carries N validated MetricRankingResult children
in one ResultSet so the user's accepted N-metric "do all N separately"
follow-up renders as N tables, each with its own typed metric / citations
/ coverage. The renderer formats only validated composite artifacts —
no free-text inference of "split this table here."
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.artifacts import (
    CompositeRankingResult,
    MetricRankingResult,
    RESULT_SET_TYPES,
    ResultSelection,
    RankingRow,
)
from compass_backend.artifacts.results import _ResultSetBase


def _ranking(
    metric_id: int,
    metric_name: str,
    *,
    total_considered: int = 5,
    excluded_count: int = 0,
) -> MetricRankingResult:
    return MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=metric_id,
                metric_name=metric_name,
                value=100.0,
                display_value="100",
                academic_year="2024 - 2025",
                rank=1,
                source="policy_answer",
                coverage_state="covered",
                coverage_display="100",
                coverage_reason="answer",
            ),
        ],
        total_considered=total_considered,
        excluded_count=excluded_count,
        order_statement=f"Ranked by {metric_name}, highest to lowest.",
    )


def test_composite_ranking_result_requires_at_least_two_children() -> None:
    """A 1-table 'composite' is just a MetricRankingResult — the composite
    shape only makes sense for N ≥ 2."""

    with pytest.raises(ValidationError):
        CompositeRankingResult(
            children=[_ranking(101, "starting salary")],
            total_considered=0,
            excluded_count=0,
            order_statement="Stub.",
        )


def test_composite_ranking_result_caps_at_eight_children() -> None:
    """Hard cap on how many tables one composite envelope can carry; keeps
    pa-eval verdict sets bounded and prevents accidental "all 47 metrics"
    accept paths."""

    children = [_ranking(100 + i, f"metric {i}") for i in range(9)]
    with pytest.raises(ValidationError):
        CompositeRankingResult(
            children=children,
            total_considered=0,
            excluded_count=0,
            order_statement="Stub.",
        )


def test_composite_ranking_result_aggregates_summary_fields() -> None:
    """When total_considered/excluded_count are 0, the validator sums them
    from children. Authors usually don't hand-calculate."""

    composite = CompositeRankingResult(
        children=[
            _ranking(101, "BA salary", total_considered=10, excluded_count=2),
            _ranking(102, "MA salary", total_considered=10, excluded_count=3),
        ],
        total_considered=0,
        excluded_count=0,
        order_statement="Two metrics, ranked highest to lowest by each.",
    )
    assert composite.total_considered == 20
    assert composite.excluded_count == 5


def test_composite_ranking_result_preserves_explicit_summary_fields() -> None:
    """If the caller passes non-zero values, the validator respects them
    (some scorecard validators set explicitly)."""

    composite = CompositeRankingResult(
        children=[
            _ranking(101, "BA salary", total_considered=10),
            _ranking(102, "MA salary", total_considered=10),
        ],
        total_considered=42,
        excluded_count=7,
        order_statement="Override.",
    )
    assert composite.total_considered == 42
    assert composite.excluded_count == 7


def test_composite_ranking_result_envelope_rows_must_be_empty() -> None:
    """Rows live on each child — the envelope itself has no rows. A
    non-empty envelope.rows would let downstream code render the composite
    as if it were a flat ranking, which the children's own tables already
    own. Surface_consistency would then double-count."""

    with pytest.raises(ValidationError):
        CompositeRankingResult(
            children=[_ranking(101, "a"), _ranking(102, "b")],
            rows=["leaked"],  # type: ignore[list-item]
            total_considered=0,
            excluded_count=0,
            order_statement="Stub.",
        )


def test_composite_ranking_result_is_in_result_set_union() -> None:
    """The discriminated union accepts the new type so downstream
    pattern-matching on ``result_type`` sees it explicitly instead of
    falling through to a default branch."""

    assert CompositeRankingResult in RESULT_SET_TYPES
    assert issubclass(CompositeRankingResult, _ResultSetBase)


def test_composite_ranking_result_result_type_discriminator() -> None:
    """Discriminator value is canonical so JSON round-trips through the
    union type adapter."""

    composite = CompositeRankingResult(
        children=[_ranking(101, "a"), _ranking(102, "b")],
        total_considered=0,
        excluded_count=0,
        order_statement="Stub.",
    )
    assert composite.result_type == "composite_ranking"


def test_composite_ranking_result_is_frozen() -> None:
    """Per the _ResultSetBase invariant, every variant is frozen after
    construction to keep the surface_consistency contract durable."""

    composite = CompositeRankingResult(
        children=[_ranking(101, "a"), _ranking(102, "b")],
        total_considered=0,
        excluded_count=0,
        order_statement="Stub.",
    )
    with pytest.raises(ValidationError):
        composite.order_statement = "mutated"  # type: ignore[misc]
