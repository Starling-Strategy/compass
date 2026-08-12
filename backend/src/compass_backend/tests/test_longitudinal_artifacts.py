"""Tests for historical and longitudinal Compass artifacts."""

from __future__ import annotations

import json

import pytest

from compass_backend.api.sse import chat_response_sse_payloads
from compass_backend.artifacts import (
    CitationRef,
    CitationSource,
    MetricLookupResult,
    MetricValueRow,
    ResultSelection,
    SelectedDistrict,
    coverage_frame_from_labels,
    coverage_label_for_answer,
)
from compass_backend.catalog import (
    CatalogAliasRecord,
    DistrictCandidate,
    DistrictResolution,
    MetricCandidate,
    normalize_district_name_for_resolution,
)
from compass_backend.contracts import (
    ChatResponse,
    DirectResponse,
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    SessionState,
)
from compass_backend.contracts.planning import TemporalSpec
from compass_backend.execution import DeterministicQueryExecutor, MetricAnswerRow
from compass_backend.planning.temporal import (
    academic_year_window_ending_at,
    normalize_academic_year,
    normalize_plan_temporal_intent,
)
from compass_backend.quality import validate_result
from compass_backend.rendering import render_response


CURRENT_YEAR = "2024 - 2025"
ANCHORAGE_DISTRICT_ID = 3
STARTING_SALARY_METRIC_ID = 89
STARTING_SALARY_METRIC_NAME = (
    "BA lane, first step teacher salary schedule amount"
)


