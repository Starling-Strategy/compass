"""#1419 (Milestone 14): single source for from-plan PendingQueryContexts.

``pending_context_from_plan`` is the ONE builder every genuine "from-plan"
clarification site routes through (orchestration.chat shape-guard + validation
pivot, planner.pending_context_from_execution_clarification, cross_box's
ambiguous-catalog clarification). Routing them all through one builder fixes the
W1-02 (#836) latent drops by construction: chat.py:1043 and cross_box's local
copy previously dropped some of ``count_kind`` / ``requires_all_metrics`` /
``requires_composite_ranking`` / ``inherit_selection_from`` / ``sort_steps``.

These tests pin:
  1. full round-trip fidelity (every W1-02-relevant QueryPlan field is copied),
  2. the keyword overrides (metrics / missing_fields /
     last_clarification_question / source_turn_index),
  3. a focused regression guard that the previously-dropped fields are present.
"""

from __future__ import annotations

from compass_backend.contracts.planning import (
    ContextSelectionMode,
    FilterSpec,
    LimitSpec,
    MetricSpec,
    OutputSpec,
    ProfileFieldSpec,
    QueryPlan,
    SelectionSpec,
    SortStepSpec,
    TemporalSpec,
    pending_context_from_plan,
)


def _fully_populated_plan() -> QueryPlan:
    """A rank plan exercising every W1-02-relevant typed field.

    ``requires_composite_ranking`` needs operation='rank' with 2..8 metrics;
    ``inherit_selection_from`` and a non-empty ``sort_steps`` exercise the two
    fields cross_box / chat.py:1043 used to drop. (A ``count`` +
    ``covered_universe_count`` shape is mutually exclusive with metrics, so a
    rank plan is the representative carrier for the whole field set.)
    """

    inherit: ContextSelectionMode = "prior_result_rows"
    return QueryPlan(
        inherit_from_session=True,
        operation="rank",
        question="Rank these districts by salary then premium.",
        selection=SelectionSpec(
            scope="named_districts", districts=["Alpha", "Bravo"]
        ),
        metrics=[
            MetricSpec(name="starting salary"),
            MetricSpec(name="health premium"),
        ],
        profile_fields=[ProfileFieldSpec(name="enrollment")],
        filters=[
            FilterSpec(field="enrollment", operator="greater_than", value=10000)
        ],
        sort=None,
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
        limit=LimitSpec(kind="top", count=5),
        inherit_selection_from=inherit,
        output=OutputSpec(format="table"),
        temporal=TemporalSpec(),
        requires_all_metrics=False,
        requires_composite_ranking=True,
        count_kind="threshold_count",
    )


def test_builder_reproduces_every_query_plan_field() -> None:
    """Round-trip fidelity: the builder copies the full W1-02 field set."""

    plan = _fully_populated_plan()
    pending = pending_context_from_plan(plan)

    assert pending.operation == plan.operation
    assert pending.question == plan.question
    assert pending.selection == plan.selection
    assert pending.metrics == plan.metrics
    assert pending.profile_fields == plan.profile_fields
    assert pending.filters == plan.filters
    assert pending.sort == plan.sort
    assert pending.sort_steps == plan.sort_steps
    assert pending.limit == plan.limit
    assert pending.inherit_selection_from == plan.inherit_selection_from
    assert pending.output == plan.output
    assert pending.temporal == plan.temporal
    assert pending.requires_all_metrics == plan.requires_all_metrics
    assert pending.requires_composite_ranking == plan.requires_composite_ranking
    assert pending.count_kind == plan.count_kind
    assert pending.similarity == plan.similarity
    assert pending.peer_overrides == plan.peer_overrides
    # Optional clarification fields default cleanly when not supplied.
    assert pending.last_clarification_question is None
    assert pending.source_turn_index is None
    assert pending.missing_fields == []


def test_builder_copies_list_fields_without_aliasing() -> None:
    """List-typed plan fields are copied, not shared (mutating one is isolated)."""

    plan = _fully_populated_plan()
    pending = pending_context_from_plan(plan)

    assert pending.metrics is not plan.metrics
    assert pending.profile_fields is not plan.profile_fields
    assert pending.filters is not plan.filters
    assert pending.sort_steps is not plan.sort_steps


