"""Tests for metric-value threshold filters (PR 2A).

Covers `_resolve_metric_value_filters` applied to lookup, rank, trend, and
peer_comparison operations, plus the count back-compat shim and the composed
state + metric-value filter case.

TDD sequence (from pr-2a-plan.md):
  Step 3 — write failing tests (all initially fail with ExecutionRefusal or
            wrong result shape because the executor doesn't handle metric-value
            filters yet).
  Steps 4–8 — implement; tests pass.
"""

from __future__ import annotations

import pytest

from compass_backend.catalog import (
    CatalogAliasRecord,
    CatalogResolver,
    DistrictCandidate,
    DistrictResolution,
    MetricCandidate,
    normalize_district_name_for_resolution,
)
from compass_backend.contracts import FilterSpec, MetricSpec, QueryPlan, SelectionSpec, SimilarityQuerySpec
from compass_backend.execution import DeterministicQueryExecutor, MetricAnswerRow
from compass_backend.execution.types import ExecutionClarification, ExecutionRefusal
from compass_backend.tests.test_degree_lane import FakeDegreeRepository


# ---------------------------------------------------------------------------
# Shared fake repository
# ---------------------------------------------------------------------------


class FakeThresholdRepository:
    """Minimal fake repository for threshold-filter execution tests."""

    def __init__(
        self,
        *,
        metrics: list[MetricCandidate],
        districts: list[DistrictCandidate],
        rows: list[MetricAnswerRow],
        aliases: list[CatalogAliasRecord] | None = None,
        peer_profile_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.metrics = metrics
        self.districts = districts
        self.rows = rows
        self.aliases = aliases or []
        self.peer_profile_rows: list[dict[str, object]] = peer_profile_rows or []
        self.metric_queries: list[str] = []
        self.fetched_metric_ids: list[int] = []

    async def search_metrics(self, query: str, *, limit: int = 5) -> list[MetricCandidate]:
        self.metric_queries.append(query)
        normalized = query.casefold()
        return [
            m
            for m in self.metrics
            if normalized in m.name.casefold() or m.name.casefold() in normalized
        ][:limit]

    async def fetch_metrics_by_ids(self, metric_ids: list[int]) -> list[MetricCandidate]:
        self.metric_queries.append("ids:" + ",".join(str(i) for i in metric_ids))
        by_id = {m.metric_id: m for m in self.metrics}
        return [by_id[i] for i in metric_ids if i in by_id]

    async def search_catalog_aliases(
        self, alias: str, *, entity_types: set[str]
    ) -> list[CatalogAliasRecord]:
        normalized = normalize_district_name_for_resolution(alias)
        return [
            r
            for r in self.aliases
            if r.entity_type in entity_types
            and r.active
            and r.normalized_alias == normalized
        ]

    async def list_covered_districts(
        self, *, states: set[str] | None = None
    ) -> list[DistrictCandidate]:
        state_filter = {s.upper() for s in (states or set())}
        return [
            d
            for d in self.districts
            if not state_filter or (d.state or "").upper() in state_filter
        ]

    async def resolve_districts(
        self, names: list[str], *, states: set[str] | None = None
    ) -> DistrictResolution:
        # Resolve by exact normalized name (a realistic repository contract).
        # The historic stub returned every district as ``resolved`` for any
        # name; that masked anchor resolution, which #1363 now exercises (peer/
        # similarity anchors pre-resolve through resolve_referent_district).
        state_filter = {state.upper() for state in states or set()}
        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        ambiguous: dict[str, list[DistrictCandidate]] = {}
        for name in names:
            normalized = normalize_district_name_for_resolution(name)
            candidates = [
                district
                for district in self.districts
                if normalize_district_name_for_resolution(district.district_name)
                == normalized
                and (
                    not state_filter
                    or (district.state or "").upper() in state_filter
                )
            ]
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif len(candidates) > 1:
                ambiguous[name] = candidates
            else:
                unresolved.append(name)
        return DistrictResolution(
            resolved=resolved, unresolved=unresolved, ambiguous=ambiguous
        )

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int | None = 5,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        result = self.districts
        if states:
            state_filter = {s.upper() for s in states}
            result = [d for d in result if (d.state or "").upper() in state_filter]
        return result[:limit] if limit is not None else result

    async def select_districts_by_enrollment_range(
        self,
        *,
        states: set[str] | None = None,
        min_enrollment: int | None = None,
        max_enrollment: int | None = None,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        """Return districts filtered by enrollment bounds (uses district_id as proxy)."""
        result = self.districts
        if states:
            state_filter = {s.upper() for s in states}
            result = [d for d in result if (d.state or "").upper() in state_filter]
        # In the fake repo, district_id is used as a proxy for enrollment.
        if min_enrollment is not None:
            result = [d for d in result if d.district_id >= min_enrollment]
        if max_enrollment is not None:
            result = [d for d in result if d.district_id <= max_enrollment]
        return result

    async def fetch_metric_answer_rows(
        self, *, metric_id: int, academic_year: str
    ) -> list[MetricAnswerRow]:
        self.fetched_metric_ids.append(metric_id)
        return [
            row.model_copy(update={"academic_year": academic_year})
            for row in self.rows
            if row.metric_id == metric_id
        ]

    async def fetch_metric_answer_rows_for_year(
        self, *, metric_id: int, academic_year: str, district_ids: set[int]
    ) -> list[MetricAnswerRow]:
        self.fetched_metric_ids.append(metric_id)
        return [
            row.model_copy(update={"academic_year": academic_year})
            for row in self.rows
            if row.metric_id == metric_id and row.district_id in district_ids
        ]

    async def fetch_metric_answer_rows_for_years(
        self, *, metric_id: int, academic_years: list[str], district_ids: set[int]
    ) -> list[MetricAnswerRow]:
        return []

    async def fetch_reviewed_district_ids(
        self, *, academic_year: str, district_ids: set[int]
    ) -> set[int]:
        return {
            row.district_id
            for row in self.rows
            if row.district_id in district_ids
        }

    async def fetch_recent_metric_answer_rows(
        self, *, metric_id: int, before_academic_year: str, district_ids: set[int]
    ) -> list[MetricAnswerRow]:
        return []

    async def list_available_academic_years(
        self, *, metric_id: int, district_ids: set[int]
    ) -> list[str]:
        return ["2024 - 2025"]

    async def list_covered_nces_profiles(
        self, *, academic_year: str
    ) -> list[dict[str, object]]:
        return self.peer_profile_rows

    async def fetch_renderer_notes(self, note_keys: list[str]) -> list[object]:
        return []


class ExactDistrictThresholdRepository(FakeThresholdRepository):
    """Threshold fake that resolves named districts by exact normalized name."""

    async def resolve_districts(
        self, names: list[str], *, states: set[str] | None = None
    ) -> DistrictResolution:
        state_filter = {state.upper() for state in states or set()}
        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        ambiguous: dict[str, list[DistrictCandidate]] = {}
        for name in names:
            normalized = normalize_district_name_for_resolution(name)
            candidates = [
                district
                for district in self.districts
                if normalize_district_name_for_resolution(district.district_name)
                == normalized
                and (
                    not state_filter
                    or (district.state or "").upper() in state_filter
                )
            ]
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif len(candidates) > 1:
                ambiguous[name] = candidates
            else:
                unresolved.append(name)
        return DistrictResolution(
            resolved=resolved,
            unresolved=unresolved,
            ambiguous=ambiguous,
        )


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

_WORKDAYS_METRIC_ID = 42
_SALARY_METRIC_ID = 99


def _workdays_metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=_WORKDAYS_METRIC_ID,
        name="Teacher workdays",
        answer_type="numeric",
    )


def _school_year_metric_family() -> list[MetricCandidate]:
    return [
        MetricCandidate(
            metric_id=69,
            name="Total contracted workdays per academic year",
            answer_type="numeric",
        ),
        MetricCandidate(
            metric_id=70,
            name=(
                "Contracted student-teacher contact days per academic year "
                "in elementary school"
            ),
            answer_type="numeric",
        ),
        MetricCandidate(
            metric_id=254,
            name=(
                "Contracted student-teacher contact days per academic year "
                "in middle school"
            ),
            answer_type="numeric",
        ),
        MetricCandidate(
            metric_id=255,
            name=(
                "Contracted student-teacher contact days per academic year "
                "in high school"
            ),
            answer_type="numeric",
        ),
    ]


def _salary_metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=_SALARY_METRIC_ID,
        name="Average teacher starting salary",
        answer_type="numeric",
    )


def _health_premium_metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=232,
        name="District covers full employee health insurance premium",
        answer_type="text",
    )


def _three_districts() -> list[DistrictCandidate]:
    return [
        DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
    ]


def _workdays_rows() -> list[MetricAnswerRow]:
    """Alpha=195 (above 190), Bravo=185 (below 190), Charlie=None (missing data)."""
    return [
        MetricAnswerRow(
            district_id=1,
            district_name="Alpha",
            state="CA",
            metric_id=_WORKDAYS_METRIC_ID,
            metric_name="Teacher workdays",
            value=195,
            academic_year="2024 - 2025",
        ),
        MetricAnswerRow(
            district_id=2,
            district_name="Bravo",
            state="CA",
            metric_id=_WORKDAYS_METRIC_ID,
            metric_name="Teacher workdays",
            value=185,
            academic_year="2024 - 2025",
        ),
        # Charlie has no row — missing data
    ]


