"""Tests for multi-turn selection referent materialisation (Phase 3 Track 3.1).

TDD sequence (from track-3-1-plan.md):
  1 — empty result_districts → no-op
  2 — sentinel district string with non-empty result_districts → materialised
  3 — inherit_selection_from="prior_result_rows" on non-inheriting plan → materialised
  4 — concrete scope="named_districts" with REAL district names → untouched
  5 — case-insensitive sentinel detection
  6 — multi-sentinel + real-name mix → conservative (untouched)
  7 — integration: fresh plan with sentinel → merge_turn_with_session_context materialises
  8 — integration: Scenario B (3-district compare → "Are these unionised?")
  9 — integration: no result_districts → fresh plan flows through unchanged
  10 — composition (PR 2A): FilterSpec metric-value thresholds survive across turns
  11 — composition (PR 2C): degree_lane on MetricSpec does NOT carry over (new metric turn-2)

All tests were written BEFORE implementation (TDD). They fail initially and pass
after the implementation lands.
"""

from __future__ import annotations

import pytest

from compass_backend.contracts import (
    FilterSpec,
    MetricSpec,
    OutputSpec,
    QueryContext,
    QueryPlan,
    PlannerTurn,
    SelectionSpec,
    SortSpec,
)


# ---------------------------------------------------------------------------
# Step 1 — empty result_districts → no-op
# ---------------------------------------------------------------------------


def test_selection_referent_no_op_when_result_districts_empty() -> None:
    """When context.result_districts is empty, normaliser leaves sentinel districts alone."""
    from compass_backend.planning.planner import _normalize_plan_selection_with_context

    plan = QueryPlan(
        question="What are those districts and their observation counts?",
        selection=SelectionSpec(scope="named_districts", districts=["those districts"]),
        metrics=[MetricSpec(name="formal observations per year")],
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=QueryPlan(
            question="Which districts observe teachers more than twice a year?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="formal observations per year")],
        ),
        result_type="metric_ranking",
        result_districts=[],  # empty — no prior result yet
    )

    result = _normalize_plan_selection_with_context(plan, context)

    # Plan must be returned unchanged (same object identity acceptable)
    assert result.selection == plan.selection
    assert result.selection.districts == ["those districts"]


# ---------------------------------------------------------------------------
# Step 2 — sentinel district string with non-empty result_districts → materialised
# ---------------------------------------------------------------------------


def test_selection_referent_materialises_from_result_districts() -> None:
    """Sentinel string in districts triggers materialisation from context."""
    from compass_backend.planning.planner import _normalize_plan_selection_with_context

    plan = QueryPlan(
        question="What are those districts and their observation counts?",
        selection=SelectionSpec(scope="named_districts", districts=["those districts"]),
        metrics=[MetricSpec(name="formal observations per year")],
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=QueryPlan(
            question="Which districts observe teachers more than twice a year?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="formal observations per year")],
        ),
        result_type="metric_ranking",
        result_districts=[
            {"district_id": 1, "district_name": "Alpha USD", "state": "CA"},
            {"district_id": 2, "district_name": "Bravo ISD", "state": "TX"},
            {"district_id": 3, "district_name": "Charlie PS", "state": "NY"},
        ],
    )

    result = _normalize_plan_selection_with_context(plan, context)

    assert result.selection == SelectionSpec(
        scope="named_districts",
        districts=["Alpha USD, CA", "Bravo ISD, TX", "Charlie PS, NY"],
    )


# ---------------------------------------------------------------------------
# Step 3 — inherit_selection_from="prior_result_rows" on non-inheriting plan
# ---------------------------------------------------------------------------


