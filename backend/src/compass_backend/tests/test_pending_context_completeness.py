"""W1-03 (#838): operation-aware pending-context completeness.

Pre-W1-03, ``_has_execution_fields`` used a generic
\"profile_lookup → needs profile_fields, else needs metrics\" rule. That
rejected valid metricless shapes the QueryPlan validators support — most
visibly ``count`` + ``count_kind == \"covered_universe_count\"`` (\"how many
districts do you cover?\"), where metrics MUST be empty per the
contracts/planning.py:535 validator.

These parametrized tests pin the per-operation rules and assert the
metricless shapes now promote successfully (the bug the audit named).
"""

from __future__ import annotations

import pytest

from compass_backend.contracts.planning import (
    FilterSpec,
    MetricSpec,
    PendingQueryContext,
    ProfileFieldSpec,
    SelectionSpec,
    SimilarityQuerySpec,
    SortStepSpec,
)
from compass_backend.planning.planner import (
    _has_execution_fields,
    promote_pending_context_to_plan,
)


def _selection_all() -> SelectionSpec:
    return SelectionSpec(scope="all_covered_districts")


@pytest.mark.parametrize(
    "context, expected",
    [
        pytest.param(
            PendingQueryContext(
                operation="profile_lookup",
                selection=_selection_all(),
                profile_fields=[ProfileFieldSpec(name="enrollment")],
            ),
            True,
            id="profile_lookup_with_profile_fields",
        ),
        pytest.param(
            PendingQueryContext(
                operation="profile_lookup",
                selection=_selection_all(),
            ),
            False,
            id="profile_lookup_without_profile_fields",
        ),
        pytest.param(
            PendingQueryContext(
                operation="count",
                count_kind="covered_universe_count",
                selection=_selection_all(),
            ),
            True,
            id="count_covered_universe_needs_no_metric",
        ),
        pytest.param(
            PendingQueryContext(
                operation="count",
                count_kind="categorical_value_count",
                selection=_selection_all(),
                metrics=[MetricSpec(name="sick leave policy")],
            ),
            True,
            id="count_categorical_with_metric",
        ),
        pytest.param(
            PendingQueryContext(
                operation="count",
                count_kind="categorical_value_count",
                selection=_selection_all(),
            ),
            False,
            id="count_categorical_without_metric",
        ),
        pytest.param(
            PendingQueryContext(
                operation="count",
                count_kind="threshold_count",
                selection=_selection_all(),
                metrics=[MetricSpec(name="starting salary")],
            ),
            True,
            id="count_threshold_with_metric",
        ),
        pytest.param(
            PendingQueryContext(
                operation="count",
                count_kind="threshold_count",
                selection=_selection_all(),
                filters=[
                    FilterSpec(field="enrollment", operator="greater_than", value=10000),
                ],
            ),
            True,
            id="count_threshold_with_filter_only",
        ),
        pytest.param(
            PendingQueryContext(
                operation="count",
                count_kind="threshold_count",
                selection=_selection_all(),
            ),
            False,
            id="count_threshold_without_metric_or_filter",
        ),
        pytest.param(
            PendingQueryContext(
                operation="similarity",
                selection=_selection_all(),
                similarity=SimilarityQuerySpec(anchor_name="Denver"),
            ),
            True,
            id="similarity_with_spec",
        ),
        pytest.param(
            PendingQueryContext(
                operation="similarity",
                selection=_selection_all(),
            ),
            False,
            id="similarity_without_spec",
        ),
        pytest.param(
            PendingQueryContext(
                operation="peer_comparison",
                selection=SelectionSpec(
                    scope="named_districts", districts=["Denver"]
                ),
                metrics=[MetricSpec(name="sick leave")],
            ),
            True,
            id="peer_comparison_with_comparison_group",
        ),
        pytest.param(
            PendingQueryContext(
                operation="rank",
                selection=_selection_all(),
                sort_steps=[
                    SortStepSpec(
                        phase="presentation",
                        field="enrollment",
                        direction="desc",
                        key_type="profile_field",
                    )
                ],
            ),
            True,
            id="rank_via_profile_sort_step_alone",
        ),
        pytest.param(
            PendingQueryContext(
                operation="rank",
                selection=_selection_all(),
            ),
            False,
            id="rank_without_metric_or_profile_or_sort_step",
        ),
        pytest.param(
            PendingQueryContext(
                operation="lookup",
                selection=_selection_all(),
                metrics=[MetricSpec(name="starting salary")],
            ),
            True,
            id="lookup_with_metric",
        ),
        pytest.param(
            PendingQueryContext(
                operation="trend",
                selection=_selection_all(),
                metrics=[MetricSpec(name="starting salary")],
            ),
            True,
            id="trend_with_metric",
        ),
    ],
)
def test_pending_context_completeness_per_operation(
    context: PendingQueryContext,
    expected: bool,
) -> None:
    """The completeness predicate must match per-operation rules, not a
    generic \"needs metrics\" gate."""

    assert _has_execution_fields(context) is expected


def test_covered_universe_count_now_promotes_to_plan() -> None:
    """W1-03 regression: a covered_universe_count pending context that
    pre-W1-03 returned None from promotion must now promote successfully.
    This is the audit-named bug: \"how many districts do you cover?\" got
    stuck in unnecessary clarification."""

    pending = PendingQueryContext(
        operation="count",
        count_kind="covered_universe_count",
        question="How many districts do you cover?",
        selection=_selection_all(),
    )
    plan = promote_pending_context_to_plan(
        pending, question="How many districts do you cover?"
    )
    assert plan is not None, (
        "W1-03: covered_universe_count pending context must promote (pre-W1-03 "
        "returned None because the generic completeness check required metrics)."
    )
    assert plan.operation == "count"
    assert plan.count_kind == "covered_universe_count"
    assert plan.metrics == [], "covered_universe forbids metrics per validator"