def _d1_rank_plan() -> QueryPlan:
    """D1: 'Which districts have more than 190 teacher workdays?' — rank shape."""
    return QueryPlan(
        operation="rank",
        question="Which districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )


def _d1_lookup_plan() -> QueryPlan:
    """D1 variant: lookup shape with the same threshold filter."""
    return QueryPlan(
        operation="lookup",
        question="Which districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )


# ---------------------------------------------------------------------------
# Step 3: write the failing executor tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_metric_value_filter_keeps_qualifying_district_only() -> None:
    """After filtering value > 190, only Alpha (195) should appear; Bravo (185)
    and Charlie (no data) must be excluded from the result rows."""

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_d1_lookup_plan())

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    district_ids_in_result = {row.district_id for row in result.rows}
    assert 1 in district_ids_in_result, "Alpha (id=1, value=195) should pass the filter"
    assert 2 not in district_ids_in_result, "Bravo (id=2, value=185) should be excluded"
    assert 3 not in district_ids_in_result, "Charlie (id=3, no data) should be excluded"


@pytest.mark.asyncio
async def test_lookup_metric_value_filter_supports_yes_no_health_premium_values() -> None:
    repository = FakeThresholdRepository(
        metrics=[_health_premium_metric()],
        districts=_three_districts(),
        rows=[
            MetricAnswerRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=232,
                metric_name="District covers full employee health insurance premium",
                value="Yes",
                academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=232,
                metric_name="District covers full employee health insurance premium",
                value="No",
                academic_year="2024 - 2025",
            ),
        ],
        aliases=[
            CatalogAliasRecord(
                alias="employee health insurance premium coverage",
                normalized_alias="employee health insurance premium coverage",
                entity_type="metric",
                resolution_status="approved",
                canonical_id="232",
                source="test",
                provenance="test fixture",
                review_status="approved",
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question=(
            "Show me districts where the district covers 100% of employees' "
            "health insurance premium."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="employee health insurance premium coverage")],
        filters=[
            FilterSpec(
                field="employee health insurance premium coverage",
                operator="equals",
                value="Yes",
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), outcome.message
    assert not isinstance(outcome, ExecutionClarification), outcome.message
    assert outcome.result is not None
    assert [(row.district_id, row.display_value) for row in outcome.result.rows] == [
        (1, "Yes")
    ]


@pytest.mark.asyncio
async def test_lookup_metric_value_filter_can_use_anchor_district_value() -> None:
    repository = ExactDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(
                district_id=10,
                district_name="Portland Public Schools",
                state="ME",
            ),
            DistrictCandidate(district_id=11, district_name="Alpha", state="ME"),
            DistrictCandidate(district_id=12, district_name="Bravo", state="ME"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=10,
                district_name="Portland Public Schools",
                state="ME",
                metric_id=_WORKDAYS_METRIC_ID,
                metric_name="Teacher workdays",
                value=185,
                academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=11,
                district_name="Alpha",
                state="ME",
                metric_id=_WORKDAYS_METRIC_ID,
                metric_name="Teacher workdays",
                value=185,
                academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=12,
                district_name="Bravo",
                state="ME",
                metric_id=_WORKDAYS_METRIC_ID,
                metric_name="Teacher workdays",
                value=190,
                academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question=(
            "I'm in Portland ME. What other districts have the same length "
            "school year?"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(
                field="teacher workdays",
                operator="equals",
                anchor_value={"district": "Portland Public Schools"},
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), outcome.message
    assert outcome.result is not None
    assert [(row.district_id, row.display_value) for row in outcome.result.rows] == [
        (11, "185")
    ]
    assert any(
        code.code == "anchor_value_filter_applied"
        for code in outcome.result.methodology_codes
    )


@pytest.mark.asyncio
async def test_anchor_filter_value_with_thousands_separator_does_not_crash() -> None:
    # #1772: the anchor district's reviewed value is a comma-formatted currency
    # ("45,602"). Resolving it into the equality filter used a raw float() and
    # crashed the turn at resolution time (ValueError -> 500). It must parse
    # comma-safe and match the districts that share that value.
    repository = ExactDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(
                district_id=10, district_name="Portland Public Schools", state="ME"
            ),
            DistrictCandidate(district_id=11, district_name="Alpha", state="ME"),
            DistrictCandidate(district_id=12, district_name="Bravo", state="ME"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=10,
                district_name="Portland Public Schools",
                state="ME",
                metric_id=_WORKDAYS_METRIC_ID,
                metric_name="Teacher workdays",
                value="45,602",
                academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=11,
                district_name="Alpha",
                state="ME",
                metric_id=_WORKDAYS_METRIC_ID,
                metric_name="Teacher workdays",
                value="45,602",
                academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=12,
                district_name="Bravo",
                state="ME",
                metric_id=_WORKDAYS_METRIC_ID,
                metric_name="Teacher workdays",
                value="44,000",
                academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="Which districts have the same value as Portland, ME?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(
                field="teacher workdays",
                operator="equals",
                anchor_value={"district": "Portland Public Schools"},
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), outcome.message
    assert outcome.result is not None
    # Alpha (11) shares Portland's "45,602"; Bravo (12) at "44,000" is excluded.
    assert [row.district_id for row in outcome.result.rows] == [11]


def test_ranking_over_comma_formatted_values_does_not_crash() -> None:
    """#1772 (ranking leg): the matched rows that flow into
    ``build_metric_ranking_result`` carry comma-formatted currency values
    ("45,602"). Each ranked row's ``value`` / ``sort_value`` must be the
    comma-stripped numeric, not a raw ``float("45,602")`` (which raises
    ``ValueError`` -> 500). This pins the ranking path of the bare-float crash
    class: a sort over comma values produces a correct numeric ordering.

    The other two legs of the class are pinned elsewhere:
    ``test_anchor_filter_value_with_thousands_separator_does_not_crash`` above
    (filter resolution, #1773) and the #1774 resolution-site test
    (``operations.py``). This test is GREEN on current main because
    ``build_metric_ranking_result`` already threads the parsed numeric into
    ``value=`` / ``sort_value=`` (since #1687); it guards against a regression
    that reintroduces a bare ``float(row.value)`` on that field.
    """
    from compass_backend.artifacts import ResultSelection, SelectedDistrict
    from compass_backend.contracts import LimitSpec
    from compass_backend.execution.ranking import build_metric_ranking_result

    metric = MetricCandidate(
        metric_id=7,
        name="Teacher workdays",
        answer_type="numeric",
    )
    districts = [
        SelectedDistrict(district_id=11, district_name="Alpha", state="ME"),
        SelectedDistrict(district_id=12, district_name="Bravo", state="ME"),
    ]
    rows = [
        MetricAnswerRow(
            district_id=11,
            district_name="Alpha",
            state="ME",
            metric_id=7,
            metric_name="Teacher workdays",
            value="45,602",  # comma-formatted: a bare float() would 500 here
            academic_year="2024 - 2025",
        ),
        MetricAnswerRow(
            district_id=12,
            district_name="Bravo",
            state="ME",
            metric_id=7,
            metric_name="Teacher workdays",
            value="44,000",
            academic_year="2024 - 2025",
        ),
    ]
    plan = QueryPlan(
        operation="rank",
        question="Rank Maine districts by teacher workdays, highest first.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha", "Bravo"]),
        metrics=[MetricSpec(name="teacher workdays")],
        sort={"field": "teacher workdays", "direction": "desc"},
        limit=LimitSpec(count=10, kind="top"),
    )

    result = build_metric_ranking_result(
        plan,
        metric,
        rows,
        recent_rows=[],
        selection=ResultSelection(scope="named_districts", districts=districts),
        selected_districts=districts,
        default_limit=10,
        reviewed_district_ids={11, 12},
        academic_year="2024 - 2025",
    )

    # The comma-formatted strings parsed to numerics and ranked, highest first.
    assert [row.district_id for row in result.rows] == [11, 12]
    assert result.rows[0].value == 45602.0
    assert result.rows[0].sort_value == 45602.0
    assert result.rows[1].value == 44000.0
    assert result.rows[1].sort_value == 44000.0


@pytest.mark.asyncio
async def test_anchor_filter_clarifies_ambiguous_school_year_length_metric() -> None:
    repository = ExactDistrictThresholdRepository(
        metrics=_school_year_metric_family(),
        districts=[
            DistrictCandidate(
                district_id=10,
                district_name="Portland Public Schools",
                state="ME",
            ),
            DistrictCandidate(district_id=11, district_name="Alpha", state="ME"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=10,
                district_name="Portland Public Schools",
                state="ME",
                metric_id=69,
                metric_name="Total contracted workdays per academic year",
                value=185,
                academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=11,
                district_name="Alpha",
                state="ME",
                metric_id=69,
                metric_name="Total contracted workdays per academic year",
                value=185,
                academic_year="2024 - 2025",
            ),
        ],
        aliases=[
            CatalogAliasRecord(
                alias="school year length",
                normalized_alias=normalize_district_name_for_resolution(
                    "school year length"
                ),
                entity_type="metric",
                resolution_status="ambiguous",
                candidate_refs=[
                    {"metric_id": 69},
                    {"metric_id": 70},
                    {"metric_id": 254},
                    {"metric_id": 255},
                ],
                canonical_ids=["69", "70", "254", "255"],
                source="test",
                provenance="test fixture",
                scenario_ids=[],
                review_status="approved",
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="I'm in Portland ME. What other districts have the same length school year?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="school year length")],
        filters=[
            FilterSpec(
                field="school year length",
                operator="equals",
                anchor_value={"district": "Portland Public Schools"},
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionClarification)
    assert outcome.clarification.missing_fields == ["metric"]
    assert outcome.clarification.candidates == [
        "Total contracted workdays per academic year",
        (
            "Contracted student-teacher contact days per academic year in "
            "elementary school"
        ),
        (
            "Contracted student-teacher contact days per academic year in "
            "middle school"
        ),
        (
            "Contracted student-teacher contact days per academic year in "
            "high school"
        ),
    ]


@pytest.mark.asyncio
async def test_rank_metric_value_filter_keeps_qualifying_district_only() -> None:
    """rank operation with value > 190 filter: only Alpha should appear."""

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_d1_rank_plan())

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    district_ids_in_result = {row.district_id for row in result.rows}
    assert 1 in district_ids_in_result, "Alpha (id=1, value=195) should pass the filter"
    assert 2 not in district_ids_in_result, "Bravo (id=2, value=185) should be excluded"
    assert 3 not in district_ids_in_result, "Charlie (id=3, no data) should be excluded"


@pytest.mark.asyncio
async def test_metric_value_filter_unresolved_returns_refusal() -> None:
    """When the filter field does not resolve to any metric, return ExecutionRefusal."""

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="lookup",
        question="Districts with high xyznonexistentfield?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="xyznonexistentfield")],
        filters=[
            FilterSpec(
                field="xyznonexistentfield", operator="greater_than", value=100
            ),
        ],
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionRefusal), (
        "Expected a refusal for an unresolvable filter field"
    )


@pytest.mark.asyncio
async def test_metric_value_filter_ambiguous_returns_clarification() -> None:
    """When the filter field resolves to multiple ambiguous metrics, return
    ExecutionClarification so the user can disambiguate.

    Uses a catalog alias with resolution_status="ambiguous" so the catalog
    signals ambiguity without requiring a live LLM adjudicator call.
    """

    salary_ba = MetricCandidate(
        metric_id=10, name="Average teacher starting salary BA", answer_type="numeric"
    )
    salary_ma = MetricCandidate(
        metric_id=11, name="Average teacher starting salary MA", answer_type="numeric"
    )
    ambiguous_alias = CatalogAliasRecord(
        alias="starting salary",
        normalized_alias=normalize_district_name_for_resolution("starting salary"),
        entity_type="metric_bundle",
        resolution_status="ambiguous",
        candidate_refs=[{"metric_id": 10}, {"metric_id": 11}],
        canonical_ids=["10", "11"],
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
    )
    repository = FakeThresholdRepository(
        metrics=[salary_ba, salary_ma],
        districts=_three_districts(),
        rows=[],
        aliases=[ambiguous_alias],
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="lookup",
        question="Show districts with starting salary above 50000.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        filters=[
            FilterSpec(field="starting salary", operator="greater_than", value=50000),
        ],
    )

    outcome = await executor.execute(plan)

    # The executor must not silently pick one metric when the catalog signals
    # ambiguity — it surfaces a clarification so the user can disambiguate.
    assert isinstance(outcome, ExecutionClarification), (
        "Expected a clarification for an ambiguous filter field"
    )


@pytest.mark.asyncio
async def test_state_plus_metric_value_filter_compose() -> None:
    """state filter + metric-value filter: only qualifying districts in that state."""

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=4, district_name="Delta", state="TX"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=195, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2, district_name="Bravo", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=4, district_name="Delta", state="TX",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=200, academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="lookup",
        question="Which CA districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="state", operator="equals", value="CA"),
            FilterSpec(
                field="teacher workdays", operator="greater_than", value=190
            ),
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    district_ids_in_result = {row.district_id for row in result.rows}
    assert 1 in district_ids_in_result, "Alpha CA (195) should pass"
    assert 2 not in district_ids_in_result, "Bravo CA (185) should be excluded"
    assert 4 not in district_ids_in_result, "Delta TX should be excluded (wrong state)"


@pytest.mark.asyncio
async def test_count_metric_value_filter_constrained_to_matching_metric_id() -> None:
    """count path with TWO metrics: a threshold filter on metric A (workdays, resolved
    to 'metric:10') must NOT be applied to rows for metric B (salary, metric_id=99).

    Regression for the latent bug in _row_matches_count_filters where all filters
    were applied to every row regardless of metric_id. This test FAILS against the
    un-fixed count.py (metric B's count would be wrong) and PASSES after the fix.

    Setup:
    - Metric A (workdays, id=10): Alpha=195, Bravo=180
    - Metric B (salary, id=99): Alpha=50000, Bravo=60000
    - Filter: workdays (metric:10) > 190  →  Alpha only
    - Expected: workdays count=1 (Alpha), salary count=2 (both, filter doesn't apply)
    """

    metric_a = MetricCandidate(
        metric_id=10, name="Teacher workdays", answer_type="numeric"
    )
    metric_b = MetricCandidate(
        metric_id=99, name="Average teacher starting salary", answer_type="numeric"
    )

    repository = FakeThresholdRepository(
        metrics=[metric_a, metric_b],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=10, metric_name="Teacher workdays",
                value=195, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2, district_name="Bravo", state="CA",
                metric_id=10, metric_name="Teacher workdays",
                value=180, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=99, metric_name="Average teacher starting salary",
                value=50000, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2, district_name="Bravo", state="CA",
                metric_id=99, metric_name="Average teacher starting salary",
                value=60000, academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    # Filter targets workdays (metric A) only — salary rows for both districts
    # should NOT be filtered by this predicate.
    plan = QueryPlan(
        operation="count",
        question="How many districts have more than 190 workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="Teacher workdays"),
            MetricSpec(name="Average teacher starting salary"),
        ],
        count_kind="threshold_count",
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    result = outcome.result
    assert result is not None

    rows_by_metric: dict[int, object] = {row.metric_id: row for row in result.rows}
    # Workdays count: only Alpha (195 > 190) qualifies.
    assert 10 in rows_by_metric, "Expected a count row for workdays (metric_id=10)"
    assert rows_by_metric[10].count == 1, (
        "Workdays count should be 1 (Alpha only); filter must not be bypassed"
    )
    # Salary count: filter does NOT apply to salary rows → both districts count.
    assert 99 in rows_by_metric, "Expected a count row for salary (metric_id=99)"
    assert rows_by_metric[99].count == 2, (
        "Salary count should be 2 (both districts); metric-scoped filter must not "
        "bleed into salary rows"
    )


@pytest.mark.asyncio
async def test_lookup_missing_data_excluded_from_filter() -> None:
    """Districts with missing (None) metric data must NOT be treated as passing
    or failing the threshold — they are excluded from the filtered result set."""

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_three_districts(),
        rows=[
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=195, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2, district_name="Bravo", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=None,  # explicitly missing
                academic_year="2024 - 2025",
            ),
            # Charlie has no row at all
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_d1_lookup_plan())

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    district_ids_in_result = {row.district_id for row in result.rows}
    assert 1 in district_ids_in_result, "Alpha (195) passes the filter"
    assert 2 not in district_ids_in_result, "Bravo (None) must not appear in filtered results"
    assert 3 not in district_ids_in_result, "Charlie (no row) must not appear"


# ---------------------------------------------------------------------------
# Fix 1 regression: enrollment bound + metric-value filter composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_enrollment_bound_plus_metric_value_filter_composes() -> None:
    """Lookup with enrollment bound + metric-value filter must apply BOTH constraints.

    Regression for code-quality reviewer's catch: requested_enrollment_bounds
    previously bailed on the metric-value filter entry in plan.filters and
    silently dropped the enrollment bound, making the scope ignore enrollment.

    Districts (using district_id as enrollment proxy in the fake repo):
    - Alpha (id=1 / enrollment=1): workdays=195, BELOW enrollment floor → excluded
    - Bravo (id=2 / enrollment=2): workdays=185, above enrollment floor but workdays fail → excluded
    - Charlie (id=3 / enrollment=3): workdays=200, above enrollment floor and workdays pass → included

    Scope: all_covered_districts + enrollment >= 3 + workdays > 190
    Expected: only Charlie
    """

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=195, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2, district_name="Bravo", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=3, district_name="Charlie", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=200, academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="lookup",
        question="Which large districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            # enrollment >= 3: excludes Alpha (id=1) and Bravo (id=2) in the fake repo
            FilterSpec(field="enrollment", operator="greater_than_or_equal", value=3),
            # workdays > 190: excludes Bravo (185)
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    district_ids_in_result = {row.district_id for row in result.rows}
    assert 3 in district_ids_in_result, (
        "Charlie (enrollment=3, workdays=200) should pass both constraints"
    )
    assert 1 not in district_ids_in_result, (
        "Alpha is excluded by enrollment bound (enrollment=1 < 3)"
    )
    assert 2 not in district_ids_in_result, (
        "Bravo is excluded by enrollment bound (enrollment=2 < 3)"
    )


# ---------------------------------------------------------------------------
# Hard filter promise: apply or refuse before rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trend_metric_value_filter_refuses_until_temporal_semantics_exist() -> None:
    """Trend must refuse threshold filters until the year basis is explicit."""
    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=195, academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="trend",
        question="Show trend of teacher workdays above 190 for Alpha.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionRefusal)
    assert "filter" in outcome.message.casefold()
    assert "trend" in outcome.message.casefold()


@pytest.mark.asyncio
async def test_peer_comparison_metric_value_filter_filters_peer_candidates() -> None:
    """Peer comparison must narrow peers that do not satisfy metric filters."""
    from compass_backend.catalog import (
        CatalogResolver,
        PeerSetResolution,
    )
    from compass_backend.catalog.resolution import (
        CoveredNCESProfileRow,
        PeerDistrictCandidate,
    )

    anchor_profile = CoveredNCESProfileRow(
        district_id=1, district_name="Alpha", state="CA"
    )
    peer_profile = PeerDistrictCandidate(
        district_id=2,
        district_name="Bravo",
        state="CA",
        rank=1,
        similarity_score=0.9,
    )
    passing_peer_profile = PeerDistrictCandidate(
        district_id=3,
        district_name="Charlie",
        state="CA",
        rank=2,
        similarity_score=0.8,
    )
    fake_peer_set = PeerSetResolution(
        anchor=anchor_profile,
        peers=[peer_profile, passing_peer_profile],
    )
    observed_peer_limits: list[int] = []

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=195, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2, district_name="Bravo", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=3, district_name="Charlie", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=200, academic_year="2024 - 2025",
            ),
        ],
    )

    # Build a CatalogResolver subclass that overrides resolve_peer_set.
    class FakeCatalogResolver(CatalogResolver):
        async def resolve_peer_set(
            self,
            anchor_name: str,
            *,
            states: set[str] | None = None,
            limit: int = 5,
            academic_year: str,
            feature_set: str = "all",
            exclude_states: list[str] | None = None,
        ) -> PeerSetResolution | None:
            observed_peer_limits.append(limit)
            return fake_peer_set

    catalog = FakeCatalogResolver(repository)
    executor = DeterministicQueryExecutor(repository, catalog_resolver=catalog)

    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Alpha's workdays above 190 to its peers.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    methodology_codes = [ref.code for ref in result.methodology_codes]
    assert "metric_value_filter_not_applied" not in methodology_codes, (
        "Peer comparison must not return success with an unapplied filter note. "
        f"Got: {methodology_codes}"
    )
    assert "metric_value_filter_applied" in methodology_codes
    district_names = {row.district_name for row in result.rows}
    assert "Alpha" in district_names
    assert "Charlie" in district_names
    assert "Bravo" not in district_names
    assert observed_peer_limits == [30], (
        "Peer comparison should score a larger internal candidate pool before "
        "coverage/diversity filtering, not only the final peer count."
    )


@pytest.mark.asyncio
async def test_peer_comparison_metric_value_filter_refuses_when_no_peers_pass() -> None:
    """If the filter excludes every peer, refuse instead of rendering anchor-only."""
    from compass_backend.catalog import (
        CatalogResolver,
        PeerSetResolution,
    )
    from compass_backend.catalog.resolution import (
        CoveredNCESProfileRow,
        PeerDistrictCandidate,
    )

    anchor_profile = CoveredNCESProfileRow(
        district_id=1, district_name="Alpha", state="CA"
    )
    peer_profile = PeerDistrictCandidate(
        district_id=2,
        district_name="Bravo",
        state="CA",
        rank=1,
        similarity_score=0.9,
    )
    fake_peer_set = PeerSetResolution(anchor=anchor_profile, peers=[peer_profile])

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=195, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2, district_name="Bravo", state="CA",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
        ],
    )

    class FakeCatalogResolver(CatalogResolver):
        async def resolve_peer_set(
            self,
            anchor_name: str,
            *,
            states: set[str] | None = None,
            limit: int = 5,
            academic_year: str,
            feature_set: str = "all",
            exclude_states: list[str] | None = None,
        ) -> PeerSetResolution | None:
            return fake_peer_set

    executor = DeterministicQueryExecutor(
        repository,
        catalog_resolver=FakeCatalogResolver(repository),
    )

    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Alpha's workdays above 190 to its peers.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionRefusal)
    assert "peer" in outcome.message.casefold()
    assert "filter" in outcome.message.casefold()


