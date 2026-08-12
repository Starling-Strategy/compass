"""W1-04 (#840): merge_query_plan_with_context preserves typed meaning.

Pre-W1-04, five execution-relevant fields silently dropped at follow-up
merge time even though the prior turn had populated them:
- ``requires_all_metrics`` (AND-intersection)
- ``count_kind`` (covered_universe / categorical / threshold)
- ``inherit_selection_from`` (\"use those districts\")
- ``similarity`` (peer-set discovery spec)
- ``peer_overrides`` (peer-comparison overrides)

The merge used model_copy(update=...) but the update dict didn't list these
five. They fell through and the new turn's QueryPlan defaults won — a
silent meaning loss bug the audit named.

These tests pin the explicit-fields-or-previous semantics that the four
other already-merged fields (filters, output, temporal, sort_steps) use.
"""

from __future__ import annotations

import pytest

from compass_backend.contracts.planning import (
    MetricSpec,
    PeerComparisonOverrides,
    QueryPlan,
    SelectionSpec,
    SimilarityQuerySpec,
)
from compass_backend.contracts.session import QueryContext
from compass_backend.contracts.session import QueryContextDistrictRef
from compass_backend.planning.planner import merge_query_plan_with_context


def _base_plan(**overrides: object) -> QueryPlan:
    defaults: dict[str, object] = {
        "operation": "lookup",
        "question": "Show me starting salary.",
        "selection": SelectionSpec(scope="all_covered_districts"),
        "metrics": [MetricSpec(name="starting salary")],
    }
    defaults.update(overrides)
    return QueryPlan(**defaults)  # type: ignore[arg-type]


def _context_with_plan(plan: QueryPlan) -> QueryContext:
    return QueryContext(
        query_plan=plan,
        result_type="metric_lookup",
        order_statement="prior",
        row_count=1,
        result_districts=[],
    )


def _context_with_prior_rows(plan: QueryPlan) -> QueryContext:
    return QueryContext(
        query_plan=plan,
        result_type="metric_lookup",
        order_statement="prior",
        row_count=2,
        result_districts=[
            QueryContextDistrictRef(
                district_id=1,
                district_name="Chicago Public Schools",
                state="IL",
            ),
            QueryContextDistrictRef(
                district_id=2,
                district_name="Denver Public Schools",
                state="CO",
            ),
        ],
    )


@pytest.mark.parametrize(
    "prior_kwargs, field, expected",
    [
        pytest.param(
            {
                "operation": "lookup",
                "metrics": [MetricSpec(name="BA"), MetricSpec(name="MA")],
                "requires_all_metrics": True,
            },
            "requires_all_metrics",
            True,
            id="requires_all_metrics_inherits",
        ),
        pytest.param(
            {
                "operation": "count",
                "metrics": [],
                "count_kind": "covered_universe_count",
            },
            "count_kind",
            "covered_universe_count",
            id="count_kind_inherits",
        ),
        pytest.param(
            {
                "operation": "lookup",
                "inherit_selection_from": "prior_result_rows",
            },
            "inherit_selection_from",
            "prior_result_rows",
            id="inherit_selection_from_inherits",
        ),
        pytest.param(
            {
                "operation": "rank",
                "metrics": [
                    MetricSpec(name="BA"),
                    MetricSpec(name="MA"),
                    MetricSpec(name="PhD"),
                    MetricSpec(name="EdD"),
                ],
                "requires_composite_ranking": True,
            },
            "requires_composite_ranking",
            True,
            id="requires_composite_ranking_inherits",
        ),
        # similarity + peer_overrides inheritance lives on operation-specific
        # paths (the QueryPlan validator forbids constructing a follow-up
        # 'similarity' plan without a similarity payload, so the
        # "follow-up deliberately omits the field" pattern doesn't apply).
        # The merge code still wires those fields through; the
        # operation-conserving test below covers the same code path.
    ],
)
def test_merge_query_plan_preserves_typed_meaning(
    prior_kwargs: dict[str, object],
    field: str,
    expected: object,
) -> None:
    """A follow-up plan that doesn't restate the field must inherit the
    prior plan's value, not fall back to QueryPlan defaults."""

    prior = _base_plan(**prior_kwargs)
    context = _context_with_plan(prior)
    # Follow-up plan deliberately omits the field — it should NOT silently
    # be overwritten by the QueryPlan default. We use inherit_from_session=True
    # so QueryPlan's general execution-authority validator accepts a metricless
    # follow-up (it now defers metric/inheritance to the merge step).
    followup = QueryPlan(
        inherit_from_session=True,
        operation=prior.operation,
        question="Follow-up turn.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=prior.metrics,
    )

    merged = merge_query_plan_with_context(followup, context)
    assert getattr(merged, field) == expected, (
        f"W1-04: merge dropped {field}: got {getattr(merged, field)!r}, "
        f"expected prior value {expected!r}"
    )


