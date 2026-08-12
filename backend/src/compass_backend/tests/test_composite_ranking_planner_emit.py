"""W2-M3-01 phase 2 (#857): planner emits ``requires_composite_ranking``.

Phase 1 (#852) introduced the ``CompositeRankingResult`` typed artifact. Phase 2
adds the planner signal that tells the future executor (phase 3) to route to
the composite envelope: ``requires_composite_ranking: bool`` on
``PendingQueryContext`` and ``QueryPlan``, set when the user accepts the group
in an N-metric clarification ("do all", "all of them", "all 4 separately").

These tests pin the contract + round-trip:

* ``QueryPlan`` rejects the flag on operations other than ``rank``.
* ``QueryPlan`` rejects the flag when metric count is outside
  ``CompositeRankingResult``'s ``2 <= N <= 8`` bounds.
* ``PendingQueryContext`` defaults the flag to ``False``.
* ``promote_pending_context_to_plan`` carries the flag onto the plan.
* ``pending_context_from_execution_clarification`` carries the flag back into
  pending context (mirrors the W1-02 round-trip pattern).
* ``_merge_pending_context`` truthy-wins for the flag (same pattern as
  ``requires_all_metrics``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.contracts.planning import (
    ClarificationRequest,
    MetricSpec,
    PendingQueryContext,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.planning.planner import (
    _merge_pending_context,
    pending_context_from_execution_clarification,
    promote_pending_context_to_plan,
)


def _composite_metrics(n: int) -> list[MetricSpec]:
    return [MetricSpec(name=f"benefits metric {i}") for i in range(n)]


def _composite_rank_plan(*, metrics_n: int, **overrides: object) -> QueryPlan:
    """Build a QueryPlan that legally carries ``requires_composite_ranking=True``."""

    defaults: dict[str, object] = {
        "operation": "rank",
        "question": "Top 10 districts on all four benefits metrics, separately.",
        "selection": SelectionSpec(scope="all_covered_districts"),
        "metrics": _composite_metrics(metrics_n),
        "requires_composite_ranking": True,
    }
    defaults.update(overrides)
    return QueryPlan(**defaults)  # type: ignore[arg-type]


class TestQueryPlanContract:
    """Contract guards on the new ``requires_composite_ranking`` field."""

    def test_accepts_rank_with_two_metrics(self) -> None:
        plan = _composite_rank_plan(metrics_n=2)
        assert plan.requires_composite_ranking is True
        assert len(plan.metrics) == 2

    def test_accepts_rank_with_eight_metrics(self) -> None:
        plan = _composite_rank_plan(metrics_n=8)
        assert plan.requires_composite_ranking is True
        assert len(plan.metrics) == 8

    def test_defaults_to_false(self) -> None:
        plan = QueryPlan(
            operation="rank",
            question="Rank by salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
        )
        assert plan.requires_composite_ranking is False

    def test_rejects_on_lookup_operation(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            QueryPlan(
                operation="lookup",
                question="Both metrics.",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=_composite_metrics(2),
                requires_composite_ranking=True,
            )
        assert "requires_composite_ranking" in str(exc_info.value)
        assert "rank" in str(exc_info.value)

    def test_rejects_on_count_operation(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            QueryPlan(
                operation="count",
                question="How many?",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=_composite_metrics(2),
                requires_composite_ranking=True,
            )
        assert "requires_composite_ranking" in str(exc_info.value)

    def test_rejects_rank_with_single_metric(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            QueryPlan(
                operation="rank",
                question="Rank by one.",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=[MetricSpec(name="solo metric")],
                requires_composite_ranking=True,
            )
        assert "requires_composite_ranking" in str(exc_info.value)
        assert "2" in str(exc_info.value)

    def test_mutually_exclusive_with_requires_all_metrics(self) -> None:
        """The two flags are operation-incompatible by construction.

        ``requires_all_metrics`` is only valid for ``operation='lookup'`` and
        ``requires_composite_ranking`` is only valid for ``operation='rank'``.
        Setting both True is impossible: whichever operation is chosen, one of
        the two validators must fail. This test pins that contract so a future
        change that loosens either validator cannot silently let both flags
        coexist.
        """

        # operation='lookup' → composite validator rejects
        with pytest.raises(ValidationError, match="requires_composite_ranking"):
            QueryPlan(
                operation="lookup",
                question="lookup with both flags",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=_composite_metrics(2),
                requires_all_metrics=True,
                requires_composite_ranking=True,
            )

        # operation='rank' → intersection validator rejects
        with pytest.raises(ValidationError, match="requires_all_metrics"):
            QueryPlan(
                operation="rank",
                question="rank with both flags",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=_composite_metrics(2),
                requires_all_metrics=True,
                requires_composite_ranking=True,
            )

    def test_rejects_rank_with_nine_metrics(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            QueryPlan(
                operation="rank",
                question="Rank by nine.",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=_composite_metrics(9),
                requires_composite_ranking=True,
            )
        assert "requires_composite_ranking" in str(exc_info.value)
        assert "8" in str(exc_info.value)


class TestPendingContextContract:
    """``PendingQueryContext`` is a partial — defaults must not over-constrain."""

    def test_defaults_to_false(self) -> None:
        pending = PendingQueryContext()
        assert pending.requires_composite_ranking is False

    def test_pending_context_does_not_validate_executable_bounds(self) -> None:
        """Pending context is a partial; metric bounds are enforced at promotion
        only when the context is complete. A pending shape with the flag set but
        only one metric so far must not raise — the planner is allowed to gather
        the second metric in a later turn."""

        pending = PendingQueryContext(
            operation="rank",
            metrics=[MetricSpec(name="metric one")],
            requires_composite_ranking=True,
        )
        assert pending.requires_composite_ranking is True


class TestPromotion:
    """``PendingQueryContext`` → ``QueryPlan`` must carry the flag."""

    def test_promote_preserves_requires_composite_ranking(self) -> None:
        pending = PendingQueryContext(
            operation="rank",
            question="Rank by all four, separately.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=_composite_metrics(4),
            requires_composite_ranking=True,
        )
        plan = promote_pending_context_to_plan(pending, question="fallback")
        assert plan is not None
        assert plan.requires_composite_ranking is True
        assert len(plan.metrics) == 4

    def test_promote_default_false_when_pending_did_not_set_it(self) -> None:
        pending = PendingQueryContext(
            operation="rank",
            question="Rank by one.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="solo")],
        )
        plan = promote_pending_context_to_plan(pending, question="fallback")
        assert plan is not None
        assert plan.requires_composite_ranking is False


class TestCaptureFromClarification:
    """``QueryPlan`` → ``PendingQueryContext`` round-trip via execution clarify."""

    def test_clarify_captures_requires_composite_ranking(self) -> None:
        plan = _composite_rank_plan(metrics_n=4)
        clarification = ClarificationRequest(
            question="Which lane should I sort by?",
            missing_fields=["scope"],  # type: ignore[list-item]
        )
        pending = pending_context_from_execution_clarification(
            plan,
            clarification,
            turn_index=2,
        )
        assert pending.requires_composite_ranking is True
        assert len(pending.metrics) == 4


class TestMerge:
    """``_merge_pending_context`` must truthy-win the new boolean."""

    def test_merge_truthy_wins_when_only_existing_has_flag(self) -> None:
        existing = PendingQueryContext(
            operation="rank",
            metrics=_composite_metrics(4),
            requires_composite_ranking=True,
        )
        incoming = PendingQueryContext()
        merged = _merge_pending_context(
            existing,
            incoming,
            turn_index=3,
            fallback_missing_fields=[],
            clarification_question="placeholder",
        )
        assert merged.requires_composite_ranking is True
        assert len(merged.metrics) == 4

    def test_merge_truthy_wins_when_only_incoming_has_flag(self) -> None:
        existing = PendingQueryContext(
            operation="rank",
            metrics=_composite_metrics(4),
        )
        incoming = PendingQueryContext(requires_composite_ranking=True)
        merged = _merge_pending_context(
            existing,
            incoming,
            turn_index=3,
            fallback_missing_fields=[],
            clarification_question="placeholder",
        )
        assert merged.requires_composite_ranking is True

    def test_merge_false_when_neither_has_flag(self) -> None:
        existing = PendingQueryContext(
            operation="rank",
            metrics=_composite_metrics(4),
        )
        incoming = PendingQueryContext()
        merged = _merge_pending_context(
            existing,
            incoming,
            turn_index=3,
            fallback_missing_fields=[],
            clarification_question="placeholder",
        )
        assert merged.requires_composite_ranking is False