# ---------------------------------------------------------------------------
# Improvement: empty post-filter result (all districts excluded by threshold)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_threshold_excludes_all_districts_returns_empty_rows() -> None:
    """When the threshold filter excludes ALL districts, the result should be
    non-Refusal, have empty rows, and not crash on the empty district set.

    Using threshold > 999 so no district (max workdays=195) qualifies.
    """

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="lookup",
        question="Which districts have more than 999 teacher workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=999),
        ],
    )

    outcome = await executor.execute(plan)

    # Must not crash or raise — the executor should return an ExecutionSuccess
    # with an empty rows list (or a graceful ExecutionRefusal is also acceptable,
    # but it must not be an unhandled exception).
    assert not isinstance(outcome, ExecutionClarification), (
        "Empty post-filter result should not produce a clarification request"
    )
    if not isinstance(outcome, ExecutionRefusal):
        # If success, rows must be empty (no districts qualify).
        result = outcome.result
        assert result is not None
        district_ids_in_result = {row.district_id for row in result.rows}
        assert len(district_ids_in_result) == 0, (
            f"All districts should be excluded; got {district_ids_in_result}"
        )


# ---------------------------------------------------------------------------
# PR 2B: similarity + metric-value filter composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_similarity_metric_value_filter_applied_post_peer_set() -> None:
    """Similarity + metric-value filter: only peers passing the threshold survive.

    Scenario (D2 shape):
    - Anchor: Denver (district_id=26)
    - Peers: Jeffco (id=29, workdays=195) + Aurora (id=30, workdays=185)
    - Filter: workdays > 190  →  only Jeffco passes; Aurora is excluded
    - Expected: similarity_post_filter_narrowed_peer_set code appears
    """

    workdays_metric_id = 42
    workdays_metric = MetricCandidate(
        metric_id=workdays_metric_id,
        name="teacher workdays",
        answer_type="numeric",
    )
    anchor_district = DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO")

    # Workdays rows for peers (anchor has no policy answer — only peers filtered)
    workdays_rows = [
        MetricAnswerRow(
            district_id=29,
            district_name="Jeffco Public Schools",
            state="CO",
            metric_id=workdays_metric_id,
            metric_name="teacher workdays",
            value=195,
            academic_year="2024 - 2025",
        ),
        MetricAnswerRow(
            district_id=30,
            district_name="Aurora Public Schools",
            state="CO",
            metric_id=workdays_metric_id,
            metric_name="teacher workdays",
            value=185,
            academic_year="2024 - 2025",
        ),
    ]

    peer_profiles = [
        {
            "district_id": 26,
            "district_name": "Denver Public Schools",
            "state": "CO",
            "city": "DENVER",
            "enrollment": 87883,
            "locale_text": "City: Large",
            "latitude": 39.74575,
            "longitude": -104.985751,
            "total_rev_pp": 18381.59,
            "total_exp_pp": 18137.07,
            "pupil_teacher_ratio": 14.8,
            "frpl_pct": 65.0,
        },
        {
            "district_id": 29,
            "district_name": "Jeffco Public Schools",
            "state": "CO",
            "city": "GOLDEN",
            "enrollment": 75327,
            "locale_text": "Suburb: Large",
            "latitude": 39.740,
            "longitude": -105.220,
            "total_rev_pp": 23900.00,
            "total_exp_pp": 21000.00,
            "pupil_teacher_ratio": 16.6,
            "frpl_pct": 38.0,
        },
        {
            "district_id": 30,
            "district_name": "Aurora Public Schools",
            "state": "CO",
            "city": "AURORA",
            "enrollment": 41000,
            "locale_text": "City: Large",
            "latitude": 39.712,
            "longitude": -104.831,
            "total_rev_pp": 14000.00,
            "total_exp_pp": 13500.00,
            "pupil_teacher_ratio": 17.2,
            "frpl_pct": 72.0,
        },
    ]

    repository = FakeThresholdRepository(
        metrics=[workdays_metric],
        districts=[anchor_district],
        rows=workdays_rows,
        peer_profile_rows=peer_profiles,
    )
    executor = DeterministicQueryExecutor(repository, default_limit=10)

    plan = QueryPlan(
        operation="similarity",
        question="Find peers to Denver with more than 190 teacher workdays.",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        similarity=SimilarityQuerySpec(
            anchor_name="Denver Public Schools",
            feature_set="all",
            limit=10,
        ),
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    # Aurora (id=30, workdays=185) must be excluded; Jeffco (id=29, workdays=195) survives.
    peer_ids = {row.district_id for row in outcome.result.rows if row.peer_role == "peer"}
    assert 30 not in peer_ids, "Aurora should be excluded by workdays filter"
    # The narrowed-set methodology code should appear.
    codes = [ref.code for ref in outcome.result.methodology_codes]
    assert "similarity_post_filter_narrowed_peer_set" in codes


# ---------------------------------------------------------------------------
# Fix 4 (PR 2C review) — Lane-qualified threshold filter: name-matching lane inheritance
# ---------------------------------------------------------------------------

_BA_SALARY_METRIC_ID = 89
_MA_SALARY_METRIC_ID = 96


@pytest.mark.asyncio
async def test_lane_qualified_threshold_filter_inherits_lane_from_metric_spec() -> None:
    """When FilterSpec.field matches MetricSpec.name with degree_lane='ma', the filter
    resolves against MA-lane data (metric_id=96), not BA-lane data (metric_id=89).

    Exercises the name-matching lane-inheritance path in _resolve_metric_value_filters
    documented in teacher-compensation-salary.md Fix 4 planner instruction update.

    Setup:
    - BA salary (id=89): Alpha=$47000, Bravo=$45000
    - MA salary (id=96): Alpha=$53000, Bravo=$51000
    - MetricSpec(name="master's starting salary", degree_lane="ma")
    - FilterSpec(field="master's starting salary", operator="greater_than", value=52000)
    - Expected: only Alpha passes (MA=53000 > 52000); Bravo excluded (MA=51000 < 52000)
    - Confirm: metric_id=96 fetched, metric_id=89 NOT fetched for filter resolution
    """
    from compass_backend.catalog import CatalogAliasRecord, normalize_district_name_for_resolution

    ba_metric = MetricCandidate(
        metric_id=_BA_SALARY_METRIC_ID,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    ma_metric = MetricCandidate(
        metric_id=_MA_SALARY_METRIC_ID,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
    )
    # Alias for the MetricSpec phrase → resolves via lane filter to MA
    ma_alias = CatalogAliasRecord(
        alias="master's starting salary",
        normalized_alias=normalize_district_name_for_resolution("master's starting salary"),
        entity_type="metric_bundle",
        resolution_status="ambiguous",
        candidate_refs=[{"metric_id": _BA_SALARY_METRIC_ID}, {"metric_id": _MA_SALARY_METRIC_ID}],
        canonical_ids=[str(_BA_SALARY_METRIC_ID), str(_MA_SALARY_METRIC_ID)],
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
    )

    districts = [
        DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
    ]
    rows = [
        # BA rows (should NOT be used since degree_lane="ma")
        MetricAnswerRow(
            district_id=1, district_name="Alpha", state="CA",
            metric_id=_BA_SALARY_METRIC_ID, metric_name="BA starting salary",
            value=47000, academic_year="2024 - 2025",
        ),
        MetricAnswerRow(
            district_id=2, district_name="Bravo", state="CA",
            metric_id=_BA_SALARY_METRIC_ID, metric_name="BA starting salary",
            value=45000, academic_year="2024 - 2025",
        ),
        # MA rows (should be used via lane inheritance)
        MetricAnswerRow(
            district_id=1, district_name="Alpha", state="CA",
            metric_id=_MA_SALARY_METRIC_ID, metric_name="MA starting salary",
            value=53000, academic_year="2024 - 2025",
        ),
        MetricAnswerRow(
            district_id=2, district_name="Bravo", state="CA",
            metric_id=_MA_SALARY_METRIC_ID, metric_name="MA starting salary",
            value=51000, academic_year="2024 - 2025",
        ),
    ]

    repository = FakeThresholdRepository(
        metrics=[ba_metric, ma_metric],
        districts=districts,
        rows=rows,
        aliases=[ma_alias],
    )
    executor = DeterministicQueryExecutor(repository)

    # FilterSpec.field matches MetricSpec.name exactly — lane should be inherited.
    plan = QueryPlan(
        operation="lookup",
        question="Which districts have master's starting salary above $52K?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="master's starting salary", degree_lane="ma")],
        filters=[
            FilterSpec(
                field="master's starting salary",
                operator="greater_than",
                value=52000,
            ),
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    district_ids = {row.district_id for row in result.rows}
    assert 1 in district_ids, (
        "Alpha (MA salary=53000 > 52000) should pass the MA-lane filter"
    )
    assert 2 not in district_ids, (
        "Bravo (MA salary=51000 < 52000) should be excluded by the MA-lane filter"
    )
    # Confirm MA metric was fetched (not BA)
    assert _MA_SALARY_METRIC_ID in repository.fetched_metric_ids, (
        "MA metric (96) must have been fetched — lane inheritance should select MA data"
    )
    assert _BA_SALARY_METRIC_ID not in repository.fetched_metric_ids, (
        "BA metric (89) must NOT be fetched — lane inheritance must not fall back to BA"
    )


# ---------------------------------------------------------------------------
# Categorical (multi-valued text) metric filters — issue #1339
#
# The eligibility metric "Who is eligible for paid parental leave?" stores a
# single comma-joined cell of parent categories, e.g.
#   "Birthing parent, Non-birthing parent (gender not specified), Foster
#    parents, Adoptive parents".
# "districts that give paid parental leave to more than just the birthing
# parent" means: the value contains a category beyond just "Birthing parent".
# These tests pin the contains / in / not_in operators on categorical text
# metrics, which numeric-threshold filters do not exercise.
# ---------------------------------------------------------------------------

_ELIGIBILITY_METRIC_ID = 216
_ELIGIBILITY_METRIC_NAME = "Who is eligible for paid parental leave?"


def _eligibility_metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=_ELIGIBILITY_METRIC_ID,
        name=_ELIGIBILITY_METRIC_NAME,
        answer_type="text",
    )


def _five_districts() -> list[DistrictCandidate]:
    return [
        DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        DistrictCandidate(district_id=4, district_name="Delta", state="CA"),
        DistrictCandidate(district_id=5, district_name="Echo", state="CA"),
    ]


def _eligibility_row(district_id: int, name: str, value: str) -> MetricAnswerRow:
    return MetricAnswerRow(
        district_id=district_id,
        district_name=name,
        state="CA",
        metric_id=_ELIGIBILITY_METRIC_ID,
        metric_name=_ELIGIBILITY_METRIC_NAME,
        value=value,
        academic_year="2024 - 2025",
    )


def _eligibility_rows() -> list[MetricAnswerRow]:
    """Alpha=multi-category, Bravo=baseline-only, Charlie=Not applicable,
    Delta=no row (missing), Echo=single non-birthing category."""
    return [
        _eligibility_row(
            1,
            "Alpha",
            "Birthing parent, Non-birthing parent (gender not specified), "
            "Foster parents, Adoptive parents",
        ),
        _eligibility_row(2, "Bravo", "Birthing parent"),
        _eligibility_row(3, "Charlie", "Not applicable for district"),
        # Delta (id=4) has no row — not reviewed / missing.
        _eligibility_row(5, "Echo", "Non-birthing parent (fathers)"),
    ]


def _beyond_birthing_lookup_plan() -> QueryPlan:
    """The shape the planner should emit for issue #1339's prompt."""
    return QueryPlan(
        operation="lookup",
        question=(
            "Which districts give paid parental leave to more than just the "
            "birthing parent?"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=_ELIGIBILITY_METRIC_NAME)],
        filters=[
            FilterSpec(
                field=_ELIGIBILITY_METRIC_NAME,
                operator="in",
                value=[
                    "Non-birthing parent",
                    "Foster parents",
                    "Adoptive parents",
                    "Other",
                ],
            )
        ],
    )


def test_categorical_in_filter_is_recognized_as_metric_value_filter() -> None:
    """Gate 1: `requested_metric_filters` must surface an `in` filter on a
    metric phrase so the executor resolves and applies it (not drops it)."""
    from compass_backend.execution.selection import requested_metric_filters

    plan = _beyond_birthing_lookup_plan()
    metric_filters = requested_metric_filters(plan)

    assert len(metric_filters) == 1, (
        "An `in` filter on a metric phrase must be treated as a metric-value "
        f"filter; got {metric_filters!r}"
    )
    assert metric_filters[0].field == _ELIGIBILITY_METRIC_NAME


@pytest.mark.asyncio
async def test_categorical_in_filter_keeps_only_beyond_birthing_districts() -> None:
    """The 'more than just birthing parent' filter keeps multi-category and
    single non-birthing districts, and excludes baseline-only, Not-applicable,
    and missing rows."""
    repository = FakeThresholdRepository(
        metrics=[_eligibility_metric()],
        districts=_five_districts(),
        rows=_eligibility_rows(),
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_beyond_birthing_lookup_plan())

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    district_ids = {row.district_id for row in result.rows}
    assert district_ids == {1, 5}, (
        "Only Alpha (multi-category) and Echo (non-birthing) should pass; "
        f"got {sorted(district_ids)}"
    )


@pytest.mark.asyncio
async def test_categorical_in_filter_does_not_confuse_birthing_with_non_birthing() -> None:
    """Token-boundary correctness guard: a filter token 'Birthing parent' must
    NOT match the data token 'Non-birthing parent ...' via naive substring."""
    repository = FakeThresholdRepository(
        metrics=[_eligibility_metric()],
        districts=_five_districts(),
        rows=[
            _eligibility_row(1, "Alpha", "Non-birthing parent (gender not specified)"),
            _eligibility_row(2, "Bravo", "Birthing parent"),
            _eligibility_row(
                3,
                "Charlie",
                "Birthing parent, Non-birthing parent (gender not specified)",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="Which districts cover the birthing parent?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=_ELIGIBILITY_METRIC_NAME)],
        filters=[
            FilterSpec(
                field=_ELIGIBILITY_METRIC_NAME,
                operator="in",
                value=["Birthing parent"],
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), outcome.message
    assert not isinstance(outcome, ExecutionClarification), outcome.message
    assert outcome.result is not None
    district_ids = {row.district_id for row in outcome.result.rows}
    assert district_ids == {2, 3}, (
        "Bravo (exact) and Charlie (token present) match 'Birthing parent'; "
        "Alpha (only 'Non-birthing parent ...') must NOT match; "
        f"got {sorted(district_ids)}"
    )


@pytest.mark.asyncio
async def test_categorical_contains_single_value_filter() -> None:
    """`contains` with a single category string keeps rows whose multi-valued
    cell includes that category."""
    repository = FakeThresholdRepository(
        metrics=[_eligibility_metric()],
        districts=_five_districts(),
        rows=_eligibility_rows(),
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="Which districts grant paid parental leave to adoptive parents?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=_ELIGIBILITY_METRIC_NAME)],
        filters=[
            FilterSpec(
                field=_ELIGIBILITY_METRIC_NAME,
                operator="contains",
                value="Adoptive parents",
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), outcome.message
    assert not isinstance(outcome, ExecutionClarification), outcome.message
    assert outcome.result is not None
    district_ids = {row.district_id for row in outcome.result.rows}
    assert district_ids == {1}, (
        "Only Alpha lists 'Adoptive parents'; Echo (non-birthing only) and the "
        f"others must be excluded; got {sorted(district_ids)}"
    )


@pytest.mark.asyncio
async def test_categorical_filter_emits_applied_explanation_methodology() -> None:
    """AC#3: the answer explains the applied filter via a methodology code."""
    repository = FakeThresholdRepository(
        metrics=[_eligibility_metric()],
        districts=_five_districts(),
        rows=_eligibility_rows(),
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_beyond_birthing_lookup_plan())

    assert not isinstance(outcome, ExecutionRefusal), outcome.message
    assert outcome.result is not None
    codes = {ref.code for ref in outcome.result.methodology_codes}
    assert "categorical_value_filter_applied" in codes, (
        "A categorical filter must surface an applied-filter explanation; "
        f"got {sorted(codes)}"
    )


@pytest.mark.asyncio
async def test_categorical_not_in_filter_excludes_listed_category() -> None:
    """`not_in` keeps reviewed rows whose categories include none of the listed
    values."""
    repository = FakeThresholdRepository(
        metrics=[_eligibility_metric()],
        districts=_five_districts(),
        rows=_eligibility_rows(),
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="Which districts do not extend leave to foster parents?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=_ELIGIBILITY_METRIC_NAME)],
        filters=[
            FilterSpec(
                field=_ELIGIBILITY_METRIC_NAME,
                operator="not_in",
                value=["Foster parents"],
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), outcome.message
    assert not isinstance(outcome, ExecutionClarification), outcome.message
    assert outcome.result is not None
    district_ids = {row.district_id for row in outcome.result.rows}
    assert 1 not in district_ids, "Alpha lists Foster parents → excluded by not_in"
    assert 5 in district_ids, "Echo has no Foster parents category → kept"


# ---------------------------------------------------------------------------
# Anchor referent resolution recovery — issues #1192 / #739
#
# These pin the recoverable referent-resolution helper that the anchor path
# (and, in Phase 2, every other dead-end resolution site) reuses:
#   (a) a state qualifier bundled into the anchor district string
#       ("Portland ME") must disambiguate a same-name district in another
#       state instead of dead-ending on a generic refusal,
#   (b) when the exact name match fails but resolution yields a single
#       confident candidate, accept that single candidate (Detroit), and
#   (c) the pure-function state-suffix splitter.
# ---------------------------------------------------------------------------


class SingleFuzzyCandidateRepository(FakeThresholdRepository):
    """Resolves named districts by exact normalized name; on exact miss with a
    single fuzzy candidate, surfaces it via ``ambiguous`` (mirroring the live
    CatalogResolver fuzzy path that puts a lone fuzzy hit into ``ambiguous``).

    Matches the CatalogResolver contract that real execution sees: an exact
    name miss does NOT mean "unresolved" — a single fuzzy candidate arrives in
    ``ambiguous``. The recovery helper accepts that lone fuzzy hit only when a
    state qualifier narrowed it; an ungated bare fuzzy hit is clarified instead.
    """

    async def resolve_districts(
        self, names: list[str], *, states: set[str] | None = None
    ) -> DistrictResolution:
        state_filter = {state.upper() for state in states or set()}
        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        ambiguous: dict[str, list[DistrictCandidate]] = {}
        for name in names:
            normalized = normalize_district_name_for_resolution(name)
            exact = [
                district
                for district in self.districts
                if normalize_district_name_for_resolution(district.district_name)
                == normalized
                and (not state_filter or (district.state or "").upper() in state_filter)
            ]
            if len(exact) == 1:
                resolved.append(exact[0])
                continue
            if len(exact) > 1:
                ambiguous[name] = exact
                continue
            # Exact miss: fuzzy by substring within the state filter.
            fuzzy = [
                district
                for district in self.districts
                if normalized in normalize_district_name_for_resolution(
                    district.district_name
                )
                and (not state_filter or (district.state or "").upper() in state_filter)
            ]
            if fuzzy:
                ambiguous[name] = fuzzy
            else:
                unresolved.append(name)
        return DistrictResolution(
            resolved=resolved, unresolved=unresolved, ambiguous=ambiguous
        )


def _portland_workdays_filter_plan() -> QueryPlan:
    """'I'm in Portland ME. What other districts have the same length school
    year?' — the bundled-state-suffix anchor shape from #1192."""
    return QueryPlan(
        operation="lookup",
        question=(
            "I'm in Portland ME. What other districts have the same length "
            "school year?"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(
                field="teacher workdays",
                operator="equals",
                anchor_value={"district": "Portland ME"},
            )
        ],
    )


@pytest.mark.asyncio
async def test_anchor_district_state_suffix_disambiguates_portland() -> None:
    """anchor_value.district='Portland ME' must resolve to the ME Portland even
    though an OR Portland exists with the same name — the trailing state
    qualifier is split and honored, then the equals-anchor filter is applied.

    Without the recovery helper this dead-ends: resolve_district("Portland ME")
    finds no exact match (the covered name is just "Portland"), so the anchor
    path emits the generic 'could not resolve the anchor district' refusal.
    """
    repository = ExactDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(district_id=10, district_name="Portland", state="ME"),
            DistrictCandidate(district_id=20, district_name="Portland", state="OR"),
            DistrictCandidate(district_id=11, district_name="Alpha", state="ME"),
            DistrictCandidate(district_id=21, district_name="Bravo", state="OR"),
        ],
        rows=[
            # ME Portland anchor value = 185
            MetricAnswerRow(
                district_id=10, district_name="Portland", state="ME",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
            # Alpha ME matches the anchor (185) → should be returned
            MetricAnswerRow(
                district_id=11, district_name="Alpha", state="ME",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
            # OR Portland is 999 — would change the anchor if the wrong Portland
            # were chosen, so this row guards against state-qualifier loss.
            MetricAnswerRow(
                district_id=20, district_name="Portland", state="OR",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=999, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=21, district_name="Bravo", state="OR",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_portland_workdays_filter_plan())

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    assert outcome.result is not None
    result_ids = {row.district_id for row in outcome.result.rows}
    # Alpha ME (185) matches the ME-Portland anchor (185) and survives.
    assert 11 in result_ids, "Alpha ME (185) should match the ME-Portland anchor"
    # Bravo OR (185) also numerically matches but is fine to include; the guard
    # is that the anchor value came from ME (185), not OR (999).
    assert 10 not in result_ids, "Anchor district (ME Portland) is excluded by default"
    assert any(
        code.code == "anchor_value_filter_applied"
        for code in outcome.result.methodology_codes
    )


def _detroit_repository() -> "SingleFuzzyCandidateRepository":
    return SingleFuzzyCandidateRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(
                district_id=30,
                district_name="Detroit Public Schools Community District",
                state="MI",
            ),
            DistrictCandidate(district_id=31, district_name="Alpha", state="MI"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=30,
                district_name="Detroit Public Schools Community District",
                state="MI",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=180, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=31, district_name="Alpha", state="MI",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=180, academic_year="2024 - 2025",
            ),
        ],
    )


def _detroit_anchor_plan(district_text: str) -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="Which districts have the same workdays as Detroit?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(
                field="teacher workdays",
                operator="equals",
                anchor_value={"district": district_text},
            )
        ],
    )


@pytest.mark.asyncio
async def test_anchor_district_bare_fuzzy_candidate_clarifies_not_autoresolves() -> None:
    """anchor_value.district='Detroit' (no state qualifier) where the exact name
    is 'Detroit Public Schools Community District'. The exact match fails and a
    single fuzzy candidate surfaces via ``ambiguous`` — but an *ungated* lone
    fuzzy hit could be a real-but-wrong district, so the helper must CLARIFY with
    the real candidate, not silently auto-resolve. This is the grounding gate
    (mirrors execution.selection, which never auto-accepts an ambiguous match)."""
    executor = DeterministicQueryExecutor(_detroit_repository())

    outcome = await executor.execute(_detroit_anchor_plan("Detroit"))

    assert isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionClarification, got {type(outcome).__name__}: "
        f"{getattr(outcome, 'message', outcome)}"
    )
    assert "Detroit" in outcome.message
    assert outcome.clarification.candidates, "clarification must list the real candidate"


@pytest.mark.asyncio
async def test_anchor_district_state_qualified_fuzzy_resolves_detroit_mi() -> None:
    """anchor_value.district='Detroit MI'. The trailing state qualifier narrows
    the fuzzy match to exactly one covered district, so the helper accepts it
    (the qualifier is the user's own disambiguation) and applies the
    equals-anchor filter — no clarification needed."""
    executor = DeterministicQueryExecutor(_detroit_repository())

    outcome = await executor.execute(_detroit_anchor_plan("Detroit MI"))

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    assert outcome.result is not None
    result_ids = {row.district_id for row in outcome.result.rows}
    assert 31 in result_ids, "Alpha (180) matches the Detroit anchor (180)"
    assert 30 not in result_ids, "Anchor district (Detroit) excluded by default"


def test_anchor_value_state_typed_field_normalizes_and_rejects() -> None:
    """WS-D (#1373): AnchorValueSpec gains a typed `state` qualifier so the
    anchor's state is structured rather than bundled into the district string.
    Full names and abbreviations normalize to 2-letter (mirroring the
    exclude_states validator); junk is rejected at the contract boundary."""
    from compass_backend.contracts import AnchorValueSpec

    assert AnchorValueSpec(district="Portland", state="Maine").state == "ME"
    assert AnchorValueSpec(district="Portland", state="ca").state == "CA"
    assert AnchorValueSpec(district="Portland", state=None).state is None
    assert AnchorValueSpec(district="Portland").state is None
    with pytest.raises(ValueError):
        AnchorValueSpec(district="Portland", state="notastate")


def _portland_typed_state_plan() -> QueryPlan:
    """Same 'same school year as me' shape as #1192, but the anchor state is a
    TYPED field (district='Portland', state='ME') rather than bundled in the
    district string — WS-D / #1373."""
    return QueryPlan(
        operation="lookup",
        question="What districts have the same length school year as Portland?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(
                field="teacher workdays",
                operator="equals",
                anchor_value={"district": "Portland", "state": "ME"},
            )
        ],
    )


@pytest.mark.asyncio
async def test_anchor_district_typed_state_disambiguates_portland() -> None:
    """With a TYPED anchor state (district='Portland', state='ME'), the resolver
    picks the ME Portland over the OR Portland without relying on a string
    suffix — the qualifier is structured end-to-end (WS-D / #1373)."""
    repository = ExactDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(district_id=10, district_name="Portland", state="ME"),
            DistrictCandidate(district_id=20, district_name="Portland", state="OR"),
            DistrictCandidate(district_id=11, district_name="Alpha", state="ME"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=10, district_name="Portland", state="ME",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=11, district_name="Alpha", state="ME",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
            # OR Portland = 999 — guards against the wrong Portland being chosen.
            MetricAnswerRow(
                district_id=20, district_name="Portland", state="OR",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=999, academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_portland_typed_state_plan())

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    assert outcome.result is not None
    result_ids = {row.district_id for row in outcome.result.rows}
    assert 11 in result_ids, "Alpha ME (185) matches the typed-state ME-Portland anchor"
    assert 10 not in result_ids, "Anchor district (ME Portland) excluded by default"


def test_split_state_suffix_pure_function() -> None:
    """The state-suffix splitter strips only a recognized trailing US-state
    qualifier, leaving a non-empty remainder; otherwise the string is unchanged."""
    from compass_backend.execution.referent_resolution import split_state_suffix

    assert split_state_suffix("Portland ME") == ("Portland", {"ME"})
    assert split_state_suffix("Portland, ME") == ("Portland", {"ME"})
    assert split_state_suffix("Detroit") == ("Detroit", set())
    # A full official name that merely ends in a non-state token is unchanged.
    assert split_state_suffix("District of Columbia Public Schools") == (
        "District of Columbia Public Schools",
        set(),
    )
    # A bare state with no remaining name must NOT be stripped to "".
    assert split_state_suffix("ME") == ("ME", set())


# ---------------------------------------------------------------------------
# Phase 2A — dead-end resolution sites that DISCARD candidate info must
# CLARIFY with the real candidates instead of emitting a generic refusal
# (SELECT-R4 / RFC #1248). These pin two operations.py sites:
#
#   (a) composite (separate) ranking metric resolution: when the catalog
#       returns candidates but cannot auto-resolve to exactly one metric
#       (resolved=[], candidates=[...], ambiguous=False), the old code
#       dropped the candidates and dead-ended with "I could not resolve
#       every requested metric". It must now clarify with the candidates.
#   (b) profile-field resolution: when the catalog surfaces candidate
#       NCES/profile fields but resolves none, the old code emitted the
#       generic "I could not resolve that NCES/profile field" refusal,
#       discarding ProfileFieldResolution.candidates. It must now clarify.
#
# Genuine no-candidate misses stay LOW-severity refusals (asserted too).
# ---------------------------------------------------------------------------


class _CompositeCandidateOnlyCatalog(CatalogResolver):
    """Real resolver whose ``resolve_metric_bundle`` returns candidates the
    bundle resolver could not auto-resolve (the live ``ambiguous=False`` +
    ``resolved=[]`` shape — e.g. an ambiguous alias whose canonical IDs no
    longer fetch any metric). Everything else (district resolution, scoping,
    unsupported-concept screen) inherits the real resolver over the fake repo.
    """

    def __init__(self, repository, *, candidates: list[MetricCandidate]) -> None:
        super().__init__(repository)
        self._bundle_candidates = candidates

    async def resolve_metric_bundle(self, query: str, **kwargs):  # type: ignore[override]
        from compass_backend.catalog import MetricBundleResolution

        return MetricBundleResolution(
            query=query,
            resolved=[],
            candidates=self._bundle_candidates,
            ambiguous=False,
        )


def _composite_candidate_metrics() -> list[MetricCandidate]:
    return [
        MetricCandidate(
            metric_id=701,
            name="Bachelor's starting salary",
            answer_type="numeric",
        ),
        MetricCandidate(
            metric_id=702,
            name="Master's starting salary",
            answer_type="numeric",
        ),
    ]


@pytest.mark.asyncio
async def test_composite_ranking_clarifies_with_metric_candidates() -> None:
    """A separate ('do all N') ranking whose metric phrase yields candidates
    but no single resolved metric must clarify with those candidates, not
    dead-end on the generic 'could not resolve every requested metric' refusal.
    """
    repository = FakeThresholdRepository(
        metrics=[],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[],
    )
    catalog = _CompositeCandidateOnlyCatalog(
        repository, candidates=_composite_candidate_metrics()
    )
    executor = DeterministicQueryExecutor(repository, catalog_resolver=catalog)
    plan = QueryPlan(
        operation="rank",
        question="Rank covered districts by starting salary, both lanes separately.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary"),
            MetricSpec(name="another salary lane"),
        ],
        requires_composite_ranking=True,
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionClarification), (
        "composite ranking with metric candidates must clarify, not refuse — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    candidate_labels = " ".join(outcome.clarification.candidates).casefold()
    assert "bachelor's starting salary" in candidate_labels
    assert "master's starting salary" in candidate_labels


class _CompositeNoCandidateCatalog(CatalogResolver):
    """Resolver whose bundle resolution yields neither resolved nor candidates."""

    async def resolve_metric_bundle(self, query: str, **kwargs):  # type: ignore[override]
        from compass_backend.catalog import MetricBundleResolution

        return MetricBundleResolution(
            query=query, resolved=[], candidates=[], ambiguous=False
        )


@pytest.mark.asyncio
async def test_composite_ranking_refuses_when_no_metric_candidates() -> None:
    """A genuine no-candidate miss stays a LOW-severity refusal — there is
    nothing to clarify with."""
    repository = FakeThresholdRepository(
        metrics=[],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[],
    )
    catalog = _CompositeNoCandidateCatalog(repository)
    executor = DeterministicQueryExecutor(repository, catalog_resolver=catalog)
    plan = QueryPlan(
        operation="rank",
        question="Rank by two made-up lanes separately.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="made up lane one"),
            MetricSpec(name="made up lane two"),
        ],
        requires_composite_ranking=True,
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionRefusal), (
        "no metric candidates → genuine refusal, not a clarification"
    )


class _ProfileFieldCandidateOnlyCatalog(CatalogResolver):
    """Resolver whose ``resolve_profile_field_authority`` surfaces candidate
    NCES/profile fields but resolves none (resolved=None, candidates=[...])."""

    def __init__(self, repository, *, candidates) -> None:
        super().__init__(repository)
        self._field_candidates = candidates

    async def resolve_profile_field_authority(self, query: str, *, limit: int = 5):  # type: ignore[override]
        from compass_backend.catalog import ProfileFieldResolution

        return ProfileFieldResolution(
            query=query,
            resolved=None,
            candidates=self._field_candidates,
            resolution_method="unresolved",
        )


def _profile_field_candidates():
    from compass_backend.catalog import NCESFieldCandidate

    return [
        NCESFieldCandidate(
            field_key="urban_centric_locale",
            label="Urban-centric locale",
            data_type="text",
        ),
        NCESFieldCandidate(
            field_key="locale_code",
            label="Locale code",
            data_type="text",
        ),
    ]


def _profile_sort_plan(field: str) -> QueryPlan:
    """A rank ordered by a profile field via a presentation sort step. This
    routes through ``_execute_profile_ranking`` → ``_resolve_profile_sort_step``,
    the call site that emits ``_profile_field_dead_end`` when the field cannot
    be resolved to a governed NCES/profile field. Mirrors the executable
    profile-rank shape in test_mvp_execution (a same-named profile-field metric
    plus a profile-field presentation sort step)."""
    from compass_backend.contracts import SortSpec, SortStepSpec

    return QueryPlan(
        operation="rank",
        question=f"Rank covered districts by {field}.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=field)],
        sort=SortSpec(field=field, direction="desc"),
        sort_steps=[
            SortStepSpec(
                phase="presentation",
                field=field,
                key_type="profile_field",
                direction="desc",
            )
        ],
    )


@pytest.mark.asyncio
async def test_profile_sort_field_clarifies_with_candidates() -> None:
    """A profile-field sort that surfaces candidate fields but resolves none
    must clarify with those candidate field labels, not dead-end on the
    generic 'could not resolve that NCES/profile field' refusal.
    """
    repository = FakeThresholdRepository(
        metrics=[],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[],
    )
    catalog = _ProfileFieldCandidateOnlyCatalog(
        repository, candidates=_profile_field_candidates()
    )
    executor = DeterministicQueryExecutor(repository, catalog_resolver=catalog)

    outcome = await executor.execute(_profile_sort_plan("locale"))

    assert isinstance(outcome, ExecutionClarification), (
        "profile-field sort with candidates must clarify, not refuse — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert outcome.clarification.missing_fields == ["profile_field"]
    candidate_labels = " ".join(outcome.clarification.candidates).casefold()
    assert "urban-centric locale" in candidate_labels
    assert "locale code" in candidate_labels


class _ProfileFieldNoCandidateCatalog(CatalogResolver):
    """Resolver whose profile-field resolution yields neither field nor candidates."""

    async def resolve_profile_field_authority(self, query: str, *, limit: int = 5):  # type: ignore[override]
        from compass_backend.catalog import ProfileFieldResolution

        return ProfileFieldResolution(
            query=query, resolved=None, candidates=[], resolution_method="unresolved"
        )


@pytest.mark.asyncio
async def test_profile_sort_field_refuses_when_no_candidates() -> None:
    """A genuine no-candidate profile-field miss stays a LOW-severity refusal."""
    repository = FakeThresholdRepository(
        metrics=[],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[],
    )
    catalog = _ProfileFieldNoCandidateCatalog(repository)
    executor = DeterministicQueryExecutor(repository, catalog_resolver=catalog)

    outcome = await executor.execute(_profile_sort_plan("made_up_field"))

    assert isinstance(outcome, ExecutionRefusal), (
        "no profile-field candidates → genuine refusal, not a clarification"
    )


# ---------------------------------------------------------------------------
# #1363 Phase 2 — peer/similarity anchor recoverable referent resolution
#
# The anchor in a peer_comparison / similarity plan is a *referent* district
# named in a typed field. The historic call sites passed the raw typed string
# straight into resolve_peer_set, which returned None on any anchor that did
# not resolve to exactly one covered district — discarding the candidate set
# (so an ambiguous "Portland" became a generic "no peer set" dead-end) and
# ignoring a state qualifier bundled into the string ("Portland ME").
#
# These migrate the two sites to the shared resolve_referent_district helper:
#   - multi-candidate anchor (no qualifier) -> clarification with real labels
#   - state-qualified anchor ("Portland ME") -> resolves, peer set built with
#     the CANONICAL district name passed into resolve_peer_set
#   - anchor resolves cleanly but no NCES peer data -> refusal kept (case 403)
# ---------------------------------------------------------------------------


def _two_portlands() -> list[DistrictCandidate]:
    """A same-name pair in two states — the #1192/#739 anchor ambiguity case."""
    return [
        DistrictCandidate(district_id=10, district_name="Portland", state="ME"),
        DistrictCandidate(district_id=20, district_name="Portland", state="OR"),
    ]


class _AmbiguousDistrictThresholdRepository(FakeThresholdRepository):
    """Threshold fake whose district resolution mirrors the live fuzzy path.

    Returns a same-name pair as an ``ambiguous`` bucket entry when no state
    filter narrows it, and resolves cleanly to the single survivor when a
    state qualifier is supplied. This is what drives the anchor-resolution
    recovery in resolve_referent_district.
    """

    async def resolve_districts(
        self, names: list[str], *, states: set[str] | None = None
    ) -> DistrictResolution:
        state_filter = {state.upper() for state in states or set()}
        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        ambiguous: dict[str, list[DistrictCandidate]] = {}
        for name in names:
            normalized = normalize_district_name_for_resolution(name)
            candidates = [
                district
                for district in self.districts
                if normalize_district_name_for_resolution(district.district_name)
                == normalized
                and (
                    not state_filter
                    or (district.state or "").upper() in state_filter
                )
            ]
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif len(candidates) > 1:
                ambiguous[name] = candidates
            else:
                unresolved.append(name)
        return DistrictResolution(
            resolved=resolved, unresolved=unresolved, ambiguous=ambiguous
        )


@pytest.mark.asyncio
async def test_peer_comparison_ambiguous_anchor_clarifies_with_candidates() -> None:
    """An ambiguous peer anchor ("Portland", no state) clarifies with the real
    covered candidates instead of the generic 'no peer set' dead-end refusal.
    """
    repository = _AmbiguousDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_two_portlands(),
        rows=[],
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Portland and its peers on teacher workdays.",
        selection=SelectionSpec(scope="named_districts", districts=["Portland"]),
        metrics=[MetricSpec(name="teacher workdays")],
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionClarification), (
        "ambiguous peer anchor must clarify, not refuse — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert outcome.clarification.missing_fields == ["district"]
    candidate_labels = " ".join(outcome.clarification.candidates)
    assert "ME" in candidate_labels and "OR" in candidate_labels, (
        "clarification must list the real covered Portland candidates by state, "
        f"got {outcome.clarification.candidates}"
    )


@pytest.mark.asyncio
async def test_peer_comparison_state_qualified_anchor_resolves_canonical_name() -> None:
    """A state-qualified anchor ("Portland ME") narrows to one covered district
    and resolve_peer_set is called with the CANONICAL name (not the raw "Portland
    ME" string), honoring the user's own disambiguation.
    """
    from compass_backend.catalog import CatalogResolver, PeerSetResolution
    from compass_backend.catalog.resolution import (
        CoveredNCESProfileRow,
        PeerDistrictCandidate,
    )

    observed_anchor_names: list[str] = []
    fake_peer_set = PeerSetResolution(
        anchor=CoveredNCESProfileRow(
            district_id=10, district_name="Portland", state="ME"
        ),
        peers=[
            PeerDistrictCandidate(
                district_id=11,
                district_name="Alpha",
                state="ME",
                rank=1,
                similarity_score=0.9,
            )
        ],
    )

    repository = _AmbiguousDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            *_two_portlands(),
            DistrictCandidate(district_id=11, district_name="Alpha", state="ME"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=10, district_name="Portland", state="ME",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=11, district_name="Alpha", state="ME",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=190, academic_year="2024 - 2025",
            ),
        ],
    )

    class _AnchorCapturingResolver(CatalogResolver):
        async def resolve_peer_set(
            self,
            anchor_name: str,
            *,
            states: set[str] | None = None,
            limit: int = 5,
            academic_year: str,
            feature_set: str = "all",
            exclude_states: list[str] | None = None,
        ) -> PeerSetResolution | None:
            observed_anchor_names.append(anchor_name)
            return fake_peer_set

    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_AnchorCapturingResolver(repository)
    )

    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Portland ME and its peers on teacher workdays.",
        selection=SelectionSpec(scope="named_districts", districts=["Portland ME"]),
        metrics=[MetricSpec(name="teacher workdays")],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, (ExecutionRefusal, ExecutionClarification)), (
        "state-qualified anchor must resolve to a peer comparison — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert observed_anchor_names == ["Portland"], (
        "resolve_peer_set must receive the canonical district name (suffix "
        f"stripped), got {observed_anchor_names}"
    )


