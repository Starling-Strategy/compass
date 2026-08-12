"""Tests for the planner's unsupported-QueryPlan-shape diagnostic.

The diagnostic powers the planner's `output_validator` retry loop: when the
LLM emits a `QueryPlan` whose ``(operation, selection.scope, metrics, sort,
filters, limit)`` combination cannot be compiled by the deterministic
executor, the validator raises ``ModelRetry`` with the diagnostic's hint so
the LLM gets one re-roll inside the same agent call. See
``src/compass_backend/execution/shape_check.py``.
"""

from __future__ import annotations

import pytest
from pydantic_ai import ModelRetry

from compass_backend.contracts import (
    ClarificationRequest,
    DirectResponse,
    FilterSpec,
    LimitSpec,
    MetricSpec,
    OutputSpec,
    PendingQueryContext,
    PlannerTurn,
    ProfileFieldSpec,
    QueryPlan,
    SelectionSpec,
    SimilarityQuerySpec,
    SortSpec,
    SortStepSpec,
)
from compass_backend.execution.shape_check import unsupported_shape_hint
from compass_backend.planning.instruction_snippets import PlannerInstructionSelection
from compass_backend.planning.negative_filters import (
    normalize_turn_negative_policy_filters,
)
from compass_backend.planning.delta_detection import DeltaIntent
from compass_backend.planning.planner import (
    PlannerDeps,
    normalize_delta_intent_turn,
    validate_planner_turn_quality,
)


# --------------------------------------------------------------------------
# Good-plan fixtures — these MUST return None from the diagnostic.
# --------------------------------------------------------------------------


def _good_rank_plan() -> QueryPlan:
    return QueryPlan(
        operation="rank",
        question="Rank covered districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
    )


def _good_lookup_plan() -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="What is starting salary in Alpha?",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
    )


def test_negative_parental_leave_filter_normalizes_to_metric_value_filter() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.8,
        query_plan=QueryPlan(
            operation="lookup",
            question="which districts have no parental leave policy",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="paid parental leave")],
            filters=[
                FilterSpec(
                    field="parental leave policy",
                    operator="contains",
                    value="no parental leave",
                )
            ],
            limit=LimitSpec(kind="all"),
        ),
    )

    normalized = normalize_turn_negative_policy_filters(turn)

    assert normalized.query_plan is not None
    assert normalized.query_plan.metrics == [
        MetricSpec(name="Does the district offer paid parental leave?")
    ]
    assert normalized.query_plan.filters == [
        FilterSpec(
            field="Does the district offer paid parental leave?",
            operator="equals",
            value=False,
        )
    ]
    assert unsupported_shape_hint(normalized.query_plan) is None


def test_negative_parental_leave_rescue_clarify_promotes_to_metric_lookup() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question="I need one district group and one Compass metric to start.",
            missing_fields=["comparison_group", "metric"],
            candidates=[],
            is_rescue_fallback=True,
        ),
    )

    normalized = normalize_turn_negative_policy_filters(
        turn,
        message="which districts have no parental leave policy",
    )

    assert normalized.route == "execute"
    assert normalized.clarification is None
    assert normalized.query_plan is not None
    assert normalized.query_plan.operation == "lookup"
    assert normalized.query_plan.metrics == [
        MetricSpec(name="Does the district offer paid parental leave?")
    ]
    assert normalized.query_plan.filters == [
        FilterSpec(
            field="Does the district offer paid parental leave?",
            operator="equals",
            value=False,
        )
    ]
    assert unsupported_shape_hint(normalized.query_plan) is None


def _good_count_plan() -> QueryPlan:
    return QueryPlan(
        operation="count",
        question="How many covered districts require background checks?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Background check required")],
    )


def _good_trend_plan() -> QueryPlan:
    return QueryPlan(
        operation="trend",
        question="Salary trend over time.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
    )


def _good_profile_lookup_plan() -> QueryPlan:
    return QueryPlan(
        operation="profile_lookup",
        question="What is the FRPL rate in Alpha?",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        profile_fields=[ProfileFieldSpec(name="FRPL")],
    )


def _good_peer_comparison_plan() -> QueryPlan:
    return QueryPlan(
        operation="peer_comparison",
        question="How does Alpha compare to its peers?",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
    )


def _good_similarity_plan() -> QueryPlan:
    return QueryPlan(
        operation="similarity",
        question="Find peers to Alpha.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        similarity=SimilarityQuerySpec(anchor_name="Alpha"),
    )


@pytest.mark.parametrize(
    "plan_factory",
    [
        _good_rank_plan,
        _good_lookup_plan,
        _good_count_plan,
        _good_trend_plan,
        _good_profile_lookup_plan,
        _good_peer_comparison_plan,
        _good_similarity_plan,
    ],
    ids=["rank", "lookup", "count", "trend", "profile_lookup", "peer_comparison", "similarity"],
)
def test_supported_plans_return_no_hint(plan_factory) -> None:
    """Every operation has at least one constructible plan the executor accepts."""
    assert unsupported_shape_hint(plan_factory()) is None


# --------------------------------------------------------------------------
# Per-operation failure modes — each MUST produce a hint with an actionable
# substring so the LLM can rewrite its plan in the retry.
# --------------------------------------------------------------------------


def test_rank_with_enrollment_filter_returns_hint() -> None:
    """rank does not accept enrollment-bounds filters (that's lookup/count territory).

    Enrollment-scoped filters pass selection_filters_are_supported but fail
    state_filters_are_supported, which is what rank gates on.  This tests the
    remaining genuine rejection case for rank filters after PR 2A.
    """
    plan = QueryPlan(
        operation="rank",
        question="Rank by salary for large districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        filters=[
            FilterSpec(field="enrollment", operator="greater_than", value=50000),
        ],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is not None
    assert "filter" in hint.lower()


def test_rank_with_governed_region_filter_is_supported() -> None:
    from compass_backend.execution.selection import requested_states
    from compass_backend.reference import CENSUS_SOUTH_STATES

    plan = QueryPlan(
        operation="rank",
        question="Rank Southern districts by differentiated pay.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="hard-to-staff school pay")],
        filters=[
            FilterSpec(field="region", operator="equals", value="Southern"),
        ],
        sort=SortSpec(field="hard-to-staff school pay", direction="desc"),
        limit=LimitSpec(count=10, kind="top"),
    )

    assert requested_states(plan) == set(CENSUS_SOUTH_STATES)
    assert unsupported_shape_hint(plan) is None


# --------------------------------------------------------------------------
# Multi-metric rank delegation (M3c-1 / #733). The executor at
# ``execution/operations.py::_execute_ranking`` (lines 636-646) silently
# delegates multi-metric ``rank`` plans to ``_execute_lookup`` (no limit) or
# ``_execute_limited_ranked_lookup`` (with limit). The planner-time validator
# must mirror that delegation so multi-metric rank plans aren't rejected
# upstream and never reach the executor that would have handled them.
# --------------------------------------------------------------------------


def test_rank_with_multi_metric_and_supported_scope_returns_no_hint() -> None:
    """Multi-metric rank with a supported scope is dispatchable —
    executor delegates to _execute_lookup (no limit path).
    """
    plan = QueryPlan(
        operation="rank",
        question="Rank covered districts by BA salary, show MA alongside.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary (BA)", role="primary"),
            MetricSpec(name="starting salary (MA)", role="primary"),
        ],
    )

    assert unsupported_shape_hint(plan) is None


def test_rank_with_multi_metric_largest_districts_limit_returns_no_hint() -> None:
    """Multi-metric rank with limit + largest_districts is dispatchable —
    executor delegates to _execute_limited_ranked_lookup.
    """
    plan = QueryPlan(
        operation="rank",
        question="Top 10 largest districts by BA salary, show MA too.",
        selection=SelectionSpec(scope="largest_districts"),
        metrics=[
            MetricSpec(name="starting salary (BA)", role="primary"),
            MetricSpec(name="starting salary (MA)", role="comparison"),
        ],
        limit=LimitSpec(count=10, kind="top"),
        sort=SortSpec(field="starting salary (BA)", direction="desc"),
    )

    assert unsupported_shape_hint(plan) is None


def test_ranking_delegates_to_multi_metric_lookup_predicate() -> None:
    """Direct unit on the shared predicate used by both layers
    (validator + orchestrator-level shape guard).

    Mirrors the rank branch in
    ``orchestration/chat.py::_plan_likely_dispatchable``: True when
    ``operation == "rank"`` AND there are at least two execution metrics
    (primary or comparison; filter/grouping roles don't count).
    """
    from compass_backend.execution.shape_check import (
        ranking_delegates_to_multi_metric_lookup,
    )

    two_primaries = QueryPlan(
        operation="rank",
        question="Rank by two metrics.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="metric A", role="primary"),
            MetricSpec(name="metric B", role="primary"),
        ],
    )
    assert ranking_delegates_to_multi_metric_lookup(two_primaries) is True

    primary_plus_comparison = QueryPlan(
        operation="rank",
        question="Rank by A, show B alongside.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="metric A", role="primary"),
            MetricSpec(name="metric B", role="comparison"),
        ],
    )
    assert ranking_delegates_to_multi_metric_lookup(primary_plus_comparison) is True

    single_metric = QueryPlan(
        operation="rank",
        question="Rank by one metric.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="metric A", role="primary")],
    )
    assert ranking_delegates_to_multi_metric_lookup(single_metric) is False

    not_rank = QueryPlan(
        operation="lookup",
        question="Two-metric lookup.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="metric A", role="primary"),
            MetricSpec(name="metric B", role="primary"),
        ],
    )
    assert ranking_delegates_to_multi_metric_lookup(not_rank) is False

    # Filter/grouping roles are excluded from the execution-metric count —
    # one primary + one grouping is still effectively single-metric for rank,
    # so the predicate must NOT delegate.
    primary_plus_grouping = QueryPlan(
        operation="rank",
        question="Rank by A, group by B (categorical).",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="metric A", role="primary"),
            MetricSpec(name="metric B", role="grouping"),
        ],
    )
    assert ranking_delegates_to_multi_metric_lookup(primary_plus_grouping) is False


def test_lookup_with_unsupported_limit_returns_hint() -> None:
    plan = QueryPlan(
        operation="lookup",
        question="Look up salary.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        limit=LimitSpec(count=3),
    )

    hint = unsupported_shape_hint(plan)

    assert hint is not None
    assert "lookup" in hint.lower()
    assert "limit" in hint.lower()


def test_lookup_all_covered_districts_with_kind_all_is_supported() -> None:
    """#763 PR-3B: charts across all covered districts must use
    LimitSpec(kind='all'), and that shape MUST be accepted by the lookup
    gate. Previously the gate rejected any non-None limit unless
    scope='largest_districts', forcing the planner to retry-loop on
    chart-with-all-districts prompts."""
    plan = QueryPlan(
        operation="lookup",
        question="Show me a chart of starting salaries across all districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        limit=LimitSpec(kind="all"),
        output=OutputSpec(format="chart"),
    )

    hint = unsupported_shape_hint(plan)

    assert hint is None, (
        f"lookup + all_covered_districts + kind='all' must be supported, got: {hint!r}"
    )