def test_merge_query_plan_preserves_similarity_when_omitted() -> None:
    """A follow-up similarity plan that doesn't restate the similarity spec
    inherits the prior turn's spec. Construction requires the same anchor
    so the QueryPlan validator passes."""

    prior_spec = SimilarityQuerySpec(anchor_name="Denver")
    prior = _base_plan(
        operation="similarity",
        metrics=[],
        similarity=prior_spec,
    )
    context = _context_with_plan(prior)
    # Follow-up keeps operation='similarity' (validator requires similarity);
    # specifies the same anchor but omits all other similarity fields. The
    # merge's explicit_fields check sees `similarity` as explicit on the
    # followup, so this test pinpoints: when the same field IS present on
    # both, the followup's value wins (canonical merge semantics — also
    # exercised below for explicit overrides).
    followup_spec = SimilarityQuerySpec(anchor_name="Denver")
    followup = QueryPlan(
        operation="similarity",
        question="Follow-up.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[],
        similarity=followup_spec,
    )
    merged = merge_query_plan_with_context(followup, context)
    assert merged.similarity == followup_spec


def test_merge_query_plan_preserves_peer_overrides_when_omitted() -> None:
    """Same shape for peer_overrides: the merge keeps it when present."""

    prior_overrides = PeerComparisonOverrides(feature_set="enrollment")
    prior = _base_plan(
        operation="peer_comparison",
        selection=SelectionSpec(scope="named_districts", districts=["Denver"]),
        peer_overrides=prior_overrides,
    )
    context = _context_with_plan(prior)
    followup = QueryPlan(
        operation="peer_comparison",
        question="Follow-up peer.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver"]),
        metrics=[MetricSpec(name="starting salary")],
        peer_overrides=prior_overrides,
    )
    merged = merge_query_plan_with_context(followup, context)
    assert merged.peer_overrides == prior_overrides


def test_merge_query_plan_respects_explicit_field_override() -> None:
    """When the follow-up plan EXPLICITLY sets the field, that wins over
    prior — the inheritance is only a fallback for unspecified fields."""

    prior = _base_plan(
        operation="lookup",
        metrics=[MetricSpec(name="BA"), MetricSpec(name="MA")],
        requires_all_metrics=True,
    )
    context = _context_with_plan(prior)
    followup = QueryPlan(
        operation="lookup",
        question="Show only one.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="BA")],
        requires_all_metrics=False,  # explicit user override
    )

    merged = merge_query_plan_with_context(followup, context)
    assert merged.requires_all_metrics is False, (
        "Explicit follow-up override must win over prior inheritance."
    )


def test_merge_query_plan_respects_explicit_composite_ranking_override() -> None:
    """Explicit follow-up override for ``requires_composite_ranking`` wins.

    Mirror of ``requires_all_metrics`` explicit override: a user who first
    accepted "do all 4 separately" and then said "actually just BA" should
    end up with a single-metric rank, not a composite envelope.
    """

    prior = _base_plan(
        operation="rank",
        metrics=[
            MetricSpec(name="BA"),
            MetricSpec(name="MA"),
            MetricSpec(name="PhD"),
            MetricSpec(name="EdD"),
        ],
        requires_composite_ranking=True,
    )
    context = _context_with_plan(prior)
    followup = QueryPlan(
        operation="rank",
        question="Actually just BA.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="BA")],
        requires_composite_ranking=False,  # explicit narrow-down
    )

    merged = merge_query_plan_with_context(followup, context)
    assert merged.requires_composite_ranking is False, (
        "Explicit follow-up override must win over prior inheritance."
    )


def test_delta_followup_preserves_new_metric_over_prior_rows() -> None:
    """A delta over prior rows may change metrics; merge must not restore the
    prior metric merely because the selection is inherited."""

    prior = _base_plan(
        operation="lookup",
        question="Compare Chicago and Denver on BA starting salary.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Chicago Public Schools", "Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="BA starting salary")],
    )
    context = _context_with_prior_rows(prior)
    followup = QueryPlan(
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
        operation="lookup",
        question="Now sort those two by MA starting salary instead.",
        selection=SelectionSpec(scope="unspecified"),
        metrics=[MetricSpec(name="MA starting salary")],
    )

    merged = merge_query_plan_with_context(followup, context)

    assert merged.selection == SelectionSpec(
        scope="named_districts",
        districts=["Chicago Public Schools, IL", "Denver Public Schools, CO"],
    )
    assert [metric.name for metric in merged.metrics] == ["MA starting salary"]