def test_builder_honors_keyword_overrides() -> None:
    """metrics / missing_fields / clarification overrides take precedence."""

    plan = _fully_populated_plan()
    override_metrics = [MetricSpec(name="class size")]

    pending = pending_context_from_plan(
        plan,
        metrics=override_metrics,
        last_clarification_question="Which lane?",
        source_turn_index=3,
        missing_fields=["metric"],
    )

    assert pending.metrics == override_metrics
    assert pending.last_clarification_question == "Which lane?"
    assert pending.source_turn_index == 3
    assert pending.missing_fields == ["metric"]
    # Everything not overridden still mirrors the plan.
    assert pending.sort_steps == plan.sort_steps
    assert pending.requires_composite_ranking is True


def test_builder_metrics_none_falls_back_to_plan_metrics() -> None:
    """metrics=None (default) means "copy the plan's metrics", not "empty"."""

    plan = _fully_populated_plan()
    pending = pending_context_from_plan(plan, metrics=None)
    assert pending.metrics == plan.metrics


def test_builder_carries_previously_dropped_w102_fields() -> None:
    """Regression guard for the chat.py:1043 + cross_box latent drops.

    Pre-#1419 those two sites copied only a subset and dropped some of these
    fields. Routing them through the builder restores all four by construction.
    """

    plan = _fully_populated_plan()
    pending = pending_context_from_plan(plan, missing_fields=["metric"])

    # The four fields chat.py:1043 dropped and the five cross_box dropped.
    assert pending.count_kind == plan.count_kind
    assert pending.requires_all_metrics == plan.requires_all_metrics
    assert pending.requires_composite_ranking == plan.requires_composite_ranking
    assert pending.inherit_selection_from == plan.inherit_selection_from
    assert pending.sort_steps == plan.sort_steps
    assert len(pending.sort_steps) == 2


def test_execution_clarification_preserves_sibling_metrics_m1() -> None:
    """m1 (#1348): a metric clarify in a multi-metric plan keeps resolved siblings.

    Only the clarified phrase's slot is replaced by its candidate expansions; the
    already-resolved sibling ("starting salary") survives into the pending context.
    """
    from compass_backend.contracts.planning import ClarificationRequest
    from compass_backend.planning import pending_context_from_execution_clarification

    plan = QueryPlan(
        operation="lookup",
        question="Compare salary and planning time across districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary"), MetricSpec(name="planning time")],
    )
    clarification = ClarificationRequest(
        question="Which planning-time metric?",
        missing_fields=["metric"],
        candidates=["elem planning", "mid planning", "high planning"],
    )
    ctx = pending_context_from_execution_clarification(
        plan,
        clarification,
        turn_index=1,
        ambiguous_metric_phrase="planning time",
    )
    assert [m.name for m in ctx.metrics] == [
        "starting salary",
        "elem planning",
        "mid planning",
        "high planning",
    ]


def test_execution_clarification_single_metric_unchanged_m1() -> None:
    """m1 back-compat: a single-metric clarify keeps only the candidate expansions.

    The clarified metric IS the only one, so siblings is empty and the result is
    exactly the candidate set — identical to the pre-m1 ``candidate_metrics``
    behavior.
    """
    from compass_backend.contracts.planning import ClarificationRequest
    from compass_backend.planning import pending_context_from_execution_clarification

    plan = QueryPlan(
        operation="lookup",
        question="Districts and their planning time.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="planning time")],
    )
    clarification = ClarificationRequest(
        question="Which planning-time metric?",
        missing_fields=["metric"],
        candidates=["elem planning", "mid planning"],
    )
    ctx = pending_context_from_execution_clarification(
        plan,
        clarification,
        turn_index=1,
        ambiguous_metric_phrase="planning time",
    )
    assert [m.name for m in ctx.metrics] == ["elem planning", "mid planning"]
