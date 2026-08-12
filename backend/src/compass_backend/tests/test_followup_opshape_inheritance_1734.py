"""#1734: a rank -> profile_lookup follow-up must DROP the ranking shape but
RETAIN the prior result districts as the named_districts selection.

Reproduction (two-turn conversation):

  Turn 1: "Show me the ten highest teacher starting salaries"
          -> operation=rank, sort=starting salary desc, limit=top 10.
  Turn 2: "What are the enrollment sizes of those districts?"
          -> operation=profile_lookup, profile_fields=[enrollment],
             selection=named_districts referent ("those districts").

The bug: the always-run merge layer
(``merge_query_plan_with_context``) only zeroed out the inherited
``sort``/``sort_steps``/``limit`` for the single transition
``previous.operation == "rank" and operation == "lookup"``. For
``rank -> profile_lookup`` (and the other non-rank operations) the three
drop-guards fell through and carried turn-1's ranking shape forward. That
produced a structurally impossible plan — ``profile_lookup`` forbids manual
sort and ``limit`` (see ``profile_lookup_shape_is_supported``) — which the
post-merge shape guard rejected, leaking the raw compile diagnostic to the
user (0 rows, 0 citations).

The fix generalizes the drop to every ``rank -> non-rank`` transition while
preserving ``rank -> rank`` re-ranking. Crucially, the district *set* is
RETAINED via ``_merge_selection_with_context`` (the referent branch
materialises the 10 prior result IDs as ``named_districts``), so the
follow-up means "enrollment OF THOSE 10 districts" — not enrollment of every
covered district (which stripping sort+limit alone would silently produce).

These tests pin the merge boundary directly, independent of the
orchestrator's post-merge shape guard, so they go green via the SEMANTIC
merge fix — not the secondary humanization guard.
"""

from __future__ import annotations

from compass_backend.contracts.planning import (
    LimitSpec,
    MetricSpec,
    ProfileFieldSpec,
    QueryPlan,
    SelectionSpec,
    SortStepSpec,
)
from compass_backend.contracts.session import QueryContext, QueryContextDistrictRef
from compass_backend.execution.profile import profile_lookup_shape_is_supported
from compass_backend.orchestration.chat import _user_facing_shape_clarification
from compass_backend.planning.planner import merge_query_plan_with_context


# The ten prior-result districts (subset of fields needed for selection
# materialisation). IDs + states stand in for turn-1's "ten highest starting
# salaries" ranked result rows.
_TEN_RESULT_DISTRICTS = [
    QueryContextDistrictRef(
        district_id=100 + i,
        district_name=name,
        state=state,
    )
    for i, (name, state) in enumerate(
        [
            ("San Francisco Unified", "CA"),
            ("Oakland Unified", "CA"),
            ("San Jose Unified", "CA"),
            ("Fresno Unified", "CA"),
            ("Long Beach Unified", "CA"),
            ("Sacramento City Unified", "CA"),
            ("San Diego Unified", "CA"),
            ("Seattle Public Schools", "WA"),
            ("Newark Public Schools", "NJ"),
            ("District of Columbia Public Schools", "DC"),
        ]
    )
]


def _ranked_prior_plan() -> QueryPlan:
    """Turn-1: top-10 teacher starting salaries (operation=rank)."""
    return QueryPlan(
        operation="rank",
        question="Show me the ten highest teacher starting salaries.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort_steps=[
            SortStepSpec(
                phase="presentation",
                field="starting salary",
                direction="desc",
                key_type="policy_metric",
            )
        ],
        limit=LimitSpec(count=10, kind="top"),
    )


def _context_with_ranked_prior() -> QueryContext:
    return QueryContext(
        query_plan=_ranked_prior_plan(),
        result_type="metric_lookup",
        order_statement="ten highest teacher starting salaries",
        row_count=10,
        result_districts=list(_TEN_RESULT_DISTRICTS),
    )


