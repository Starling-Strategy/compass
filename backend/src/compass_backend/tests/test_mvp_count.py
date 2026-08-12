"""Tests for deterministic count and denominator artifacts."""

from __future__ import annotations

from typing import get_args

import pytest

from compass_backend.artifacts import (
    COUNT_ROW_TYPES,
    CategoricalCountRow,
    CountRow,
    CoveredUniverseCountRow,
    MethodologyRef,
    ThresholdCountRow,
)
from compass_backend.artifacts.results import CountKind as RowCountKind
from compass_backend.catalog import (
    CatalogAliasRecord,
    DistrictCandidate,
    DistrictResolution,
    MetricCandidate,
    normalize_district_name_for_resolution,
)
from compass_backend.catalog.resolution import TopicCandidate
from compass_backend.contracts import FilterSpec, MetricSpec, QueryPlan, SelectionSpec
from compass_backend.contracts.planning import CountKind as PlanCountKind
from compass_backend.execution import DeterministicQueryExecutor, MetricAnswerRow
from compass_backend.execution.types import (
    ExecutionClarification,
    ExecutionRefusal,
)
from compass_backend.quality import validate_result


def _methodology_codes(result: object) -> list[str]:
    return [ref.code for ref in result.methodology_codes]  # type: ignore[attr-defined]


def test_count_kind_alias_in_sync() -> None:
    """The plan-side and row-side CountKind Literals must stay structurally equal.

    The canonical declaration lives in compass_backend.contracts.planning;
    artifacts.results re-declares the same Literal locally to avoid a
    circular import (contracts/__init__.py eagerly imports chat.py which
    imports artifacts). The two Literal definitions are doctrine-tied: the
    planner sets QueryPlan.count_kind, execution produces CountRow.count_kind
    with the same discriminator, and validation walks both. This test fails
    loudly if a future PR drifts one definition out of sync with the other.
    """

    assert get_args(PlanCountKind) == get_args(RowCountKind)


def _count_plan() -> QueryPlan:
    return QueryPlan(
        operation="count",
        question=(
            "How many districts require formal observations at least 3 times "
            "per year for non-tenured teachers?"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(
                name=(
                    "Minimum number of formal observations per evaluation cycle "
                    "for non-tenured teachers"
                )
            )
        ],
        filters=[
            FilterSpec(
                field="value",
                operator="greater_than_or_equal",
                value=3,
            )
        ],
    )