def test_selection_referent_inherit_selection_from_triggers_materialisation() -> None:
    """Non-delta plan with inherit_selection_from="prior_result_rows" triggers materialisation.

    Defense-in-depth: when the planner correctly sets inherit_selection_from=
    "prior_result_rows" but omits inherit_from_session=True, the normaliser still
    materialises the result districts.  The plan must have a concrete scope (contract
    validator blocks scope="unspecified" on non-inheriting plans), so the realistic
    shape here is all_covered_districts combined with the inherit_selection_from signal.
    """
    from compass_backend.planning.planner import _normalize_plan_selection_with_context

    plan = QueryPlan(
        question="Are those districts unionised?",
        inherit_from_session=False,
        inherit_selection_from="prior_result_rows",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="unionisation status")],
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=QueryPlan(
            question="Compare salaries for Chicago, Denver, Dallas.",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Chicago Public Schools", "Denver Public Schools", "Dallas ISD"],
            ),
            metrics=[MetricSpec(name="starting salary")],
        ),
        result_type="metric_lookup",
        result_districts=[
            {"district_id": 10, "district_name": "Chicago Public Schools", "state": "IL"},
            {"district_id": 11, "district_name": "Denver Public Schools", "state": "CO"},
            {"district_id": 12, "district_name": "Dallas ISD", "state": "TX"},
        ],
    )

    result = _normalize_plan_selection_with_context(plan, context)

    assert result.selection == SelectionSpec(
        scope="named_districts",
        districts=["Chicago Public Schools, IL", "Denver Public Schools, CO", "Dallas ISD, TX"],
    )


# ---------------------------------------------------------------------------
# Step 4 — concrete scope="named_districts" with REAL district names → untouched
# ---------------------------------------------------------------------------


def test_selection_referent_leaves_concrete_named_districts_untouched() -> None:
    """Concrete real district names are left completely unchanged."""
    from compass_backend.planning.planner import _normalize_plan_selection_with_context

    plan = QueryPlan(
        question="What are the salaries in Seattle and Portland?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Seattle Public Schools", "Portland Public Schools"],
        ),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=QueryPlan(
            question="Show teachers in Chicago.",
            selection=SelectionSpec(scope="named_districts", districts=["Chicago Public Schools"]),
            metrics=[MetricSpec(name="teacher count")],
        ),
        result_type="metric_lookup",
        result_districts=[
            {"district_id": 5, "district_name": "Chicago Public Schools", "state": "IL"},
        ],
    )

    result = _normalize_plan_selection_with_context(plan, context)

    # Must be completely untouched — real district names pass through
    assert result.selection.districts == ["Seattle Public Schools", "Portland Public Schools"]
    assert result.selection.scope == "named_districts"


# ---------------------------------------------------------------------------
# Step 5 — case-insensitive sentinel detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentinel", [
    "Those Districts",
    "THOSE DISTRICTS",
    "Those",
    "THOSE",
    "These Districts",
    "THESE",
    "That",
    "THAT LIST",
    "That List",
    "The Previous",
    "THE PREVIOUS",
    "Previous Districts",
    "Prior Districts",
    "PRIOR RESULT",
    "prior result",
    "<inherit>",
    "INHERIT",
    "Inherit",
])
def test_selection_referent_case_insensitive_sentinel_detection(sentinel: str) -> None:
    """Sentinel detection is case-insensitive."""
    from compass_backend.planning.planner import _normalize_plan_selection_with_context

    plan = QueryPlan(
        question="Tell me about those districts.",
        selection=SelectionSpec(scope="named_districts", districts=[sentinel]),
        metrics=[MetricSpec(name="some metric")],
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=QueryPlan(
            question="Show me all districts.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="some metric")],
        ),
        result_type="metric_ranking",
        result_districts=[
            {"district_id": 99, "district_name": "Test District", "state": "TX"},
        ],
    )

    result = _normalize_plan_selection_with_context(plan, context)

    assert result.selection == SelectionSpec(
        scope="named_districts",
        districts=["Test District, TX"],
    )


# ---------------------------------------------------------------------------
# Step 6 — multi-sentinel + real-name mix → conservative (untouched)
# ---------------------------------------------------------------------------


def test_selection_referent_mixed_sentinel_and_real_name_is_left_untouched() -> None:
    """When districts contain both sentinels and real names, be conservative and leave untouched."""
    from compass_backend.planning.planner import _normalize_plan_selection_with_context

    plan = QueryPlan(
        question="Tell me about Chicago and those districts.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Chicago Public Schools", "those districts"],
        ),
        metrics=[MetricSpec(name="formal observations per year")],
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=QueryPlan(
            question="Which districts observe teachers more than twice a year?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="formal observations per year")],
        ),
        result_type="metric_ranking",
        result_districts=[
            {"district_id": 1, "district_name": "Alpha USD", "state": "CA"},
        ],
    )

    result = _normalize_plan_selection_with_context(plan, context)

    # Mixed list → conservative, leave untouched
    assert result.selection.districts == ["Chicago Public Schools", "those districts"]