def _enrollment_followup_plan() -> QueryPlan:
    """Turn-2: "enrollment sizes of those districts" (profile_lookup).

    The planner names the operation and the profile field explicitly but does
    NOT restate sort/sort_steps/limit (they are absent from
    ``model_fields_set``), and points selection at the prior result via a
    referent sentinel — the exact shape the live planner emits for #1734.
    """
    return QueryPlan(
        inherit_from_session=True,
        operation="profile_lookup",
        question="What are the enrollment sizes of those districts?",
        selection=SelectionSpec(scope="named_districts", districts=["those districts"]),
        metrics=[],
        profile_fields=[ProfileFieldSpec(name="enrollment")],
    )


def test_rank_to_profile_lookup_followup_drops_ranking_shape() -> None:
    """The carried ranking shape (sort/sort_steps/limit) must be dropped."""

    merged = merge_query_plan_with_context(
        _enrollment_followup_plan(), _context_with_ranked_prior()
    )

    assert merged.operation == "profile_lookup"
    assert merged.sort is None, f"inherited sort leaked: {merged.sort!r}"
    assert merged.sort_steps == [], (
        f"inherited sort_steps leaked: {merged.sort_steps!r}"
    )
    assert merged.limit is None, f"inherited limit leaked: {merged.limit!r}"


def test_rank_to_profile_lookup_followup_retains_district_set() -> None:
    """"Those districts" must materialise the 10 prior result IDs as the
    named_districts selection — NOT collapse to all covered districts."""

    merged = merge_query_plan_with_context(
        _enrollment_followup_plan(), _context_with_ranked_prior()
    )

    assert merged.selection.scope == "named_districts"
    # The referent ("those districts") is replaced by the 10 ID-disambiguated
    # "Name, ST" handles from the prior result rows — not the sentinel string,
    # and not the full covered universe.
    assert "those districts" not in [d.casefold() for d in merged.selection.districts]
    assert len(merged.selection.districts) == len(_TEN_RESULT_DISTRICTS)
    assert all(
        any(ref.district_name.casefold() in d.casefold() for d in merged.selection.districts)
        for ref in _TEN_RESULT_DISTRICTS
    )


def test_rank_to_profile_lookup_merged_plan_is_compilable() -> None:
    """The end-to-end invariant: after merge, the profile_lookup plan must
    pass its own shape predicate (no sort/limit, named_districts, has fields).

    This is the assertion that fails on main (sort+limit carried) and that the
    semantic merge fix makes pass — guaranteeing the post-merge shape guard
    never fires and no raw diagnostic can leak."""

    merged = merge_query_plan_with_context(
        _enrollment_followup_plan(), _context_with_ranked_prior()
    )

    assert profile_lookup_shape_is_supported(merged) is True


def test_rank_to_rank_followup_keeps_ranking_shape() -> None:
    """Guard against over-correction: a rank -> rank re-rank follow-up that
    omits sort/limit must still INHERIT them (legitimate carry). Only
    rank -> NON-rank transitions drop the ranking shape."""

    rerank_followup = QueryPlan(
        inherit_from_session=True,
        operation="rank",
        question="Now show me the top 10 again.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
    )

    merged = merge_query_plan_with_context(
        rerank_followup, _context_with_ranked_prior()
    )

    assert merged.operation == "rank"
    assert merged.sort_steps, "rank->rank must keep inherited sort_steps"
    assert merged.limit is not None, "rank->rank must keep inherited limit"
    assert merged.limit.count == 10


# --- Secondary, defense-in-depth guard (NOT what closes the regression) ---


def test_user_facing_shape_clarification_leaks_no_raw_diagnostic() -> None:
    """The post-merge shape guard's user-facing clarification must never carry
    the raw ``unsupported_shape_hint`` diagnostic.

    This is the SECONDARY guard (#1734): on the regression path it does not
    fire (the merge fix prevents the impossible shape). It only catches OTHER
    latent inheritance defects so a raw internal hint can't reach a user.
    """

    for operation in ("profile_lookup", "peer_comparison", "trend", "similarity"):
        message = _user_facing_shape_clarification(operation)
        lowered = message.casefold()
        assert "cannot be compiled" not in lowered
        assert "does not accept" not in lowered
        assert "sort_steps" not in lowered
        assert "`limit`" not in message
        # Still actionable: invites the user to name districts + metric/field.
        assert "metric" in lowered
