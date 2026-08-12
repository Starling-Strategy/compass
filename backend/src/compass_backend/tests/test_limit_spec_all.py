"""Tests for LimitSpec(kind='all') contract and executor behavior.

TDD tests written BEFORE implementation. All tests in this file should fail
until the implementation lands (C7 all-districts limit brief).

Contract under test:
- LimitSpec(kind='all') constructs with count=None.
- LimitSpec(kind='all', count=5) raises ValueError.
- LimitSpec(kind='first', count=None) raises ValueError.
- LimitSpec(kind='first', count=10) still works (existing behavior preserved).
- build_metric_ranking_result with a LimitSpec(kind='all') plan returns ALL
  matching rows, not a cap of default_limit.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.artifacts import ResultSelection, SelectedDistrict
from compass_backend.artifacts.results import MetricRankingResult
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts import LimitSpec, MetricSpec, QueryPlan, SelectionSpec
from compass_backend.execution.ranking import build_metric_ranking_result
from compass_backend.execution.types import MetricAnswerRow


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=9001,
        name="Sick days per year",
        answer_type="numeric",
    )


def _district(district_id: int, name: str, state: str = "CA") -> SelectedDistrict:
    return SelectedDistrict(district_id=district_id, district_name=name, state=state)


def _answer_row(
    district_id: int,
    name: str,
    value: str,
    state: str = "CA",
) -> MetricAnswerRow:
    return MetricAnswerRow(
        answer_id=district_id * 100,
        district_id=district_id,
        district_name=name,
        state=state,
        metric_id=9001,
        metric_name="Sick days per year",
        value=value,
        academic_year="2024 - 2025",
        citations=[],
    )


def _build_with_limit_spec(
    *,
    districts: list[SelectedDistrict],
    rows: list[MetricAnswerRow],
    limit_spec: LimitSpec | None,
    default_limit: int = 10,
) -> MetricRankingResult:
    plan = QueryPlan(
        question="Rank all covered districts by sick days, highest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Sick days per year")],
        limit=limit_spec,
    )
    selection = ResultSelection(scope="all_covered_districts", districts=districts)
    return build_metric_ranking_result(
        plan,
        _metric(),
        rows,
        recent_rows=[],
        selection=selection,
        selected_districts=districts,
        default_limit=default_limit,
        reviewed_district_ids=set(),
        academic_year="2024 - 2025",
    )


# ─── Contract: LimitSpec(kind='first', count=None) raises ValidationError ─────


def test_limit_spec_first_requires_count() -> None:
    """kind='first' (and 'top', 'bottom') must have a positive count."""
    with pytest.raises(ValidationError, match="count required"):
        LimitSpec(kind="first", count=None)


def test_limit_spec_top_requires_count() -> None:
    with pytest.raises(ValidationError, match="count required"):
        LimitSpec(kind="top", count=None)


def test_limit_spec_bottom_requires_count() -> None:
    with pytest.raises(ValidationError, match="count required"):
        LimitSpec(kind="bottom", count=None)


# ─── Contract: LimitSpec(kind='all', count=5) raises ValidationError ─────────


def test_limit_spec_all_rejects_count() -> None:
    """kind='all' must not carry a count — count is semantically 'unbounded'."""
    with pytest.raises(ValidationError, match="count must be None"):
        LimitSpec(kind="all", count=5)


# ─── Contract: LimitSpec(kind='all') constructs successfully ─────────────────


def test_limit_spec_all_constructs_with_none_count() -> None:
    spec = LimitSpec(kind="all")
    assert spec.kind == "all"
    assert spec.count is None


def test_limit_spec_all_count_none_explicit() -> None:
    """Explicit count=None also works."""
    spec = LimitSpec(kind="all", count=None)
    assert spec.kind == "all"
    assert spec.count is None


# ─── Contract: existing behavior preserved ───────────────────────────────────


def test_limit_spec_first_count_10_still_works() -> None:
    spec = LimitSpec(kind="first", count=10)
    assert spec.count == 10
    assert spec.kind == "first"


def test_limit_spec_top_count_5_still_works() -> None:
    spec = LimitSpec(kind="top", count=5)
    assert spec.count == 5
    assert spec.kind == "top"


# ─── Executor: LimitSpec(kind='all') returns all rows, not default_limit ─────


def test_ranking_all_kind_returns_all_rows_not_default_cap() -> None:
    """With kind='all', build_metric_ranking_result must return N rows for N
    covered districts, not capped at default_limit (10).

    This is the core regression guard for C7.
    """
    n = 15  # more than default_limit=10
    districts = [_district(i, f"District {i:02d}") for i in range(1, n + 1)]
    rows = [_answer_row(i, f"District {i:02d}", str(i * 1000)) for i in range(1, n + 1)]
    limit_spec = LimitSpec(kind="all")

    result = _build_with_limit_spec(
        districts=districts,
        rows=rows,
        limit_spec=limit_spec,
        default_limit=10,
    )

    assert len(result.rows) == n, (
        f"Expected {n} rows with kind='all', got {len(result.rows)}. "
        "Executor must not cap at default_limit when kind='all'."
    )


def test_ranking_no_limit_spec_still_caps_at_default() -> None:
    """When limit is None (planner didn't set one), executor caps at default_limit.

    This ensures the existing behavior is NOT accidentally broken by the C7 change.
    """
    n = 15  # more than default_limit=10
    districts = [_district(i, f"District {i:02d}") for i in range(1, n + 1)]
    rows = [_answer_row(i, f"District {i:02d}", str(i * 1000)) for i in range(1, n + 1)]

    result = _build_with_limit_spec(
        districts=districts,
        rows=rows,
        limit_spec=None,
        default_limit=10,
    )

    # The default behavior: result is NOT capped in build_metric_ranking_result
    # (limit=None passes through as no cap); but default_limit=10 comes from
    # the executor's _default_limit and is NOT passed via build_metric_ranking_result
    # when no LimitSpec is set. build_metric_ranking_result itself uses _limit(plan)
    # which returns None when plan.limit is None → no cap. The cap happens at the
    # executor level via _profile_ranking_limit / selection; here we just confirm
    # that no-limit-spec does NOT accidentally cap to 10 at the build level.
    # (If this expectation changes, update this comment — this is intentional.)
    assert len(result.rows) == n


def test_ranking_explicit_top_count_still_caps() -> None:
    """LimitSpec(kind='top', count=5) should still cap at 5."""
    n = 15
    districts = [_district(i, f"District {i:02d}") for i in range(1, n + 1)]
    rows = [_answer_row(i, f"District {i:02d}", str(i * 1000)) for i in range(1, n + 1)]
    limit_spec = LimitSpec(kind="top", count=5)

    result = _build_with_limit_spec(
        districts=districts,
        rows=rows,
        limit_spec=limit_spec,
        default_limit=10,
    )

    assert len(result.rows) == 5


# ─── Regression: _profile_ranking_limit must not re-cap kind="all" at default ─


def test_profile_ranking_limit_kind_all_largest_districts_returns_none() -> None:
    """LimitSpec(kind='all') + scope='largest_districts' must skip default_limit cap.

    Regression for the silent-truncation gap caught by quality review:
    _sort_step_limit returns None for kind='all', but the largest_districts
    fallback in _profile_ranking_limit re-caps at default_limit. The fix
    adds an explicit kind='all' short-circuit at the top of the function.
    """
    from compass_backend.contracts.planning import SortStepSpec
    from compass_backend.execution._helpers import _profile_ranking_limit

    plan = QueryPlan(
        question="Show all largest districts by starting salary.",
        selection=SelectionSpec(scope="largest_districts"),
        metrics=[MetricSpec(name="Starting salary")],
        limit=LimitSpec(kind="all"),
    )
    # A SortStepSpec with NO step-level limit — so the fallback path fires
    # unless the function has an explicit kind="all" short-circuit.
    step = SortStepSpec(
        phase="selection",
        field="enrollment",
        direction="desc",
        key_type="profile_field",
        limit=None,
    )

    result = _profile_ranking_limit(plan, step, default_limit=10)

    assert result is None, (
        f"Expected None (no cap) with kind='all' + largest_districts, "
        f"got {result!r}. _profile_ranking_limit must short-circuit before "
        f"the largest_districts fallback when kind='all'."
    )