def test_lookup_state_with_kind_all_is_supported() -> None:
    """#763 PR-3B: kind='all' is also valid with selection.scope='state' per the
    planner instructions at planner.py:93-100."""
    plan = QueryPlan(
        operation="lookup",
        question="Show me a chart of starting salaries for all districts in California.",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        limit=LimitSpec(kind="all"),
        output=OutputSpec(format="chart"),
    )

    hint = unsupported_shape_hint(plan)

    assert hint is None, (
        f"lookup + state + kind='all' must be supported, got: {hint!r}"
    )


def test_lookup_named_districts_with_kind_all_is_supported() -> None:
    """#1012: kind='all' must be supported for named_districts. The multi-turn
    inheritance recipe (inherit_selection_from='prior_result_rows') materializes
    selection.scope='named_districts' from the prior result while preserving
    the planner's LimitSpec(kind='all'). Rejecting that combination forced case
    995 turn 3 ("for those 12 districts, list them out sorted ...") down a
    compile-failure path and demoted the response to a clarify-route error
    message. The combination is a semantic no-op — named_districts already
    bounds the result — so accepting it has no executor consequence.
    """
    plan = QueryPlan(
        operation="lookup",
        question="Look up salary for these districts.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha", "Beta"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        limit=LimitSpec(kind="all"),
    )

    hint = unsupported_shape_hint(plan)

    assert hint is None, (
        f"lookup + named_districts + kind='all' must be supported, got: {hint!r}"
    )


def test_count_without_metrics_returns_hint() -> None:
    plan = QueryPlan(
        operation="count",
        question="How many districts?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="filler")],  # parses, then we strip below
    )
    # The contract requires at least one metric to *parse*; the diagnostic
    # speaks about runtime executability after parsing. We exercise the
    # constructor-friendly equivalent failure: a non-state filter that the
    # count path rejects.
    plan = plan.model_copy(
        update={
            "filters": [
                FilterSpec(field="district_name", operator="contains", value="Alpha"),
            ],
        }
    )

    hint = unsupported_shape_hint(plan)

    assert hint is not None
    assert "count" in hint.lower()


def test_trend_with_sort_returns_hint() -> None:
    plan = QueryPlan(
        operation="trend",
        question="Salary trend.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        sort=SortSpec(field="value", direction="desc"),
    )

    hint = unsupported_shape_hint(plan)

    assert hint is not None
    assert "trend" in hint.lower()
    assert "sort" in hint.lower()


def test_trend_with_sort_steps_returns_hint() -> None:
    plan = QueryPlan(
        operation="trend",
        question="Salary trend.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        sort_steps=[SortStepSpec(field="value", direction="desc")],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is not None
    assert "trend" in hint.lower()
    assert "sort" in hint.lower()


def test_profile_lookup_with_non_named_scope_returns_hint() -> None:
    plan = QueryPlan(
        operation="profile_lookup",
        question="FRPL across all districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        profile_fields=[ProfileFieldSpec(name="FRPL")],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is not None
    assert "profile_lookup" in hint.lower()
    assert "named" in hint.lower()


def test_profile_lookup_with_limit_returns_hint() -> None:
    plan = QueryPlan(
        operation="profile_lookup",
        question="FRPL in Alpha.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        profile_fields=[ProfileFieldSpec(name="FRPL")],
        limit=LimitSpec(count=3),
    )

    hint = unsupported_shape_hint(plan)

    assert hint is not None
    assert "profile_lookup" in hint.lower()


def test_peer_comparison_with_two_districts_returns_hint() -> None:
    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Alpha and Bravo to peers.",
        selection=SelectionSpec(
            scope="named_districts", districts=["Alpha", "Bravo"]
        ),
        metrics=[MetricSpec(name="Average teacher starting salary")],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is not None
    assert "peer_comparison" in hint.lower()
    assert "one" in hint.lower() or "single" in hint.lower()


def test_peer_comparison_with_non_named_scope_returns_hint() -> None:
    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare to peers.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is not None
    assert "peer_comparison" in hint.lower()


# --------------------------------------------------------------------------
# Inherit-from-session: the validator skips these. Confirm the diagnostic
# itself does not crash on minimally-populated plans (defense-in-depth) — it
# may return a hint, the validator will just not consult it.
# --------------------------------------------------------------------------


def test_inherited_plan_does_not_crash() -> None:
    plan = QueryPlan(
        inherit_from_session=True,
        operation="lookup",
        question="What did we just see?",
        selection=SelectionSpec(scope="unspecified"),
    )

    # The diagnostic just needs to not raise.
    unsupported_shape_hint(plan)


# --------------------------------------------------------------------------
# Validator wiring — assert validate_planner_turn_quality raises ModelRetry
# with the diagnostic's hint when the shape is bad, and returns the turn
# unchanged when the shape is good.
# --------------------------------------------------------------------------


def _planner_turn(query_plan: QueryPlan) -> PlannerTurn:
    return PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=query_plan,
    )


def test_normalize_forces_lookup_for_name_list_intent() -> None:
    """C2 (#1414): a bare 'name/list the inherited set' ask forces the list view.

    A prior count, then "What are the 18 districts?" — the structural rule flips
    the operation to ``lookup`` so the answer enumerates the inherited set
    instead of re-running the count that discards the names. The metric inherits
    downstream, so a lookup that named no metric still lists those districts.
    """
    turn = _planner_turn(_good_count_plan())
    delta = DeltaIntent(
        matched_anaphora=("the N districts",),
        matched_verbs=(),
        has_prior_query=True,
        matched_list_referents=("the N districts",),
    )

    normalized, changed = normalize_delta_intent_turn(turn, delta)

    assert changed is True
    assert normalized.query_plan is not None
    assert normalized.query_plan.operation == "lookup"
    assert normalized.query_plan.inherit_from_session is True
    assert normalized.query_plan.inherit_selection_from == "prior_result_rows"
    # Enumerate the whole inherited set: all-rows limit (a numeric limit is
    # rejected on a named_districts lookup) shown in full, not a preview.
    assert normalized.query_plan.limit is not None
    assert normalized.query_plan.limit.kind == "all"
    assert normalized.query_plan.output is not None
    assert normalized.query_plan.output.row_display == "all"


def test_normalize_keeps_operation_for_sort_delta() -> None:
    """A sort/reorder delta inherits the set but keeps its own (rank) operation."""
    turn = _planner_turn(_good_rank_plan())
    delta = DeltaIntent(
        matched_anaphora=("those N",),
        matched_verbs=("reverse",),
        has_prior_query=True,
        matched_list_referents=("those N",),
    )

    normalized, _changed = normalize_delta_intent_turn(turn, delta)

    assert normalized.query_plan is not None
    assert normalized.query_plan.operation == "rank"


def test_normalize_forces_lookup_when_prior_metric_inheritable() -> None:
    """AK's bare ask names no metric but inherits the prior count's — force list.

    A metric-less plan whose prior turn carried a metric (prior_has_metric) is
    forced to lookup; the metric inherits downstream so the list has a value.
    """
    plan = QueryPlan(
        operation="count",
        count_kind="covered_universe_count",
        question="What are the 18 districts?",
        selection=SelectionSpec(scope="all_covered_districts"),
    )
    turn = _planner_turn(plan)
    delta = DeltaIntent(
        matched_anaphora=("the N districts",),
        matched_verbs=(),
        has_prior_query=True,
        matched_list_referents=("the N districts",),
        prior_has_metric=True,
    )

    normalized, _changed = normalize_delta_intent_turn(turn, delta)

    assert normalized.query_plan is not None
    assert normalized.query_plan.operation == "lookup"


def test_normalize_skips_lookup_when_no_metric_to_inherit() -> None:
    """A metric-less prior must NOT be forced into a lookup that would refuse.

    With no metric on this turn and none to inherit (prior_has_metric=False), a
    forced lookup has no value to list and would hit ExecutionRefusal, so the
    force is suppressed and the turn keeps its operation (C2 #1414).
    """
    plan = QueryPlan(
        operation="count",
        count_kind="covered_universe_count",
        question="What are those 133 districts?",
        selection=SelectionSpec(scope="all_covered_districts"),
    )
    turn = _planner_turn(plan)
    delta = DeltaIntent(
        matched_anaphora=("those N",),
        matched_verbs=(),
        has_prior_query=True,
        matched_list_referents=("those N",),
        prior_has_metric=False,
    )

    normalized, _changed = normalize_delta_intent_turn(turn, delta)

    assert normalized.query_plan is not None
    assert normalized.query_plan.operation == "count"


def test_forced_list_lookup_shape_compiles_on_named_districts() -> None:
    """C2 (#1414): the forced name/list lookup compiles on a named_districts set.

    Regression guard for the shape-check reject the live replay caught — a
    named_districts lookup with a numeric limit is invalid, so the forced shape
    must carry LimitSpec(kind="all").
    """
    plan = QueryPlan(
        operation="lookup",
        question="What are the 18 districts?",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha", "Bravo"]),
        metrics=[MetricSpec(name="Minimum number of formal observations")],
        limit=LimitSpec(kind="all"),
        output=OutputSpec(row_display="all"),
    )

    assert unsupported_shape_hint(plan) is None


def test_named_districts_lookup_with_numeric_limit_is_rejected() -> None:
    """Negative control: the shape the replay tripped before the limit fix."""
    plan = QueryPlan(
        operation="lookup",
        question="What are the 18 districts?",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha", "Bravo"]),
        metrics=[MetricSpec(name="Minimum number of formal observations")],
        limit=LimitSpec(kind="first", count=5),
    )

    assert unsupported_shape_hint(plan) is not None


def test_validator_raises_modelretry_with_hint_on_unsupported_shape() -> None:
    # Single-metric rank with an enrollment filter still fails — that filter
    # type is unsupported on the rank operation (lookup territory). The
    # planner-time validator should raise ModelRetry with the filter hint so
    # the LLM can re-plan against `lookup`.
    plan = QueryPlan(
        operation="rank",
        question="Rank by salary among large districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        filters=[
            FilterSpec(field="enrollment", operator="greater_than", value=50000),
        ],
    )
    deps = PlannerDeps(message="rank by salary among large districts")

    with pytest.raises(ModelRetry, match="enrollment-bounds filters are not supported"):
        validate_planner_turn_quality(_planner_turn(plan), deps)