@pytest.mark.asyncio
async def test_peer_comparison_resolved_anchor_no_peer_data_refuses() -> None:
    """When the anchor resolves cleanly but no deterministic NCES peer set
    exists (resolve_peer_set returns None), the genuine data-absent refusal is
    kept — this preserves the Denver case-403 behavior.
    """
    from compass_backend.catalog import CatalogResolver, PeerSetResolution

    class _NoPeerDataResolver(CatalogResolver):
        async def resolve_peer_set(
            self,
            anchor_name: str,
            *,
            states: set[str] | None = None,
            limit: int = 5,
            academic_year: str,
            feature_set: str = "all",
            exclude_states: list[str] | None = None,
        ) -> PeerSetResolution | None:
            return None

    repository = ExactDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(
                district_id=26, district_name="Denver Public Schools", state="CO"
            )
        ],
        rows=[],
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_NoPeerDataResolver(repository)
    )

    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Denver and its peers on teacher workdays.",
        selection=SelectionSpec(
            scope="named_districts", districts=["Denver Public Schools"]
        ),
        metrics=[MetricSpec(name="teacher workdays")],
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionRefusal), (
        "a cleanly-resolved anchor with no peer data must keep the refusal — "
        f"got {type(outcome).__name__}"
    )
    assert "peer set" in outcome.message.casefold()