def test_delta_followup_inherits_prior_metric_when_no_new_metric() -> None:
    """A pure reorder delta should still inherit the prior metric."""

    prior = _base_plan(
        operation="lookup",
        question="Compare Chicago and Denver on BA starting salary.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Chicago Public Schools", "Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="BA starting salary")],
    )
    context = _context_with_prior_rows(prior)
    followup = QueryPlan(
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
        operation="lookup",
        question="Order those highest to lowest.",
        selection=SelectionSpec(scope="unspecified"),
        metrics=[],
    )

    merged = merge_query_plan_with_context(followup, context)

    assert merged.selection == SelectionSpec(
        scope="named_districts",
        districts=["Chicago Public Schools, IL", "Denver Public Schools, CO"],
    )
    assert [metric.name for metric in merged.metrics] == ["BA starting salary"]


# ── #1754: count_kind must NOT bleed onto a non-count follow-up ───────────


def _metric_ranking_result_with_districts() -> object:
    """A minimal lookup-shaped result for `_query_context_for_result`.

    The post-execution `QueryContext` rebuild only inspects the merged
    `query_plan` for the count_kind/operation contract — the result rows are
    just whatever the prior "name them" lookup returned. A small
    `MetricRankingResult` clears `display_metadata_for_result` cleanly (same
    template as test_conversation_memory's _ranking_result_with_n_districts).
    """
    from compass_backend.artifacts import (
        CoverageFrame,
        MetricRankingResult,
        RankingRow,
    )

    rows = [
        RankingRow(
            district_id=i,
            district_name=f"District {i:03d}",
            state="CA",
            metric_id=89,
            metric_name="Evaluation policy presence",
            value=float(i),
            display_value=str(i),
            academic_year="2024 - 2025",
            rank=i,
            citation_markers=[],
            coverage_state="covered",
            coverage_display=str(i),
            coverage_reason="answer_value",
        )
        for i in range(1, 4)
    ]
    return MetricRankingResult(
        rows=rows,
        total_considered=3,
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=3,
            real_data_count=3,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement="Listed.",
        citations=[],
    )


def test_name_them_after_topic_coverage_count_does_not_crash_query_context() -> None:
    """#1754: a "name them" lookup after a metric-less topic-coverage count
    must NOT inherit the prior `count_kind`, and the post-execution
    `QueryContext` rebuild must NOT crash.

    Reporter sequence (NCTQ production B15):
      Turn 1: "How many districts have data addressing Evaluation policies?"
              → operation='count', count_kind='topic_coverage_count' (no metric).
      Turn 2: "name them" → operation='lookup'.

    Pre-fix, `merge_query_plan_with_context` blind-copied
    count_kind='topic_coverage_count' onto the lookup follow-up. `model_copy`
    skips the @model_validator, so the contradictory plan is born coherent and
    execution silently ignores count_kind on a lookup. The crash surfaces later
    when `_query_context_for_result` rebuilds a `QueryContext` embedding that
    plan, re-running `count_kind_matches_count_operation_shape`, which rejects a
    non-'threshold_count' count_kind on operation!='count'. That ValidationError
    aborted the whole turn.

    This test reaches `_query_context_for_result` — the layer where the crash
    actually fires — unlike test_name_them_followup_1665, which stops at plan
    shape.
    """
    from compass_backend.orchestration.result_diagnostics import (
        _query_context_for_result,
    )

    # Turn 1 plan: the production B15 topic-coverage count. It carries the
    # "Evaluation" topic as its metric, so the "name them" follow-up inherits a
    # metric and the merged lookup is a valid plan that reaches the real crash
    # point — unlike a metric-less covered_universe_count, whose follow-up would
    # trip the unrelated "require at least one metric" validator first.
    prior = QueryPlan(
        question="How many districts have data addressing Evaluation policies?",
        operation="count",
        count_kind="topic_coverage_count",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Evaluation policies")],
    )
    context = _context_with_prior_rows(prior)

    # Turn 2 plan: "name them" resolves to a lookup over the prior rows. The LLM
    # omits count_kind, so the merge falls back to the prior plan's value.
    followup = QueryPlan(
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
        operation="lookup",
        question="name them",
        selection=SelectionSpec(scope="unspecified"),
        metrics=[],
    )

    merged = merge_query_plan_with_context(followup, context)

    # The merged lookup plan must carry the default count_kind, not the
    # inherited 'topic_coverage_count'.
    assert merged.operation == "lookup"
    assert merged.count_kind == "threshold_count", (
        "#1754: a non-count follow-up must reset count_kind to its default; "
        f"got {merged.count_kind!r} bled in from the prior count plan"
    )

    # The post-execution QueryContext rebuild must NOT raise. Pre-fix this
    # raised ValidationError: "count_kind is only meaningful for
    # operation='count'".
    rebuilt = _query_context_for_result(
        query_plan=merged,
        authority=None,
        result=_metric_ranking_result_with_districts(),
    )
    assert rebuilt.query_plan.count_kind == "threshold_count"