def test_validator_accepts_profile_sort_rank_for_cross_box_review() -> None:
    plan = QueryPlan(
        operation="rank",
        question="Rank salaries over districts with high FRPL.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting teacher salary"),
            MetricSpec(name="free and reduced price lunch percentage", role="comparison"),
        ],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="free and reduced price lunch percentage",
                direction="desc",
                key_type="profile_field",
                limit=LimitSpec(count=25, kind="top"),
            )
        ],
    )
    deps = PlannerDeps(message="rank salaries by FRPL")
    turn = _planner_turn(plan)

    assert unsupported_shape_hint(plan) is None
    assert validate_planner_turn_quality(turn, deps) is turn


def test_validator_defers_profile_field_only_rank_to_normalizer() -> None:
    plan = QueryPlan(
        operation="rank",
        question="Top 10 largest districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        profile_fields=[ProfileFieldSpec(name="enrollment")],
        sort=SortSpec(field="enrollment", direction="desc"),
        limit=LimitSpec(count=10, kind="top"),
    )
    deps = PlannerDeps(message="largest districts")
    turn = _planner_turn(plan)

    assert unsupported_shape_hint(plan) is not None
    assert validate_planner_turn_quality(turn, deps) is turn


def test_validator_passes_through_supported_shape() -> None:
    plan = _good_rank_plan()
    deps = PlannerDeps(message="rank by salary")
    turn = _planner_turn(plan)

    assert validate_planner_turn_quality(turn, deps) is turn


def test_validator_skips_inherited_plans() -> None:
    """Inherited plans merge selection downstream; skip the shape check."""

    plan = QueryPlan(
        inherit_from_session=True,
        operation="lookup",
        question="What did we just see?",
        selection=SelectionSpec(scope="unspecified"),
    )
    # No frame.query_context yet, but the validator should not raise on the
    # shape check — the existing inherit_from_session-without-context check
    # downstream is what should fire in this scenario.
    deps = PlannerDeps(message="follow-up")
    turn = _planner_turn(plan)

    with pytest.raises(ModelRetry, match="inherit_from_session"):
        validate_planner_turn_quality(turn, deps)


def test_validator_shape_check_fires_before_inherit_check() -> None:
    """A non-inherited plan with a bad shape returns the shape hint, not the
    downstream inherit-context error."""

    plan = QueryPlan(
        operation="profile_lookup",
        question="FRPL across all districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        profile_fields=[ProfileFieldSpec(name="FRPL")],
    )
    deps = PlannerDeps(message="frpl rate")

    with pytest.raises(ModelRetry) as exc:
        validate_planner_turn_quality(_planner_turn(plan), deps)

    assert "profile_lookup" in str(exc.value).lower()
    assert "named" in str(exc.value).lower()


# --------------------------------------------------------------------------
# PR 2A / hard-filter promise: metric-value filters may be carried in
# QueryPlan.filters when the field is a metric phrase (not state or enrollment).
# Execution remains responsible for applying the filter or refusing before render.
# --------------------------------------------------------------------------


def test_rank_with_metric_value_filter_returns_no_hint() -> None:
    """rank + metric-value filter (D1 shape) must be accepted after PR 2A."""
    plan = QueryPlan(
        operation="rank",
        question="Which districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is None


def test_lookup_with_metric_value_filter_returns_no_hint() -> None:
    """lookup + metric-value filter must be accepted after PR 2A."""
    plan = QueryPlan(
        operation="lookup",
        question="Show me districts with starting salary above 50000.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher starting salary")],
        filters=[
            FilterSpec(
                field="teacher starting salary",
                operator="greater_than_or_equal",
                value=50000,
            ),
        ],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is None


def test_trend_with_metric_value_filter_returns_no_hint() -> None:
    """trend may carry a metric-value filter; execution decides year semantics."""
    plan = QueryPlan(
        operation="trend",
        question="Salary trend for districts paying above 45000.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="teacher starting salary")],
        filters=[
            FilterSpec(
                field="teacher starting salary",
                operator="greater_than",
                value=45000,
            ),
        ],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is None


def test_peer_comparison_with_metric_value_filter_returns_no_hint() -> None:
    """peer_comparison may carry a metric-value filter for execution to enforce."""
    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Alpha to peers with more than 190 workdays.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is None


def test_rank_with_state_plus_metric_value_filter_returns_no_hint() -> None:
    """rank with combined state + metric-value filters must be accepted after PR 2A."""
    plan = QueryPlan(
        operation="rank",
        question="Which CA districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="state", operator="equals", value="CA"),
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    hint = unsupported_shape_hint(plan)

    assert hint is None


# --------------------------------------------------------------------------
# PR 2B: similarity operation shape checks
# --------------------------------------------------------------------------


def test_similarity_shape_is_supported() -> None:
    """A well-formed similarity plan returns no hint."""

    plan = _good_similarity_plan()
    hint = unsupported_shape_hint(plan)
    assert hint is None


def test_similarity_with_feature_set_and_exclude_states_returns_no_hint() -> None:
    """similarity with feature_set + exclude_states is still valid."""

    plan = QueryPlan(
        operation="similarity",
        question="Comparable enrollment outside California",
        selection=SelectionSpec(scope="named_districts", districts=["Denver"]),
        similarity=SimilarityQuerySpec(
            anchor_name="Denver",
            feature_set="enrollment",
            exclude_states=["CA"],
        ),
    )
    hint = unsupported_shape_hint(plan)
    assert hint is None


def test_similarity_with_sort_returns_hint() -> None:
    """similarity does not accept manual sort."""

    plan = QueryPlan(
        operation="similarity",
        question="Find peers to Alpha sorted by name.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        similarity=SimilarityQuerySpec(anchor_name="Alpha"),
        sort=SortSpec(field="district_name", direction="asc"),
    )
    hint = unsupported_shape_hint(plan)
    assert hint is not None
    assert "similarity" in hint.lower()
    assert "sort" in hint.lower()


def test_similarity_without_payload_returns_hint_via_contract() -> None:
    """similarity without payload is rejected at the contract layer (model_validator)."""

    with pytest.raises(Exception) as exc_info:
        QueryPlan(
            operation="similarity",
            question="Find peers to Alpha.",
            selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
            # similarity payload intentionally omitted
        )
    assert "similarity" in str(exc_info.value).lower()


def test_peer_comparison_with_similarity_payload_rejected_by_contract() -> None:
    """peer_comparison must not carry a similarity payload (contract-layer guard)."""

    with pytest.raises(Exception) as exc_info:
        QueryPlan(
            operation="peer_comparison",
            question="Compare Alpha to peers.",
            selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
            metrics=[MetricSpec(name="Average teacher starting salary")],
            similarity=SimilarityQuerySpec(anchor_name="Alpha"),
        )
    assert "similarity" in str(exc_info.value).lower()


# --------------------------------------------------------------------------
# PR 2B (Improvement B): anchor_name must match selection.districts[0]
# --------------------------------------------------------------------------


def test_similarity_anchor_name_must_match_selection_districts_first() -> None:
    """anchor_name MUST match selection.districts[0] (case-fold-strip equality).

    Mismatch triggers ValueError at construction time. Regression for PR 2B
    reviewer's Improvement B — the model_validator path was previously untested
    for the failure case.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="anchor_name"):
        QueryPlan(
            operation="similarity",
            question="Find peers to Denver.",
            selection=SelectionSpec(scope="named_districts", districts=["Seattle"]),
            similarity=SimilarityQuerySpec(anchor_name="Denver"),
        )


def test_similarity_anchor_name_case_insensitive_match_is_accepted() -> None:
    """anchor_name matching is case-fold-strip insensitive: 'denver' == 'Denver'."""
    # Should not raise even though capitalisation differs, because model_validator
    # strips + casefolds before comparing.
    plan = QueryPlan(
        operation="similarity",
        question="Find peers.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver"]),
        similarity=SimilarityQuerySpec(anchor_name="denver"),
    )
    assert plan.similarity is not None
    assert plan.similarity.anchor_name == "denver"


# --------------------------------------------------------------------------
# PR 2B (Improvement A): exclude_states normalization
# --------------------------------------------------------------------------


def test_exclude_states_valid_abbreviations_accepted() -> None:
    """Valid 2-letter abbreviations are accepted and upper-cased."""
    spec = SimilarityQuerySpec(anchor_name="Denver", exclude_states=["ca", "tx"])
    assert spec.exclude_states == ["CA", "TX"]


def test_exclude_states_full_name_normalizes_to_abbreviation() -> None:
    """Full state name 'California' normalizes to 'CA'."""
    spec = SimilarityQuerySpec(anchor_name="Denver", exclude_states=["California"])
    assert spec.exclude_states == ["CA"]


def test_exclude_states_invalid_entry_raises_value_error() -> None:
    """An entry that cannot be normalized to 2 characters raises ValueError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="2-letter state abbreviation"):
        SimilarityQuerySpec(anchor_name="Denver", exclude_states=["Not A State XYZ"])


# --------------------------------------------------------------------------
# PR 2B (Fix 3B): planner snippet selection — disambiguation regression
# --------------------------------------------------------------------------


def test_similarity_discovery_snippet_fires_for_find_peers_prompt() -> None:
    """SIMILARITY_DISCOVERY snippet fires on 'find peers' prompts without a metric phrase."""
    from compass_backend.planning.instruction_snippets import (
        SIMILARITY_DISCOVERY,
        select_planner_instruction_snippets,
    )

    class _FakeDeps:
        message = "find me peers to Denver"
        query_context = None
        recent_routes: tuple[str, ...] = ()

    snippets = select_planner_instruction_snippets(_FakeDeps())
    names = [s.name for s in snippets]
    assert SIMILARITY_DISCOVERY.name in names


def test_similarity_discovery_snippet_fires_for_adversarial_peer_salary_prompt() -> None:
    """SIMILARITY_DISCOVERY snippet still fires on 'Find peers … and show their salaries'.

    Both SIMILARITY_DISCOVERY and PEER_SALARY_COMPARISON fire; the LLM sees
    the mandatory disambiguation rule in the SIMILARITY_DISCOVERY body and
    must pick operation='peer_comparison' per the rule.  This test confirms:
    - SIMILARITY_DISCOVERY snippet is present (disambiguation instruction visible)
    - The disambiguation text in the snippet body is a mandatory imperative
    """
    from compass_backend.planning.instruction_snippets import (
        SIMILARITY_DISCOVERY,
        select_planner_instruction_snippets,
    )

    class _FakeDeps:
        message = "find peers to denver and show their salaries"
        query_context = None
        recent_routes: tuple[str, ...] = ()

    snippets = select_planner_instruction_snippets(_FakeDeps())
    names = [s.name for s in snippets]
    # SIMILARITY_DISCOVERY fires so LLM sees the disambiguation rule
    assert SIMILARITY_DISCOVERY.name in names
    # The body text contains the mandatory disambiguation rule
    sim_snippet = next(s for s in snippets if s.name == SIMILARITY_DISCOVERY.name)
    assert "If the prompt mentions any policy metric phrase" in sim_snippet.body


def test_similarity_disambiguation_rule_in_snippet_body() -> None:
    """The snippet body contains a strong mandatory disambiguation imperative.

    Regression for PR 2B Fix 3A: the original disambiguation rule was advisory
    ('use peer_comparison'); the revised rule is mandatory (explicit 'If ... route to').
    """
    from compass_backend.planning.instruction_snippets import SIMILARITY_DISCOVERY

    body = SIMILARITY_DISCOVERY.body
    assert "If the prompt mentions any policy metric phrase" in body
    assert "route to `operation=\"peer_comparison\"`" in body
    assert "operation=\"peer_comparison\"" in body


def test_similarity_discovery_snippet_fires_alongside_peer_salary_for_max_salary_peers_prompt() -> None:
    """SIMILARITY_DISCOVERY fires even alongside 'maximum salary' peer prompts.

    Both snippets fire so the LLM sees the disambiguation rule in
    SIMILARITY_DISCOVERY body and the peer-salary instruction in
    PEER_SALARY_COMPARISON body. The LLM picks operation='peer_comparison'
    per the mandatory disambiguation rule.
    """
    from compass_backend.planning.instruction_snippets import (
        PEER_SALARY_COMPARISON,
        SIMILARITY_DISCOVERY,
        select_planner_instruction_snippets,
    )

    class _FakeDeps:
        message = "find peers to denver and compare maximum salary"
        query_context = None
        recent_routes: tuple[str, ...] = ()

    snippets = select_planner_instruction_snippets(_FakeDeps())
    names = [s.name for s in snippets]
    # BOTH snippets fire: LLM sees disambiguation rule + peer-salary instruction
    assert SIMILARITY_DISCOVERY.name in names
    assert PEER_SALARY_COMPARISON.name in names


def _peer_required_guidance(
    name: str = "peer-policy-comparison",
) -> PlannerInstructionSelection:
    return PlannerInstructionSelection(
        name=name,
        body="",
        metadata={"required_operation": "peer_comparison"},
    )


def test_peer_policy_prompt_retries_direct_route() -> None:
    """Peer-policy comparisons are data turns, not direct prose turns."""

    turn = PlannerTurn(
        route="direct",
        confidence=0.9,
        direct_response=DirectResponse(
            message="I can help compare peer policies.",
            reason="capability explanation",
        ),
    )
    frame = PlannerDeps(
        message="planner guidance already selected",
        planner_guidance=(_peer_required_guidance(),),
    )

    with pytest.raises(ModelRetry, match="peer_comparison"):
        validate_planner_turn_quality(turn, frame)


def test_validator_retries_generic_unsupported_shape_clarification() -> None:
    """Planner clarifications must not surface obsolete shape-refusal prose."""

    turn = PlannerTurn(
        route="clarify",
        confidence=0.0,
        clarification=ClarificationRequest(
            question=(
                "I couldn't structure that request into a supported query shape. "
                "Compass works best when you focus on one metric at a time. "
                "If you want two metrics side-by-side, try running each one "
                "separately first."
            ),
            missing_fields=["comparison_group", "metric"],
            pending_context=PendingQueryContext(),
        ),
    )
    frame = PlannerDeps(
        message=(
            "starting salary for teachers with a BA and teachers with an MA"
        )
    )

    with pytest.raises(ModelRetry, match="generic unsupported-shape refusal"):
        validate_planner_turn_quality(turn, frame)


def test_peer_policy_prompt_retries_clarify_route() -> None:
    """Known anchor + peer wording + policy topic should not clarify for scope."""

    turn = PlannerTurn(
        route="clarify",
        confidence=0.72,
        clarification=ClarificationRequest(
            question="Which comparison group should I use?",
            missing_fields=["comparison_group"],
            pending_context=None,
        ),
    )
    frame = PlannerDeps(
        message="planner guidance already selected",
        planner_guidance=(_peer_required_guidance(),),
    )

    with pytest.raises(ModelRetry, match="peer_comparison"):
        validate_planner_turn_quality(turn, frame)


def test_peer_policy_prompt_retries_similarity_route() -> None:
    """Similarity is discovery-only; policy-topic peer prompts need peer_comparison."""

    turn = PlannerTurn(
        route="execute",
        confidence=0.85,
        query_plan=QueryPlan(
            operation="similarity",
            question=(
                "I work in Denver Public Schools. Who are our peer districts "
                "and how do our sick leave policies compare?"
            ),
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Denver Public Schools"],
            ),
            similarity=SimilarityQuerySpec(anchor_name="Denver Public Schools"),
        ),
    )
    frame = PlannerDeps(
        message="planner guidance already selected",
        planner_guidance=(_peer_required_guidance(),),
    )

    with pytest.raises(ModelRetry, match="peer_comparison"):
        validate_planner_turn_quality(turn, frame)