# ---------------------------------------------------------------------------
# Step 7 — Integration: fresh plan sentinel goes through merge_turn_with_session_context
# ---------------------------------------------------------------------------


def test_merge_turn_with_session_context_materialises_sentinel_for_fresh_plan() -> None:
    """merge_turn_with_session_context materialises sentinel districts for a fresh (non-delta) plan."""
    from compass_backend.planning.planner import merge_turn_with_session_context

    previous_plan = QueryPlan(
        question="Which districts observe teachers more than twice a year?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations per year")],
        sort=SortSpec(field="formal observations per year", direction="desc"),
        output=OutputSpec(format="table"),
    )
    followup_plan = QueryPlan(
        question="What are those districts and their observation counts?",
        inherit_from_session=False,
        selection=SelectionSpec(
            scope="named_districts",
            districts=["those districts"],
        ),
        metrics=[MetricSpec(name="formal observations per year")],
        output=OutputSpec(format="table"),
    )
    turn = PlannerTurn(
        route="execute",
        confidence=1.0,
        query_plan=followup_plan,
    )
    context = QueryContext(
        query_plan=previous_plan,
        result_type="metric_ranking",
        result_districts=[
            {"district_id": 1, "district_name": "Alpha USD", "state": "CA"},
            {"district_id": 2, "district_name": "Bravo ISD", "state": "TX"},
            {"district_id": 3, "district_name": "Charlie PS", "state": "NY"},
        ],
    )

    merged_turn = merge_turn_with_session_context(turn, context)

    assert merged_turn.query_plan is not None
    assert merged_turn.query_plan.selection == SelectionSpec(
        scope="named_districts",
        districts=["Alpha USD, CA", "Bravo ISD, TX", "Charlie PS, NY"],
    )


# ---------------------------------------------------------------------------
# Step 8 — Integration: Scenario B (3-district compare → "Are these unionised?")
# ---------------------------------------------------------------------------


def test_merge_turn_materialises_these_for_prior_named_district_result() -> None:
    """Scenario B: prior turn compared 3 named districts; follow-up references 'these districts'."""
    from compass_backend.planning.planner import merge_turn_with_session_context

    previous_plan = QueryPlan(
        question="Compare salaries for Chicago, Denver, Dallas.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Chicago Public Schools", "Denver Public Schools", "Dallas ISD"],
        ),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    followup_plan = QueryPlan(
        question="Are these districts unionised?",
        inherit_from_session=False,
        selection=SelectionSpec(
            scope="named_districts",
            districts=["these districts"],
        ),
        metrics=[MetricSpec(name="unionisation status")],
        output=OutputSpec(format="table"),
    )
    turn = PlannerTurn(
        route="execute",
        confidence=1.0,
        query_plan=followup_plan,
    )
    context = QueryContext(
        query_plan=previous_plan,
        result_type="metric_lookup",
        result_districts=[
            {"district_id": 10, "district_name": "Chicago Public Schools", "state": "IL"},
            {"district_id": 11, "district_name": "Denver Public Schools", "state": "CO"},
            {"district_id": 12, "district_name": "Dallas ISD", "state": "TX"},
        ],
    )

    merged_turn = merge_turn_with_session_context(turn, context)

    assert merged_turn.query_plan is not None
    assert merged_turn.query_plan.selection == SelectionSpec(
        scope="named_districts",
        districts=[
            "Chicago Public Schools, IL",
            "Denver Public Schools, CO",
            "Dallas ISD, TX",
        ],
    )


# ---------------------------------------------------------------------------
# Step 9 — Integration: no result_districts → fresh plan flows through unchanged
# ---------------------------------------------------------------------------