@pytest.mark.asyncio
async def test_similarity_ambiguous_anchor_clarifies_with_candidates() -> None:
    """An ambiguous similarity anchor ("Portland") clarifies with the real
    candidates instead of the generic 'no peer set' dead-end refusal.
    """
    repository = _AmbiguousDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_two_portlands(),
        rows=[],
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="similarity",
        question="Find districts similar to Portland.",
        selection=SelectionSpec(scope="named_districts", districts=["Portland"]),
        similarity=SimilarityQuerySpec(anchor_name="Portland"),
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionClarification), (
        "ambiguous similarity anchor must clarify, not refuse — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert outcome.clarification.missing_fields == ["district"]
    candidate_labels = " ".join(outcome.clarification.candidates)
    assert "ME" in candidate_labels and "OR" in candidate_labels


# ---------------------------------------------------------------------------
# #1363 Phase 2 (WS-B) — harmonize the metric-clarify gate to the single
# >1-distinct rule (matches scoping._metric_clarification_or_refusal).
#
# Two operations.py metric sites historically gated clarify on a *truthy*
# candidate list (`if candidates:`), so a lone-candidate miss clarified
# ("which of this one?") while the parallel scoping.py path refused. After the
# fix both clarify ONLY on >1 distinct candidate; a single distinct candidate
# keeps the deterministic refusal, never clarifies on zero candidates.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anchor_metric_single_candidate_refuses_not_clarifies() -> None:
    """anchor_value.metric resolving to a *single* distinct candidate (but no
    confident resolution) keeps the refusal — it does not clarify with one
    option. Mirrors scoping's >1-distinct rule.
    """
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricResolution

    class _SingleAnchorMetricResolver(CatalogResolver):
        async def resolve_primary_metric(
            self,
            query: str,
            *,
            numeric_only: bool = True,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricResolution:
            # Only the anchor metric phrase surfaces the single-distinct miss;
            # the filter field / primary metric resolves cleanly via the parent.
            if query != "workdays-ish":
                return await super().resolve_primary_metric(
                    query,
                    numeric_only=numeric_only,
                    limit=limit,
                    degree_lane=degree_lane,
                )
            return MetricResolution(
                query=query,
                resolved=None,
                candidates=[
                    MetricCandidate(
                        metric_id=_WORKDAYS_METRIC_ID,
                        name="Teacher workdays",
                        answer_type="numeric",
                    )
                ],
            )

    repository = ExactDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(
                district_id=10, district_name="Portland Public Schools", state="ME"
            ),
            DistrictCandidate(district_id=11, district_name="Alpha", state="ME"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=10, district_name="Portland Public Schools", state="ME",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_SingleAnchorMetricResolver(repository)
    )

    plan = QueryPlan(
        operation="lookup",
        question="Districts with the same workdays as Portland.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(
                field="teacher workdays",
                operator="equals",
                anchor_value={
                    "district": "Portland Public Schools",
                    "metric": "workdays-ish",
                },
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionRefusal), (
        "a single distinct anchor-metric candidate must refuse, not clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert "anchor metric" in outcome.message.casefold()


@pytest.mark.asyncio
async def test_anchor_metric_multiple_candidates_clarifies() -> None:
    """anchor_value.metric resolving to >1 distinct candidate clarifies with the
    real candidate labels.
    """
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricResolution

    class _MultiAnchorMetricResolver(CatalogResolver):
        async def resolve_primary_metric(
            self,
            query: str,
            *,
            numeric_only: bool = True,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricResolution:
            if query != "school year length":
                return await super().resolve_primary_metric(
                    query,
                    numeric_only=numeric_only,
                    limit=limit,
                    degree_lane=degree_lane,
                )
            return MetricResolution(
                query=query,
                resolved=None,
                candidates=[
                    MetricCandidate(
                        metric_id=69,
                        name="Total contracted workdays per academic year",
                        answer_type="numeric",
                    ),
                    MetricCandidate(
                        metric_id=70,
                        name="Contracted contact days (elementary)",
                        answer_type="numeric",
                    ),
                ],
            )

    repository = ExactDistrictThresholdRepository(
        metrics=[_workdays_metric()],
        districts=[
            DistrictCandidate(
                district_id=10, district_name="Portland Public Schools", state="ME"
            ),
            DistrictCandidate(district_id=11, district_name="Alpha", state="ME"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=10, district_name="Portland Public Schools", state="ME",
                metric_id=_WORKDAYS_METRIC_ID, metric_name="Teacher workdays",
                value=185, academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_MultiAnchorMetricResolver(repository)
    )

    plan = QueryPlan(
        operation="lookup",
        question="Districts with the same year length as Portland.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(
                field="teacher workdays",
                operator="equals",
                anchor_value={
                    "district": "Portland Public Schools",
                    "metric": "school year length",
                },
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionClarification), (
        ">1 distinct anchor-metric candidate must clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert outcome.clarification.missing_fields == ["metric"]


@pytest.mark.asyncio
async def test_separate_ranking_single_candidate_metric_refuses_not_clarifies() -> None:
    """A composite-ranking child metric that surfaces a *single* distinct
    candidate (resolved empty) keeps the per-metric refusal rather than
    clarifying with one option. Mirrors scoping's >1-distinct rule.
    """
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricBundleResolution

    class _SingleChildMetricResolver(CatalogResolver):
        async def resolve_metric_bundle(
            self,
            query: str,
            *,
            numeric_only: bool = False,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricBundleResolution:
            if query == "teacher workdays":
                return MetricBundleResolution(
                    query=query, resolved=[_workdays_metric()]
                )
            return MetricBundleResolution(
                query=query,
                resolved=[],
                candidates=[
                    MetricCandidate(
                        metric_id=_SALARY_METRIC_ID,
                        name="Average teacher starting salary",
                        answer_type="numeric",
                    )
                ],
            )

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric(), _salary_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_SingleChildMetricResolver(repository)
    )

    plan = QueryPlan(
        operation="rank",
        question="Rank by workdays and by mystery metric, separately.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="teacher workdays"),
            MetricSpec(name="mystery metric"),
        ],
        requires_composite_ranking=True,
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionRefusal), (
        "a single distinct child-metric candidate must refuse, not clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert "separate ranking" in outcome.message.casefold()


@pytest.mark.asyncio
async def test_separate_ranking_multiple_candidate_metric_clarifies() -> None:
    """A composite-ranking child metric that surfaces >1 distinct candidate
    clarifies with the real candidate labels.
    """
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricBundleResolution

    class _MultiChildMetricResolver(CatalogResolver):
        async def resolve_metric_bundle(
            self,
            query: str,
            *,
            numeric_only: bool = False,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricBundleResolution:
            if query == "teacher workdays":
                return MetricBundleResolution(
                    query=query, resolved=[_workdays_metric()]
                )
            return MetricBundleResolution(
                query=query,
                resolved=[],
                candidates=[
                    MetricCandidate(
                        metric_id=69,
                        name="Total contracted workdays per academic year",
                        answer_type="numeric",
                    ),
                    MetricCandidate(
                        metric_id=70,
                        name="Contracted contact days (elementary)",
                        answer_type="numeric",
                    ),
                ],
            )

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric(), _salary_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_MultiChildMetricResolver(repository)
    )

    plan = QueryPlan(
        operation="rank",
        question="Rank by workdays and by year length, separately.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="teacher workdays"),
            MetricSpec(name="school year length"),
        ],
        requires_composite_ranking=True,
    )

    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionClarification), (
        ">1 distinct child-metric candidate must clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert outcome.clarification.missing_fields == ["metric"]


# ---------------------------------------------------------------------------
# #1363 round 2 — eliminate the last 4 candidate-discarding metric-resolution
# dead-ends in operations.py. All four sites historically refused (or
# one-option-clarified) when metric resolution could not auto-resolve but the
# catalog DID return real candidates. The fix harmonizes every site to the same
# shared rule the template sites (operations.py:961/:1166/:2115) already use:
#
#   clarify IFF >1 DISTINCT metric_id  (_has_multiple_metric_candidates)
#   refuse on a single distinct candidate or zero candidates.
#
# Each site gets a "multiple-distinct → clarify" test and a boundary
# "single-distinct → refuse" test.
# ---------------------------------------------------------------------------


# --- Site 1: filter-field metric resolution (_resolve_metric_value_filters) ---


def _filter_field_plan() -> QueryPlan:
    """A lookup with a metric-value filter whose field must resolve to a metric.

    Routes through ``_resolve_metric_value_filters`` → ``resolve_primary_metric``
    on ``filter_spec.field``. The plan's own metric ("teacher workdays") resolves
    cleanly so only the filter-field resolution exercises the contested gate.
    """
    return QueryPlan(
        operation="lookup",
        question="Districts with more than 180 mystery-policy days.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(
                field="mystery policy days",
                operator="greater_than",
                value=180,
            )
        ],
    )


@pytest.mark.asyncio
async def test_filter_field_single_candidate_refuses_not_clarifies() -> None:
    """A filter field resolving to a *single* distinct candidate (resolved=None,
    ambiguous=False) keeps the deterministic refusal — it must not one-option
    clarify. Mirrors scoping's >1-distinct rule (WS-B gating fix)."""
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricResolution

    class _SingleFilterFieldResolver(CatalogResolver):
        async def resolve_primary_metric(
            self,
            query: str,
            *,
            numeric_only: bool = True,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricResolution:
            if query != "mystery policy days":
                return await super().resolve_primary_metric(
                    query,
                    numeric_only=numeric_only,
                    limit=limit,
                    degree_lane=degree_lane,
                )
            return MetricResolution(
                query=query,
                resolved=None,
                ambiguous=False,
                candidates=[
                    MetricCandidate(
                        metric_id=_WORKDAYS_METRIC_ID,
                        name="Teacher workdays",
                        answer_type="numeric",
                    )
                ],
            )

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_SingleFilterFieldResolver(repository)
    )

    outcome = await executor.execute(_filter_field_plan())

    assert isinstance(outcome, ExecutionRefusal), (
        "a single distinct filter-field candidate must refuse, not clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert "filter field" in outcome.message.casefold()


@pytest.mark.asyncio
async def test_filter_field_multiple_candidates_clarifies() -> None:
    """A filter field resolving to >1 distinct candidate (resolved=None,
    ambiguous=False) must clarify with the real candidate labels instead of
    refusing or dead-ending."""
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricResolution

    class _MultiFilterFieldResolver(CatalogResolver):
        async def resolve_primary_metric(
            self,
            query: str,
            *,
            numeric_only: bool = True,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricResolution:
            if query != "mystery policy days":
                return await super().resolve_primary_metric(
                    query,
                    numeric_only=numeric_only,
                    limit=limit,
                    degree_lane=degree_lane,
                )
            return MetricResolution(
                query=query,
                resolved=None,
                ambiguous=False,
                candidates=[
                    MetricCandidate(
                        metric_id=69,
                        name="Total contracted workdays per academic year",
                        answer_type="numeric",
                    ),
                    MetricCandidate(
                        metric_id=70,
                        name="Contracted contact days (elementary)",
                        answer_type="numeric",
                    ),
                ],
            )

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_MultiFilterFieldResolver(repository)
    )

    outcome = await executor.execute(_filter_field_plan())

    assert isinstance(outcome, ExecutionClarification), (
        ">1 distinct filter-field candidate must clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert outcome.clarification.missing_fields == ["metric"]


# --- Site 2: primary ranking metric (_resolve_rank_primary_metric_with_default)


def _limited_ranked_lookup_plan() -> QueryPlan:
    """A rank with a limit AND >1 metric → routes through
    ``_execute_limited_ranked_lookup`` → ``_resolve_rank_primary_metric_with_default``
    for the index-0 (primary) metric, then the comparison loop for index>0.

    ``degree_lane="ba"`` on the primary spec makes the contextual-default
    fallback (a BA proxy gated on ``degree_lane is None``) a no-op so the
    contested resolved=None / ambiguous=False path is reached.
    """
    return QueryPlan(
        operation="rank",
        question="Top 3 by primary salary lane, plus a comparison metric.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="mystery primary metric", degree_lane="ba"),
            MetricSpec(name="teacher workdays"),
        ],
        limit={"kind": "top", "count": 3},
    )


@pytest.mark.asyncio
async def test_rank_primary_metric_single_candidate_refuses_not_clarifies() -> None:
    """The primary RANKING metric resolving to a *single* distinct candidate
    (resolved=None, ambiguous=False) keeps the refusal — the message that
    historically said 'I found several possible Compass metrics. Please choose
    one' but listed none must not fire with a single option."""
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricResolution

    class _SinglePrimaryMetricResolver(CatalogResolver):
        async def resolve_primary_metric(
            self,
            query: str,
            *,
            numeric_only: bool = True,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricResolution:
            if query != "mystery primary metric":
                return await super().resolve_primary_metric(
                    query,
                    numeric_only=numeric_only,
                    limit=limit,
                    degree_lane=degree_lane,
                )
            return MetricResolution(
                query=query,
                resolved=None,
                ambiguous=False,
                candidates=[
                    MetricCandidate(
                        metric_id=_SALARY_METRIC_ID,
                        name="Average teacher starting salary",
                        answer_type="numeric",
                    )
                ],
            )

    repository = FakeThresholdRepository(
        metrics=[_salary_metric(), _workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_SinglePrimaryMetricResolver(repository)
    )

    outcome = await executor.execute(_limited_ranked_lookup_plan())

    assert isinstance(outcome, ExecutionRefusal), (
        "a single distinct primary-metric candidate must refuse, not clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )


@pytest.mark.asyncio
async def test_rank_primary_metric_multiple_candidates_clarifies() -> None:
    """The primary RANKING metric resolving to >1 distinct candidate
    (resolved=None, ambiguous=False) must clarify with the real candidate
    labels instead of the generic 'choose one' refusal that lists none."""
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricResolution

    class _MultiPrimaryMetricResolver(CatalogResolver):
        async def resolve_primary_metric(
            self,
            query: str,
            *,
            numeric_only: bool = True,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricResolution:
            if query != "mystery primary metric":
                return await super().resolve_primary_metric(
                    query,
                    numeric_only=numeric_only,
                    limit=limit,
                    degree_lane=degree_lane,
                )
            return MetricResolution(
                query=query,
                resolved=None,
                ambiguous=False,
                candidates=[
                    MetricCandidate(
                        metric_id=89,
                        name=(
                            "Annual base salary for a first year teacher with a "
                            "bachelor's degree"
                        ),
                        answer_type="numeric",
                    ),
                    MetricCandidate(
                        metric_id=96,
                        name=(
                            "Annual base salary for a first year teacher with a "
                            "master's degree"
                        ),
                        answer_type="numeric",
                    ),
                ],
            )

    repository = FakeThresholdRepository(
        metrics=[_salary_metric(), _workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_MultiPrimaryMetricResolver(repository)
    )

    outcome = await executor.execute(_limited_ranked_lookup_plan())

    assert isinstance(outcome, ExecutionClarification), (
        ">1 distinct primary-metric candidate must clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert outcome.clarification.missing_fields == ["metric"]


# --- Site 3: comparison-metric loop (_execute_limited_ranked_lookup, index>0) -


def _comparison_metric_plan() -> QueryPlan:
    """A rank with a limit AND >1 metric where the index-0 metric resolves
    cleanly (so the primary path passes) and the index>0 comparison metric is
    the contested one — routes through ``_execute_limited_ranked_lookup``'s
    per-comparison ``resolve_metric_bundle`` loop."""
    return QueryPlan(
        operation="rank",
        question="Top 3 by workdays, with a comparison metric.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="teacher workdays"),
            MetricSpec(name="mystery comparison metric"),
        ],
        limit={"kind": "top", "count": 3},
    )


@pytest.mark.asyncio
async def test_comparison_metric_single_candidate_refuses_not_clarifies() -> None:
    """An index>0 comparison metric whose bundle resolution surfaces a *single*
    distinct candidate (resolved=[], ambiguous=False) keeps the per-metric
    refusal rather than one-option clarifying."""
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricBundleResolution

    class _SingleComparisonResolver(CatalogResolver):
        async def resolve_metric_bundle(
            self,
            query: str,
            *,
            numeric_only: bool = False,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricBundleResolution:
            if query == "teacher workdays":
                return MetricBundleResolution(
                    query=query, resolved=[_workdays_metric()]
                )
            return MetricBundleResolution(
                query=query,
                resolved=[],
                ambiguous=False,
                candidates=[
                    MetricCandidate(
                        metric_id=_SALARY_METRIC_ID,
                        name="Average teacher starting salary",
                        answer_type="numeric",
                    )
                ],
            )

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric(), _salary_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_SingleComparisonResolver(repository)
    )

    outcome = await executor.execute(_comparison_metric_plan())

    assert isinstance(outcome, ExecutionRefusal), (
        "a single distinct comparison-metric candidate must refuse, not clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )


@pytest.mark.asyncio
async def test_comparison_metric_multiple_candidates_clarifies() -> None:
    """An index>0 comparison metric whose bundle resolution surfaces >1 distinct
    candidate (resolved=[], ambiguous=False) must clarify with the real
    candidate labels instead of discarding them with a generic refusal."""
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricBundleResolution

    class _MultiComparisonResolver(CatalogResolver):
        async def resolve_metric_bundle(
            self,
            query: str,
            *,
            numeric_only: bool = False,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricBundleResolution:
            if query == "teacher workdays":
                return MetricBundleResolution(
                    query=query, resolved=[_workdays_metric()]
                )
            return MetricBundleResolution(
                query=query,
                resolved=[],
                ambiguous=False,
                candidates=[
                    MetricCandidate(
                        metric_id=69,
                        name="Total contracted workdays per academic year",
                        answer_type="numeric",
                    ),
                    MetricCandidate(
                        metric_id=70,
                        name="Contracted contact days (elementary)",
                        answer_type="numeric",
                    ),
                ],
            )

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric(), _salary_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_MultiComparisonResolver(repository)
    )

    outcome = await executor.execute(_comparison_metric_plan())

    assert isinstance(outcome, ExecutionClarification), (
        ">1 distinct comparison-metric candidate must clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert outcome.clarification.missing_fields == ["metric"]


# --- Site 4: display metric (_execute_profile_ordered_metric_ranking) ---------


def _profile_ordered_plan() -> QueryPlan:
    """A rank with ``profile_fields`` and a policy primary metric → routes
    through ``_execute_profile_ordered_metric_ranking``. ``degree_lane="ba"`` on
    the display metric makes the display-default fallback a no-op (it is gated on
    ``degree_lane is None``) so the contested ``metric is None`` branch is hit.
    """
    from compass_backend.contracts import ProfileFieldSpec

    return QueryPlan(
        operation="rank",
        question="Rank districts by FRPL share and show the mystery display metric.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="mystery display metric", degree_lane="ba")],
        profile_fields=[ProfileFieldSpec(name="frpl_pct")],
    )


def _profile_ordered_repository() -> FakeDegreeRepository:
    """Reuse the degree-lane fake (it implements ``rank_districts_by_profile_field``
    and ``search_nces_fields``) so profile rows exist and routing succeeds."""
    from compass_backend.catalog.resolution import NCESFieldCandidate

    frpl_field = NCESFieldCandidate(
        field_key="frpl_pct",
        label="FRPL %",
        data_type="numeric",
        description="Free and reduced-price lunch share.",
    )
    return FakeDegreeRepository(
        nces_fields=[frpl_field],
        profile_rank_rows=[
            {
                "district_id": 1,
                "district_name": "Wake County",
                "state": "NC",
                "field_key": "frpl_pct",
                "label": "FRPL %",
                "value": 60.0,
                "display_value": "60%",
                "academic_year": "2024 - 2025",
                "vintage": "NCES release year: 2023",
            },
            {
                "district_id": 2,
                "district_name": "Aldine ISD",
                "state": "TX",
                "field_key": "frpl_pct",
                "label": "FRPL %",
                "value": 75.0,
                "display_value": "75%",
                "academic_year": "2024 - 2025",
                "vintage": "NCES release year: 2023",
            },
        ],
    )


@pytest.mark.asyncio
async def test_profile_ordered_display_metric_single_candidate_refuses() -> None:
    """The profile-ordered display metric resolving to a *single* distinct
    candidate (resolved=None, ambiguous=False) — after the contextual-default
    fallback is skipped — keeps the refusal rather than one-option clarifying."""
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricResolution

    class _SingleDisplayMetricResolver(CatalogResolver):
        async def resolve_primary_metric(
            self,
            query: str,
            *,
            numeric_only: bool = True,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricResolution:
            if query != "mystery display metric":
                return await super().resolve_primary_metric(
                    query,
                    numeric_only=numeric_only,
                    limit=limit,
                    degree_lane=degree_lane,
                )
            return MetricResolution(
                query=query,
                resolved=None,
                ambiguous=False,
                candidates=[
                    MetricCandidate(
                        metric_id=_SALARY_METRIC_ID,
                        name="Average teacher starting salary",
                        answer_type="numeric",
                    )
                ],
            )

    repository = _profile_ordered_repository()
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_SingleDisplayMetricResolver(repository)
    )

    outcome = await executor.execute(_profile_ordered_plan())

    assert isinstance(outcome, ExecutionRefusal), (
        "a single distinct display-metric candidate must refuse, not clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )


@pytest.mark.asyncio
async def test_profile_ordered_display_metric_multiple_candidates_clarifies() -> None:
    """The profile-ordered display metric resolving to >1 distinct candidate
    (resolved=None, ambiguous=False) — after the contextual-default fallback is
    skipped — must clarify with the real candidate labels instead of refusing."""
    from compass_backend.catalog import CatalogResolver, MetricCandidate
    from compass_backend.catalog.resolution import MetricResolution

    class _MultiDisplayMetricResolver(CatalogResolver):
        async def resolve_primary_metric(
            self,
            query: str,
            *,
            numeric_only: bool = True,
            limit: int = 5,
            degree_lane=None,
        ) -> MetricResolution:
            if query != "mystery display metric":
                return await super().resolve_primary_metric(
                    query,
                    numeric_only=numeric_only,
                    limit=limit,
                    degree_lane=degree_lane,
                )
            return MetricResolution(
                query=query,
                resolved=None,
                ambiguous=False,
                candidates=[
                    MetricCandidate(
                        metric_id=89,
                        name=(
                            "Annual base salary for a first year teacher with a "
                            "bachelor's degree"
                        ),
                        answer_type="numeric",
                    ),
                    MetricCandidate(
                        metric_id=96,
                        name=(
                            "Annual base salary for a first year teacher with a "
                            "master's degree"
                        ),
                        answer_type="numeric",
                    ),
                ],
            )

    repository = _profile_ordered_repository()
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=_MultiDisplayMetricResolver(repository)
    )

    outcome = await executor.execute(_profile_ordered_plan())

    assert isinstance(outcome, ExecutionClarification), (
        ">1 distinct display-metric candidate must clarify — "
        f"got {type(outcome).__name__}: {outcome.message}"
    )
    assert outcome.clarification.missing_fields == ["metric"]


# ---------------------------------------------------------------------------
# #1373 Stage 2 — kill the runtime "metric:<id>" field rewrite.
#
# The resolver used to overload ``FilterSpec.field`` with a runtime-rewritten
# ``"metric:<id>"`` token doing two jobs: (a) marking a filter "already
# resolved, skip profile-field resolution" and (b) carrying the metric_id for
# the count path's per-metric row scoping. Stage 2 replaces BOTH with typed
# state: (a) derives from ``filter_kind(spec) == "metric_value"`` and (b)
# threads the ``ResolvedMetricFilter.metric_id`` into the count row-matcher.
# These tests pin that the field is left free-form (never a ``metric:`` token)
# while the behaviors the token used to provide survive.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_threshold_scopes_to_metric_without_field_token_rewrite() -> None:
    """Multi-metric count: a metric-A threshold must NOT filter metric-B rows,
    and the resolver must NOT rewrite ``FilterSpec.field`` to a ``metric:<id>``
    token to achieve that scoping (#1373 Stage 2).

    Setup mirrors the existing constrained-scoping regression but additionally
    asserts the field stays the free-form metric phrase the planner emitted —
    the per-metric scoping now rides on ``ResolvedMetricFilter.metric_id``, not
    a string token.
    """

    metric_a = MetricCandidate(
        metric_id=10, name="Teacher workdays", answer_type="numeric"
    )
    metric_b = MetricCandidate(
        metric_id=99, name="Average teacher starting salary", answer_type="numeric"
    )
    repository = FakeThresholdRepository(
        metrics=[metric_a, metric_b],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=10, metric_name="Teacher workdays",
                value=195, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2, district_name="Bravo", state="CA",
                metric_id=10, metric_name="Teacher workdays",
                value=180, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=1, district_name="Alpha", state="CA",
                metric_id=99, metric_name="Average teacher starting salary",
                value=50000, academic_year="2024 - 2025",
            ),
            MetricAnswerRow(
                district_id=2, district_name="Bravo", state="CA",
                metric_id=99, metric_name="Average teacher starting salary",
                value=60000, academic_year="2024 - 2025",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="count",
        question="How many districts have more than 190 workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="Teacher workdays"),
            MetricSpec(name="Average teacher starting salary"),
        ],
        count_kind="threshold_count",
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    # Resolve filters directly to inspect the rewritten plan's field.
    resolved = await executor._resolve_metric_value_filters(plan)
    assert not isinstance(resolved, ExecutionRefusal)
    rewritten_plan, resolved_filters = resolved
    assert [f.field for f in rewritten_plan.filters] == ["teacher workdays"], (
        "the metric-value filter field must stay the free-form phrase; the "
        "'metric:<id>' rewrite is removed in #1373 Stage 2"
    )
    assert [rf.metric_id for rf in resolved_filters] == [10], (
        "the resolved filter must carry metric_id=10 to scope the count"
    )

    outcome = await executor.execute(plan)
    assert not isinstance(outcome, ExecutionRefusal), outcome.message
    rows_by_metric = {row.metric_id: row for row in outcome.result.rows}
    assert rows_by_metric[10].count == 1, (
        "workdays count is 1 (Alpha 195 > 190); the threshold must apply"
    )
    assert rows_by_metric[99].count == 2, (
        "salary count is 2 (both); a metric-10-scoped threshold must NOT bleed "
        "into metric-99 rows"
    )


@pytest.mark.asyncio
async def test_resolved_metric_value_filter_skipped_by_profile_resolution() -> None:
    """A resolved metric-value filter must be skipped by profile-field
    resolution without depending on a ``metric:<id>`` token (#1373 Stage 2).

    Profile-field resolution must leave the (free-form) metric-value filter
    field untouched — no rewrite to a profile ``field_key``, no dead-end —
    because ``filter_kind(spec) == 'metric_value'`` already classifies it.
    """

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="lookup",
        question="Which districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    resolved = await executor._resolve_metric_value_filters(plan)
    assert not isinstance(resolved, ExecutionRefusal)
    rewritten_plan, _resolved_filters = resolved

    profile_result = await executor._resolve_profile_filter_fields(rewritten_plan)
    assert not isinstance(profile_result, (ExecutionRefusal, ExecutionClarification)), (
        "a resolved metric-value filter must be skipped by profile-field "
        "resolution, not dead-end as an unresolved profile field"
    )
    after_plan, profile_authority = profile_result
    assert profile_authority == [], (
        "metric-value filters must not be claimed as profile fields"
    )
    assert [f.field for f in after_plan.filters] == ["teacher workdays"], (
        "profile-field resolution must leave the metric-value filter field as-is"
    )


@pytest.mark.asyncio
async def test_resolve_metric_value_filters_is_idempotent() -> None:
    """Re-running ``_resolve_metric_value_filters`` on its own output is a no-op:
    the filter field is unchanged and the resolved metric_ids are identical
    (#1373 Stage 2 idempotency — previously guarded by the ``metric:<id>``
    pass-through, now by the free-form field already classifying as
    ``metric_value``)."""

    repository = FakeThresholdRepository(
        metrics=[_workdays_metric()],
        districts=_three_districts(),
        rows=_workdays_rows(),
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="lookup",
        question="Which districts have more than 190 teacher workdays?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher workdays")],
        filters=[
            FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        ],
    )

    first = await executor._resolve_metric_value_filters(plan)
    assert not isinstance(first, ExecutionRefusal)
    first_plan, first_filters = first

    second = await executor._resolve_metric_value_filters(first_plan)
    assert not isinstance(second, ExecutionRefusal)
    second_plan, second_filters = second

    assert [f.field for f in first_plan.filters] == [f.field for f in second_plan.filters]
    assert [f.field for f in second_plan.filters] == ["teacher workdays"]
    assert [rf.metric_id for rf in first_filters] == [rf.metric_id for rf in second_filters]
    assert [rf.metric_id for rf in second_filters] == [_WORKDAYS_METRIC_ID]


def test_count_filter_statement_skips_typed_state_kind() -> None:
    """The single-source ``filter_statement`` must classify a state filter via
    ``filter_kind`` so a planner-set ``kind='state'`` is excluded from the
    rendered denominator (#1373 Stage 1; #1419 single-source).

    Before #1419 the count renderer (``execution.count._filter_statement``) and
    the validator (``quality._validation_helpers._count_filter_statement``) were
    byte-identical twins that had to agree, or the validator tripped a false
    ``filter_statement_mismatch``. #1419 collapsed both into the one public
    ``execution.filters.filter_statement``, so there is no longer a second
    function to compare against — the original intent (a typed-state filter is
    excluded from the count denominator statement) is now pinned against the
    single source directly. A state filter the planner typed via ``kind``
    (rather than the legacy ``field='state'`` token) must still be skipped.
    """

    from compass_backend.execution.filters import filter_statement

    # A state filter expressed via the TYPED kind on a non-"state" field token.
    typed_state_filter = FilterSpec(
        field="home state", operator="equals", value="CA", kind="state"
    )
    metric_filter = FilterSpec(
        field="teacher workdays", operator="greater_than", value=190
    )
    filters = [typed_state_filter, metric_filter]

    rendered = filter_statement(filters)

    # The typed-state filter is dropped; only the metric threshold survives.
    assert "home state" not in rendered, (
        "a typed-state filter (kind='state') must not leak into the rendered "
        "count denominator statement"
    )
    assert rendered == "value > 190"