def test_peer_salary_guidance_retries_similarity_route() -> None:
    """Peer-salary selected guidance also requires peer_comparison."""

    turn = PlannerTurn(
        route="execute",
        confidence=0.85,
        query_plan=QueryPlan(
            operation="similarity",
            question=(
                "What is the maximum teacher salary in San Bernardino City "
                "Unified and comparable districts?"
            ),
            selection=SelectionSpec(
                scope="named_districts",
                districts=["San Bernardino City Unified"],
            ),
            similarity=SimilarityQuerySpec(
                anchor_name="San Bernardino City Unified"
            ),
        ),
    )
    frame = PlannerDeps(
        message="planner guidance already selected",
        planner_guidance=(_peer_required_guidance("peer-salary-comparison"),),
    )

    with pytest.raises(ModelRetry, match="peer_comparison"):
        validate_planner_turn_quality(turn, frame)


def test_peer_policy_prompt_accepts_peer_comparison_route() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.88,
        query_plan=QueryPlan(
            operation="peer_comparison",
            question=(
                "I work in Denver Public Schools. Who are our peer districts "
                "and how do our sick leave policies compare?"
            ),
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Denver Public Schools"],
            ),
            metrics=[MetricSpec(name="sick leave policy")],
        ),
    )
    frame = PlannerDeps(
        message="planner guidance already selected",
        planner_guidance=(_peer_required_guidance(),),
    )

    assert validate_planner_turn_quality(turn, frame) is turn


def test_peer_only_similarity_prompt_is_not_rejected() -> None:
    """The #729 guard must not collapse discovery-only peer prompts."""

    turn = PlannerTurn(
        route="execute",
        confidence=0.86,
        query_plan=QueryPlan(
            operation="similarity",
            question="Find peer districts to Denver Public Schools.",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Denver Public Schools"],
            ),
            similarity=SimilarityQuerySpec(anchor_name="Denver Public Schools"),
        ),
    )
    frame = PlannerDeps(message="Find peer districts to Denver Public Schools.")

    assert validate_planner_turn_quality(turn, frame) is turn


def test_peer_policy_raw_message_without_guidance_is_not_rejected() -> None:
    """The validator must not re-parse raw prose when guidance was not selected."""

    turn = PlannerTurn(
        route="direct",
        confidence=0.9,
        direct_response=DirectResponse(
            message="I can help compare peer policies.",
            reason="capability explanation",
        ),
    )
    frame = PlannerDeps(
        message=(
            "I work in Denver Public Schools. Who are our peer districts and "
            "how do our sick leave policies compare?"
        )
    )

    assert validate_planner_turn_quality(turn, frame) is turn


# --------------------------------------------------------------------------
# PR 2B (Improvement C): methodology post-filter narrowed code
# --------------------------------------------------------------------------


def test_similarity_post_filter_narrowed_methodology_code() -> None:
    """When pre_filter_peer_count > len(peers), similarity_post_filter_narrowed_peer_set
    is added to methodology_codes. When equal, it is not added.

    This exercises the empty-after-filter path where all peers were filtered out
    (pre_filter_count=3, post_filter_count=0 via build_similarity_result).
    """
    from compass_backend.catalog import CoveredNCESProfileRow, PeerSetResolution
    from compass_backend.execution.peer import build_similarity_result

    # Build a PeerSetResolution with no peers (all filtered out by metric-value filter)
    peer_set = PeerSetResolution(
        anchor=CoveredNCESProfileRow(
            district_id=1,
            district_name="Denver",
            state="CO",
        ),
        peers=[],
        policy_version="v1",
        selection_method="nces_similarity",
    )

    plan = QueryPlan(
        operation="similarity",
        question="Find peers to Denver with more than 190 workdays.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver"]),
        similarity=SimilarityQuerySpec(anchor_name="Denver"),
    )

    # pre_filter_peer_count=3, post_filter_count=0 (all peers filtered)
    result = build_similarity_result(plan, peer_set=peer_set, pre_filter_peer_count=3)
    codes = [ref.code for ref in result.methodology_codes]
    assert "similarity_post_filter_narrowed_peer_set" in codes

    # When pre_filter_peer_count equals post (no narrowing), code is absent
    result_no_narrowing = build_similarity_result(plan, peer_set=peer_set, pre_filter_peer_count=0)
    codes_no_narrowing = [ref.code for ref in result_no_narrowing.methodology_codes]
    assert "similarity_post_filter_narrowed_peer_set" not in codes_no_narrowing


# --------------------------------------------------------------------------
# M3 #731/#732: FOLLOW_UP_REFERENCE selection for new multi-turn patterns
# --------------------------------------------------------------------------