def test_merge_turn_fresh_plan_unchanged_when_no_result_districts() -> None:
    """When context has no result_districts, sentinel plan flows through unchanged (no-op)."""
    from compass_backend.planning.planner import merge_turn_with_session_context

    previous_plan = QueryPlan(
        question="Show me all districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    followup_plan = QueryPlan(
        question="What are those districts and their salaries?",
        inherit_from_session=False,
        selection=SelectionSpec(
            scope="named_districts",
            districts=["those districts"],
        ),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    turn = PlannerTurn(
        route="execute",
        confidence=1.0,
        query_plan=followup_plan,
    )
    context = QueryContext(
        query_plan=previous_plan,
        result_type="metric_ranking",
        result_districts=[],  # empty — prior turn had no result rows
    )

    merged_turn = merge_turn_with_session_context(turn, context)

    # Should be unchanged (sentinel can't materialise without result rows)
    assert merged_turn.query_plan is not None
    assert merged_turn.query_plan.selection.districts == ["those districts"]


# ---------------------------------------------------------------------------
# Step 10 — Composition (PR 2A): FilterSpec metric-value thresholds survive
# ---------------------------------------------------------------------------


def test_selection_referent_preserves_filter_spec_on_materialized_plan() -> None:
    """Materialised selection does not clobber FilterSpec (PR 2A composition)."""
    from compass_backend.planning.planner import _normalize_plan_selection_with_context

    plan = QueryPlan(
        question="What are the salaries for those districts?",
        selection=SelectionSpec(scope="named_districts", districts=["those districts"]),
        metrics=[MetricSpec(name="starting salary")],
        filters=[FilterSpec(field="starting salary", operator="greater_than_or_equal", value=50000.0)],
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=QueryPlan(
            question="Which districts pay starting salary above 50k?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
        ),
        result_type="metric_ranking",
        result_districts=[
            {"district_id": 1, "district_name": "Alpha USD", "state": "CA"},
            {"district_id": 2, "district_name": "Bravo ISD", "state": "TX"},
        ],
    )

    result = _normalize_plan_selection_with_context(plan, context)

    # Selection materialised
    assert result.selection == SelectionSpec(
        scope="named_districts",
        districts=["Alpha USD, CA", "Bravo ISD, TX"],
    )
    # FilterSpec untouched
    assert result.filters == [FilterSpec(field="starting salary", operator="greater_than_or_equal", value=50000.0)]


# ---------------------------------------------------------------------------
# Step 11 — Composition (PR 2C): degree_lane does NOT carry over (new metric, new turn)
# ---------------------------------------------------------------------------


def test_selection_referent_does_not_carry_over_degree_lane_from_prior_turn() -> None:
    """degree_lane on turn-2 MetricSpec is owned by turn-2, not inherited from turn-1 (PR 2C composition)."""
    from compass_backend.planning.planner import _normalize_plan_selection_with_context

    # Turn 1: BA lane salary lookup for named districts
    previous_plan = QueryPlan(
        question="Show BA salaries for Chicago and Denver.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Chicago Public Schools", "Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="starting salary", degree_lane="ba")],
        output=OutputSpec(format="table"),
    )
    # Turn 2: new metric (no degree_lane) referencing "those districts"
    followup_plan = QueryPlan(
        question="Are those districts unionised?",
        selection=SelectionSpec(scope="named_districts", districts=["those districts"]),
        metrics=[MetricSpec(name="unionisation status")],  # no degree_lane
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=previous_plan,
        result_type="metric_lookup",
        result_districts=[
            {"district_id": 10, "district_name": "Chicago Public Schools", "state": "IL"},
            {"district_id": 11, "district_name": "Denver Public Schools", "state": "CO"},
        ],
    )

    result = _normalize_plan_selection_with_context(followup_plan, context)

    # Selection materialised from context
    assert result.selection == SelectionSpec(
        scope="named_districts",
        districts=["Chicago Public Schools, IL", "Denver Public Schools, CO"],
    )
    # degree_lane on turn-2 metric stays as declared (None), not inherited from prior
    assert result.metrics[0].degree_lane is None


# ---------------------------------------------------------------------------
# Delta path: _merge_selection_with_context also handles sentinel (Risk #3)
# ---------------------------------------------------------------------------


def test_merge_selection_with_context_handles_sentinel_in_delta_plan() -> None:
    """_merge_selection_with_context extends: sentinel districts in a delta plan are also materialised."""
    from compass_backend.planning.planner import merge_query_plan_with_context

    previous_plan = QueryPlan(
        question="Which districts observe teachers more than twice a year?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="formal observations per year")],
        sort=SortSpec(field="formal observations per year", direction="desc"),
        output=OutputSpec(format="table"),
    )
    # Delta plan with inherit_from_session=True AND sentinel districts (Risk #3 scenario)
    followup = QueryPlan(
        inherit_from_session=True,
        question="Export those districts to CSV.",
        selection=SelectionSpec(scope="named_districts", districts=["those districts"]),
        metrics=[],
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=previous_plan,
        result_type="metric_ranking",
        result_districts=[
            {"district_id": 1, "district_name": "Alpha USD", "state": "CA"},
            {"district_id": 2, "district_name": "Bravo ISD", "state": "TX"},
        ],
    )

    merged = merge_query_plan_with_context(followup, context)

    assert merged.selection == SelectionSpec(
        scope="named_districts",
        districts=["Alpha USD, CA", "Bravo ISD, TX"],
    )


def test_merge_selection_with_context_filters_prior_rows_by_region() -> None:
    """Region follow-ups inherit prior rows and then apply the governed region."""
    from compass_backend.planning.planner import merge_query_plan_with_context
    from compass_backend.reference import CENSUS_SOUTH_STATES

    previous_plan = QueryPlan(
        question="Which districts offer differentiated pay?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="hard-to-staff school pay")],
        sort=SortSpec(field="hard-to-staff school pay", direction="desc"),
        output=OutputSpec(format="table"),
    )
    followup = QueryPlan(
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
        question="Narrow that to just districts in the South.",
        selection=SelectionSpec(scope="state", states=["South"]),
        metrics=[],
        output=OutputSpec(format="table"),
    )
    context = QueryContext(
        query_plan=previous_plan,
        result_type="metric_ranking",
        result_districts=[
            {"district_id": 1, "district_name": "Los Angeles Unified", "state": "CA"},
            {"district_id": 2, "district_name": "Dallas ISD", "state": "TX"},
            {"district_id": 3, "district_name": "Wake County Schools", "state": "NC"},
        ],
    )

    merged = merge_query_plan_with_context(followup, context)

    assert merged.selection == SelectionSpec(
        scope="named_districts",
        districts=["Dallas ISD, TX", "Wake County Schools, NC"],
        states=list(CENSUS_SOUTH_STATES),
    )


# ---------------------------------------------------------------------------
# sentinel helper unit tests
# ---------------------------------------------------------------------------


def test_selection_districts_are_referent_true_for_known_sentinels() -> None:
    """_selection_districts_are_referent returns True for known sentinel values."""
    from compass_backend.planning.planner import _selection_districts_are_referent

    assert _selection_districts_are_referent(["those districts"]) is True
    assert _selection_districts_are_referent(["these districts"]) is True
    assert _selection_districts_are_referent(["<inherit>"]) is True
    assert _selection_districts_are_referent(["inherit"]) is True
    assert _selection_districts_are_referent(["that list"]) is True
    assert _selection_districts_are_referent(["prior result"]) is True
    assert _selection_districts_are_referent(["those", "these"]) is True  # multi-sentinel OK


def test_selection_districts_are_referent_false_for_real_names() -> None:
    """_selection_districts_are_referent returns False for real district names."""
    from compass_backend.planning.planner import _selection_districts_are_referent

    assert _selection_districts_are_referent(["Chicago Public Schools"]) is False
    assert _selection_districts_are_referent(["Denver Public Schools"]) is False


def test_selection_districts_are_referent_false_for_mixed_list() -> None:
    """_selection_districts_are_referent returns False for mixed sentinel + real name."""
    from compass_backend.planning.planner import _selection_districts_are_referent

    assert _selection_districts_are_referent(["Chicago", "those districts"]) is False


def test_selection_districts_are_referent_false_for_empty_list() -> None:
    """_selection_districts_are_referent returns False for empty list (no referent signal)."""
    from compass_backend.planning.planner import _selection_districts_are_referent

    assert _selection_districts_are_referent([]) is False