class FakeCountRepository:
    """Fake repository for count execution tests."""

    def __init__(
        self,
        *,
        metrics: list[MetricCandidate],
        districts: list[DistrictCandidate],
        rows: list[MetricAnswerRow],
        aliases: list[CatalogAliasRecord] | None = None,
        topics: list[TopicCandidate] | None = None,
    ) -> None:
        self.metrics = metrics
        self.districts = districts
        self.rows = rows
        self.aliases = aliases or []
        self.topics = topics or []
        self.metric_queries: list[str] = []
        self.fetched_metric_ids: list[int] = []
        self.topic_queries: list[str] = []

    async def search_topics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[TopicCandidate]:
        # Mirror the real repo: topic_name LIKE '%query%' (topic name CONTAINS
        # the query) — so a query longer than the topic name does NOT match,
        # exercising the _topic_search_variants fallback.
        self.topic_queries.append(query)
        normalized = query.casefold()
        return [
            topic for topic in self.topics if normalized in topic.topic_name.casefold()
        ][:limit]

    async def fetch_topic_metric_candidates(
        self,
        topics: list[TopicCandidate],
        *,
        limit: int = 10,
    ) -> list[MetricCandidate]:
        return self.metrics[:limit]

    async def search_metrics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MetricCandidate]:
        self.metric_queries.append(query)
        normalized = query.casefold()
        return [
            metric
            for metric in self.metrics
            if normalized in metric.name.casefold()
            or metric.name.casefold() in normalized
        ][:limit]

    async def fetch_metrics_by_ids(
        self,
        metric_ids: list[int],
    ) -> list[MetricCandidate]:
        self.metric_queries.append(
            "ids:" + ",".join(str(metric_id) for metric_id in metric_ids)
        )
        metrics_by_id = {metric.metric_id: metric for metric in self.metrics}
        return [
            metrics_by_id[metric_id]
            for metric_id in metric_ids
            if metric_id in metrics_by_id
        ]

    async def search_catalog_aliases(
        self,
        alias: str,
        *,
        entity_types: set[str],
    ) -> list[CatalogAliasRecord]:
        normalized = normalize_district_name_for_resolution(alias)
        return [
            record
            for record in self.aliases
            if record.entity_type in entity_types
            and record.active
            and record.normalized_alias == normalized
        ]

    async def list_covered_districts(
        self,
        *,
        states: set[str] | None = None,
    ) -> list[DistrictCandidate]:
        state_filter = {state.upper() for state in states or set()}
        return [
            district
            for district in self.districts
            if not state_filter or (district.state or "").upper() in state_filter
        ]

    async def resolve_districts(
        self,
        names: list[str],
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        return DistrictResolution(resolved=self.districts)

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 5,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        return self.districts[:limit]

    async def fetch_metric_answer_rows(
        self,
        *,
        metric_id: int,
        academic_year: str,
    ) -> list[MetricAnswerRow]:
        self.fetched_metric_ids.append(metric_id)
        return [
            row.model_copy(update={"academic_year": academic_year})
            for row in self.rows
            if row.metric_id == metric_id
        ]

    async def fetch_reviewed_district_ids(
        self,
        *,
        academic_year: str,
        district_ids: set[int],
    ) -> set[int]:
        return {
            row.district_id
            for row in self.rows
            if row.academic_year == academic_year and row.district_id in district_ids
        }

    async def fetch_recent_metric_answer_rows(
        self,
        *,
        metric_id: int,
        before_academic_year: str,
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        return []


def _answer_row(
    district_id: int,
    district_name: str,
    value: object,
    *,
    metric_id: int,
    metric_name: str = "Selected metric",
    state: str = "CA",
) -> MetricAnswerRow:
    return MetricAnswerRow(
        district_id=district_id,
        district_name=district_name,
        state=state,
        metric_id=metric_id,
        metric_name=metric_name,
        value=value,
        academic_year="2024 - 2025",
    )


def _approved_metric_alias(
    alias: str,
    *,
    metric_ids: list[int],
) -> CatalogAliasRecord:
    return CatalogAliasRecord(
        alias=alias,
        normalized_alias=normalize_district_name_for_resolution(alias),
        entity_type="metric_bundle",
        resolution_status="approved",
        canonical_ids=[str(metric_id) for metric_id in metric_ids],
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
    )


@pytest.mark.asyncio
async def test_count_threshold_filter_returns_denominator_and_qualifying_districts() -> None:
    repository = FakeCountRepository(
        metrics=[
            MetricCandidate(
                metric_id=39,
                name=(
                    "Minimum number of formal observations per evaluation cycle "
                    "for non-tenured teachers"
                ),
                answer_type="numeric",
            )
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "3", metric_id=39),
            _answer_row(2, "Bravo", "2", metric_id=39),
            _answer_row(3, "Charlie", "INA", metric_id=39),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_count_plan())

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_count"
    assert len(outcome.result.rows) == 1
    row = outcome.result.rows[0]
    assert isinstance(row, COUNT_ROW_TYPES)
    assert row.metric_id == 39
    assert row.count == 1
    assert row.denominator == 2
    assert row.percent == 50.0  # C3 #1415: grounded share, 1 of 2 = 50%
    assert row.qualifying_district_ids == [1]
    # #393 selection-vs-presentation: the row retains its selected POPULATION
    # (the covered districts with data — the denominator members), not just the
    # matching subset, so a "who are the <denominator>?" follow-up can present it.
    assert row.denominator_district_ids == [1, 2]
    assert row.filter_statement == "value >= 3"
    assert row.display_value == "1 of 2 covered districts"
    assert outcome.result.coverage_frame is not None
    assert outcome.result.coverage_frame.universe_count == 3
    assert outcome.result.coverage_frame.real_data_count == 2
    assert _methodology_codes(outcome.result) == [
        "count_denominator_current_reviewed_rows",
    ]
    assert outcome.result.source_notes == []


@pytest.mark.asyncio
async def test_state_scoped_count_validates_aggregate_selection_metadata() -> None:
    plan = QueryPlan(
        operation="count",
        question="Starting salary comparison for Texas - what districts do you have?",
        selection=SelectionSpec(scope="all_covered_districts", states=["TX"]),
        metrics=[MetricSpec(name="starting salary")],
    )
    repository = FakeCountRepository(
        metrics=[
            MetricCandidate(
                metric_id=89,
                name="starting salary",
                answer_type="numeric",
            )
        ],
        districts=[
            DistrictCandidate(district_id=101, district_name="Austin ISD", state="TX"),
            DistrictCandidate(district_id=102, district_name="Dallas ISD", state="TX"),
            DistrictCandidate(
                district_id=201,
                district_name="Los Angeles USD",
                state="CA",
            ),
        ],
        rows=[
            _answer_row(101, "Austin ISD", "61000", metric_id=89, state="TX"),
            _answer_row(102, "Dallas ISD", "62000", metric_id=89, state="TX"),
            _answer_row(201, "Los Angeles USD", "70000", metric_id=89, state="CA"),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_count"
    assert outcome.result.selection is not None
    assert outcome.result.selection.states == ["TX"]
    assert [district.state for district in outcome.result.selection.districts] == [
        "TX",
        "TX",
    ]
    row = outcome.result.rows[0]
    assert isinstance(row, COUNT_ROW_TYPES)
    assert row.state is None
    assert row.count == 2
    assert row.denominator == 2
    assert row.qualifying_district_ids == [101, 102]

    report = validate_result(plan, outcome.result, authority=outcome.authority)

    assert report.valid is True
    assert report.findings == []


@pytest.mark.asyncio
async def test_count_metric_bundle_returns_one_count_row_per_metric() -> None:
    repository = FakeCountRepository(
        metrics=[
            MetricCandidate(
                metric_id=232,
                name="Does the district cover 100% of employees' health insurance premium?",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=233,
                name="What percent of the employees' health insurance premium does the district cover?",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=234,
                name=(
                    "Maximum portion of the employee's dependents' health insurance "
                    "premium paid by the employer"
                ),
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=235,
                name="Dollar cap for portion of health insurance premium covered by employer",
                answer_type="text",
            ),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "Yes", metric_id=232),
            _answer_row(2, "Bravo", "No", metric_id=232),
            _answer_row(1, "Alpha", "100%", metric_id=233),
            _answer_row(2, "Bravo", "INA", metric_id=233),
            _answer_row(1, "Alpha", "$100", metric_id=234),
            _answer_row(2, "Bravo", "N/A", metric_id=234),
            _answer_row(1, "Alpha", "$50", metric_id=235),
            _answer_row(2, "Bravo", "$75", metric_id=235),
        ],
        aliases=[
            _approved_metric_alias(
                "health benefits",
                metric_ids=[232, 233, 234, 235],
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="count",
            question="How many districts have reviewed health benefits data?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="health benefits")],
        )
    )

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_count"
    assert [(row.metric_id, row.count, row.denominator) for row in outcome.result.rows] == [
        (232, 2, 2),
        (233, 1, 1),
        (234, 1, 1),
        (235, 2, 2),
    ]
    assert all(isinstance(row, COUNT_ROW_TYPES) for row in outcome.result.rows)


@pytest.mark.asyncio
async def test_count_metric_bundle_unresolved_with_candidates_clarifies() -> None:
    """Regression (#1248 SELECT-R4): the count metric-bundle path
    (_resolve_plan_metrics) must clarify with the real candidates when a phrase
    surfaces multiple candidates but does not resolve cleanly, rather than emit
    a generic "could not resolve every requested metric" dead-end.

    Construction: the approved alias bundle includes a missing id (999), so the
    bundle does not resolve cleanly; the phrase "premium" still matches several
    real catalog metrics, so MetricBundleResolution.candidates is multi-candidate.
    """

    repository = FakeCountRepository(
        metrics=[
            MetricCandidate(
                metric_id=232,
                name="Does the district cover 100% of the health premium?",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=233,
                name="What percent of the health premium does the district cover?",
                answer_type="text",
            ),
        ],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[],
        aliases=[
            _approved_metric_alias("premium", metric_ids=[232, 999]),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="count",
            question="How many districts cover the premium?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="premium")],
        )
    )

    assert isinstance(outcome, ExecutionClarification), (
        f"Expected a candidate-listing clarification, got {type(outcome).__name__}: "
        f"{outcome.message}"
    )
    assert outcome.clarification is not None
    assert outcome.clarification.missing_fields == ["metric"]
    assert len(outcome.clarification.candidates) >= 2
    assert "could not resolve every requested metric" not in outcome.message


@pytest.mark.asyncio
async def test_categorical_count_unresolved_with_candidates_clarifies() -> None:
    """Regression (#1248 SELECT-R4): _resolve_categorical_count_metrics must
    clarify with the real candidates when the grouping field surfaces multiple
    candidates but does not resolve cleanly — not emit a generic "could not
    resolve an approved categorical field" dead-end.
    """

    repository = FakeCountRepository(
        metrics=[
            MetricCandidate(
                metric_id=232,
                name="Does the district cover the full health premium?",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=233,
                name="What percent of the health premium does the district cover?",
                answer_type="text",
            ),
        ],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[],
        aliases=[
            _approved_metric_alias("premium", metric_ids=[232, 999]),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="count",
            count_kind="categorical_value_count",
            question="How many districts fall into each premium category?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="premium", role="grouping")],
        )
    )

    assert isinstance(outcome, ExecutionClarification), (
        f"Expected a candidate-listing clarification, got {type(outcome).__name__}: "
        f"{outcome.message}"
    )
    assert outcome.clarification is not None
    assert outcome.clarification.missing_fields == ["metric"]
    assert len(outcome.clarification.candidates) >= 2
    assert "could not resolve an approved categorical field" not in outcome.message


@pytest.mark.asyncio
async def test_categorical_count_zero_candidate_still_refuses() -> None:
    """A grouping field with no candidates at all keeps the deterministic
    categorical-count refusal — clarification only fires with real candidates.
    """

    repository = FakeCountRepository(
        metrics=[
            MetricCandidate(
                metric_id=232, name="Collective bargaining status", answer_type="text"
            )
        ],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[],
        aliases=[],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="count",
            count_kind="categorical_value_count",
            question="How many districts fall into each widget category?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="nonexistent widget field", role="grouping")],
        )
    )

    assert isinstance(outcome, ExecutionRefusal), (
        f"Expected refusal for a zero-candidate grouping field, got "
        f"{type(outcome).__name__}"
    )
    assert "could not resolve an approved categorical field" in outcome.message


@pytest.mark.asyncio
async def test_health_benefits_type_count_returns_categorical_distribution() -> None:
    repository = FakeCountRepository(
        metrics=[
            MetricCandidate(
                metric_id=232,
                name="Does the district cover 100% of employees' health insurance premium?",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=233,
                name=(
                    "What percent of the employees' health insurance premium "
                    "does the district cover?"
                ),
                answer_type="numeric",
            ),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
            DistrictCandidate(district_id=4, district_name="Delta", state="CA"),
            DistrictCandidate(district_id=5, district_name="Echo", state="CA"),
            DistrictCandidate(district_id=6, district_name="Foxtrot", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "Yes", metric_id=232),
            _answer_row(2, "Bravo", "No", metric_id=232),
            _answer_row(3, "Charlie", "Yes", metric_id=232),
            _answer_row(4, "Delta", "Issue not addressed", metric_id=232),
            _answer_row(5, "Echo", "Unavailable", metric_id=232),
            _answer_row(1, "Alpha", "100%", metric_id=233),
            _answer_row(2, "Bravo", "95%", metric_id=233),
        ],
        aliases=[
            _approved_metric_alias(
                "health benefits",
                metric_ids=[232, 233],
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="count",
        count_kind="categorical_value_count",
        question=(
            "How many districts offer each type of health benefits? "
            "Use Gold, Platinum, and Bronze if needed."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="health benefits", role="grouping"),
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_count"
    rows = outcome.result.rows
    assert all(isinstance(row, COUNT_ROW_TYPES) for row in rows)
    assert [(row.category, row.count, row.denominator) for row in rows] == [
        ("Yes", 2, 6),
        ("No", 1, 6),
        ("Issue not addressed", 1, 6),
        ("Unavailable", 1, 6),
        ("Not reviewed", 1, 6),
    ]
    assert {row.count_kind for row in rows} == {"categorical_value_count"}
    assert [row.qualifying_district_ids for row in rows] == [
        [1, 3],
        [2],
        [4],
        [5],
        [6],
    ]
    assert all(">=" not in row.filter_statement for row in rows)
    assert "Gold" not in {row.category for row in rows}
    assert "Platinum" not in {row.category for row in rows}
    assert outcome.result.coverage_frame is not None
    assert outcome.result.coverage_frame.universe_count == 6
    assert outcome.result.coverage_frame.real_data_count == 3
    assert outcome.result.coverage_frame.not_reviewed_count == 2
    assert outcome.result.chart is not None
    assert _methodology_codes(outcome.result) == [
        "categorical_count_grouped_current_values",
        "categorical_count_missing_unavailable_separate",
    ]
    assert outcome.result.source_notes == []
    # #1514 D12: charts hold answer buckets only — the "Unavailable" and
    # "Not reviewed" buckets stay in result.rows (full record) but never
    # become chart points; their district counts are narrated instead.
    assert [(point.label, point.value) for point in outcome.result.chart.points] == [
        ("Yes", 2.0),
        ("No", 1.0),
        ("Issue not addressed", 1.0),
    ]
    assert outcome.result.csv_export is not None
    assert outcome.result.csv_export.rows[0]["category"] == "Yes"
    assert outcome.result.csv_export.rows[0]["percent"] == 33.3

    report = validate_result(plan, outcome.result, authority=outcome.authority)

    assert report.valid is True
    assert report.findings == []


def test_validation_rejects_categorical_count_sum_mismatch() -> None:
    result = _count_result(
        CategoricalCountRow(
            metric_id=232,
            metric_name="Does the district cover 100% of employees' health insurance premium?",
            value="Yes",
            category="Yes",
            display_value="1 of 3 covered districts",
            academic_year="2024 - 2025",
            count=1,
            denominator=3,
            percent=33.333333,
            filter_statement="group by reviewed value",
            qualifying_district_ids=[1],
            coverage_state="covered",
            coverage_display="1 of 3 covered districts",
            coverage_reason="categorical_value_count",
        )
    )

    report = validate_result(
        QueryPlan(
            operation="count",
            question="Tell me how many districts offer each type of health benefits",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="health benefits type", role="grouping")],
        ),
        result,
    )

    assert report.valid is False
    assert "denominator_mismatch" in [finding.code for finding in report.findings]


@pytest.mark.asyncio
async def test_covered_district_universe_count_uses_resolved_selection() -> None:
    repository = FakeCountRepository(
        metrics=[],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="TX"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="OH"),
        ],
        rows=[],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="count",
        count_kind="covered_universe_count",
        question="How many districts do you have data for?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_count"
    assert repository.fetched_metric_ids == []
    assert outcome.result.selection is not None
    assert [district.district_id for district in outcome.result.selection.districts] == [
        1,
        2,
        3,
    ]
    row = outcome.result.rows[0]
    assert isinstance(row, COUNT_ROW_TYPES)
    assert row.count == 3
    assert row.denominator == 3
    assert row.qualifying_district_ids == [1, 2, 3]
    assert row.filter_statement == "covered district universe"
    assert row.source == "coverage_state"
    assert outcome.result.coverage_frame is not None
    assert outcome.result.coverage_frame.universe_count == 3
    assert outcome.result.coverage_frame.real_data_count == 3
    assert _methodology_codes(outcome.result) == [
        "covered_universe_selection_count"
    ]
    assert outcome.result.source_notes == []

    report = validate_result(plan, outcome.result, authority=outcome.authority)

    assert report.valid is True
    assert report.findings == []


@pytest.mark.asyncio
async def test_state_limited_covered_district_universe_count_counts_selection() -> None:
    repository = FakeCountRepository(
        metrics=[],
        districts=[
            DistrictCandidate(district_id=10, district_name="Cleveland", state="OH"),
            DistrictCandidate(district_id=11, district_name="Columbus", state="OH"),
            DistrictCandidate(district_id=12, district_name="Austin", state="TX"),
        ],
        rows=[],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="count",
        count_kind="covered_universe_count",
        question="How many districts do you cover in Ohio?",
        selection=SelectionSpec(scope="all_covered_districts", states=["OH"]),
        metrics=[],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_count"
    assert repository.fetched_metric_ids == []
    assert outcome.result.selection is not None
    assert outcome.result.selection.states == ["OH"]
    assert [
        (district.district_id, district.state)
        for district in outcome.result.selection.districts
    ] == [(10, "OH"), (11, "OH")]
    row = outcome.result.rows[0]
    assert isinstance(row, COUNT_ROW_TYPES)
    assert row.count == 2
    assert row.denominator == 2
    assert row.qualifying_district_ids == [10, 11]
    assert row.filter_statement == "covered district universe"

    report = validate_result(plan, outcome.result, authority=outcome.authority)

    assert report.valid is True
    assert report.findings == []


@pytest.mark.asyncio
async def test_count_threshold_filter_accepts_plus_suffixed_numeric_string() -> None:
    repository = FakeCountRepository(
        metrics=[
            MetricCandidate(
                metric_id=39,
                name=(
                    "Minimum number of formal observations per evaluation cycle "
                    "for non-tenured teachers"
                ),
                answer_type="numeric",
            )
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "3", metric_id=39),
            _answer_row(2, "Bravo", "4", metric_id=39),
            _answer_row(3, "Charlie", "2", metric_id=39),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = _count_plan().model_copy(
        update={
            "filters": [
                FilterSpec(
                    field="value",
                    operator="greater_than_or_equal",
                    value="3+",
                )
            ]
        }
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    row = outcome.result.rows[0]
    assert isinstance(row, COUNT_ROW_TYPES)
    assert row.count == 2
    assert row.denominator == 3
    assert row.qualifying_district_ids == [1, 2]
    assert row.filter_statement == "value >= 3+"


def test_validation_rejects_count_denominator_mismatch() -> None:
    result = _count_result(
        ThresholdCountRow(
            metric_id=39,
            metric_name=(
                "Minimum number of formal observations per evaluation cycle "
                "for non-tenured teachers"
            ),
            value=1,
            display_value="1 of 99 covered districts",
            academic_year="2024 - 2025",
            count=1,
            denominator=99,
            filter_statement="value >= 3",
            qualifying_district_ids=[1],
            coverage_state="covered",
            coverage_display="1 of 99 covered districts",
            coverage_reason="count_summary",
        )
    )

    report = validate_result(_count_plan(), result)

    assert report.valid is False
    assert "denominator_mismatch" in [finding.code for finding in report.findings]
    assert "denominator" in report.dimensions_checked


def test_count_result_csv_export_includes_count_specific_artifact_fields() -> None:
    result = _count_result(
        ThresholdCountRow(
            metric_id=39,
            metric_name=(
                "Minimum number of formal observations per evaluation cycle "
                "for non-tenured teachers"
            ),
            value=1,
            display_value="1 of 2 covered districts",
            academic_year="2024 - 2025",
            count=1,
            denominator=2,
            filter_statement="value >= 3",
            qualifying_district_ids=[1],
            coverage_state="covered",
            coverage_display="1 of 2 covered districts",
            coverage_reason="count_summary",
        )
    )

    assert result.csv_export is not None
    assert result.csv_export.columns == [
        "metric_id",
        "metric_name",
        "count_kind",
        "category",
        "count",
        "denominator",
        "percent",
        "filter_statement",
        "qualifying_district_ids",
        "source",
        "display_value",
        "academic_year",
        "value",
        "coverage_state",
        "coverage_display",
        "coverage_reason",
        "coverage_qualifier",
        "coverage_prior_academic_year",
        "coverage_prior_display_value",
        "citation_markers",
        "source_document",
        "source_document_type",
        "source_page",
        "source_url",
        "source_valid_from",
        "source_valid_to",
        "source_urls",
    ]
    assert result.csv_export.rows[0]["count_kind"] == "threshold_count"
    assert result.csv_export.rows[0]["category"] == ""
    assert result.csv_export.rows[0]["count"] == 1
    assert result.csv_export.rows[0]["coverage_reason"] == "count_summary"
    assert result.csv_export.rows[0]["coverage_prior_academic_year"] is None
    assert result.csv_export.rows[0]["denominator"] == 2
    assert result.csv_export.rows[0]["percent"] == ""
    assert result.csv_export.rows[0]["filter_statement"] == "value >= 3"
    assert result.csv_export.rows[0]["qualifying_district_ids"] == "1"
    assert result.csv_export.rows[0]["source"] == "policy_answer"


def test_covered_universe_count_csv_export_does_not_invent_policy_sources() -> None:
    result = _count_result(
        CoveredUniverseCountRow(
            metric_id=0,
            metric_name="Covered district universe",
            value=2,
            display_value="2 covered districts",
            academic_year="2024 - 2025",
            count=2,
            denominator=2,
            filter_statement="covered district universe",
            qualifying_district_ids=[10, 11],
            source="coverage_state",
            coverage_state="covered",
            coverage_display="2 covered districts",
            coverage_reason="covered_universe_count",
        )
    )

    assert result.citations == []
    assert result.csv_export is not None
    csv_row = result.csv_export.rows[0]
    assert csv_row["qualifying_district_ids"] == "10 11"
    assert csv_row["source"] == "coverage_state"
    assert csv_row["source_document"] == ""
    assert csv_row["source_document_type"] == ""
    assert csv_row["source_url"] == ""
    assert csv_row["source_urls"] == ""
    assert csv_row["citation_markers"] == ""


def _count_result(row: CountRow):
    from compass_backend.artifacts import CoverageFrame, MetricCountResult

    return MetricCountResult(
        rows=[row],
        total_considered=3,
        excluded_count=1,
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=3,
            real_data_count=2,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=2 / 3,
        ),
        order_statement="Counted qualifying districts for selected metrics.",
        source_notes=[],
        methodology_codes=[
            MethodologyRef(code="count_denominator_current_reviewed_rows"),
        ],
    )


def test_multi_metric_count_narrates_district_tallies_not_cell_counts() -> None:
    """#1514 review blockers A2+A3 (reviewer repro): 3 districts x 2 metrics
    with one district unreviewed on BOTH metrics. The coverage frame counts 2
    not-reviewed CELLS (count frames are per-metric-label), but the narrated
    number must be the 1 DISTRICT — read from the serialized
    ``district_coverage`` summary the builder computes from its per-district
    label sets."""

    from compass_backend.artifacts import ResultSelection, SelectedDistrict
    from compass_backend.execution.count import build_metric_count_result
    from compass_backend.rendering.composer import compose_response

    plan = QueryPlan(
        operation="count",
        question="How many of these districts meet both thresholds?",
        selection=SelectionSpec(
            scope="named_districts", districts=["Alpha", "Bravo", "Charlie"]
        ),
        metrics=[MetricSpec(name="Metric A"), MetricSpec(name="Metric B")],
    )
    selection = ResultSelection(
        scope="named_districts",
        districts=[
            SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
            SelectedDistrict(district_id=2, district_name="Bravo", state="CA"),
            SelectedDistrict(district_id=3, district_name="Charlie", state="CA"),
        ],
    )
    metric_a = MetricCandidate(metric_id=11, name="Metric A", answer_type="numeric")
    metric_b = MetricCandidate(metric_id=22, name="Metric B", answer_type="numeric")
    def rows_for(metric_id: int, metric_name: str) -> list[MetricAnswerRow]:
        return [
            _answer_row(1, "Alpha", "3", metric_id=metric_id, metric_name=metric_name),
            _answer_row(2, "Bravo", "4", metric_id=metric_id, metric_name=metric_name),
            # Charlie is unreviewed on BOTH metrics -> 2 not-reviewed cells.
            _answer_row(
                3, "Charlie", None, metric_id=metric_id, metric_name=metric_name
            ),
        ]

    result = build_metric_count_result(
        plan,
        [(metric_a, rows_for(11, "Metric A")), (metric_b, rows_for(22, "Metric B"))],
        selection=selection,
        academic_year="2024 - 2025",
    )

    assert result.coverage_frame is not None
    assert result.coverage_frame.not_reviewed_count == 2  # cells, per metric label
    assert result.district_coverage is not None
    assert result.district_coverage.districts_asked == 3
    assert result.district_coverage.districts_with_current_data == 2
    assert result.district_coverage.districts_not_reviewed == 1  # the district truth

    composition = compose_response(plan, result)

    assert (
        "NCTQ hasn't reviewed 1 district in this selection for 2024 - 2025 yet."
        in composition.lead_lines
    )
    assert not any(
        "2 districts in this selection" in line for line in composition.lead_lines
    )


@pytest.mark.asyncio
async def test_topic_coverage_count_end_to_end_passes_validation() -> None:
    """373: topic-coverage count executes AND passes grounding validation.

    Reproduces the live swallow locally (filter_statement / denominator /
    coverage_frame_count mismatches) so the validator threading can be fixed
    without a deploy. All 3 districts have >=1 answer across the topic's two
    metrics -> "3 of 3 covered districts have Evaluation data". The fake
    search_topics matches a topic name as a substring of the query, so the
    longer "Evaluation policies" phrase exercises the variant fallback too.
    """
    repository = FakeCountRepository(
        metrics=[
            MetricCandidate(metric_id=1001, name="Eval metric A", answer_type="text"),
            MetricCandidate(metric_id=1002, name="Eval metric B", answer_type="text"),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        # Alpha + Bravo have >=1 answer; Charlie has NO evaluation answer at all
        # -> count (2) < denominator (3 covered universe). This is the real 373
        # shape (live: 121 of 133) and the case the denominator/coverage_frame
        # validators must accept: the denominator is the UNIVERSE, not the
        # reviewed count.
        rows=[
            _answer_row(1, "Alpha", "Yes", metric_id=1001),
            _answer_row(2, "Bravo", "No", metric_id=1001),
        ],
        topics=[TopicCandidate(topic_id=1, topic_name="Evaluation")],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="count",
        count_kind="topic_coverage_count",
        question="How many districts have data addressing Evaluation policies?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Evaluation policies")],
    )

    outcome = await executor.execute(plan)
    assert outcome.result is not None, getattr(outcome, "message", outcome)
    assert outcome.result.result_type == "metric_count"
    row = outcome.result.rows[0]
    assert row.count == 2
    assert row.denominator == 3
    assert "have Evaluation data" in row.display_value

    report = validate_result(plan, outcome.result, authority=outcome.authority)
    assert [finding.code for finding in report.findings] == [], [
        (finding.code, finding.metadata) for finding in report.findings
    ]