class _FollowUpDeps:
    """Minimal deps fixture for follow-up snippet selection tests.

    `query_context=object()` satisfies `requires_query_context=True` on the
    FOLLOW_UP_REFERENCE snippet — the selector only checks truthiness.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        self.query_context: object = object()
        self.recent_routes: tuple[str, ...] = ()


def test_follow_up_reference_fires_on_in_list_above() -> None:
    """M3a-5 / M3b-1 T3 pattern: 'how many days do teachers work in list above'."""
    from compass_backend.planning.instruction_snippets import (
        FOLLOW_UP_REFERENCE,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _FollowUpDeps("how many days do teachers work in list above")
    )

    assert FOLLOW_UP_REFERENCE.name in [s.name for s in snippets]


def test_follow_up_reference_fires_on_for_those_n() -> None:
    """M3a-2 pattern: 'for those 12 districts, list them out sorted by ...'.

    The "for those" trigger covers the canonical M3a Shape-A pattern (inherit
    prior result set, apply new sort or new attribute lookup). The body
    teaches the planner to recognise this referent shape; the trigger gets
    the snippet into the context.
    """
    from compass_backend.planning.instruction_snippets import (
        FOLLOW_UP_REFERENCE,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _FollowUpDeps("for those 12 districts, list them out sorted by fewest to most")
    )

    assert FOLLOW_UP_REFERENCE.name in [s.name for s in snippets]


def test_follow_up_reference_does_not_fire_without_prior_context() -> None:
    """Snippet requires_query_context — must not fire on cold-start turns."""
    from compass_backend.planning.instruction_snippets import (
        FOLLOW_UP_REFERENCE,
        select_planner_instruction_snippets,
    )

    class _NoContextDeps:
        message = "What are the 17 districts and how many observations?"
        query_context = None
        recent_routes: tuple[str, ...] = ()

    snippets = select_planner_instruction_snippets(_NoContextDeps())

    assert FOLLOW_UP_REFERENCE.name not in [s.name for s in snippets]


def test_follow_up_reference_body_documents_inheritance_pattern() -> None:
    """Body must explicitly teach inherit_selection_from='prior_result_rows'
    and the referent sentinel pattern. Without these, the planner has no
    typed protocol to express multi-turn inheritance.
    """
    from compass_backend.planning.instruction_snippets import FOLLOW_UP_REFERENCE

    body = FOLLOW_UP_REFERENCE.body
    assert "prior_result_rows" in body
    assert "inherit_selection_from" in body
    # At least one referent sentinel literal must be mentioned in the body
    # so the planner knows what to emit.
    assert any(s in body for s in ('"<inherit>"', '"those"', '"that"'))


# --------------------------------------------------------------------------
# M3 #731/#732/#733: Post-merge shape guard (_plan_likely_dispatchable)
# --------------------------------------------------------------------------


def test_plan_likely_dispatchable_skips_multi_metric_rank() -> None:
    """Multi-metric rank delegates to lookup/limited_ranked_lookup — not
    pre-empted by the orchestrator-level shape guard.
    """
    from compass_backend.orchestration.chat import _plan_likely_dispatchable

    plan = QueryPlan(
        question="Rank covered districts by BA salary, show MA alongside.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary (BA)", role="primary"),
            MetricSpec(name="starting salary (MA)", role="comparison"),
        ],
        limit=LimitSpec(count=10, kind="top"),
        sort=SortSpec(field="starting salary (BA)", direction="desc"),
    )

    assert _plan_likely_dispatchable(plan) is True


def test_plan_likely_dispatchable_skips_profile_ordered_rank() -> None:
    """Profile-ordered rankings have their own dispatch via sort_steps."""
    from compass_backend.orchestration.chat import _plan_likely_dispatchable

    plan = QueryPlan(
        question="Show salaries for the highest-FRPL districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary", role="primary"),
            MetricSpec(name="FRPL share", role="grouping"),
        ],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="FRPL share",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )

    assert _plan_likely_dispatchable(plan) is True


def test_plan_likely_dispatchable_does_not_skip_single_metric_rank_with_unsupported_scope() -> None:
    """Single-metric rank with a clean shape — guard would fire if shape_check
    reports a hint. This test exercises the negative case of the dispatch
    helper: when no delegation path applies, the guard runs.
    """
    from compass_backend.orchestration.chat import _plan_likely_dispatchable

    plan = QueryPlan(
        question="Rank covered districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary", role="primary")],
    )

    # No multi-metric, no profile sort_step → no delegation; guard runs.
    assert _plan_likely_dispatchable(plan) is False


def test_shape_guard_does_not_introduce_prose_dispatch() -> None:
    """The shape guard's helpers must read typed QueryPlan fields only —
    no inspection of plan.question / user message text. CLAUDE.md doctrine.
    """
    from pathlib import Path

    chat_py = (
        Path(__file__).resolve().parents[1] / "orchestration" / "chat.py"
    ).read_text()

    # Locate the guard helper region and confirm it doesn't read plan.question
    # via casefold / regex / substring operations on free text.
    guard_region_start = chat_py.find("def _apply_post_merge_shape_guard")
    assert guard_region_start != -1, "guard helper missing"
    # Take from the helper through the next top-level def.
    rest = chat_py[guard_region_start:]
    next_def = rest.find("\n\ndef ", 10)
    guard_region = rest[:next_def] if next_def != -1 else rest
    # plan.question is passed VERBATIM into PendingQueryContext.question
    # (typed field transfer) — that's allowed. What's forbidden is reading
    # plan.question via casefold / regex / phrase-set membership.
    forbidden = ["plan.question.casefold", "plan.question.lower", "in plan.question"]
    for marker in forbidden:
        assert marker not in guard_region, (
            f"shape guard must not inspect plan.question prose; found {marker!r}"
        )


# --------------------------------------------------------------------------
# M3 rescue-path enrichment (extends post-merge shape guard)
# --------------------------------------------------------------------------


def _build_prior_context_with_districts(
    district_names: list[str],
    metric_name: str = "starting salary",
):
    """Build a QueryContext with concrete result_districts for tests."""
    from compass_backend.contracts import QueryContext
    from compass_backend.contracts.session import (
        QueryContextDistrictRef,
        QueryContextMetricRef,
    )

    return QueryContext(
        query_plan=QueryPlan(
            question="Show districts ranked by salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name=metric_name)],
        ),
        result_type="metric_ranking",
        row_count=len(district_names),
        result_districts=[
            QueryContextDistrictRef(
                district_id=i + 1,
                district_name=name,
                state="CA",
            )
            for i, name in enumerate(district_names)
        ],
        result_metrics=[
            QueryContextMetricRef(metric_id=1001, metric_name=metric_name),
        ],
    )


def test_rescue_enrichment_helper_recognises_safe_structure_clarification() -> None:
    """The rescue-enrichment predicate fires only on the exact
    safe-structure rescue clarification (is_rescue_fallback=True), with prior context.
    """
    from compass_backend.contracts import (
        ClarificationRequest,
        ConversationMemory,
        PlannerTurn,
    )
    from compass_backend.contracts.session import SessionState
    from compass_backend.orchestration.chat import (
        _rescue_clarification_can_be_enriched,
        _TurnContext,
    )
    from compass_backend.config import settings as _settings
    from compass_backend.contracts.session import new_id
    from compass_backend.contracts import ChatRequest

    prior = _build_prior_context_with_districts(["Alpha", "Bravo", "Charlie"])
    memory = ConversationMemory(latest_query_context=prior)
    session = SessionState(session_id=new_id(), memory=memory)
    request = ChatRequest(message="how about something else for those districts?")
    ctx = _TurnContext(
        request=request, app_settings=_settings, auth_user=None, trace_id=None
    )
    ctx.session_state = session
    ctx.turn = PlannerTurn(
        route="clarify",
        confidence=0.0,
        clarification=ClarificationRequest(
            # W1-08 (#848): prose constant removed; rescue detection is
            # typed via is_rescue_fallback=True.
            question="placeholder rescue prose (typed marker is what matters)",
            missing_fields=["comparison_group", "metric"],
            is_rescue_fallback=True,
        ),
    )

    assert _rescue_clarification_can_be_enriched(ctx) is True


def test_rescue_enrichment_helper_fires_without_prior_context_for_fallback() -> None:
    """No prior QueryContext → enrichment STILL fires (fallback rephrase branch).

    Closes M3c-1: when T1 is a clarification (not an execute) and T2 hits the
    planner's safe-structure rescue, we have no prior districts to anchor on,
    but the orchestrator still replaces the verbatim refusal with a typed
    rephrase-suggestion clarification so milestone acceptance "never return
    the generic refusal" holds.
    """
    from compass_backend.contracts import (
        ClarificationRequest,
        ConversationMemory,
        PlannerTurn,
    )
    from compass_backend.contracts.session import SessionState
    from compass_backend.orchestration.chat import (
        _rescue_clarification_can_be_enriched,
        _TurnContext,
    )
    from compass_backend.config import settings as _settings
    from compass_backend.contracts.session import new_id
    from compass_backend.contracts import ChatRequest

    memory = ConversationMemory(latest_query_context=None)
    session = SessionState(session_id=new_id(), memory=memory)
    request = ChatRequest(message="starting salary for BA and MA")
    ctx = _TurnContext(
        request=request, app_settings=_settings, auth_user=None, trace_id=None
    )
    ctx.session_state = session
    ctx.turn = PlannerTurn(
        route="clarify",
        confidence=0.0,
        clarification=ClarificationRequest(
            # W1-08 (#848): prose constant removed; rescue detection is
            # typed via is_rescue_fallback=True.
            question="placeholder rescue prose (typed marker is what matters)",
            missing_fields=["comparison_group", "metric"],
            is_rescue_fallback=True,
        ),
    )

    assert _rescue_clarification_can_be_enriched(ctx) is True


def test_enrich_rescue_without_prior_context_uses_fallback_message() -> None:
    """When no prior context exists, the rescue branch emits a typed
    rephrase-suggestion message that does NOT contain the verbatim
    safe-structure rescue prose. This is the contract for M3c-1.
    """
    from compass_backend.contracts import (
        ClarificationRequest,
        ConversationMemory,
        PlannerTurn,
    )
    from compass_backend.contracts.session import SessionState
    from compass_backend.orchestration.chat import (
        _enrich_rescue_with_prior_context,
        _TurnContext,
    )
    from compass_backend.config import settings as _settings
    from compass_backend.contracts.session import new_id
    from compass_backend.contracts import ChatRequest

    memory = ConversationMemory(latest_query_context=None)
    session = SessionState(session_id=new_id(), memory=memory)
    request = ChatRequest(message="starting salary for BA and MA in one table")
    ctx = _TurnContext(
        request=request, app_settings=_settings, auth_user=None, trace_id=None
    )
    ctx.session_state = session
    ctx.turn = PlannerTurn(
        route="clarify",
        confidence=0.0,
        clarification=ClarificationRequest(
            # W1-08 (#848): prose constant removed; rescue detection is
            # typed via is_rescue_fallback=True.
            question="placeholder rescue prose (typed marker is what matters)",
            missing_fields=["comparison_group", "metric"],
            is_rescue_fallback=True,
        ),
    )

    _enrich_rescue_with_prior_context(ctx, turn_span=None)

    assert ctx.turn is not None
    assert ctx.turn.clarification is not None
    new_question = ctx.turn.clarification.question
    # W1-08 (#848): the enriched message MUST diverge from the original
    # placeholder rescue prose — milestone acceptance ("never return the
    # generic refusal") survives whether the source prose was string-matched
    # or typed-marker matched.
    assert new_question != "placeholder rescue prose (typed marker is what matters)"
    # The new message names at least one supported query pattern so the user
    # has a concrete rephrase template.
    assert "top 10 districts" in new_question or "compare" in new_question
    # PendingQueryContext is still emitted (typed-clarification contract).
    assert ctx.next_pending_context is not None
    assert ctx.next_pending_context.question == request.message


def test_rescue_enrichment_helper_does_not_fire_on_non_rescue_clarify() -> None:
    """Other clarifications (e.g., metric-disambiguation) pass through unchanged."""
    from compass_backend.contracts import (
        ClarificationRequest,
        ConversationMemory,
        PlannerTurn,
    )
    from compass_backend.contracts.session import SessionState
    from compass_backend.orchestration.chat import (
        _rescue_clarification_can_be_enriched,
        _TurnContext,
    )
    from compass_backend.config import settings as _settings
    from compass_backend.contracts.session import new_id
    from compass_backend.contracts import ChatRequest

    prior = _build_prior_context_with_districts(["Alpha", "Bravo"])
    memory = ConversationMemory(latest_query_context=prior)
    session = SessionState(session_id=new_id(), memory=memory)
    request = ChatRequest(message="and salaries please")
    ctx = _TurnContext(
        request=request, app_settings=_settings, auth_user=None, trace_id=None
    )
    ctx.session_state = session
    ctx.turn = PlannerTurn(
        route="clarify",
        confidence=0.6,
        clarification=ClarificationRequest(
            question='Did you mean "Starting salary" or "Maximum salary"?',
            missing_fields=["metric"],
            candidates=["Starting salary", "Maximum salary"],
        ),
    )

    assert _rescue_clarification_can_be_enriched(ctx) is False


def test_planner_delta_intent_instructions_empty_when_unset() -> None:
    """Without a DeltaIntent, the helper returns the empty string."""
    from compass_backend.contracts.session import ConversationMemory
    from compass_backend.planning.planner import (
        PlannerDeps,
        planner_delta_intent_instructions,
    )

    deps = PlannerDeps(message="test", memory=ConversationMemory())
    assert planner_delta_intent_instructions(deps) == ""


def test_planner_delta_intent_instructions_contains_constraint_when_set() -> None:
    """When DeltaIntent is present, the constraint block is rendered."""
    from compass_backend.contracts.session import ConversationMemory
    from compass_backend.planning.delta_detection import DeltaIntent
    from compass_backend.planning.planner import (
        PlannerDeps,
        planner_delta_intent_instructions,
    )

    deps = PlannerDeps(
        message="reverse that",
        memory=ConversationMemory(),
        delta_intent=DeltaIntent(
            matched_anaphora=("that order",),
            matched_verbs=("reverse",),
            has_prior_query=True,
        ),
    )
    rendered = planner_delta_intent_instructions(deps)
    assert "inherit_from_session: true" in rendered
    assert 'selection.scope: "unspecified"' in rendered
    assert "metrics: []" not in rendered
    assert "new metric or profile field" in rendered
    assert "DELTA-INTENT CONSTRAINT" in rendered


def test_normalize_delta_intent_turn_forces_prior_rows_and_preserves_metric() -> None:
    """Detector-owned normalization forces selection inheritance only."""
    from compass_backend.planning.delta_detection import DeltaIntent
    from compass_backend.planning.planner import normalize_delta_intent_turn

    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="rank",
            question="Sort those by MA salary instead.",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["those districts"],
            ),
            metrics=[MetricSpec(name="MA starting salary")],
            sort=SortSpec(field="MA starting salary", direction="desc"),
        ),
    )
    delta = DeltaIntent(
        matched_anaphora=("those",),
        matched_verbs=("sort",),
        has_prior_query=True,
    )

    normalized, changed = normalize_delta_intent_turn(turn, delta)

    assert changed is True
    assert normalized.query_plan is not None
    assert normalized.query_plan.inherit_from_session is True
    assert normalized.query_plan.inherit_selection_from == "prior_result_rows"
    assert normalized.query_plan.selection.scope == "unspecified"
    assert normalized.query_plan.metrics == [MetricSpec(name="MA starting salary")]
    assert normalized.query_plan.sort == SortSpec(
        field="MA starting salary",
        direction="desc",
    )


def test_normalize_delta_intent_turn_preserves_empty_metrics_for_pure_reorder() -> None:
    """Pure reorder deltas can still inherit the prior metric downstream."""
    from compass_backend.planning.delta_detection import DeltaIntent
    from compass_backend.planning.planner import normalize_delta_intent_turn

    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            inherit_from_session=True,
            operation="rank",
            question="Order those highest to lowest.",
            selection=SelectionSpec(scope="unspecified"),
            sort=SortSpec(field="value", direction="desc"),
        ),
    )
    delta = DeltaIntent(
        matched_anaphora=("those",),
        matched_verbs=("order",),
        has_prior_query=True,
    )

    normalized, changed = normalize_delta_intent_turn(turn, delta)

    assert changed is True
    assert normalized.query_plan is not None
    assert normalized.query_plan.inherit_from_session is True
    assert normalized.query_plan.inherit_selection_from == "prior_result_rows"
    assert normalized.query_plan.metrics == []


def test_normalize_delta_intent_turn_does_not_touch_clarifications() -> None:
    """Clarify is the allowed override when the new metric cannot resolve."""
    from compass_backend.contracts import ClarificationRequest
    from compass_backend.planning.delta_detection import DeltaIntent
    from compass_backend.planning.planner import normalize_delta_intent_turn

    turn = PlannerTurn(
        route="clarify",
        confidence=0.6,
        clarification=ClarificationRequest(
            question="Which salary measure did you mean?",
            missing_fields=["metric"],
        ),
    )
    delta = DeltaIntent(
        matched_anaphora=("those",),
        matched_verbs=("sort",),
        has_prior_query=True,
    )

    normalized, changed = normalize_delta_intent_turn(turn, delta)

    assert normalized is turn
    assert changed is False


def test_validator_applies_delta_normalization_before_returning_turn() -> None:
    """Output validation should return the normalized delta plan."""
    from compass_backend.contracts.session import (
        ConversationMemory,
        QueryContext,
        QueryContextDistrictRef,
    )
    from compass_backend.planning.delta_detection import DeltaIntent

    prior = QueryPlan(
        operation="rank",
        question="Rank districts by BA salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="BA starting salary")],
    )
    context = QueryContext(
        query_plan=prior,
        result_districts=[
            QueryContextDistrictRef(
                district_id=1,
                district_name="Chicago Public Schools",
                state="IL",
            )
        ],
    )
    deps = PlannerDeps(
        message="sort those by MA salary instead",
        memory=ConversationMemory(latest_query_context=context),
        delta_intent=DeltaIntent(
            matched_anaphora=("those",),
            matched_verbs=("sort",),
            has_prior_query=True,
        ),
    )
    turn = _planner_turn(
        QueryPlan(
            operation="rank",
            question="Sort those by MA salary instead.",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["those districts"],
            ),
            metrics=[MetricSpec(name="MA starting salary")],
            sort=SortSpec(field="MA starting salary", direction="desc"),
        )
    )

    normalized = validate_planner_turn_quality(turn, deps)

    assert normalized.query_plan is not None
    assert normalized.query_plan.inherit_from_session is True
    assert normalized.query_plan.inherit_selection_from == "prior_result_rows"
    assert normalized.query_plan.selection.scope == "unspecified"
    assert [metric.name for metric in normalized.query_plan.metrics] == [
        "MA starting salary"
    ]


# --------------------------------------------------------------------------
# M1 #1006 part 1: HEALTH_BENEFIT_EXEMPLAR snippet replaces the retired
# health-benefit recovery branch from planning/answerability_recovery.py.
# Phrase authority now sits in governed planner-snippet data; the planner
# LLM emits the policy_guidance PlannerTurn natively.
# --------------------------------------------------------------------------


def _health_benefit_deps(message: str):
    class _FakeDeps:
        pass

    deps = _FakeDeps()
    deps.message = message
    deps.query_context = None
    deps.recent_routes: tuple[str, ...] = ()
    return deps


def test_health_benefit_exemplar_fires_for_great_health_coverage() -> None:
    """The literal prompt from the retired recovery test still selects the snippet."""
    from compass_backend.planning.instruction_snippets import (
        HEALTH_BENEFIT_EXEMPLAR,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _health_benefit_deps("Show me districts where teachers get great health coverage.")
    )
    names = [s.name for s in snippets]
    assert HEALTH_BENEFIT_EXEMPLAR.name in names
    selected = next(s for s in snippets if s.name == HEALTH_BENEFIT_EXEMPLAR.name)
    assert selected.metadata == {"topic_id": "benefits", "intent": "exemplar_request"}


def test_health_benefit_exemplar_fires_for_best_health_benefits() -> None:
    """Subjective-exemplar cue 'best' paired with 'health benefits' also fires."""
    from compass_backend.planning.instruction_snippets import (
        HEALTH_BENEFIT_EXEMPLAR,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _health_benefit_deps("Which districts have the best health benefits for teachers?")
    )
    assert HEALTH_BENEFIT_EXEMPLAR.name in [s.name for s in snippets]


def test_health_benefit_exemplar_blocked_for_ranking_request() -> None:
    """Data-ranking shape ('rank districts by benefits') must not select the snippet."""
    from compass_backend.planning.instruction_snippets import (
        HEALTH_BENEFIT_EXEMPLAR,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _health_benefit_deps("Rank districts by their teacher health benefits.")
    )
    assert HEALTH_BENEFIT_EXEMPLAR.name not in [s.name for s in snippets]


def test_health_benefit_exemplar_blocked_for_count_request() -> None:
    """'How many districts have good health insurance?' is a count, not exemplar."""
    from compass_backend.planning.instruction_snippets import (
        HEALTH_BENEFIT_EXEMPLAR,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _health_benefit_deps("How many districts have good teacher health insurance?")
    )
    assert HEALTH_BENEFIT_EXEMPLAR.name not in [s.name for s in snippets]


def test_health_benefit_exemplar_blocked_for_premium_data_question() -> None:
    """Asking about premium coverage (a specific metric) is a data question."""
    from compass_backend.planning.instruction_snippets import (
        HEALTH_BENEFIT_EXEMPLAR,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _health_benefit_deps(
            "Which districts have the best coverage with 100% of premium paid?"
        )
    )
    assert HEALTH_BENEFIT_EXEMPLAR.name not in [s.name for s in snippets]


def test_health_benefit_exemplar_does_not_fire_without_health_concept() -> None:
    """Subjective cue alone (without a health concept) must not fire the snippet."""
    from compass_backend.planning.instruction_snippets import (
        HEALTH_BENEFIT_EXEMPLAR,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _health_benefit_deps("Which districts have the best teacher salaries?")
    )
    assert HEALTH_BENEFIT_EXEMPLAR.name not in [s.name for s in snippets]


# --------------------------------------------------------------------------
# M1 #1006 part 2: four planner snippets replace the remaining
# answerability_recovery branches. Each test exercises the snippet selector
# to confirm the snippet matches the literal retired-recovery prompts and
# stays away from data-shaped queries that should reach the planner's
# normal recognition.
# --------------------------------------------------------------------------


def _snippet_deps(message: str):
    class _FakeDeps:
        pass

    deps = _FakeDeps()
    deps.message = message
    deps.query_context = None
    deps.recent_routes: tuple[str, ...] = ()
    return deps


# --- DATA_INVENTORY ---------------------------------------------------------


def test_data_inventory_fires_for_district_phrase() -> None:
    from compass_backend.planning.instruction_snippets import (
        DATA_INVENTORY,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("what data do you have about Philadelphia school district?")
    )
    assert DATA_INVENTORY.name in [s.name for s in snippets]


def test_data_inventory_fires_for_aurora_public_schools() -> None:
    from compass_backend.planning.instruction_snippets import (
        DATA_INVENTORY,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("what data do you have about Aurora Public Schools?")
    )
    assert DATA_INVENTORY.name in [s.name for s in snippets]


def test_data_inventory_guidance_names_performance_pay_metric_lanes() -> None:
    from compass_backend.instructions.loader import load_planner_guidance

    text = load_planner_guidance("data-inventory.md")

    assert "performance pay" in text
    assert "Minimum annual performance pay bonus, if eligible" in text
    assert "Maximum annual performance pay bonus, if eligible" in text
    assert 'reason="performance pay data inventory"' in text


def test_data_inventory_blocked_for_count_request() -> None:
    from compass_backend.planning.instruction_snippets import (
        DATA_INVENTORY,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("how many districts do you have data for?")
    )
    assert DATA_INVENTORY.name not in [s.name for s in snippets]


def test_data_inventory_blocked_for_comparison() -> None:
    from compass_backend.planning.instruction_snippets import (
        DATA_INVENTORY,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("compare what data do you have between Denver and Chicago")
    )
    assert DATA_INVENTORY.name not in [s.name for s in snippets]


# --- SALARY_SCHEDULE_LOOKUP -------------------------------------------------


def test_salary_schedule_fires_for_named_district_possessive() -> None:
    from compass_backend.planning.instruction_snippets import (
        SALARY_SCHEDULE_LOOKUP,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("Denver's salary schedule")
    )
    assert SALARY_SCHEDULE_LOOKUP.name in [s.name for s in snippets]


def test_salary_schedule_fires_for_pull_up_denver_phrasing() -> None:
    from compass_backend.planning.instruction_snippets import (
        SALARY_SCHEDULE_LOOKUP,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("Pull up the salary schedule for Denver Public Schools")
    )
    assert SALARY_SCHEDULE_LOOKUP.name in [s.name for s in snippets]


def test_salary_schedule_blocked_for_comparison() -> None:
    from compass_backend.planning.instruction_snippets import (
        SALARY_SCHEDULE_LOOKUP,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("Compare Denver's salary schedule with Chicago's")
    )
    assert SALARY_SCHEDULE_LOOKUP.name not in [s.name for s in snippets]


def test_salary_schedule_blocked_for_highest_ranking() -> None:
    from compass_backend.planning.instruction_snippets import (
        SALARY_SCHEDULE_LOOKUP,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("Show me the highest salary schedule in the state")
    )
    assert SALARY_SCHEDULE_LOOKUP.name not in [s.name for s in snippets]


# --- PARENTAL_LEAVE_BEYOND_BIRTHING -----------------------------------------


def test_parental_leave_beyond_birthing_fires_for_retired_recovery_prompt() -> None:
    from compass_backend.planning.instruction_snippets import (
        PARENTAL_LEAVE_BEYOND_BIRTHING,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps(
            "show me which districts give paid parental leave to more than just "
            "the birthing parent"
        )
    )
    assert PARENTAL_LEAVE_BEYOND_BIRTHING.name in [s.name for s in snippets]


def test_parental_leave_beyond_birthing_fires_for_adoptive_lane() -> None:
    from compass_backend.planning.instruction_snippets import (
        PARENTAL_LEAVE_BEYOND_BIRTHING,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps(
            "Which districts provide paid parental leave to the birthing parent "
            "and to adoptive parents too?"
        )
    )
    assert PARENTAL_LEAVE_BEYOND_BIRTHING.name in [s.name for s in snippets]


def test_parental_leave_beyond_birthing_does_not_fire_for_single_lane_only() -> None:
    """Birthing-only prompts must not trigger the multi-lane snippet."""
    from compass_backend.planning.instruction_snippets import (
        PARENTAL_LEAVE_BEYOND_BIRTHING,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("How many days of paid parental leave do birthing parents get?")
    )
    assert PARENTAL_LEAVE_BEYOND_BIRTHING.name not in [s.name for s in snippets]


def test_parental_leave_beyond_birthing_does_not_fire_without_birthing_contrast() -> None:
    """No birthing-parent contrast point → snippet doesn't fire."""
    from compass_backend.planning.instruction_snippets import (
        PARENTAL_LEAVE_BEYOND_BIRTHING,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("Show me adoptive leave policies across districts")
    )
    assert PARENTAL_LEAVE_BEYOND_BIRTHING.name not in [s.name for s in snippets]


