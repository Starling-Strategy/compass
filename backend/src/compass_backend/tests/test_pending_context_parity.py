"""W1-02 (#836): PendingQueryContext parity with QueryPlan execution-relevant fields.

Pre-W1-02, a clarification → answer cycle dropped these typed fields:
- ``sort_steps``           (multi-step sort intent)
- ``requires_all_metrics`` (AND-intersection semantics)
- ``count_kind``           (covered-universe vs threshold vs categorical)
- ``inherit_selection_from`` (\"use those districts\" inheritance scope)

These tests pin the round-trip: capture from a QueryPlan into a
PendingQueryContext (via ``pending_context_from_execution_clarification``),
then promote back into a QueryPlan (via ``promote_pending_context_to_plan``),
and assert each field survives.
"""

from __future__ import annotations

import pytest

from compass_backend.contracts.planning import (
    ClarificationRequest,
    MetricSpec,
    PendingQueryContext,
    QueryPlan,
    SelectionSpec,
    SortStepSpec,
)
from compass_backend.planning.planner import (
    pending_context_from_execution_clarification,
    promote_pending_context_to_plan,
)


def _base_plan(**overrides: object) -> QueryPlan:
    """Build a QueryPlan with the minimum required surface for a rank op."""

    defaults: dict[str, object] = {
        "operation": "rank",
        "question": "Rank districts by salary.",
        "selection": SelectionSpec(scope="all_covered_districts"),
        "metrics": [MetricSpec(name="starting salary")],
    }
    defaults.update(overrides)
    return QueryPlan(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "plan_kwargs, clarification_missing, expected_field, expected_value",
    [
        pytest.param(
            {
                "sort_steps": [
                    SortStepSpec(
                        phase="selection",
                        field="frpl_pct",
                        direction="desc",
                        key_type="profile_field",
                    ),
                    SortStepSpec(
                        phase="presentation",
                        field="starting salary",
                        direction="desc",
                        key_type="policy_metric",
                    ),
                ],
            },
            ["metric"],
            "sort_steps",
            2,
            id="sort_steps_multi_step",
        ),
        pytest.param(
            {
                "operation": "lookup",
                "metrics": [
                    MetricSpec(name="starting salary"),
                    MetricSpec(name="health premium"),
                ],
                "requires_all_metrics": True,
            },
            ["scope"],
            "requires_all_metrics",
            True,
            id="requires_all_metrics_intersection",
        ),
        pytest.param(
            {
                "operation": "count",
                "metrics": [],
                "count_kind": "covered_universe_count",
            },
            # covered_universe_count requires metrics=[]; use a non-metric
            # missing_field so the candidate-metric expansion does not
            # repopulate metrics in the captured pending context.
            ["scope"],
            "count_kind",
            "covered_universe_count",
            id="count_kind_covered_universe",
        ),
        pytest.param(
            {
                "inherit_selection_from": "prior_result_rows",
            },
            ["metric"],
            "inherit_selection_from",
            "prior_result_rows",
            id="inherit_selection_from_prior_rows",
        ),
    ],
)
def test_pending_context_captures_query_plan_shape(
    plan_kwargs: dict[str, object],
    clarification_missing: list[str],
    expected_field: str,
    expected_value: object,
) -> None:
    """Capture half of the round-trip: QueryPlan → PendingQueryContext."""

    original_plan = _base_plan(**plan_kwargs)
    clarification = ClarificationRequest(
        question="Which lane?",
        missing_fields=clarification_missing,  # type: ignore[arg-type]
        candidates=["BA", "MA"] if "metric" in clarification_missing else [],
    )

    pending = pending_context_from_execution_clarification(
        original_plan,
        clarification,
        turn_index=1,
    )

    captured_value = getattr(pending, expected_field)
    if expected_field == "sort_steps":
        assert len(captured_value) == expected_value
        for step_original, step_captured in zip(
            original_plan.sort_steps, pending.sort_steps, strict=True
        ):
            assert step_original == step_captured
    else:
        assert captured_value == expected_value, (
            f"PendingQueryContext.{expected_field} lost typed meaning at "
            f"capture: got {captured_value!r}, expected {expected_value!r}"
        )