class FakeLongitudinalRepository:
    """Fake catalog and answer repository for historical/trend execution tests."""

    def __init__(
        self,
        *,
        rows: list[MetricAnswerRow] | None = None,
        districts: list[DistrictCandidate] | None = None,
        aliases: list[CatalogAliasRecord] | None = None,
    ) -> None:
        self.metrics = [
            MetricCandidate(
                metric_id=STARTING_SALARY_METRIC_ID,
                name=STARTING_SALARY_METRIC_NAME,
                answer_type="numeric",
            )
        ]
        self.rows = rows or []
        self.districts = districts or [_anchorage_district()]
        self.aliases = aliases or []
        self.answer_year_calls: list[str] = []
        self.answer_years_calls: list[list[str]] = []
        self.available_year_calls: list[tuple[int, set[int]]] = []
        self.recent_calls: list[tuple[int, str, set[int]]] = []

    async def search_metrics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MetricCandidate]:
        normalized_query = query.casefold()
        return [
            metric
            for metric in self.metrics
            if normalized_query in metric.name.casefold()
            or metric.name.casefold() in normalized_query
        ][:limit]

    async def fetch_metrics_by_ids(
        self,
        metric_ids: list[int],
    ) -> list[MetricCandidate]:
        by_id = {metric.metric_id: metric for metric in self.metrics}
        return [by_id[metric_id] for metric_id in metric_ids if metric_id in by_id]

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

    async def resolve_districts(
        self,
        names: list[str],
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        state_filter = {state.upper() for state in states or set()}
        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        for name in names:
            normalized = normalize_district_name_for_resolution(name)
            matches = [
                district
                for district in self.districts
                if normalize_district_name_for_resolution(district.district_name)
                == normalized
                and (not state_filter or (district.state or "").upper() in state_filter)
            ]
            if len(matches) == 1:
                resolved.append(matches[0])
            else:
                unresolved.append(name)
        return DistrictResolution(resolved=resolved, unresolved=unresolved)

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

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 5,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        return (await self.list_covered_districts(states=states))[:limit]

    async def select_districts_by_enrollment_range(
        self,
        *,
        states: set[str] | None = None,
        min_enrollment: int | None = None,
        max_enrollment: int | None = None,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        return await self.list_covered_districts(states=states)

    async def fetch_metric_answer_rows(
        self,
        *,
        metric_id: int,
        academic_year: str,
    ) -> list[MetricAnswerRow]:
        self.answer_year_calls.append(academic_year)
        return [
            row
            for row in self.rows
            if row.metric_id == metric_id and row.academic_year == academic_year
        ]

    async def fetch_metric_answer_rows_for_year(
        self,
        *,
        metric_id: int,
        academic_year: str,
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        self.answer_year_calls.append(academic_year)
        return [
            row
            for row in self.rows
            if row.metric_id == metric_id
            and row.academic_year == academic_year
            and row.district_id in district_ids
        ]

    async def fetch_metric_answer_rows_for_years(
        self,
        *,
        metric_id: int,
        academic_years: list[str],
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        self.answer_years_calls.append(list(academic_years))
        year_set = set(academic_years)
        return [
            row
            for row in self.rows
            if row.metric_id == metric_id
            and row.academic_year in year_set
            and row.district_id in district_ids
        ]

    async def list_available_academic_years(
        self,
        *,
        metric_id: int,
        district_ids: set[int],
    ) -> list[str]:
        self.available_year_calls.append((metric_id, set(district_ids)))
        return sorted(
            {
                row.academic_year
                for row in self.rows
                if row.metric_id == metric_id and row.district_id in district_ids
            }
        )

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
        self.recent_calls.append((metric_id, before_academic_year, set(district_ids)))
        return []


def test_academic_year_normalization_and_windows_are_canonical() -> None:
    assert normalize_academic_year("2018-19") == "2018 - 2019"
    assert normalize_academic_year("2018 / 2019") == "2018 - 2019"
    assert normalize_academic_year("2024 - 2025") == "2024 - 2025"
    assert academic_year_window_ending_at("2024 - 2025", 5) == [
        "2020 - 2021",
        "2021 - 2022",
        "2022 - 2023",
        "2023 - 2024",
        "2024 - 2025",
    ]


def test_temporal_normalization_promotes_specific_year_and_trend_intent() -> None:
    specific = normalize_plan_temporal_intent(
        _lookup_plan(
            question=(
                "What was the starting salary for a first year teacher with a "
                "bachelors degree in Anchorage in 2018-19?"
            )
        ),
        message=(
            "What was the starting salary for a first year teacher with a "
            "bachelors degree in Anchorage in 2018-19?"
        ),
        current_academic_year=CURRENT_YEAR,
    )
    assert specific.operation == "lookup"
    assert specific.temporal.intent == "specific_year"
    assert specific.temporal.academic_year == "2018 - 2019"

    trend = normalize_plan_temporal_intent(
        _lookup_plan(
            question="How have teacher starting salaries changed in Anchorage over the past 5 years?",
            temporal=TemporalSpec(intent="relative_trend_window", window_years=5),
        ),
        message="How have teacher starting salaries changed in Anchorage over the past 5 years?",
        current_academic_year=CURRENT_YEAR,
    )
    assert trend.operation == "trend"
    assert trend.temporal.intent == "relative_trend_window"
    assert trend.temporal.academic_years == academic_year_window_ending_at(
        CURRENT_YEAR,
        5,
    )

    comparison = normalize_plan_temporal_intent(
        _lookup_plan(question="Compare Anchorage starting salary in 2018-19 vs 2024-25."),
        message="Compare Anchorage starting salary in 2018-19 vs 2024-25.",
        current_academic_year=CURRENT_YEAR,
    )
    assert comparison.temporal.intent == "comparison_years"
    assert comparison.temporal.academic_years == ["2018 - 2019", "2024 - 2025"]

    ranged = normalize_plan_temporal_intent(
        _lookup_plan(
            question="Show Anchorage starting salary from 2018-19 through 2020-21.",
            temporal=TemporalSpec(
                intent="year_range",
                start_academic_year="2018-19",
                end_academic_year="2020-21",
            ),
        ),
        message="Show Anchorage starting salary from 2018-19 through 2020-21.",
        current_academic_year=CURRENT_YEAR,
    )
    assert ranged.temporal.intent == "year_range"
    assert ranged.temporal.academic_years == [
        "2018 - 2019",
        "2019 - 2020",
        "2020 - 2021",
    ]


def test_temporal_normalization_requires_typed_planner_temporal_for_relative_windows() -> None:
    plan = normalize_plan_temporal_intent(
        _lookup_plan(
            question=(
                "How have teacher starting salaries changed in Anchorage over "
                "the past 5 years?"
            )
        ),
        message=(
            "How have teacher starting salaries changed in Anchorage over the "
            "past 5 years?"
        ),
        current_academic_year=CURRENT_YEAR,
    )

    assert plan.operation == "lookup"
    assert plan.temporal.intent == "current"
    assert plan.temporal.academic_year is None
    assert plan.temporal.window_years is None
    assert plan.temporal.academic_years == []


def test_temporal_normalization_requires_typed_planner_temporal_for_year_ranges() -> None:
    plan = normalize_plan_temporal_intent(
        _lookup_plan(
            question=(
                "Show Anchorage starting salary from 2018-19 through 2020-21."
            )
        ),
        message="Show Anchorage starting salary from 2018-19 through 2020-21.",
        current_academic_year=CURRENT_YEAR,
    )

    assert plan.operation == "trend"
    assert plan.temporal.intent == "comparison_years"
    assert plan.temporal.academic_years == ["2018 - 2019", "2020 - 2021"]


@pytest.mark.anyio
async def test_specific_historical_lookup_uses_requested_year_without_current_fallback() -> None:
    repository = FakeLongitudinalRepository(
        rows=[
            _salary_row("$50,213", "2018 - 2019", answer_id=1819),
            _salary_row("$56,823", CURRENT_YEAR, answer_id=2425),
        ],
        aliases=[_metric_alias("first year teacher with a bachelors degree")],
    )
    executor = DeterministicQueryExecutor(
        repository,
        current_academic_year=CURRENT_YEAR,
    )
    plan = _lookup_plan(
        question=(
            "What was the starting salary for a first year teacher with a "
            "bachelors degree in Anchorage in 2018-19?"
        ),
        metric_name="first year teacher with a bachelors degree",
        temporal=TemporalSpec(
            intent="specific_year",
            academic_year="2018 - 2019",
        ),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert repository.answer_year_calls == ["2018 - 2019"]
    assert repository.recent_calls == []
    row = outcome.result.rows[0]
    assert row.academic_year == "2018 - 2019"
    assert row.display_value == "$50,213"
    assert row.citation_markers
    assert all(citation.academic_year == "2018 - 2019" for citation in outcome.result.citations)
    assert "$56,823" not in outcome.result.model_dump_json()


@pytest.mark.anyio
async def test_missing_historical_lookup_does_not_fall_back_to_current_year() -> None:
    repository = FakeLongitudinalRepository(
        rows=[
            _salary_row("$56,823", CURRENT_YEAR, answer_id=2425),
        ],
        aliases=[_metric_alias("first year teacher with a bachelors degree")],
    )
    executor = DeterministicQueryExecutor(
        repository,
        current_academic_year=CURRENT_YEAR,
    )
    plan = _lookup_plan(
        metric_name="first year teacher with a bachelors degree",
        temporal=TemporalSpec(
            intent="specific_year",
            academic_year="2018 - 2019",
        ),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    row = outcome.result.rows[0]
    assert row.academic_year == "2018 - 2019"
    assert row.coverage_state == "not_reviewed"
    assert row.value is None
    assert "$56,823" not in row.display_value
    assert repository.answer_year_calls == ["2018 - 2019"]
    assert repository.recent_calls == []


@pytest.mark.anyio
async def test_five_year_trend_returns_chronological_artifact_with_visible_gap() -> None:
    repository = FakeLongitudinalRepository(
        rows=[
            _salary_row("$52,242", "2020 - 2021", answer_id=2021),
            _salary_row("$53,287", "2021 - 2022", answer_id=2122),
            _salary_row("$54,086", "2022 - 2023", answer_id=2223),
            _salary_row("$56,823", "2024 - 2025", answer_id=2425),
        ],
        aliases=[_metric_alias("starting salary")],
    )
    executor = DeterministicQueryExecutor(
        repository,
        current_academic_year=CURRENT_YEAR,
    )
    plan = _lookup_plan(
        question="How have teacher starting salaries changed in Anchorage over the past 5 years?",
        metric_name="starting salary",
        output=OutputSpec(format="chart"),
        temporal=TemporalSpec(
            intent="relative_trend_window",
            window_years=5,
            academic_years=academic_year_window_ending_at(CURRENT_YEAR, 5),
        ),
    ).model_copy(update={"operation": "trend"})

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_trend"
    assert [ref.code for ref in outcome.result.methodology_codes] == [
        "trend_chronological_coverage_gaps",
        "trend_deltas_from_artifact_values",
    ]
    assert outcome.result.source_notes == []
    assert [row.academic_year for row in outcome.result.rows] == [
        "2020 - 2021",
        "2021 - 2022",
        "2022 - 2023",
        "2023 - 2024",
        "2024 - 2025",
    ]
    gap_row = outcome.result.rows[3]
    assert gap_row.coverage_state == "not_reviewed"
    assert gap_row.value is None
    assert gap_row.citation_markers == []
    assert repository.answer_years_calls == [
        [
            "2020 - 2021",
            "2021 - 2022",
            "2022 - 2023",
            "2023 - 2024",
            "2024 - 2025",
        ]
    ]
    assert outcome.result.csv_export is not None
    assert "academic_year" in outcome.result.csv_export.columns
    assert "delta_value" in outcome.result.csv_export.columns
    assert outcome.result.chart is not None
    assert outcome.result.chart.artifact_type == "line_chart"
    assert outcome.result.chart.series[0].points[-1].label == "2024 - 2025"
    events = list(
        chat_response_sse_payloads(
            ChatResponse(
                message="Trend response.",
                turn=_direct_turn(),
                session=SessionState(session_id="session-1", turn_count=1),
                snapshot_id="snapshot-1",
                result=outcome.result,
            )
        )
    )
    data_payload = json.loads(
        next(event["data"] for event in events if event["event"] == "data")
    )
    chart_payload = json.loads(
        next(event["data"] for event in events if event["event"] == "chart")
    )
    assert data_payload["csv_export"]["columns"] == outcome.result.csv_export.columns
    assert chart_payload["artifact_type"] == "line_chart"
    assert chart_payload["series"][0]["points"][-1]["label"] == "2024 - 2025"

    validation = validate_result(plan, outcome.result)
    assert validation.valid
    manifest = render_response(plan, outcome.result, validation)
    assert "| Academic year | District | State |" in manifest.body
    assert "2023 - 2024" in manifest.body
    assert "$56,823" in manifest.body


@pytest.mark.anyio
async def test_trend_row_budget_requires_narrowing_before_fetching_rows() -> None:
    districts = [
        DistrictCandidate(
            district_id=index,
            district_name=f"District {index}",
            state="CA",
        )
        for index in range(1, 8)
    ]
    repository = FakeLongitudinalRepository(districts=districts)
    executor = DeterministicQueryExecutor(
        repository,
        current_academic_year=CURRENT_YEAR,
    )
    plan = QueryPlan(
        operation="trend",
        question="Show five-year starting salary trends for all districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=STARTING_SALARY_METRIC_NAME)],
        temporal=TemporalSpec(
            intent="relative_trend_window",
            window_years=5,
            academic_years=academic_year_window_ending_at(CURRENT_YEAR, 5),
        ),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is None
    assert "narrow" in outcome.message.casefold()
    assert repository.answer_year_calls == []
    assert repository.answer_years_calls == []


def test_temporal_validation_rejects_current_year_leakage_for_specific_year() -> None:
    plan = _lookup_plan(
        temporal=TemporalSpec(
            intent="specific_year",
            academic_year="2018 - 2019",
        ),
    )
    labels = [
        coverage_label_for_answer(
            "$56,823",
            district_name="Anchorage School District",
            metric_name=STARTING_SALARY_METRIC_NAME,
            academic_year=CURRENT_YEAR,
        )
    ]
    result = MetricLookupResult(        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(
                    district_id=ANCHORAGE_DISTRICT_ID,
                    district_name="Anchorage School District",
                    state="AK",
                )
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=ANCHORAGE_DISTRICT_ID,
                district_name="Anchorage School District",
                state="AK",
                metric_id=STARTING_SALARY_METRIC_ID,
                metric_name=STARTING_SALARY_METRIC_NAME,
                value="$56,823",
                display_value="$56,823",
                academic_year=CURRENT_YEAR,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$56,823",
                coverage_reason="answer_value",
            )
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Anchorage Salary Schedule, 2024-2025",
                url="https://example.org/anchorage-2024.pdf",
                academic_year=CURRENT_YEAR,
                district_id=ANCHORAGE_DISTRICT_ID,
            )
        ],
        coverage_frame=coverage_frame_from_labels(labels),
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up starting salary for selected districts.",
        source_notes=[],
    )

    validation = validate_result(plan, result)

    assert not validation.valid
    assert "requested_year_mismatch" in {finding.code for finding in validation.findings}


def _lookup_plan(
    *,
    question: str = "What was the starting salary in Anchorage?",
    metric_name: str = STARTING_SALARY_METRIC_NAME,
    temporal: TemporalSpec | None = None,
    output: OutputSpec | None = None,
) -> QueryPlan:
    payload = {
        "operation": "lookup",
        "question": question,
        "selection": SelectionSpec(
            scope="named_districts",
            districts=["Anchorage School District"],
        ),
        "metrics": [MetricSpec(name=metric_name)],
        "output": output or OutputSpec(),
    }
    if temporal is not None:
        payload["temporal"] = temporal
    return QueryPlan(
        **payload,
    )


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=1.0,
        direct_response=DirectResponse(
            message="Trend response.",
            reason="Test fixture.",
        ),
    )


def _salary_row(
    value: str,
    academic_year: str,
    *,
    answer_id: int,
) -> MetricAnswerRow:
    return MetricAnswerRow(
        answer_id=answer_id,
        district_id=ANCHORAGE_DISTRICT_ID,
        district_name="Anchorage School District",
        state="AK",
        metric_id=STARTING_SALARY_METRIC_ID,
        metric_name=STARTING_SALARY_METRIC_NAME,
        value=value,
        academic_year=academic_year,
        citations=[
            CitationSource(
                source_name=(
                    f"Anchorage School District. AK. ({academic_year.replace(' - ', '-')}). "
                    "Salary Schedule. pp. 1-2"
                ),
                source_url=f"https://example.org/anchorage-{academic_year[:4]}.pdf",
                document_type="Salary Schedule",
                academic_year=academic_year,
                district_id=ANCHORAGE_DISTRICT_ID,
                citation_order=1,
            )
        ],
    )


def _anchorage_district() -> DistrictCandidate:
    return DistrictCandidate(
        district_id=ANCHORAGE_DISTRICT_ID,
        district_name="Anchorage School District",
        state="AK",
    )


def _metric_alias(alias: str) -> CatalogAliasRecord:
    return CatalogAliasRecord(
        alias=alias,
        normalized_alias=normalize_district_name_for_resolution(alias),
        entity_type="metric",
        resolution_status="approved",
        canonical_id=str(STARTING_SALARY_METRIC_ID),
        source="test",
        provenance="test fixture",
        review_status="approved",
    )