# --- SICK_LEAVE_DAY_RANKING (recipe migration #1060) -------------------------
#
# These assert the recipe-shaped half of #1060: the planner LLM is taught the
# governed Texas sick/leave-day ranking shape via a selected guidance snippet.
# The planning/sick_leave.py prose-dispatch normalizer it replaced has now been
# deleted (#1060 deletion half), so the guidance snippet is the sole mechanism.


def test_sick_leave_ranking_snippet_fires_for_texas_ranking_prompt() -> None:
    from compass_backend.planning.instruction_snippets import (
        SICK_LEAVE_DAY_RANKING,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps(
            "How many paid sick leave days do teachers in Texas districts get, "
            "ranked lowest to highest?"
        )
    )
    assert SICK_LEAVE_DAY_RANKING.name in [s.name for s in snippets]


def test_sick_leave_ranking_snippet_does_not_fire_for_single_district_lookup() -> None:
    """A single-district Texas lookup with no ranking verb must not fire."""
    from compass_backend.planning.instruction_snippets import (
        SICK_LEAVE_DAY_RANKING,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("How many paid sick leave days does Austin ISD in Texas offer?")
    )
    assert SICK_LEAVE_DAY_RANKING.name not in [s.name for s in snippets]


def test_sick_leave_ranking_snippet_does_not_fire_without_texas_scope() -> None:
    """A ranking sick-leave prompt with no Texas scope must not fire."""
    from compass_backend.planning.instruction_snippets import (
        SICK_LEAVE_DAY_RANKING,
        select_planner_instruction_snippets,
    )

    snippets = select_planner_instruction_snippets(
        _snippet_deps("Rank California districts by paid sick leave days, most first.")
    )
    assert SICK_LEAVE_DAY_RANKING.name not in [s.name for s in snippets]