def test_promote_preserves_sort_steps() -> None:
    """Promotion half of the round-trip: PendingQueryContext → QueryPlan."""

    pending = PendingQueryContext(
        operation="rank",
        question="Rank.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="frpl_pct",
                direction="desc",
                key_type="profile_field",
            ),
            SortStepSpec(
                phase="presentation",
                field="starting salary",
                direction="desc",
                key_type="policy_metric",
            ),
        ],
    )
    plan = promote_pending_context_to_plan(pending, question="fallback")
    assert plan is not None
    assert len(plan.sort_steps) == 2
    assert plan.sort_steps[0].field == "frpl_pct"
    assert plan.sort_steps[1].field == "starting salary"


def test_shape_guard_capture_round_trips_finalized_sort_steps() -> None:
    """Site #6 regression: the post-merge shape guard captures the FINALIZED
    plan, whose sort intent lives only on ``plan.sort_steps`` (``plan.sort`` is
    None). The guard's PendingQueryContext construction must round-trip
    ``sort_steps`` or the next-turn promotion resumes in unsorted order.
    """
    from compass_backend.contracts.catalog_resolution import (
        CatalogReconciliationReport,
    )
    from compass_backend.contracts.planning import SortSpec
    from compass_backend.planning.finalizer import finalize_plan

    draft = QueryPlan(
        operation="lookup",
        question="Teacher salary for these districts, highest first.",
        selection=SelectionSpec(
            scope="named_districts", districts=["Alpha", "Bravo"]
        ),
        metrics=[MetricSpec(name="starting salary")],
        sort=SortSpec(field="starting salary", direction="desc"),
    )
    finalized = finalize_plan(draft, CatalogReconciliationReport()).plan
    assert finalized.sort is None
    assert finalized.sort_steps  # direction now lives only here

    # Mirror orchestration.chat._apply_post_merge_shape_guard's construction.
    pending = PendingQueryContext(
        operation=finalized.operation,
        question=finalized.question,
        selection=finalized.selection,
        metrics=list(finalized.metrics),
        profile_fields=list(finalized.profile_fields),
        filters=list(finalized.filters),
        sort=finalized.sort,
        sort_steps=list(finalized.sort_steps),
        limit=finalized.limit,
    )
    promoted = promote_pending_context_to_plan(pending, question="fallback")
    assert promoted is not None
    assert [s.direction for s in promoted.sort_steps] == [
        s.direction for s in finalized.sort_steps
    ]
    assert promoted.sort_steps[0].direction == "desc"


def test_promote_preserves_requires_all_metrics() -> None:
    """Intersection AND-semantics must round-trip."""

    pending = PendingQueryContext(
        operation="lookup",
        question="Both.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary"), MetricSpec(name="health premium")],
        requires_all_metrics=True,
    )
    plan = promote_pending_context_to_plan(pending, question="fallback")
    assert plan is not None
    assert plan.requires_all_metrics is True


def test_promote_preserves_inherit_selection_from() -> None:
    """\"use those districts\" inheritance must round-trip."""

    pending = PendingQueryContext(
        operation="rank",
        question="Those rows.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        inherit_selection_from="prior_result_rows",
    )
    plan = promote_pending_context_to_plan(pending, question="fallback")
    assert plan is not None
    assert plan.inherit_selection_from == "prior_result_rows"


def test_promote_preserves_count_kind_for_threshold_count() -> None:
    """count_kind must round-trip on the threshold_count path (covered_universe
    requires W1-03 operation-aware completeness and is tested there)."""

    pending = PendingQueryContext(
        operation="count",
        question="How many?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        count_kind="threshold_count",
    )
    plan = promote_pending_context_to_plan(pending, question="fallback")
    assert plan is not None
    assert plan.count_kind == "threshold_count"


def test_pending_context_defaults_match_query_plan_defaults() -> None:
    """A bare PendingQueryContext must default each new field to QueryPlan's default."""

    pending = PendingQueryContext()
    plan = QueryPlan(
        operation="lookup",
        question="placeholder",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="placeholder")],
    )
    assert pending.sort_steps == [] == plan.sort_steps
    assert pending.requires_all_metrics is False is plan.requires_all_metrics
    assert (
        pending.requires_composite_ranking
        is False
        is plan.requires_composite_ranking
    )
    assert pending.count_kind == "threshold_count" == plan.count_kind
    assert pending.inherit_selection_from is None is plan.inherit_selection_from