def test_sick_leave_ranking_recipe_documents_governed_three_metric_shape() -> None:
    """The selected snippet body teaches the exact governed ranking shape."""
    from compass_backend.planning.instruction_snippets import (
        SICK_LEAVE_DAY_RANKING,
    )

    body = SICK_LEAVE_DAY_RANKING.body
    assert 'operation="rank"' in body
    assert 'selection.states=["TX"]' in body
    assert "Maximum number of annual paid sick days" in body
    assert (
        "Number of paid leave days granted in first year of employment if "
        "district does not distinguish between paid sick and personal leave days"
    ) in body
    assert (
        "Total number of paid sick and paid personal leave days granted in "
        "first year of employment if district does differentiate between "
        "personal and sick leave"
    ) in body


# --- PARENTAL_LEAVE_BEYOND_BIRTHING negative-filter recipe (#1059) -----------
#
# The negative parental-leave shape ("districts with no parental leave") now
# lives as an explicit typed example in the parental-leave-beyond-birthing
# recipe, replacing the planning/negative_filters.py prose-dispatch normalizer.


def test_parental_leave_recipe_documents_negative_filter_shape() -> None:
    from compass_backend.planning.instruction_snippets import (
        PARENTAL_LEAVE_BEYOND_BIRTHING,
    )

    body = PARENTAL_LEAVE_BEYOND_BIRTHING.body
    assert "Does the district offer paid parental leave?" in body
    assert 'operator="equals"' in body
    assert "value=False" in body
    assert "no parental leave" in body


# --------------------------------------------------------------------------
# #1315 FIX 2b — profile-ordered metric display
# (REGR-M1-CLOSURE-MA-ENROLLMENT-SORT-C00). "Show <policy metric> for the
# districts with the highest <profile field>" must rank/select BY the profile
# field but DISPLAY the policy metric (routes to the profile-ordered branch).
# --------------------------------------------------------------------------


def _profile_ordered_metric_display_plan() -> QueryPlan:
    """A correctly-shaped MA-salary-by-enrollment-desc profile-ordered plan."""

    return QueryPlan(
        operation="rank",
        question=(
            "Show me master's starting salaries for the districts with the "
            "highest enrollment."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="master's starting salary", degree_lane="ma")],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="enrollment",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )


def _profile_intent_guidance() -> PlannerInstructionSelection:
    return PlannerInstructionSelection(
        name="profile-sort-metric-display",
        body="",
        metadata={"intent": "profile_ordered_metric_display"},
    )


def test_profile_ordered_metric_plan_routes_to_profile_ordered_branch() -> None:
    # (f) The routing trio in execution/operations.py:719-728 selects the
    # profile-ordered branch for this plan, and the shape check raises no hint.
    from compass_backend.execution._helpers import (
        _policy_presentation_step,
        _primary_metric_is_policy_metric,
        _profile_selection_step,
    )

    plan = _profile_ordered_metric_display_plan()

    assert _profile_selection_step(plan) is not None
    assert _primary_metric_is_policy_metric(plan) is True
    assert _policy_presentation_step(plan) is None
    assert unsupported_shape_hint(plan) is None


def test_grounding_guard_retries_dropped_policy_metric_profile_ranking() -> None:
    # (g) The grounding guard fires when the selected guidance carries the
    # profile-ordered intent but the plan dropped the policy metric and emitted
    # a plain enrollment ranking instead.
    dropped_plan = QueryPlan(
        operation="rank",
        question=(
            "Show me master's starting salaries for the districts with the "
            "highest enrollment."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        profile_fields=[ProfileFieldSpec(name="enrollment")],
        sort=SortSpec(field="enrollment", direction="desc"),
    )
    frame = PlannerDeps(
        message="planner guidance already selected",
        planner_guidance=(_profile_intent_guidance(),),
    )

    with pytest.raises(ModelRetry, match="profile-ordered metric display"):
        validate_planner_turn_quality(_planner_turn(dropped_plan), frame)


def test_grounding_guard_passes_profile_ordered_metric_plan_unchanged() -> None:
    # (g) The correctly-shaped profile-ordered plan passes the guard unchanged.
    frame = PlannerDeps(
        message="planner guidance already selected",
        planner_guidance=(_profile_intent_guidance(),),
    )
    turn = _planner_turn(_profile_ordered_metric_display_plan())

    assert validate_planner_turn_quality(turn, frame) is turn


def test_direction_pins_ascending_for_sort_and_selection_step() -> None:
    # (h) direction() honors an explicit asc SortSpec and an asc selection-phase
    # SortStepSpec.
    from compass_backend.execution.selection import direction

    sort_plan = QueryPlan(
        operation="rank",
        question="Rank districts by starting salary, cheapest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort=SortSpec(field="starting salary", direction="asc"),
    )
    assert direction(sort_plan) == "asc"

    step_plan = QueryPlan(
        operation="rank",
        question="Show salaries for the smallest districts by enrollment.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="enrollment",
                direction="asc",
                key_type="profile_field",
            )
        ],
    )
    assert direction(step_plan) == "asc"


def test_direction_survives_finalize_for_lowest_first_compare() -> None:
    """S41/S107 regression: 'compare … ranked lowest first' must stay ``asc``
    through the *finalized* plan the executor consumes — not just the draft.

    The planner emits ``sort=SortSpec(direction="asc")`` correctly, but
    ``finalizer._collapse_legacy_sort`` folds that top-level sort into a
    ``presentation``-phase ``SortStepSpec`` and clears ``plan.sort``. Pinning
    ``direction()`` on the *draft* (the test above) never caught that the
    finalized plan dropped the direction to the ``desc`` default, rendering
    'highest to lowest' for an explicit 'lowest first'. The check belongs at
    the execution boundary — the finalized plan — per the #1248 ends invariant.
    """
    from compass_backend.contracts.catalog_resolution import (
        CatalogReconciliationReport,
    )
    from compass_backend.execution.selection import direction
    from compass_backend.planning.finalizer import finalize_plan

    draft = QueryPlan(
        operation="rank",
        question=(
            "Compare starting teacher salaries for teachers with a masters' "
            "degree in San Bernardino, Capistrano, and Los Angeles, ranked "
            "lowest first"
        ),
        selection=SelectionSpec(
            scope="named_districts",
            districts=["San Bernardino", "Capistrano", "Los Angeles"],
        ),
        metrics=[MetricSpec(name="starting teacher salary masters degree")],
        sort=SortSpec(field="starting teacher salary masters degree", direction="asc"),
    )
    assert direction(draft) == "asc"

    finalized = finalize_plan(draft, CatalogReconciliationReport()).plan
    # Finalizer normalizes the display sort into a presentation-phase step and
    # clears plan.sort — the direction must still resolve to asc.
    assert finalized.sort is None
    assert [s.phase for s in finalized.sort_steps] == ["presentation"]
    assert direction(finalized) == "asc"


def test_lookup_sort_accessors_survive_finalize_for_value_desc() -> None:
    """A single-metric value lookup sorted highest-first must keep its
    ``value``/``desc`` intent through the *finalized* plan the executor reads.

    The finalizer folds ``plan.sort`` into a presentation-phase SortStepSpec and
    clears ``plan.sort``, so the lookup readers must consult ``sort_steps`` (via
    the canonical accessors) — not the always-None ``plan.sort``. Without the
    fix ``_lookup_sort_kind`` returns ``"district"`` and the rows render in
    alphabetical district order instead of the requested value descending.
    """
    from compass_backend.contracts.catalog_resolution import (
        CatalogReconciliationReport,
    )
    from compass_backend.execution.lookup import (
        _lookup_direction_for_kind,
        _lookup_sort_is_supported,
        _lookup_sort_kind,
    )
    from compass_backend.execution.selection import sort_kind as _sort_kind
    from compass_backend.planning.finalizer import finalize_plan

    draft = QueryPlan(
        operation="lookup",
        question="Teacher salary for these districts, highest to lowest",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["San Bernardino", "Capistrano", "Los Angeles"],
        ),
        metrics=[MetricSpec(name="starting teacher salary")],
        sort=SortSpec(field="starting teacher salary", direction="desc"),
    )

    finalized = finalize_plan(draft, CatalogReconciliationReport()).plan
    assert finalized.sort is None
    assert _sort_kind(finalized) == "value"
    assert _lookup_sort_kind(finalized) == "value"
    assert _lookup_direction_for_kind(finalized, "value") == "desc"
    # Single-metric value sort is still an honest supported shape.
    assert _lookup_sort_is_supported(finalized) is True


def test_lookup_sort_is_supported_refuses_multi_metric_value_after_finalize() -> None:
    """A multi-metric value sort the row-sorter cannot honor must still be
    refused post-finalize — the gate reads the folded sort step, not plan.sort.
    """
    from compass_backend.contracts.catalog_resolution import (
        CatalogReconciliationReport,
    )
    from compass_backend.execution.lookup import _lookup_sort_is_supported
    from compass_backend.planning.finalizer import finalize_plan

    draft = QueryPlan(
        operation="lookup",
        question="Salary and benefits for these districts, sorted by salary",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["San Bernardino", "Los Angeles"],
        ),
        metrics=[
            MetricSpec(name="starting teacher salary"),
            MetricSpec(name="health benefits", role="comparison"),
        ],
        sort=SortSpec(field="starting teacher salary", direction="desc"),
    )

    finalized = finalize_plan(draft, CatalogReconciliationReport()).plan
    assert finalized.sort is None
    # Two metrics + a folded value sort the lookup row-sorter cannot honor.
    assert _lookup_sort_is_supported(finalized) is False


def test_lookup_district_sort_step_resolves_district_kind() -> None:
    """A district-keyed presentation sort step resolves to ``district`` kind and
    preserves its direction through the canonical accessors."""
    from compass_backend.execution.lookup import (
        _lookup_direction_for_kind,
        _lookup_sort_kind,
    )

    plan = QueryPlan(
        operation="lookup",
        question="List these districts, reverse alphabetical",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["San Bernardino", "Los Angeles"],
        ),
        metrics=[MetricSpec(name="starting teacher salary")],
        sort_steps=[
            SortStepSpec(
                field="district",
                direction="desc",
                key_type="district",
                phase="presentation",
            )
        ],
    )
    assert plan.sort is None
    assert _lookup_sort_kind(plan) == "district"
    assert _lookup_direction_for_kind(plan, "district") == "desc"


# --------------------------------------------------------------------------
# #1373 Stage 3: the planner emits the typed FilterSpec.kind. These cheap
# unit tests (no LLM, no DB) pin the two guarantees Stage 3 leans on:
#
#   1. A QueryPlan whose FilterSpec carries an explicit ``kind`` is still a
#      supported shape — ``unsupported_shape_hint`` returns ``None`` — for
#      every kind the planner is now instructed to emit. The typed decision
#      rides alongside the same field/value the planner already produced, so
#      the executor's shape gates (which route through ``filter_kind``) see
#      the same classification with or without the planner setting ``kind``.
#
#   2. ``filter_kind`` prefers the planner-set ``kind`` over re-deriving it
#      from ``field`` — the dual-read precedence that lets the planner own the
#      decision (mirrors PR #1374's ``AnchorValueSpec.state`` dual-read).
#
# The field-derivation fallback (kind=None) stays the safety net and is
# covered exhaustively in test_filter_kind.py; here we only confirm that a
# *typed* plan is accepted, since that is the shape Stage 3's planner.md now
# instructs the model to produce.
# --------------------------------------------------------------------------


def test_typed_state_kind_filter_is_supported() -> None:
    """A state filter that names its kind explicitly is still a supported shape."""
    plan = QueryPlan(
        operation="rank",
        question="Which CA districts have the highest starting salary?",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="starting teacher salary")],
        filters=[
            FilterSpec(field="state", operator="equals", value="CA", kind="state"),
        ],
    )

    assert unsupported_shape_hint(plan) is None


def test_typed_region_kind_filter_is_supported() -> None:
    """A region filter that names its kind explicitly is still a supported shape."""
    from compass_backend.execution.selection import requested_states
    from compass_backend.reference import CENSUS_SOUTH_STATES

    plan = QueryPlan(
        operation="rank",
        question="Rank Southern districts by differentiated pay.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="hard-to-staff school pay")],
        filters=[
            FilterSpec(
                field="region", operator="equals", value="the South", kind="region"
            ),
        ],
        sort=SortSpec(field="hard-to-staff school pay", direction="desc"),
        limit=LimitSpec(count=10, kind="top"),
    )

    # The typed region filter still expands to its governed member states and
    # the plan still compiles.
    assert requested_states(plan) == set(CENSUS_SOUTH_STATES)
    assert unsupported_shape_hint(plan) is None


def test_typed_enrollment_kind_filter_is_supported() -> None:
    """An enrollment-bounds filter that names its kind explicitly is supported
    on lookup (enrollment bounds are not a rank-supported filter)."""
    plan = QueryPlan(
        operation="lookup",
        question="Salaries for districts enrolling more than 50000 students.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting teacher salary")],
        filters=[
            FilterSpec(
                field="enrollment",
                operator="greater_than",
                value=50000,
                kind="enrollment",
            ),
        ],
        limit=LimitSpec(kind="all"),
    )

    assert unsupported_shape_hint(plan) is None


def test_typed_metric_value_kind_filter_is_supported() -> None:
    """A metric-value threshold filter that names its kind explicitly — while
    keeping its free-form metric phrase in ``field`` — is a supported shape."""
    plan = QueryPlan(
        operation="rank",
        question="Which districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(
                field="teacher workdays",
                operator="greater_than",
                value=190,
                kind="metric_value",
            ),
        ],
    )

    assert unsupported_shape_hint(plan) is None


def test_typed_state_plus_metric_value_kinds_combined_is_supported() -> None:
    """The mixed shape the planner now emits — a typed state filter alongside a
    typed metric-value filter — still compiles."""
    plan = QueryPlan(
        operation="rank",
        question="Which CA districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="state", operator="equals", value="CA", kind="state"),
            FilterSpec(
                field="teacher workdays",
                operator="greater_than",
                value=190,
                kind="metric_value",
            ),
        ],
    )

    assert unsupported_shape_hint(plan) is None


def test_filter_kind_prefers_planner_set_kind_over_field_derivation() -> None:
    """``filter_kind`` trusts a planner-set ``kind`` instead of re-deriving it
    from ``field`` — the dual-read precedence Stage 3's planner relies on."""
    from compass_backend.execution.filters import filter_kind

    # A free-form metric phrase the deriver would classify ``metric_value``,
    # but typed explicitly as ``state`` — the typed decision wins.
    typed_state = FilterSpec(
        field="teacher workdays", operator="greater_than", value=1, kind="state"
    )
    assert filter_kind(typed_state) == "state"

    # The legacy ``"value"`` token derives to ``None``; typed as
    # ``metric_value`` it returns that kind.
    typed_metric_value = FilterSpec(
        field="value", operator="greater_than", value=1, kind="metric_value"
    )
    assert filter_kind(typed_metric_value) == "metric_value"

    # Each kind round-trips verbatim through the helper.
    for declared in ("state", "region", "enrollment", "metric_value"):
        spec = FilterSpec(field="state", operator="equals", value="CA", kind=declared)
        assert filter_kind(spec) == declared
