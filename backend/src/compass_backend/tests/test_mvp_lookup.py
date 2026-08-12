"""Tests for exact district metric lookup."""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from compass_backend.api.app import create_app
from compass_backend.artifacts import (
    CitationRef,
    CitationSource,
    CoverageFrame,
    FilterPrevalenceSummary,
    MethodologyRef,
    MetricLookupResult,
    MetricValueRow,
    ResultSelection,
    ResultSet,
    SelectedDistrict,
)
from compass_backend.catalog import (
    CatalogAliasRecord,
    CatalogResolver,
    DistrictCandidate,
    DistrictResolution,
    MetricCandidate,
    NCESFieldCandidate,
    normalize_district_name_for_resolution,
)
from compass_backend.catalog.adjudication import (
    CatalogAdjudicationDecision,
    create_catalog_adjudicator_agent,
)
from compass_backend.contracts import (
    FilterSpec,
    LimitSpec,
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    SortSpec,
    ValidationReport,
)
from compass_backend.contracts.validation import ValidationAuthority
from compass_backend.config import Settings
from compass_backend.execution import (
    DeterministicQueryExecutor,
    ExecutionOutcome,
    ExecutionSuccess,
    MetricAnswerRow,
)
from compass_backend.execution.types import (
    ExecutionClarification,
    ExecutionRefusal,
)
from compass_backend.execution._helpers import _metric_clarification
from compass_backend.execution.selection import requested_states
from compass_backend.quality import validate_result
from compass_backend.rendering import attach_adjacent_metrics_manifest_metadata, render_response
from compass_backend.session import InMemorySessionStore


def _create_offline_app(*args, app_settings: Settings | None = None, **kwargs):
    """Keep fake-executor route tests isolated from live catalog dependencies."""

    base = app_settings or Settings(session_store_backend="memory")
    return create_app(
        *args,
        app_settings=base.model_copy(
            update={
                "catalog_recall_shadow_enabled": False,
                "catalog_resolver_recall_enabled": False,
            }
        ),
        **kwargs,
    )


def _methodology_codes(result: object) -> list[str]:
    return [ref.code for ref in result.methodology_codes]  # type: ignore[attr-defined]


def test_metric_clarification_removes_duplicate_candidates_by_metric_id() -> None:
    minimum_bonus = MetricCandidate(
        metric_id=171,
        name="Minimum annual performance pay bonus, if eligible",
    )
    maximum_bonus = MetricCandidate(
        metric_id=172,
        name="Maximum annual performance pay bonus, if eligible",
    )

    clarification = _metric_clarification(
        "performance pay bonus amount",
        operation="lookup",
        candidates=[minimum_bonus, maximum_bonus, minimum_bonus, maximum_bonus],
    )

    assert clarification.candidates == [
        "Minimum annual performance pay bonus, if eligible",
        "Maximum annual performance pay bonus, if eligible",
    ]


class FakeLookupRepository:
    """Fake catalog and answer repository for lookup execution tests."""

    def __init__(
        self,
        *,
        metrics: list[MetricCandidate] | None = None,
        rows: list[MetricAnswerRow] | None = None,
        recent_rows: list[MetricAnswerRow] | None = None,
        districts: list[DistrictCandidate] | None = None,
        aliases: list[CatalogAliasRecord] | None = None,
        ambiguous: dict[str, list[DistrictCandidate]] | None = None,
        reviewed_district_ids: set[int] | None = None,
        enrollment_by_district_id: dict[int, int] | None = None,
        nces_fields: list[NCESFieldCandidate] | None = None,
        renderer_notes: dict[str, str] | None = None,
    ) -> None:
        self.metrics = (
            [
                MetricCandidate(
                    metric_id=4321,
                    name="Collective bargaining status",
                    answer_type="text",
                )
            ]
            if metrics is None
            else metrics
        )
        self.rows = rows or []
        self.recent_rows = recent_rows or []
        self.districts = districts or []
        self.aliases = aliases or []
        self.ambiguous = ambiguous or {}
        self.reviewed_district_ids = reviewed_district_ids
        self.enrollment_by_district_id = enrollment_by_district_id or {}
        self.nces_fields = nces_fields or [
            NCESFieldCandidate(
                field_key="enrollment",
                label="Enrollment",
                data_type="integer",
                description="Total district enrollment.",
            )
        ]
        self.renderer_notes = renderer_notes or {}
        self.metric_queries: list[str] = []
        self.alias_queries: list[tuple[str, set[str]]] = []
        self.fetched_metric_ids: list[int] = []
        self.resolved_district_names: list[list[str]] = []
        self.largest_district_calls: list[tuple[set[str] | None, int, str]] = []
        self.enrollment_range_calls: list[
            tuple[set[str] | None, int | None, int | None, str]
        ] = []

    async def search_metrics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MetricCandidate]:
        self.metric_queries.append(query)
        normalized = query.casefold()
        matches = [
            metric
            for metric in self.metrics
            if normalized in metric.name.casefold()
            or metric.name.casefold() in normalized
        ]
        return matches[:limit]

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
        self.alias_queries.append((alias, entity_types))
        normalized = normalize_district_name_for_resolution(alias)
        return [
            record
            for record in self.aliases
            if record.entity_type in entity_types
            and record.active
            and record.normalized_alias == normalized
        ]

    async def search_nces_fields(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[NCESFieldCandidate]:
        return self.nces_fields[:limit]

    async def fetch_renderer_notes(self, note_keys: list[str]) -> list[object]:
        from compass_backend.catalog import RendererNote

        return [
            RendererNote(
                note_key=key,
                note_text=self.renderer_notes[key],
                source="test",
                provenance="test fixture",
                scenario_ids=["24"],
                review_status="approved",
                active=True,
            )
            for key in note_keys
            if key in self.renderer_notes
        ]

    async def fetch_metric_answer_rows(
        self,
        *,
        metric_id: int,
        academic_year: str,
    ) -> list[MetricAnswerRow]:
        self.fetched_metric_ids.append(metric_id)
        return [
            row.model_copy(update={"metric_id": metric_id, "academic_year": academic_year})
            for row in self.rows
            if row.metric_id == metric_id
        ]

    async def fetch_reviewed_district_ids(
        self,
        *,
        academic_year: str,
        district_ids: set[int],
    ) -> set[int]:
        if self.reviewed_district_ids is not None:
            return self.reviewed_district_ids & district_ids
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
        return [
            row.model_copy(update={"metric_id": metric_id})
            for row in self.recent_rows
            if row.metric_id == metric_id and row.district_id in district_ids
        ]

    async def resolve_districts(
        self,
        names: list[str],
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        self.resolved_district_names.append(names)
        state_filter = {state.upper() for state in states or set()}
        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        ambiguous: dict[str, list[DistrictCandidate]] = {}

        for name in names:
            if name in self.ambiguous:
                candidates = [
                    candidate
                    for candidate in self.ambiguous[name]
                    if not state_filter or (candidate.state or "").upper() in state_filter
                ]
            else:
                normalized = normalize_district_name_for_resolution(name)
                candidates = [
                    district
                    for district in self.districts
                    if normalize_district_name_for_resolution(district.district_name)
                    == normalized
                    and (not state_filter or (district.state or "").upper() in state_filter)
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

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 5,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        self.largest_district_calls.append((states, limit, academic_year))
        state_filter = {state.upper() for state in states or set()}
        candidates = [
            district
            for district in self.districts
            if not state_filter or (district.state or "").upper() in state_filter
        ]
        return candidates[:limit]

    async def select_districts_by_enrollment_range(
        self,
        *,
        states: set[str] | None = None,
        min_enrollment: int | None = None,
        max_enrollment: int | None = None,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        self.enrollment_range_calls.append(
            (states, min_enrollment, max_enrollment, academic_year)
        )
        state_filter = {state.upper() for state in states or set()}
        candidates = [
            district
            for district in self.districts
            if not state_filter or (district.state or "").upper() in state_filter
        ]
        return [
            district
            for district in candidates
            if (
                min_enrollment is None
                or self.enrollment_by_district_id.get(district.district_id, 0)
                >= min_enrollment
            )
            and (
                max_enrollment is None
                or self.enrollment_by_district_id.get(district.district_id, 0)
                <= max_enrollment
            )
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


def _approved_metric_alias(
    alias: str,
    *,
    metric_id: int | None = None,
    metric_ids: list[int] | None = None,
    entity_type: str = "metric",
    metadata: dict[str, object] | None = None,
) -> CatalogAliasRecord:
    return CatalogAliasRecord(
        alias=alias,
        normalized_alias=normalize_district_name_for_resolution(alias),
        entity_type=entity_type,
        resolution_status="approved",
        canonical_id=str(metric_id) if metric_id is not None else None,
        canonical_ids=[str(value) for value in metric_ids or []],
        metadata=metadata or {},
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
    )


def _ambiguous_metric_alias(
    alias: str,
    *,
    metric_ids: list[int],
    entity_type: str = "metric",
    metadata: dict[str, object] | None = None,
) -> CatalogAliasRecord:
    return CatalogAliasRecord(
        alias=alias,
        normalized_alias=normalize_district_name_for_resolution(alias),
        entity_type=entity_type,
        resolution_status="ambiguous",
        candidate_refs=[{"metric_id": metric_id} for metric_id in metric_ids],
        metadata=metadata or {},
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
    )


class FakeQueryExecutor:
    """Fake deterministic executor for chat route tests."""

    def __init__(self, outcome: ExecutionOutcome) -> None:
        self.outcome = outcome
        self.plans: list[QueryPlan] = []

    async def execute(self, plan: QueryPlan) -> ExecutionOutcome:
        self.plans.append(plan)
        return self.outcome


def _source(
    source_name: str,
    *,
    source_url: str = "https://example.org/source.pdf",
    citation_order: int | None = 1,
    district_id: int = 1,
) -> CitationSource:
    return CitationSource(
        source_name=source_name,
        source_url=source_url,
        document_type=None,
        academic_year="2024 - 2025",
        district_id=district_id,
        citation_order=citation_order,
    )


def _answer_row(
    district_id: int,
    district_name: str,
    value: object,
    *,
    answer_id: int | None = None,
    citations: list[CitationSource] | None = None,
    state: str = "CA",
    metric_id: int = 4321,
    metric_name: str = "Collective bargaining status",
) -> MetricAnswerRow:
    return MetricAnswerRow(
        answer_id=answer_id,
        district_id=district_id,
        district_name=district_name,
        state=state,
        metric_id=metric_id,
        metric_name=metric_name,
        value=value,
        academic_year="2024 - 2025",
        citations=citations or [],
    )


def _lookup_plan(
    *,
    scope: str = "named_districts",
    districts: list[str] | None = None,
    states: list[str] | None = None,
    sort: SortSpec | None = None,
    limit: LimitSpec | None = None,
    metrics: list[MetricSpec] | None = None,
) -> QueryPlan:
    if districts is None:
        districts = ["Charlie", "Alpha"] if scope == "named_districts" else []
    return QueryPlan(
        operation="lookup",
        question="What is collective bargaining status for these districts?",
        selection=SelectionSpec(
            scope=scope,
            districts=districts,
            states=states or [],
        ),
        metrics=metrics or [MetricSpec(name="Collective bargaining status")],
        sort=sort,
        limit=limit,
    )


def _lookup_result() -> ResultSet:
    return MetricLookupResult(        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                SelectedDistrict(district_id=3, district_name="Charlie", state="CA"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=4321,
                metric_name="Collective bargaining status",
                value="Yes",
                display_value="Yes",
                academic_year="2024 - 2025",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="Yes",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=3,
                district_name="Charlie",
                state="CA",
                metric_id=4321,
                metric_name="Collective bargaining status",
                value="No",
                display_value="No",
                academic_year="2024 - 2025",
                citation_markers=[2],
                coverage_state="covered",
                coverage_display="No",
                coverage_reason="answer_value",
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha District Contract, 2024-2025",
                url="https://example.org/alpha.pdf",
                page_number=1,
                page_ref="p. 1",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            ),
            CitationRef(
                marker=2,
                title="Charlie District Contract, 2024-2025",
                url="https://example.org/charlie.pdf",
                page_number=3,
                page_ref="p. 3",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=3,
            ),
        ],
        total_considered=2,
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=2,
            real_data_count=2,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement=(
            "Looked up Collective bargaining status for selected districts, "
            "alphabetical by district name."
        ),
        source_notes=[],
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
            MethodologyRef(code="lookup_default_district_order"),
        ],
    )


@pytest.mark.asyncio
async def test_lookup_metric_value_filter_accepts_boolean_yes_no_metric() -> None:
    full_health_premium = MetricCandidate(
        metric_id=232,
        name="Does the district cover 100% of employees' health insurance premium?",
        answer_type="text",
    )
    districts = [
        DistrictCandidate(district_id=1, district_name="Alpha", state="TX"),
        DistrictCandidate(district_id=2, district_name="Bravo", state="TX"),
        DistrictCandidate(district_id=3, district_name="Charlie", state="TX"),
    ]
    repository = FakeLookupRepository(
        metrics=[full_health_premium],
        districts=districts,
        rows=[
            _answer_row(
                1,
                "Alpha",
                "Yes",
                state="TX",
                metric_id=232,
                metric_name=full_health_premium.name,
            ),
            _answer_row(
                2,
                "Bravo",
                "No",
                state="TX",
                metric_id=232,
                metric_name=full_health_premium.name,
            ),
            _answer_row(
                3,
                "Charlie",
                "Issue not addressed",
                state="TX",
                metric_id=232,
                metric_name=full_health_premium.name,
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="lookup",
            question="Show districts where the district covers the full health premium.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name=full_health_premium.name)],
            filters=[
                FilterSpec(
                    field=full_health_premium.name,
                    operator="equals",
                    value=True,
                )
            ],
        )
    )

    assert isinstance(outcome, ExecutionSuccess)
    assert [row.district_name for row in outcome.result.rows] == ["Alpha"]


@pytest.mark.asyncio
async def test_rank_with_comparison_metric_returns_top_primary_rows_with_comparison_cells() -> None:
    salary = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    workdays = MetricCandidate(
        metric_id=69,
        name="Total teacher workdays",
        answer_type="numeric",
    )
    districts = [
        DistrictCandidate(district_id=1, district_name="Alpha", state="TX"),
        DistrictCandidate(district_id=2, district_name="Bravo", state="TX"),
        DistrictCandidate(district_id=3, district_name="Charlie", state="TX"),
    ]
    repository = FakeLookupRepository(
        metrics=[salary, workdays],
        districts=districts,
        rows=[
            _answer_row(
                1,
                "Alpha",
                "$50,000",
                state="TX",
                metric_id=89,
                metric_name=salary.name,
            ),
            _answer_row(
                2,
                "Bravo",
                "$70,000",
                state="TX",
                metric_id=89,
                metric_name=salary.name,
            ),
            _answer_row(
                3,
                "Charlie",
                "$60,000",
                state="TX",
                metric_id=89,
                metric_name=salary.name,
            ),
            _answer_row(
                1,
                "Alpha",
                "184",
                state="TX",
                metric_id=69,
                metric_name=workdays.name,
            ),
            _answer_row(
                2,
                "Bravo",
                "190",
                state="TX",
                metric_id=69,
                metric_name=workdays.name,
            ),
            _answer_row(
                3,
                "Charlie",
                "188",
                state="TX",
                metric_id=69,
                metric_name=workdays.name,
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="rank",
        question=(
            "Show me the 2 highest starting salaries and how many days "
            "teachers work in those districts."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name=salary.name),
            MetricSpec(name=workdays.name, role="comparison"),
        ],
        sort=SortSpec(field=salary.name, direction="desc"),
        limit=LimitSpec(count=2, kind="top"),
    )
    outcome = await executor.execute(plan)

    assert isinstance(outcome, ExecutionSuccess)
    assert outcome.result.result_type == "metric_lookup"
    assert [(row.district_name, row.metric_id) for row in outcome.result.rows] == [
        ("Bravo", 89),
        ("Bravo", 69),
        ("Charlie", 89),
        ("Charlie", 69),
    ]
    assert "Ranked by" in outcome.result.order_statement
    report = validate_result(
        plan,
        outcome.result,
        authority=outcome.authority,
    )
    assert "sort_order_mismatch" not in [finding.code for finding in report.findings]


def _valid_report() -> ValidationReport:
    return ValidationReport(
        valid=True,
        dimensions_checked=[
            "selection",
            "metric",
            "sort_order",
            "coverage_state",
            "citation_coverage",
            "numeric_token_provenance",
        ],
        findings=[],
    )


@pytest.mark.asyncio
async def test_lookup_named_district_metric_returns_metric_lookup_result() -> None:
    repository = FakeLookupRepository(
        districts=[
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[
            _answer_row(
                3,
                "Charlie",
                "No",
                answer_id=103,
                citations=[
                    _source(
                        "Charlie District. CA. (2024-2025). Contract. p. 3.",
                        source_url="https://example.org/charlie.pdf",
                        district_id=3,
                    )
                ],
            ),
            _answer_row(
                1,
                "Alpha",
                "Yes",
                answer_id=101,
                citations=[
                    _source(
                        "Alpha District. CA. (2024-2025). Contract. p. 1.",
                        source_url="https://example.org/alpha.pdf",
                        district_id=1,
                    )
                ],
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_lookup_plan())

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert [row.district_name for row in outcome.result.rows] == ["Alpha", "Charlie"]
    assert [row.display_value for row in outcome.result.rows] == ["Yes", "No"]
    assert [row.coverage_state for row in outcome.result.rows] == ["covered", "covered"]
    assert [row.citation_markers for row in outcome.result.rows] == [[1], [2]]
    assert outcome.result.selection is not None
    assert outcome.result.selection.scope == "named_districts"
    assert _methodology_codes(outcome.result) == [
        "citation_answer_level_preferred_source_fallback",
        "lookup_default_district_order",
    ]
    assert not any(
        note == "Looked up compass.navigator_answers using resolved catalog IDs."
        for note in outcome.result.source_notes
    )
    assert repository.metric_queries == ["Collective bargaining status"]
    assert repository.fetched_metric_ids == [4321]
    assert repository.resolved_district_names == [["Charlie", "Alpha"]]


@pytest.mark.asyncio
async def test_lookup_texas_five_largest_resolves_selection_before_metric_lookup() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=89,
                name=(
                    "Annual base salary for a first year teacher with a "
                    "bachelor's degree"
                ),
                answer_type="numeric",
            )
        ],
        aliases=[_approved_metric_alias("starting salary", metric_id=89)],
        districts=[
            DistrictCandidate(district_id=3, district_name="Charlie ISD", state="TX"),
            DistrictCandidate(district_id=1, district_name="Alpha ISD", state="TX"),
            DistrictCandidate(district_id=9, district_name="Outside USD", state="CA"),
        ],
        rows=[
            _answer_row(
                3,
                "Charlie ISD",
                "$60,000",
                metric_id=89,
                metric_name=(
                    "Annual base salary for a first year teacher with a "
                    "bachelor's degree"
                ),
                state="TX",
            ),
            _answer_row(
                1,
                "Alpha ISD",
                "$50,000",
                metric_id=89,
                metric_name=(
                    "Annual base salary for a first year teacher with a "
                    "bachelor's degree"
                ),
                state="TX",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            scope="largest_districts",
            states=["TX"],
            limit=LimitSpec(count=2),
            districts=[],
            metrics=[MetricSpec(name="starting salary")],
        )
    )

    assert outcome.result is not None
    assert outcome.result.selection is not None
    assert outcome.result.selection.scope == "largest_districts"
    assert [district.district_id for district in outcome.result.selection.districts] == [
        3,
        1,
    ]
    assert [row.district_name for row in outcome.result.rows] == [
        "Alpha ISD",
        "Charlie ISD",
    ]
    assert repository.largest_district_calls == [({"TX"}, 2, "2024 - 2025")]
    assert repository.resolved_district_names == []
    assert repository.metric_queries == [
        "starting salary",
        "ids:89",
    ]
    assert repository.fetched_metric_ids == [89]


@pytest.mark.asyncio
async def test_lookup_national_largest_selection_is_deterministic() -> None:
    repository = FakeLookupRepository(
        districts=[
            DistrictCandidate(district_id=2, district_name="Bravo", state="NY"),
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[
            _answer_row(2, "Bravo", "No", answer_id=102, state="NY"),
            _answer_row(1, "Alpha", "Yes", answer_id=101, state="CA"),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            scope="largest_districts",
            districts=[],
            limit=LimitSpec(count=2),
        )
    )

    assert outcome.result is not None
    assert outcome.result.selection is not None
    assert outcome.result.selection.scope == "largest_districts"
    assert outcome.result.selection.states == []
    assert [row.district_name for row in outcome.result.rows] == ["Alpha", "Bravo"]
    assert repository.largest_district_calls == [(None, 2, "2024 - 2025")]


@pytest.mark.asyncio
async def test_lookup_multiple_metrics_returns_comparison_rows_without_new_result_type() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=4321,
                name="Collective bargaining status",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=9876,
                name="Average teacher starting salary",
                answer_type="numeric",
            ),
        ],
        districts=[
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "Yes", answer_id=101, metric_id=4321),
            _answer_row(3, "Charlie", "No", answer_id=103, metric_id=4321),
            _answer_row(
                1,
                "Alpha",
                "$50,000",
                answer_id=201,
                metric_id=9876,
                metric_name="Average teacher starting salary",
            ),
            _answer_row(
                3,
                "Charlie",
                "$60,000",
                answer_id=203,
                metric_id=9876,
                metric_name="Average teacher starting salary",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            metrics=[
                MetricSpec(name="Collective bargaining status"),
                    MetricSpec(
                        name="Average teacher starting salary",
                        role="comparison",
                    ),
            ]
        )
    )

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert [
        (row.district_name, row.metric_name, row.display_value)
        for row in outcome.result.rows
    ] == [
        ("Alpha", "Average teacher starting salary", "$50,000"),
        ("Alpha", "Collective bargaining status", "Yes"),
        ("Charlie", "Average teacher starting salary", "$60,000"),
        ("Charlie", "Collective bargaining status", "No"),
    ]
    assert repository.metric_queries == [
        "Collective bargaining status",
        "Average teacher starting salary",
    ]
    assert repository.fetched_metric_ids == [4321, 9876]
    assert outcome.result.coverage_frame is not None
    assert outcome.result.coverage_frame.universe_count == 4
    assert outcome.result.coverage_frame.real_data_count == 4


@pytest.mark.asyncio
async def test_lookup_tolerates_planner_primary_role_for_additional_metrics() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=4321,
                name="Collective bargaining status",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=9876,
                name="Average teacher starting salary",
                answer_type="numeric",
            ),
        ],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[
            _answer_row(1, "Alpha", "Yes", answer_id=101, metric_id=4321),
            _answer_row(
                1,
                "Alpha",
                "$50,000",
                answer_id=201,
                metric_id=9876,
                metric_name="Average teacher starting salary",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            districts=["Alpha"],
            metrics=[
                MetricSpec(name="Collective bargaining status"),
                MetricSpec(
                    name="Average teacher starting salary",
                    role="primary",
                ),
            ],
        )
    )

    assert outcome.result is not None
    assert [row.metric_id for row in outcome.result.rows] == [9876, 4321]
    assert outcome.authority is not None
    assert [metric.role for metric in outcome.authority.metrics] == [
        "primary",
        "comparison",
    ]


@pytest.mark.asyncio
async def test_explicit_bachelor_salary_chart_uses_resultset_rows_and_csv() -> None:
    metric_name = (
        "Annual base salary for a first year teacher with a bachelor's degree"
    )
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=89,
                name=metric_name,
                answer_type="numeric",
            )
        ],
        aliases=[
            _approved_metric_alias(
                "starting salaries for teachers with a bachelor's degree",
                metric_id=89,
            )
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "$50,000",
                answer_id=101,
                metric_id=89,
                metric_name=metric_name,
                citations=[_source("Alpha Salary Schedule.pdf", district_id=1)],
            ),
            _answer_row(
                2,
                "Bravo",
                "$60,000",
                answer_id=102,
                metric_id=89,
                metric_name=metric_name,
                citations=[_source("Bravo Salary Schedule.pdf", district_id=2)],
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question=(
            "Show me a chart of starting salaries for teachers with a bachelor's "
            "degree across all districts in your database."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salaries for teachers with a bachelor's degree")
        ],
        output=OutputSpec(format="chart"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    # Charts are suppressed for lookup results with fewer than 3 numeric points
    assert outcome.result.chart is None
    assert outcome.result.csv_export is not None
    assert [
        (csv_row["district_id"], csv_row["value"])
        for csv_row in outcome.result.csv_export.rows
    ] == [(row.district_id, row.value) for row in outcome.result.rows]


@pytest.mark.asyncio
async def test_non_numeric_chart_request_does_not_fetch_or_fabricate_chart() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=4321,
                name="Collective bargaining status",
                answer_type="text",
            )
        ],
        aliases=[
            _approved_metric_alias("collective bargaining status", metric_id=4321)
        ],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "Required",
                metric_id=4321,
                metric_name="Collective bargaining status",
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="Show me a chart of collective bargaining status across districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="collective bargaining status")],
        output=OutputSpec(format="chart"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is None
    assert outcome.clarification is None
    assert "could not resolve every requested metric" in outcome.message
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_prior_result_rows_chart_can_keep_mixed_metric_bundle_rows() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=198,
                name="Maximum number of annual paid sick days",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=201,
                name="Paid sick day allowance increases with years of service",
                answer_type="text",
            ),
        ],
        aliases=[
            _approved_metric_alias(
                "sick leave policy",
                metric_ids=[198, 201],
                entity_type="metric_bundle",
            )
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Denver Public Schools", state="CO"),
            DistrictCandidate(district_id=2, district_name="Aurora Public Schools", state="CO"),
        ],
        rows=[
            _answer_row(
                1,
                "Denver Public Schools",
                "10",
                metric_id=198,
                metric_name="Maximum number of annual paid sick days",
                state="CO",
            ),
            _answer_row(
                2,
                "Aurora Public Schools",
                "12",
                metric_id=198,
                metric_name="Maximum number of annual paid sick days",
                state="CO",
            ),
            _answer_row(
                1,
                "Denver Public Schools",
                "No",
                metric_id=201,
                metric_name="Paid sick day allowance increases with years of service",
                state="CO",
            ),
            _answer_row(
                2,
                "Aurora Public Schools",
                "No",
                metric_id=201,
                metric_name="Paid sick day allowance increases with years of service",
                state="CO",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="Can you show me the sick days comparison in a graph?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver Public Schools", "Aurora Public Schools"],
        ),
        inherit_selection_from="prior_result_rows",
        metrics=[MetricSpec(name="sick leave policy")],
        output=OutputSpec(format="chart"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    # Charts are suppressed when no series have >= 3 numeric points.
    # Metric 198 has 2 numeric points (Denver, Aurora), so it's suppressed.
    # Metric 201 is non-numeric, so no series created for it either.
    assert outcome.result.chart is None
    assert [row.metric_id for row in outcome.result.rows] == [198, 201, 198, 201]
    assert repository.fetched_metric_ids == [198, 201]


@pytest.mark.asyncio
async def test_scenario_42_split_performance_pay_metrics_produce_multi_series_chart() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=171,
                name="Minimum annual performance pay bonus, if eligible",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=172,
                name="Maximum annual performance pay bonus, if eligible",
                answer_type="numeric",
            ),
        ],
        aliases=[
            _approved_metric_alias(
                "minimum performance pay bonus amount",
                metric_id=171,
            ),
            _approved_metric_alias(
                "maximum performance pay bonus amount",
                metric_id=172,
            ),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "$500",
                metric_id=171,
                metric_name="Minimum annual performance pay bonus, if eligible",
            ),
            _answer_row(
                2,
                "Bravo",
                "$700",
                metric_id=171,
                metric_name="Minimum annual performance pay bonus, if eligible",
            ),
            _answer_row(
                1,
                "Alpha",
                "$5,000",
                metric_id=172,
                metric_name="Maximum annual performance pay bonus, if eligible",
            ),
            _answer_row(
                2,
                "Bravo",
                "$7,000",
                metric_id=172,
                metric_name="Maximum annual performance pay bonus, if eligible",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question=(
            "Show me a graph of the minimum and maximum performance pay bonus "
            "amounts across districts"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="minimum performance pay bonus amount"),
            MetricSpec(name="maximum performance pay bonus amount"),
        ],
        output=OutputSpec(format="chart"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    # Charts are suppressed when no series have >= 3 numeric points.
    # Metrics 171 and 172 each have 2 points (Alpha, Bravo), both < 3.
    assert outcome.result.chart is None


@pytest.mark.asyncio
async def test_lookup_metric_bundle_supports_all_covered_districts_and_chart_artifact() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=171,
                name="Minimum annual performance pay bonus, if eligible",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=172,
                name="Maximum annual performance pay bonus, if eligible",
                answer_type="numeric",
            ),
        ],
        aliases=[
            _approved_metric_alias(
                "minimum and maximum annual performance pay bonuses",
                metric_ids=[171, 172],
                entity_type="metric_bundle",
            )
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "$500",
                answer_id=101,
                metric_id=171,
                metric_name="Minimum annual performance pay bonus, if eligible",
            ),
            _answer_row(
                2,
                "Bravo",
                "INA",
                answer_id=102,
                metric_id=171,
                metric_name="Minimum annual performance pay bonus, if eligible",
            ),
            _answer_row(
                1,
                "Alpha",
                "$5,000",
                answer_id=201,
                metric_id=172,
                metric_name="Maximum annual performance pay bonus, if eligible",
            ),
            _answer_row(
                2,
                "Bravo",
                "$7,000",
                answer_id=202,
                metric_id=172,
                metric_name="Maximum annual performance pay bonus, if eligible",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    plan = QueryPlan(
        operation="lookup",
        question="What are the minimum and maximum annual performance pay bonuses?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="minimum and maximum annual performance pay bonuses")],
        output=OutputSpec(format="chart"),
    )
    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert outcome.result.selection is not None
    assert outcome.result.selection.scope == "all_covered_districts"
    assert [district.district_id for district in outcome.result.selection.districts] == [
        1,
        2,
    ]
    assert [
        (row.district_name, row.metric_id, row.display_value)
        for row in outcome.result.rows
    ] == [
        ("Alpha", 172, "$5,000"),
        ("Alpha", 171, "$500"),
        ("Bravo", 172, "$7,000"),
        ("Bravo", 171, "Issue not addressed in the documents reviewed."),
    ]
    # Charts are suppressed when no series have >= 3 numeric points.
    # Metric 172 has 2 points (Alpha, Bravo); metric 171 has 1 numeric + 1 non-numeric.
    assert outcome.result.chart is None
    assert repository.metric_queries == [
        "minimum and maximum annual performance pay bonuses",
        "ids:171,172",
    ]


@pytest.mark.asyncio
async def test_lookup_csv_export_populates_source_metadata_from_citations() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=69,
                name="Total contracted workdays per academic year",
                answer_type="numeric",
            )
        ],
        aliases=[_approved_metric_alias("teacher working hours", metric_id=69)],
        districts=[
            DistrictCandidate(
                district_id=150,
                district_name="Dallas Independent School District",
                state="TX",
            )
        ],
        rows=[
            _answer_row(
                150,
                "Dallas Independent School District",
                "187",
                answer_id=15069,
                metric_id=69,
                metric_name="Total contracted workdays per academic year",
                state="TX",
                citations=[
                    _source(
                        "Dallas Independent School District. TX. (2024-2025). Annual Calendar. p. 7.",
                        source_url="https://example.org/dallas-calendar.pdf",
                        district_id=150,
                    )
                ],
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            districts=["Dallas Independent School District"],
            metrics=[MetricSpec(name="teacher working hours")],
        )
    )

    assert outcome.result is not None
    csv_row = outcome.result.csv_export.rows[0]
    assert csv_row["source_document"] == "Dallas Independent School District Annual Calendar, 2024-2025"
    assert csv_row["source_document_type"] == "Annual Calendar"
    assert csv_row["source_page"] == "p. 7"
    assert csv_row["source_url"] == "https://example.org/dallas-calendar.pdf"
    assert csv_row["source_valid_from"] == "2024"
    assert csv_row["source_valid_to"] == "2025"


@pytest.mark.asyncio
async def test_lookup_multiple_metrics_unresolved_metric_returns_no_result() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=4321,
                name="Collective bargaining status",
                answer_type="text",
            )
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[_answer_row(1, "Alpha", "Yes", answer_id=101, metric_id=4321)],
    )
    missing_comparison_metric_ids = [
        repository.metrics[0].metric_id + offset for offset in (1, 2)
    ]
    repository.aliases = [
        _ambiguous_metric_alias(
            "starting salary",
            metric_ids=missing_comparison_metric_ids,
        )
    ]
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            districts=["Alpha"],
            metrics=[
                MetricSpec(name="Collective bargaining status"),
                MetricSpec(name="starting salary", role="comparison"),
            ],
        )
    )

    assert outcome.result is None
    assert "could not resolve every requested metric" in outcome.message
    assert repository.metric_queries == [
        "Collective bargaining status",
        "starting salary",
        "ids:" + ",".join(str(value) for value in missing_comparison_metric_ids),
    ]
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_lookup_honors_explicit_district_name_desc_sort() -> None:
    repository = FakeLookupRepository(
        districts=[
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "Yes", answer_id=101),
            _answer_row(3, "Charlie", "No", answer_id=103),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(sort=SortSpec(field="district_name", direction="desc"))
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Charlie", "Alpha"]
    assert "reverse alphabetical" in outcome.result.order_statement


@pytest.mark.asyncio
async def test_lookup_honors_explicit_value_desc_sort_for_numeric_rows() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=4321,
                name="Starting salary",
                answer_type="numeric",
            )
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "$50,000", metric_name="Starting salary"),
            _answer_row(3, "Charlie", "$60,000", metric_name="Starting salary"),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            sort=SortSpec(field="value", direction="desc"),
            metrics=[MetricSpec(name="Starting salary")],
        )
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Charlie", "Alpha"]
    assert "value, highest to lowest" in outcome.result.order_statement


@pytest.mark.asyncio
async def test_lookup_validation_accepts_value_sort_by_planner_metric_alias() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=89,
                name="Annual base salary for a first year teacher with a bachelor's degree",
                answer_type="numeric",
            )
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "$50,000",
                answer_id=101,
                citations=[_source("Alpha Salary Schedule.pdf", district_id=1)],
                metric_id=89,
                metric_name="Annual base salary for a first year teacher with a bachelor's degree",
            ),
            _answer_row(
                3,
                "Charlie",
                "$60,000",
                answer_id=103,
                citations=[_source("Charlie Salary Schedule.pdf", district_id=3)],
                metric_id=89,
                metric_name="Annual base salary for a first year teacher with a bachelor's degree",
            ),
        ],
        aliases=[
            _approved_metric_alias(
                "starting teacher salary",
                metric_id=89,
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = _lookup_plan(
        scope="all_covered_districts",
        districts=[],
        metrics=[MetricSpec(name="starting teacher salary")],
        sort=SortSpec(field="starting teacher salary", direction="desc"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    validation = validate_result(plan, outcome.result, authority=outcome.authority)
    assert validation.valid is True
    assert not [
        finding
        for finding in validation.findings
        if finding.code == "sort_order_mismatch"
    ]


@pytest.mark.asyncio
async def test_lookup_uses_enrollment_filters_as_selection_authority() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(metric_id=198, name="Maximum number of annual paid sick days"),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        enrollment_by_district_id={1: 10_000, 2: 20_000, 3: 30_000},
        rows=[
            _answer_row(
                1,
                "Alpha",
                "5",
                metric_id=198,
                metric_name="Maximum number of annual paid sick days",
            ),
            _answer_row(
                2,
                "Bravo",
                "8",
                metric_id=198,
                metric_name="Maximum number of annual paid sick days",
            ),
            _answer_row(
                3,
                "Charlie",
                "10",
                metric_id=198,
                metric_name="Maximum number of annual paid sick days",
            ),
        ],
        aliases=[
            _approved_metric_alias(
                "sick leave policy",
                metric_ids=[198],
                entity_type="metric_bundle",
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="what do districts with about 20,000-30,000 students do for sick leave?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="sick leave policy"),
            MetricSpec(name="enrollment", role="filter"),
        ],
        filters=[
            FilterSpec(field="enrollment", operator="greater_than_or_equal", value=20_000),
            FilterSpec(field="enrollment", operator="less_than_or_equal", value=30_000),
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Bravo", "Charlie"]
    assert repository.enrollment_range_calls == [
        (None, 20_000, 30_000, "2024 - 2025")
    ]
    assert repository.fetched_metric_ids == [198]


@pytest.mark.asyncio
async def test_lookup_normalizes_full_state_names_before_selection() -> None:
    repository = FakeLookupRepository(
        metrics=[MetricCandidate(metric_id=89, name="Starting salary")],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="TX"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "$50,000", state="TX", metric_id=89),
            _answer_row(2, "Bravo", "$60,000", state="CA", metric_id=89),
        ],
        aliases=[_approved_metric_alias("starting salary", metric_id=89)],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            scope="state",
            states=["Texas"],
            districts=[],
            metrics=[MetricSpec(name="starting salary")],
        )
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha"]


@pytest.mark.asyncio
async def test_lookup_applies_selection_states_for_all_covered_scope() -> None:
    repository = FakeLookupRepository(
        metrics=[MetricCandidate(metric_id=89, name="Starting salary")],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="TX"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "$50,000", state="TX", metric_id=89),
            _answer_row(2, "Bravo", "$60,000", state="CA", metric_id=89),
        ],
        aliases=[_approved_metric_alias("starting salary", metric_id=89)],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            scope="all_covered_districts",
            states=["Texas"],
            districts=[],
            metrics=[MetricSpec(name="starting salary")],
        )
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha"]


@pytest.mark.asyncio
async def test_lookup_preserves_state_scope_with_metric_value_filter() -> None:
    metric_phrase = "substantial performance pay maximum"
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=172,
                name="Maximum annual performance pay bonus, if eligible",
                answer_type="numeric",
            )
        ],
        districts=[
            DistrictCandidate(
                district_id=1,
                district_name="Cumberland County Schools",
                state="NC",
            ),
            DistrictCandidate(
                district_id=2,
                district_name="Dallas Independent School District",
                state="TX",
            ),
            DistrictCandidate(
                district_id=3,
                district_name="Buffalo School District",
                state="NY",
            ),
        ],
        rows=[
            _answer_row(
                1,
                "Cumberland County Schools",
                "$10,000",
                state="NC",
                metric_id=172,
                metric_name="Maximum annual performance pay bonus, if eligible",
            ),
            _answer_row(
                2,
                "Dallas Independent School District",
                "$7,000",
                state="TX",
                metric_id=172,
                metric_name="Maximum annual performance pay bonus, if eligible",
            ),
            _answer_row(
                3,
                "Buffalo School District",
                "$10,000",
                state="NY",
                metric_id=172,
                metric_name="Maximum annual performance pay bonus, if eligible",
            ),
        ],
        aliases=[
            _approved_metric_alias(
                metric_phrase,
                metric_id=172,
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question=(
            "Find districts in the South with real performance pay - not just "
            "token bonuses."
        ),
        selection=SelectionSpec(scope="state", states=["NC", "TX"]),
        metrics=[MetricSpec(name=metric_phrase)],
        filters=[
            FilterSpec(
                field=metric_phrase,
                operator="greater_than_or_equal",
                value=5000,
                threshold_hint="real (not token)",
            )
        ],
    )

    assert requested_states(plan) == {"NC", "TX"}

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.selection is not None
    assert outcome.result.selection.states == ["NC", "TX"]
    assert [(row.district_name, row.state) for row in outcome.result.rows] == [
        ("Cumberland County Schools", "NC"),
        ("Dallas Independent School District", "TX"),
    ]


@pytest.mark.asyncio
async def test_rank_plan_for_multi_metric_bundle_executes_as_lookup_without_sort() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(metric_id=171, name="Minimum performance pay bonus"),
            MetricCandidate(metric_id=172, name="Maximum performance pay bonus"),
        ],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[
            _answer_row(1, "Alpha", "$1,000", metric_id=171),
            _answer_row(1, "Alpha", "$5,000", metric_id=172),
        ],
        aliases=[
            _approved_metric_alias(
                "performance pay bonus amount",
                metric_ids=[171, 172],
                entity_type="metric_bundle",
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question="Compare the performance pay bonus amounts across districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="performance pay bonus amount")],
        sort=SortSpec(field="performance pay bonus amount", direction="desc"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert [row.metric_id for row in outcome.result.rows] == [172, 171]


@pytest.mark.asyncio
async def test_rank_plan_with_multiple_metric_specs_and_no_limit_executes_as_lookup() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(metric_id=39, name="Classroom observation frequency"),
            MetricCandidate(metric_id=40, name="Instructional walkthroughs policy"),
        ],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[
            _answer_row(1, "Alpha", "3", metric_id=39),
            _answer_row(1, "Alpha", "Yes", metric_id=40),
        ],
        aliases=[
            _approved_metric_alias("classroom observation frequency", metric_id=39),
            _approved_metric_alias("instructional walkthroughs policy", metric_id=40),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question="Are there districts that get into classrooms regularly?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="classroom observation frequency"),
            MetricSpec(name="instructional walkthroughs policy", role="comparison"),
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert [row.metric_id for row in outcome.result.rows] == [39, 40]


@pytest.mark.asyncio
async def test_rank_plan_with_limit_ranks_by_defaulted_salary_and_displays_workdays() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=89,
                name="Starting salary BA",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=96,
                name="Starting salary MA",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=69,
                name="Teacher workdays",
                answer_type="numeric",
            ),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "$50,000",
                metric_id=89,
                metric_name="Starting salary BA",
            ),
            _answer_row(
                2,
                "Bravo",
                "$60,000",
                metric_id=89,
                metric_name="Starting salary BA",
            ),
            _answer_row(
                3,
                "Charlie",
                "$55,000",
                metric_id=89,
                metric_name="Starting salary BA",
            ),
            _answer_row(
                1,
                "Alpha",
                "185",
                metric_id=69,
                metric_name="Teacher workdays",
            ),
            _answer_row(
                2,
                "Bravo",
                "190",
                metric_id=69,
                metric_name="Teacher workdays",
            ),
            _answer_row(
                3,
                "Charlie",
                "188",
                metric_id=69,
                metric_name="Teacher workdays",
            ),
        ],
        aliases=[
            _ambiguous_metric_alias(
                "starting salary",
                metric_ids=[89, 96],
                metadata={
                    "contextual_defaults": {
                        "launch_starting_salary_default": {
                            "metric_id": 89,
                            "note_keys": [
                                "bachelor_starting_salary_default_lane"
                            ],
                        }
                    }
                },
            ),
            _approved_metric_alias("teacher workdays", metric_id=69),
        ],
        renderer_notes={
            "bachelor_starting_salary_default_lane": (
                "I used first-year BA starting salary because no degree lane "
                "was specified."
            )
        },
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question=(
            "Show me the 10 highest starting salaries and how many days "
            "teachers work in those districts."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary"),
            MetricSpec(name="teacher workdays", role="comparison"),
        ],
        sort=SortSpec(field="starting salary", direction="desc"),
        limit=LimitSpec(count=2, kind="top"),
        output=OutputSpec(format="table"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert [
        (row.district_name, row.metric_id, row.display_value)
        for row in outcome.result.rows
    ] == [
        ("Bravo", 89, "$60,000"),
        ("Bravo", 69, "190"),
        ("Charlie", 89, "$55,000"),
        ("Charlie", 69, "188"),
    ]
    assert any(
        note.startswith("I used first-year BA starting salary")
        for note in outcome.result.source_notes
    )
    validation = validate_result(plan, outcome.result, authority=outcome.authority)
    assert "sort_order_mismatch" not in [
        finding.code for finding in validation.findings
    ]


@pytest.mark.asyncio
async def test_rank_plan_with_two_primary_metrics_executes_as_multi_column_lookup() -> None:
    """End-to-end exemplar for M3c-1 / #733.

    The planner currently emits multi-metric rank plans with BOTH metrics
    marked ``role="primary"`` (the LLM does not yet pick which to rank by).
    With the validator delegation fix, the executor must accept this shape
    and produce a multi-column table ranked by the first primary metric,
    with rows for the comparison column too. Verified end-to-end so the
    contract between planner pass-through and executor delegation stays
    intact.
    """
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(metric_id=89, name="Starting salary BA", answer_type="numeric"),
            MetricCandidate(metric_id=96, name="Starting salary MA", answer_type="numeric"),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "$50,000", metric_id=89, metric_name="Starting salary BA"),
            _answer_row(2, "Bravo", "$60,000", metric_id=89, metric_name="Starting salary BA"),
            _answer_row(3, "Charlie", "$55,000", metric_id=89, metric_name="Starting salary BA"),
            _answer_row(1, "Alpha", "$60,000", metric_id=96, metric_name="Starting salary MA"),
            _answer_row(2, "Bravo", "$70,000", metric_id=96, metric_name="Starting salary MA"),
            _answer_row(3, "Charlie", "$65,000", metric_id=96, metric_name="Starting salary MA"),
        ],
        aliases=[
            _approved_metric_alias("starting salary (BA)", metric_id=89),
            _approved_metric_alias("starting salary (MA)", metric_id=96),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question=(
            "starting salary for teachers with a BA and teachers with an MA"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting salary (BA)", role="primary"),
            MetricSpec(name="starting salary (MA)", role="primary"),
        ],
        sort=SortSpec(field="starting salary (BA)", direction="desc"),
        limit=LimitSpec(count=2, kind="top"),
        output=OutputSpec(format="table"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    # Ranked by BA salary descending, top-2 = Bravo ($60k) then Charlie ($55k).
    # Each district contributes one row per metric — the multi-column shape.
    assert [
        (row.district_name, row.metric_id, row.display_value)
        for row in outcome.result.rows
    ] == [
        ("Bravo", 89, "$60,000"),
        ("Bravo", 96, "$70,000"),
        ("Charlie", 89, "$55,000"),
        ("Charlie", 96, "$65,000"),
    ]


@pytest.mark.asyncio
async def test_multi_metric_lookup_validates_bundle_sort_as_value_sort() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(metric_id=171, name="Minimum performance pay bonus"),
            MetricCandidate(metric_id=172, name="Maximum performance pay bonus"),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "$1,000",
                metric_id=171,
                metric_name="Minimum performance pay bonus",
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _answer_row(
                1,
                "Alpha",
                "$5,000",
                metric_id=172,
                metric_name="Maximum performance pay bonus",
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _answer_row(
                2,
                "Bravo",
                "$2,000",
                metric_id=171,
                metric_name="Minimum performance pay bonus",
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
            _answer_row(
                2,
                "Bravo",
                "$6,000",
                metric_id=172,
                metric_name="Maximum performance pay bonus",
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
        ],
        aliases=[
            _approved_metric_alias(
                "performance pay bonus amount",
                metric_ids=[171, 172],
                entity_type="metric_bundle",
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question="Compare the performance pay bonus amounts across districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="performance pay bonus amount")],
        sort=SortSpec(field="performance pay bonus amount", direction="desc"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [
        (row.district_name, row.metric_id, row.display_value)
        for row in outcome.result.rows
    ] == [
        ("Bravo", 172, "$6,000"),
        ("Alpha", 172, "$5,000"),
        ("Bravo", 171, "$2,000"),
        ("Alpha", 171, "$1,000"),
    ]
    validation = validate_result(plan, outcome.result, authority=outcome.authority)
    assert validation.valid is True
    assert "sort_order_mismatch" not in [
        finding.code for finding in validation.findings
    ]


@pytest.mark.asyncio
async def test_lookup_plan_for_multi_metric_bundle_uses_table_order_not_value_sort() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(metric_id=171, name="Minimum performance pay bonus"),
            MetricCandidate(metric_id=172, name="Maximum performance pay bonus"),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "$1,000",
                metric_id=171,
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _answer_row(
                1,
                "Alpha",
                "$5,000",
                metric_id=172,
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _answer_row(
                2,
                "Bravo",
                "$2,000",
                metric_id=171,
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
            _answer_row(
                2,
                "Bravo",
                "$6,000",
                metric_id=172,
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
        ],
        aliases=[
            _approved_metric_alias(
                "performance pay bonus amount",
                metric_ids=[171, 172],
                entity_type="metric_bundle",
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="Compare the performance pay bonus amounts across districts.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="performance pay bonus amount")],
        sort=SortSpec(field="performance pay bonus amount", direction="desc"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [
        (row.district_name, row.metric_id)
        for row in outcome.result.rows
    ] == [
        ("Alpha", 172),
        ("Alpha", 171),
        ("Bravo", 172),
        ("Bravo", 171),
    ]
    assert "alphabetical by district name" in outcome.result.order_statement
    validation = validate_result(plan, outcome.result, authority=outcome.authority)
    assert validation.valid is True


@pytest.mark.asyncio
async def test_compensation_intersection_returns_only_districts_satisfying_both_criteria() -> None:
    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(metric_id=171, name="Minimum performance pay bonus"),
            MetricCandidate(metric_id=172, name="Maximum performance pay bonus"),
            MetricCandidate(metric_id=175, name="Hard-to-staff school pay offered"),
            MetricCandidate(metric_id=176, name="Minimum hard-to-staff school pay"),
            MetricCandidate(metric_id=178, name="Hard-to-staff school pay target met"),
        ],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
            DistrictCandidate(district_id=4, district_name="Delta", state="CA"),
            DistrictCandidate(district_id=5, district_name="Echo", state="CA"),
        ],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "$1,000",
                metric_id=171,
                metric_name="Minimum performance pay bonus",
                citations=[_source("Alpha Performance.pdf", district_id=1)],
            ),
            _answer_row(
                1,
                "Alpha",
                "Yes - annual pay increase",
                metric_id=175,
                metric_name="Hard-to-staff school pay offered",
                citations=[_source("Alpha Staffing.pdf", district_id=1)],
            ),
            _answer_row(2, "Bravo", "Issue not addressed", metric_id=171),
            _answer_row(
                2,
                "Bravo",
                "Yes",
                metric_id=175,
                citations=[_source("Bravo Staffing.pdf", district_id=2)],
            ),
            _answer_row(
                3,
                "Charlie",
                "$2,000",
                metric_id=172,
                metric_name="Maximum performance pay bonus",
                citations=[_source("Charlie Performance.pdf", district_id=3)],
            ),
            _answer_row(3, "Charlie", "No", metric_id=178),
            _answer_row(4, "Delta", "$0", metric_id=171),
            _answer_row(
                4,
                "Delta",
                "$500",
                metric_id=176,
                citations=[_source("Delta Staffing.pdf", district_id=4)],
            ),
            _answer_row(
                5,
                "Echo",
                "5%",
                metric_id=172,
                metric_name="Maximum performance pay bonus",
                citations=[_source("Echo Performance.pdf", district_id=5)],
            ),
            _answer_row(
                5,
                "Echo",
                "Yes",
                metric_id=178,
                metric_name="Hard-to-staff school pay target met",
                citations=[_source("Echo Staffing.pdf", district_id=5)],
            ),
        ],
        aliases=[
            _approved_metric_alias(
                "performance pay bonuses",
                metric_ids=[171, 172],
                entity_type="metric_bundle",
            ),
            _approved_metric_alias(
                "hard-to-staff school pay",
                metric_ids=[175, 176, 178],
                entity_type="metric_bundle",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question=(
            "Which districts offer both performance pay bonuses AND extra pay "
            "for working in hard-to-staff schools?"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="performance pay bonuses"),
            MetricSpec(name="hard-to-staff school pay", role="comparison"),
        ],
        requires_all_metrics=True,
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert [criterion.label for criterion in outcome.result.criteria] == [
        "performance pay bonuses",
        "hard-to-staff school pay",
    ]
    assert [
        (row.district_name, row.metric_id, row.criterion_id, row.criterion_satisfied)
        for row in outcome.result.rows
    ] == [
        ("Alpha", 175, "criterion_2", True),
        ("Alpha", 171, "criterion_1", True),
        ("Echo", 178, "criterion_2", True),
        ("Echo", 172, "criterion_1", True),
    ]
    assert [district.district_name for district in outcome.result.selection.districts] == [
        "Alpha",
        "Echo",
    ]
    assert outcome.result.excluded_count == 3
    assert _methodology_codes(outcome.result) == [
        "intersection_requires_all_criteria",
        "intersection_accepts_any_current_positive_value",
        "citation_answer_level_preferred_source_fallback",
    ]
    assert outcome.result.source_notes == []
    assert "criterion_id" in outcome.result.csv_export.columns
    assert outcome.result.csv_export.rows[0]["criterion_satisfied"] is True

    validation = validate_result(plan, outcome.result, authority=outcome.authority)
    assert validation.valid is True
    assert "sort_order_mismatch" not in [
        finding.code for finding in validation.findings
    ]


@pytest.mark.asyncio
async def test_lookup_deduplicates_metric_aliases_that_resolve_to_same_metric() -> None:
    repository = FakeLookupRepository(
        metrics=[MetricCandidate(metric_id=202, name="Maximum sick leave accumulation")],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[
            _answer_row(
                1,
                "Alpha",
                "30",
                metric_id=202,
                metric_name="Maximum sick leave accumulation",
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            )
        ],
        aliases=[
            _approved_metric_alias("sick leave carryover limit", metric_id=202),
            _approved_metric_alias("sick leave maximum accumulation", metric_id=202),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = _lookup_plan(
        districts=["Alpha"],
        metrics=[
            MetricSpec(name="sick leave carryover limit"),
            MetricSpec(name="sick leave maximum accumulation", role="comparison"),
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [row.metric_id for row in outcome.result.rows] == [202]
    validation = validate_result(plan, outcome.result, authority=outcome.authority)
    assert validation.valid is True
    assert "metric_lookup_duplicate" not in [
        finding.code for finding in validation.findings
    ]


@pytest.mark.asyncio
async def test_lookup_rejects_limit_without_lookup_side_effects() -> None:
    repository = FakeLookupRepository(
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[_answer_row(1, "Alpha", "Yes", answer_id=101)],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(districts=["Alpha"], limit=LimitSpec(count=1))
    )

    assert outcome.result is None
    assert "governed data" in outcome.message
    assert "supported query shape" not in outcome.message
    assert repository.resolved_district_names == []
    assert repository.metric_queries == []
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_lookup_rejects_unsupported_sort_field_without_lookup_side_effects() -> None:
    repository = FakeLookupRepository(
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[_answer_row(1, "Alpha", "Yes", answer_id=101)],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            districts=["Alpha"],
            sort=SortSpec(field="student_enrollment", direction="desc"),
        )
    )

    assert outcome.result is None
    assert "governed data" in outcome.message
    assert "supported query shape" not in outcome.message
    assert repository.resolved_district_names == []
    assert repository.metric_queries == []
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metrics",
    [
        [
            MetricSpec(name="Collective bargaining status", role="comparison"),
            MetricSpec(name="starting salary", role="comparison"),
        ],
        [
            MetricSpec(name="Collective bargaining status"),
            MetricSpec(name="starting salary", role="filter"),
        ],
        [
            MetricSpec(name="Collective bargaining status"),
            MetricSpec(name="starting salary", role="grouping"),
        ],
    ],
)
async def test_lookup_rejects_invalid_multi_metric_role_sequences(
    metrics: list[MetricSpec],
) -> None:
    repository = FakeLookupRepository(
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[_answer_row(1, "Alpha", "Yes", answer_id=101)],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_lookup_plan(districts=["Alpha"], metrics=metrics))

    assert outcome.result is None
    assert "governed data" in outcome.message
    assert "supported query shape" not in outcome.message
    assert repository.resolved_district_names == []
    assert repository.metric_queries == []
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_lookup_unresolved_or_ambiguous_districts_return_no_result() -> None:
    unknown_repository = FakeLookupRepository(
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")]
    )
    ambiguous_repository = FakeLookupRepository(
        ambiguous={
            "Portland": [
                DistrictCandidate(district_id=10, district_name="Portland", state="OR"),
                DistrictCandidate(district_id=11, district_name="Portland", state="ME"),
            ]
        }
    )

    unknown = await DeterministicQueryExecutor(unknown_repository).execute(
        _lookup_plan(districts=["Missing"])
    )
    ambiguous = await DeterministicQueryExecutor(ambiguous_repository).execute(
        _lookup_plan(districts=["Portland"])
    )

    assert unknown.result is not None
    assert unknown.result.rows[0].district_id is None
    assert unknown.result.rows[0].district_name == "Missing"
    assert unknown.result.rows[0].coverage_state == "out_of_universe"
    assert unknown.result.rows[0].coverage_reason == "out_of_universe"
    assert unknown.result.rows[0].display_value == (
        "Missing is not in the District Policy Pathfinder."
    )
    assert unknown.result.coverage_frame is not None
    assert unknown.result.coverage_frame.out_of_universe_count == 1
    assert unknown.result.coverage_frame.breakdown.out_of_universe_count == 1
    assert unknown_repository.metric_queries == ["Collective bargaining status"]
    assert unknown_repository.fetched_metric_ids == [4321]
    assert ambiguous.result is None
    assert ambiguous.clarification is not None
    assert ambiguous.clarification.missing_fields == ["district"]
    assert ambiguous.clarification.candidates == ["Portland, OR", "Portland, ME"]
    assert ambiguous.message == (
        'I found more than one covered district matching "Portland". '
        "Which district do you mean?"
    )
    assert "deterministic execution" not in ambiguous.message
    assert ambiguous_repository.metric_queries == []
    assert ambiguous_repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_lookup_out_of_universe_label_mentions_state_from_typed_suffix() -> None:
    """#1514 D6 — an unresolved 'Name, ST' derives ST for the canonical
    sentence and the emitted row's state column; the bare-name case above
    stays stateless (data-honest fallback)."""

    repository = FakeLookupRepository(
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")]
    )

    outcome = await DeterministicQueryExecutor(repository).execute(
        _lookup_plan(districts=["Cincinnati, OH"])
    )

    assert outcome.result is not None
    row = outcome.result.rows[0]
    assert row.district_id is None
    assert row.district_name == "Cincinnati, OH"
    assert row.state == "OH"
    assert row.coverage_state == "out_of_universe"
    assert row.coverage_reason == "out_of_universe"
    assert row.display_value == (
        "Cincinnati, OH is not in the District Policy Pathfinder."
    )


@pytest.mark.asyncio
async def test_lookup_zero_matching_answer_rows_returns_not_reviewed_result() -> None:
    repository = FakeLookupRepository(
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[_answer_row(2, "Bravo", "Yes", answer_id=102)],
        reviewed_district_ids={1, 2},
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_lookup_plan(districts=["Alpha"]))

    assert outcome.result is not None
    assert outcome.result.rows[0].coverage_state == "not_reviewed"
    assert outcome.result.rows[0].display_value == (
        "NCTQ hasn't reviewed Alpha for Collective bargaining status "
        "(2024 - 2025 data not yet reviewed)."
    )
    assert outcome.result.coverage_frame is not None
    assert outcome.result.coverage_frame.not_reviewed_count == 1
    assert repository.fetched_metric_ids == [4321]


@pytest.mark.asyncio
async def test_lookup_uses_stale_recent_answer_as_not_reviewed_disclosure() -> None:
    repository = FakeLookupRepository(
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[],
        recent_rows=[
            _answer_row(
                1,
                "Alpha",
                "Yes",
                answer_id=90,
                metric_id=4321,
            ).model_copy(update={"academic_year": "2023 - 2024"})
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_lookup_plan(districts=["Alpha"]))

    assert outcome.result is not None
    row = outcome.result.rows[0]
    assert row.coverage_state == "not_reviewed"
    assert row.coverage_reason == "stale_recent_answer"
    assert row.coverage_prior_academic_year == "2023 - 2024"
    assert row.coverage_prior_display_value == "Yes"
    # #1514 D7 — the canonical "last reviewed" narrative sentence.
    assert row.display_value == (
        "NCTQ last reviewed Alpha for Collective bargaining status in "
        "2023 - 2024; the value then was Yes."
    )
    assert row.citation_markers == []


@pytest.mark.asyncio
async def test_lookup_retains_ina_and_na_rows_with_canonical_coverage_display() -> None:
    repository = FakeLookupRepository(
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
        rows=[
            _answer_row(1, "Alpha", "INA", answer_id=101),
            _answer_row(
                2,
                "Bravo",
                "N/A - district does not have collective bargaining",
                answer_id=102,
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_lookup_plan(districts=["Alpha", "Bravo"]))

    assert outcome.result is not None
    assert [
        (row.district_name, row.coverage_state, row.display_value)
        for row in outcome.result.rows
    ] == [
        ("Alpha", "ina", "Issue not addressed in the documents reviewed."),
        (
            "Bravo",
            "na",
            "Not applicable for Bravo: district does not have collective bargaining.",
        ),
    ]
    assert outcome.result.coverage_frame is not None
    assert outcome.result.coverage_frame.addressed_count == 2
    assert outcome.result.coverage_frame.real_data_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["all_covered_districts", "state"])
async def test_lookup_supports_materialized_covered_scopes(scope: str) -> None:
    repository = FakeLookupRepository(rows=[_answer_row(1, "Alpha", "Yes")])
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(scope=scope, states=["CA"] if scope == "state" else [])
    )

    assert outcome.result is not None
    assert outcome.result.selection is not None
    assert outcome.result.selection.scope == scope
    assert outcome.result.rows == []
    assert repository.metric_queries == ["Collective bargaining status"]
    assert repository.fetched_metric_ids == [4321]


def test_lookup_validation_accepts_resolved_selected_ids() -> None:
    report = validate_result(_lookup_plan(), _lookup_result())

    assert report.valid is True
    assert report.findings == []


def test_lookup_validation_requires_selection_metadata() -> None:
    result = _lookup_result().model_copy(update={"selection": None})

    report = validate_result(_lookup_plan(), result)

    assert report.valid is False
    assert [finding.code for finding in report.findings] == [
        "selection_metadata_missing"
    ]


def test_lookup_validation_rejects_rows_outside_selected_ids() -> None:
    result = _lookup_result().model_copy(
        update={
            "rows": [
                MetricValueRow(
                    district_id=99,
                    district_name="Outside",
                    state="CA",
                    metric_id=4321,
                    metric_name="Collective bargaining status",
                    value="Yes",
                    display_value="Yes",
                    academic_year="2024 - 2025",
                    citation_markers=[1],
                    coverage_state="covered",
                    coverage_display="Yes",
                    coverage_reason="answer_value",
                )
            ]
        }
    )

    report = validate_result(_lookup_plan(), result)

    assert report.valid is False
    assert "selection_district_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_lookup_validation_accepts_largest_district_selection_metadata() -> None:
    result = _lookup_result().model_copy(
        update={
            "selection": _lookup_result().selection.model_copy(
                update={"scope": "largest_districts", "states": ["CA"]}
            )
        }
    )

    report = validate_result(
        _lookup_plan(
            scope="largest_districts",
            districts=[],
            states=["CA"],
            limit=LimitSpec(count=2),
        ),
        result,
    )

    assert report.valid is True


def test_lookup_validation_rejects_largest_selection_rows_outside_selected_ids() -> None:
    result = _lookup_result().model_copy(
        update={
            "selection": ResultSelection(
                scope="largest_districts",
                districts=[
                    SelectedDistrict(district_id=1, district_name="Alpha", state="CA")
                ],
                states=["CA"],
            )
        }
    )

    report = validate_result(
        _lookup_plan(
            scope="largest_districts",
            districts=[],
            states=["CA"],
            limit=LimitSpec(count=2),
        ),
        result,
    )

    assert report.valid is False
    assert "selection_district_mismatch" in [
        finding.code for finding in report.findings
    ]


def test_lookup_validation_rejects_default_alphabetical_order_mismatch() -> None:
    result = _lookup_result().model_copy(
        update={"rows": list(reversed(_lookup_result().rows))}
    )

    report = validate_result(_lookup_plan(), result)

    assert report.valid is False
    assert "sort_order_mismatch" in [finding.code for finding in report.findings]


def test_lookup_validation_accepts_value_desc_order() -> None:
    base = _lookup_result()
    # Re-construct (instead of `base.model_copy(update={"rows": ...})`) so the
    # post-init `populate_artifact_surfaces` validator re-runs and keeps
    # `csv_export` in sync with the new rows. `model_copy` skips validators
    # by design, which would leave a stale csv_export that the new
    # surface_consistency validator correctly fails on.
    result = MetricLookupResult(
        selection=base.selection,
        rows=[
            base.rows[1].model_copy(update={"value": "$60,000"}),
            base.rows[0].model_copy(update={"value": "$50,000"}),
        ],
        citations=base.citations,
        coverage_frame=base.coverage_frame,
        total_considered=base.total_considered,
        excluded_count=base.excluded_count,
        order_statement=base.order_statement,
        methodology_codes=base.methodology_codes,
    )

    report = validate_result(
        _lookup_plan(sort=SortSpec(field="value", direction="desc")),
        result,
    )

    assert report.valid is True


def test_lookup_validation_ignores_selection_filter_metric_specs() -> None:
    plan = _lookup_plan(
        scope="all_covered_districts",
        districts=[],
        metrics=[
            MetricSpec(name="sick leave policy"),
            MetricSpec(name="enrollment", role="filter"),
        ],
        sort=None,
    )

    report = validate_result(plan, _lookup_result())

    assert report.valid is True
    assert "unsupported_metric_plan" not in [
        finding.code for finding in report.findings
    ]


def test_lookup_validation_accepts_multiple_primary_lookup_metric_specs() -> None:
    base = _lookup_result()
    # See `test_lookup_validation_accepts_value_desc_order` above for why this
    # uses a fresh `MetricLookupResult(...)` instead of `model_copy(update=...)`.
    result = MetricLookupResult(
        selection=base.selection,
        rows=[
            base.rows[0],
            base.rows[0].model_copy(
                update={
                    "metric_id": 202,
                    "metric_name": "Maximum sick leave accumulation",
                    "citation_markers": [2],
                }
            ),
        ],
        citations=[
            base.citations[0],
            base.citations[1].model_copy(update={"district_id": 1}),
        ],
        coverage_frame=base.coverage_frame,
        total_considered=base.total_considered,
        excluded_count=base.excluded_count,
        order_statement=base.order_statement,
        methodology_codes=base.methodology_codes,
    )
    plan = _lookup_plan(
        districts=["Alpha"],
        metrics=[
            MetricSpec(name="sick days allotted per year"),
            MetricSpec(name="sick leave carryover"),
        ],
    )

    report = validate_result(plan, result)

    assert report.valid is True
    assert "unsupported_metric_plan" not in [
        finding.code for finding in report.findings
    ]


def test_lookup_validation_does_not_treat_out_of_universe_rows_as_duplicates() -> None:
    result = _lookup_result().model_copy(
        update={
            "rows": [
                _lookup_result().rows[0].model_copy(
                    update={
                        "district_id": None,
                        "district_name": "Cincinnati",
                        "value": None,
                        "display_value": (
                            "Cincinnati is not in the District Policy Pathfinder."
                        ),
                        "citation_markers": [],
                        "coverage_state": "out_of_universe",
                        "coverage_display": (
                            "Cincinnati is not in the District Policy Pathfinder."
                        ),
                        "coverage_reason": "out_of_universe",
                    }
                ),
                _lookup_result().rows[0].model_copy(
                    update={
                        "district_id": None,
                        "district_name": "Toledo",
                        "value": None,
                        "display_value": (
                            "Toledo is not in the District Policy Pathfinder."
                        ),
                        "citation_markers": [],
                        "coverage_state": "out_of_universe",
                        "coverage_display": (
                            "Toledo is not in the District Policy Pathfinder."
                        ),
                        "coverage_reason": "out_of_universe",
                    }
                ),
            ]
        }
    )

    report = validate_result(_lookup_plan(), result)

    assert "metric_lookup_duplicate" not in [
        finding.code for finding in report.findings
    ]


def test_render_response_formats_lookup_without_rank_column() -> None:
    manifest = render_response(_lookup_plan(), _lookup_result(), _valid_report())

    assert manifest.status == "rendered"
    assert manifest.result_type == "metric_lookup"
    assert manifest.body.startswith(
        "I looked up Collective bargaining status for selected districts."
    )
    assert "| Rank |" not in manifest.body
    assert (
        "| District | State | Academic year | Collective bargaining status | Sources |"
        in manifest.body
    )
    assert "| Alpha | CA | 2024 - 2025 | Yes | [1] |" in manifest.body
    assert "[1] Alpha District Contract, 2024-2025, p. 1" not in manifest.body
    assert "\nSources\n" not in manifest.body


def test_chat_serializes_metric_lookup_result_and_snapshot() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.86,
        query_plan=_lookup_plan(),
    )
    agent = Agent(
        # call_tools=[] keeps the canned-output TestModel from auto-invoking
        # the always-attached Compass catalog toolset (#1248); the offline app
        # has no live DB pool for a tool call to reach.
        TestModel(
            custom_output_args=turn.model_dump(mode="json"),
            call_tools=[],
        ),
        output_type=PlannerTurn,
    )
    store = InMemorySessionStore()
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_lookup_result(),
            authority=ValidationAuthority(),
            message="Looked up Collective bargaining status for selected districts.",
        )
    )

    with TestClient(
        _create_offline_app(
            planner_agent=agent,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": (
                    "What is collective bargaining status for Charlie and Alpha?"
                )
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["result_type"] == "metric_lookup"
    assert [row["district_name"] for row in body["result"]["rows"]] == [
        "Alpha",
        "Charlie",
    ]
    assert "Rank" not in body["message"]
    assert body["manifest"]["status"] == "rendered"
    assert executor.plans == [turn.query_plan]

    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    assert snapshots[0].assistant_message == body["message"]
    assert snapshots[0].planner_turn == turn


def test_lookup_result_contains_csv_export_artifact_with_coverage_and_sources() -> None:
    result = _lookup_result()

    assert result.csv_export is not None
    assert result.csv_export.columns == [
        "district_id",
        "district_name",
        "state",
        "metric_id",
        "metric_name",
        "value",
        "display_value",
        "sort_metric_id",
        "sort_metric_name",
        "sort_value",
        "sort_display_value",
        "sort_academic_year",
        "sort_source_document",
        "sort_source_document_type",
        "sort_source_url",
        "sort_source_valid_from",
        "sort_source_valid_to",
        "criterion_id",
        "criterion_label",
        "criterion_satisfied",
        "academic_year",
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
    assert result.csv_export.rows[0]["coverage_state"] == "covered"
    assert result.csv_export.rows[0]["coverage_reason"] == "answer_value"
    assert result.csv_export.rows[0]["source_urls"] == "https://example.org/alpha.pdf"


# ---------------------------------------------------------------------------
# adjacent_metrics in manifest metadata (Refs #1018)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_response_manifest_carries_adjacent_metric_labels() -> None:
    """When the catalog adjudicator picks select_with_alternates, the executor
    must surface the adjacent metric labels in the response manifest's metadata
    for the answer-layer brief to consume."""

    # Three teacher salary metrics: BA (primary), MA, and max-BA (alternates).
    # Names contain "teacher salary" so FakeLookupRepository.search_metrics
    # returns them as candidates when the plan query is "teacher salary".
    ba_salary = MetricCandidate(
        metric_id=10,
        name="teacher salary — first year, bachelor's degree",
        answer_type="numeric",
    )
    ma_salary = MetricCandidate(
        metric_id=11,
        name="teacher salary — first year, master's degree",
        answer_type="numeric",
    )
    max_ba_salary = MetricCandidate(
        metric_id=12,
        name="teacher salary — max, bachelor's degree schedule",
        answer_type="numeric",
    )
    vermont = DistrictCandidate(
        district_id=901,
        district_name="Burlington School District",
        state="VT",
    )
    repository = FakeLookupRepository(
        metrics=[ba_salary, ma_salary, max_ba_salary],
        districts=[vermont],
        rows=[
            _answer_row(
                901,
                "Burlington School District",
                "$52,000",
                state="VT",
                metric_id=10,
                metric_name=ba_salary.name,
            ),
        ],
    )
    # Adjudicator always selects metric_id=10 as primary and 11+12 as alternates.
    adjudicator = create_catalog_adjudicator_agent(
        TestModel(
            custom_output_args=CatalogAdjudicationDecision(
                action="select_with_alternates",
                selected_ids=["10"],
                alternate_ids=["11", "12"],
                confidence=0.9,
                rationale="BA salary is the conventional default; MA and max-BA are adjacent.",
            ).model_dump(mode="json")
        )
    )
    executor = DeterministicQueryExecutor(
        repository,
        catalog_resolver=CatalogResolver(repository, adjudicator=adjudicator),
    )

    outcome = await executor.execute(
        QueryPlan(
            operation="lookup",
            question="What is the teacher salary in Burlington, Vermont?",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Burlington School District"],
                states=["VT"],
            ),
            metrics=[MetricSpec(name="teacher salary")],
        )
    )

    assert isinstance(outcome, ExecutionSuccess), f"Expected success, got {outcome}"
    assert outcome.adjacent_candidates == [ma_salary, max_ba_salary]

    # Build the manifest and attach adjacent_metrics metadata.
    plan = QueryPlan(
        operation="lookup",
        question="What is the teacher salary in Burlington, Vermont?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Burlington School District"],
            states=["VT"],
        ),
        metrics=[MetricSpec(name="teacher salary")],
    )
    validation = validate_result(plan, outcome.result, authority=outcome.authority)
    manifest = render_response(plan, outcome.result, validation)
    manifest = attach_adjacent_metrics_manifest_metadata(
        manifest,
        outcome.adjacent_candidates,
    )

    adjacent = manifest.metadata.get("adjacent_metrics")
    assert adjacent is not None, "manifest.metadata['adjacent_metrics'] should be present"
    assert adjacent == [
        {"metric_id": 11, "label": ma_salary.name},
        {"metric_id": 12, "label": max_ba_salary.name},
    ]


@pytest.mark.asyncio
async def test_scoping_clarify_calls_compose_clarify_question_async_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _resolve_plan_metrics encounters an ambiguous metric, it routes
    through compose_clarify_question_async passing the resolution's hint."""
    from compass_backend.contracts.planning import ClarificationRequest

    calls: list[dict[str, object]] = []

    async def _fake_compose(
        metric_phrase: str,
        *,
        operation: str,
        candidates: list[MetricCandidate],
        adjudicator_hint: str | None = None,
        stylist_agent: object = None,
    ) -> ClarificationRequest:
        calls.append(
            {
                "metric_phrase": metric_phrase,
                "operation": operation,
                "adjudicator_hint": adjudicator_hint,
            }
        )
        return ClarificationRequest(
            question="composed question",
            missing_fields=["metric"],
            candidates=[c.name for c in candidates],
        )

    monkeypatch.setattr(
        "compass_backend.execution.scoping.compose_clarify_question_async",
        _fake_compose,
    )

    # Two salary metrics so the adjudicator sees real candidates.
    ba_salary = MetricCandidate(
        metric_id=89, name="Starting salary BA", answer_type="numeric"
    )
    ma_salary = MetricCandidate(
        metric_id=96, name="Starting salary MA", answer_type="numeric"
    )
    repository = FakeLookupRepository(metrics=[ba_salary, ma_salary])

    # Adjudicator returns action="clarify" with a hint — this populates
    # MetricBundleResolution.clarification_hint so we can assert it threads
    # through to compose_clarify_question_async.
    adjudicator = create_catalog_adjudicator_agent(
        TestModel(
            custom_output_args=CatalogAdjudicationDecision(
                action="clarify",
                selected_ids=[],
                clarification_question="some hint",
                confidence=0.4,
                rationale="unit test adjudicator emits clarify",
            ).model_dump(mode="json")
        )
    )
    catalog_resolver = CatalogResolver(repository, adjudicator=adjudicator)
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=catalog_resolver
    )

    plan = QueryPlan(
        operation="lookup",
        question="What is starting salary?",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="starting salary")],
    )
    outcome = await executor.execute(plan)

    assert calls, "compose_clarify_question_async was never invoked"
    assert calls[0]["adjudicator_hint"] == "some hint"
    assert outcome.clarification is not None
    assert outcome.clarification.question == "composed question"


@pytest.mark.asyncio
async def test_ranking_clarify_routes_through_compose_clarify_question_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ranking plan with an ambiguous primary metric routes through the
    stylist service rather than the deterministic f-string."""
    from compass_backend.contracts.planning import ClarificationRequest

    calls: list[dict[str, object]] = []

    async def _fake_compose(
        metric_phrase: str,
        *,
        operation: str,
        candidates: list[MetricCandidate],
        adjudicator_hint: str | None = None,
        stylist_agent: object = None,
    ) -> ClarificationRequest:
        calls.append(
            {
                "operation": operation,
                "adjudicator_hint": adjudicator_hint,
            }
        )
        return ClarificationRequest(
            question="composed question",
            missing_fields=["metric"],
            candidates=[c.name for c in candidates],
        )

    monkeypatch.setattr(
        "compass_backend.execution.operations.compose_clarify_question_async",
        _fake_compose,
    )

    ba_salary = MetricCandidate(
        metric_id=89, name="Starting salary BA", answer_type="numeric"
    )
    ma_salary = MetricCandidate(
        metric_id=96, name="Starting salary MA", answer_type="numeric"
    )
    # No alias registered — two candidates with similar names forces the
    # adjudicator path (rather than the alias-ambiguous shortcut).
    repository = FakeLookupRepository(
        metrics=[ba_salary, ma_salary],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        ],
    )

    adjudicator = create_catalog_adjudicator_agent(
        TestModel(
            custom_output_args=CatalogAdjudicationDecision(
                action="clarify",
                selected_ids=[],
                clarification_question="ranking hint",
                confidence=0.4,
                rationale="unit test adjudicator emits clarify for ranking",
            ).model_dump(mode="json")
        )
    )
    catalog_resolver = CatalogResolver(repository, adjudicator=adjudicator)
    executor = DeterministicQueryExecutor(
        repository, catalog_resolver=catalog_resolver
    )

    plan = QueryPlan(
        operation="rank",
        question="Which districts have the highest starting salary?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        limit=LimitSpec(count=10),
    )
    outcome = await executor.execute(plan)

    assert calls, "compose_clarify_question_async was never invoked from operations"
    assert calls[0]["operation"] == "rank"
    assert calls[0]["adjudicator_hint"] == "ranking hint"
    assert outcome.clarification is not None
    assert outcome.clarification.question == "composed question"


@pytest.mark.asyncio
async def test_lookup_unresolved_metric_with_candidates_clarifies_not_dead_ends() -> None:
    """Regression (#1248 SELECT-R4): _resolve_plan_metric_groups must clarify
    with the real candidates when a metric phrase surfaces multiple candidates
    but none cleanly resolve — not emit a generic "could not resolve" refusal.

    Construction: an approved alias points at [11, 99], but metric 99 does not
    exist, so the bundle does not resolve cleanly. The phrase still matches two
    real catalog metrics (11 and 13), so MetricBundleResolution.candidates is a
    genuine multi-candidate set — the recoverable signal the dead-end discarded.
    """

    pay_scale = MetricCandidate(metric_id=11, name="Teacher pay scale", answer_type="text")
    pay_minimum = MetricCandidate(
        metric_id=13, name="Teacher pay minimum", answer_type="text"
    )
    repository = FakeLookupRepository(
        metrics=[pay_scale, pay_minimum],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        aliases=[
            # Approved single-metric alias whose bundle includes a missing id (99)
            # so the alias path returns resolved=[] with candidates populated.
            _approved_metric_alias("teacher pay", metric_ids=[11, 99]),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(districts=["Alpha"], metrics=[MetricSpec(name="teacher pay")])
    )

    assert isinstance(outcome, ExecutionClarification), (
        f"Expected a candidate-listing clarification, got {type(outcome).__name__}: "
        f"{outcome.message}"
    )
    assert outcome.clarification is not None
    assert outcome.clarification.missing_fields == ["metric"]
    # Real candidate labels, not a generic dead-end.
    assert set(outcome.clarification.candidates) >= {
        "Teacher pay scale",
        "Teacher pay minimum",
    }
    assert "could not resolve every requested metric" not in outcome.message
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_lookup_zero_candidate_metric_still_refuses() -> None:
    """A genuinely unresolvable metric (no candidates at all) keeps the
    deterministic refusal — clarification only fires with real candidates.
    """

    repository = FakeLookupRepository(
        metrics=[
            MetricCandidate(
                metric_id=4321, name="Collective bargaining status", answer_type="text"
            )
        ],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[_answer_row(1, "Alpha", "Yes", answer_id=101, metric_id=4321)],
        aliases=[
            # Alias bundle points only at a missing id, and the phrase matches no
            # catalog metric by name -> candidates is empty.
            _approved_metric_alias("starting salary", metric_id=9999),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _lookup_plan(
            districts=["Alpha"],
            metrics=[
                MetricSpec(name="Collective bargaining status"),
                MetricSpec(name="starting salary", role="comparison"),
            ],
        )
    )

    assert isinstance(outcome, ExecutionRefusal), (
        f"Expected refusal for a zero-candidate metric, got {type(outcome).__name__}"
    )
    assert "could not resolve every requested metric" in outcome.message


# ---------------------------------------------------------------------------
# FilterPrevalenceSummary tests (U1 + U2, #1337 / FILT-88)
# ---------------------------------------------------------------------------


def _numeric_filter_plan(
    *,
    metric_name: str,
    operator: str,
    value: float,
) -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question=f"Which districts have {metric_name} {operator} {value}?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=metric_name)],
        filters=[FilterSpec(field=metric_name, operator=operator, value=value)],
    )


def _workdays_repository() -> FakeLookupRepository:
    """Five districts: 3 covered (2 match >190), 1 INA, 1 not-reviewed.

    Universe = 5; covered = 3; matched = 2; not_reviewed = 1; INA = 1.
    The INA district contributes to the answered set but not the denominator.
    """
    workdays = MetricCandidate(
        metric_id=69,
        name="Total teacher workdays",
        answer_type="numeric",
    )
    districts = [
        DistrictCandidate(district_id=1, district_name="Alpha", state="TX"),
        DistrictCandidate(district_id=2, district_name="Bravo", state="TX"),
        DistrictCandidate(district_id=3, district_name="Charlie", state="TX"),
        DistrictCandidate(district_id=4, district_name="Delta", state="TX"),
        DistrictCandidate(district_id=5, district_name="Echo", state="TX"),
    ]
    rows = [
        _answer_row(1, "Alpha", "195", state="TX", metric_id=69, metric_name=workdays.name),
        _answer_row(2, "Bravo", "185", state="TX", metric_id=69, metric_name=workdays.name),
        _answer_row(3, "Charlie", "200", state="TX", metric_id=69, metric_name=workdays.name),
        _answer_row(4, "Delta", "Issue not addressed", state="TX", metric_id=69, metric_name=workdays.name),
        # District 5 (Echo) has no row → not-reviewed
    ]
    return FakeLookupRepository(
        metrics=[workdays],
        districts=districts,
        rows=rows,
    )


@pytest.mark.asyncio
async def test_filter_prevalence_numeric_threshold_pre_narrow_snapshot() -> None:
    """Numeric threshold filter: filter_prevalence reflects the pre-narrow universe.

    Universe = 5 districts; covered (real numeric value) = 3; matched (>190) = 2;
    not_reviewed = 1 (no row); INA = 1 (excluded from denominator).
    The not_reviewed_count must NOT be 0 — that was the root-cause bug.
    """
    repository = _workdays_repository()
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _numeric_filter_plan(
            metric_name="Total teacher workdays",
            operator="greater_than",
            value=190.0,
        )
    )

    assert isinstance(outcome, ExecutionSuccess)
    result = outcome.result
    assert isinstance(result, MetricLookupResult)

    # The post-narrow table shows only matching districts.
    assert {row.district_name for row in result.rows} == {"Alpha", "Charlie"}

    # filter_prevalence carries the pre-narrow tally.
    prevalence = result.filter_prevalence
    assert prevalence is not None
    assert prevalence.matched == 2          # Alpha + Charlie
    assert prevalence.denominator == 3      # Alpha + Bravo + Charlie (covered numeric)
    assert prevalence.not_reviewed_count == 1  # Echo — the key fix; was 0 before
    assert prevalence.na_count == 0         # no NA for numeric metric
    assert prevalence.percent is not None
    assert abs(prevalence.percent - round(2 / 3 * 100, 1)) < 0.01


def test_filter_prevalence_suppressed_when_matched_exceeds_denominator() -> None:
    """Anchor-equality on an NA value makes matched > the covered denominator —
    every NA district "matches", but NA districts are not in covered_count — so
    the "Of N covered districts, X match" tally is incoherent and percent would
    breach the FilterPrevalenceSummary.percent <= 100 bound and 500 the turn.

    Regression for the "same maximum performance bonus as Portland, ME" crash
    (2026-06-26 production-tab reconciliation; trace 019eff9400290c489805c26c52e559ba):
    the summary must be SUPPRESSED (None), never clamped, so the answer still
    lists the matching districts without an incoherent prevalence lead.
    """
    from compass_backend.execution.count import build_filter_prevalence_summary

    # Three districts, all NA for the filter metric -> covered_count == 0.
    rows = [
        _answer_row(1, "Alpha", "Issue not addressed", state="TX", metric_id=69, metric_name="Max performance bonus"),
        _answer_row(2, "Bravo", "Issue not addressed", state="TX", metric_id=69, metric_name="Max performance bonus"),
        _answer_row(3, "Charlie", "Issue not addressed", state="TX", metric_id=69, metric_name="Max performance bonus"),
    ]
    pre_narrow = ResultSelection(
        scope="named_districts",
        districts=[
            SelectedDistrict(district_id=1, district_name="Alpha", state="TX"),
            SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
            SelectedDistrict(district_id=3, district_name="Charlie", state="TX"),
        ],
    )

    # All three "match" the NA anchor value -> matched(3) > covered_count(0).
    summary = build_filter_prevalence_summary(
        rows, pre_narrow_selection=pre_narrow, post_narrow_district_count=3
    )
    assert summary is None


def test_filter_prevalence_returned_when_matched_within_denominator() -> None:
    """The guard must not over-suppress: when matched <= denominator the summary
    is returned normally (here matched == denominator -> 100%)."""
    from compass_backend.execution.count import build_filter_prevalence_summary

    rows = [
        _answer_row(1, "Alpha", "195", state="TX", metric_id=69, metric_name="Total teacher workdays"),
        _answer_row(2, "Bravo", "200", state="TX", metric_id=69, metric_name="Total teacher workdays"),
    ]
    pre_narrow = ResultSelection(
        scope="named_districts",
        districts=[
            SelectedDistrict(district_id=1, district_name="Alpha", state="TX"),
            SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
        ],
    )

    summary = build_filter_prevalence_summary(
        rows, pre_narrow_selection=pre_narrow, post_narrow_district_count=2
    )
    assert summary is not None
    assert summary.matched == 2
    assert summary.denominator == 2
    assert summary.percent == 100.0


@pytest.mark.asyncio
async def test_filter_prevalence_not_set_when_no_filter() -> None:
    """Unfiltered lookup: filter_prevalence is None; existing behavior unchanged."""
    repository = _workdays_repository()
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="lookup",
            question="Show all teacher workdays",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="Total teacher workdays")],
        )
    )

    assert isinstance(outcome, ExecutionSuccess)
    result = outcome.result
    assert isinstance(result, MetricLookupResult)
    assert result.filter_prevalence is None


@pytest.mark.asyncio
async def test_filter_prevalence_categorical_na_counted() -> None:
    """Categorical filter: NA values are counted separately, not in denominator.

    Mirrors the parental-leave scenario: some districts have "Not applicable"
    (NA state) for the metric, which should not be counted as covered.
    """
    parental_leave = MetricCandidate(
        metric_id=216,
        name="Parental leave eligibility",
        answer_type="text",
    )
    districts = [
        DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        DistrictCandidate(district_id=4, district_name="Delta", state="CA"),
        # District 5 is not-reviewed (no row)
        DistrictCandidate(district_id=5, district_name="Echo", state="CA"),
    ]
    rows = [
        _answer_row(
            1, "Alpha", "Birthing parent, Non-birthing parent",
            state="CA", metric_id=216, metric_name=parental_leave.name,
        ),
        _answer_row(
            2, "Bravo", "Birthing parent only",
            state="CA", metric_id=216, metric_name=parental_leave.name,
        ),
        _answer_row(
            3, "Charlie", "Not applicable",
            state="CA", metric_id=216, metric_name=parental_leave.name,
        ),
        _answer_row(
            4, "Delta", "Not applicable",
            state="CA", metric_id=216, metric_name=parental_leave.name,
        ),
        # Echo has no row → not-reviewed
    ]
    aliases = [
        _approved_metric_alias(parental_leave.name, metric_id=216),
    ]
    repository = FakeLookupRepository(
        metrics=[parental_leave],
        districts=districts,
        rows=rows,
        aliases=aliases,
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="lookup",
            question="Which districts offer parental leave beyond just the Birthing parent?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name=parental_leave.name)],
            filters=[
                FilterSpec(
                    field=parental_leave.name,
                    operator="contains",
                    value="Non-birthing parent",
                )
            ],
        )
    )

    assert isinstance(outcome, ExecutionSuccess)
    result = outcome.result
    assert isinstance(result, MetricLookupResult)

    # Only Alpha (which has Non-birthing parent) passes the filter.
    assert [row.district_name for row in result.rows] == ["Alpha"]

    prevalence = result.filter_prevalence
    assert prevalence is not None
    assert prevalence.matched == 1        # Alpha only
    assert prevalence.denominator == 2   # Alpha + Bravo (covered, real values)
    assert prevalence.na_count == 2      # Charlie + Delta
    assert prevalence.not_reviewed_count == 1  # Echo
    assert prevalence.percent is not None
    assert abs(prevalence.percent - 50.0) < 0.01  # 1/2 * 100


def test_filter_prevalence_summary_extra_forbid_round_trip() -> None:
    """FilterPrevalenceSummary serializes and deserializes with extra='forbid'."""
    summary = FilterPrevalenceSummary(
        matched=42,
        denominator=100,
        percent=42.0,
        not_reviewed_count=27,
        na_count=0,
    )
    serialized = summary.model_dump()
    assert serialized == {
        "matched": 42,
        "denominator": 100,
        "percent": 42.0,
        "not_reviewed_count": 27,
        "na_count": 0,
    }
    # Round-trip through model_validate (tests extra='forbid' contract).
    restored = FilterPrevalenceSummary.model_validate(serialized)
    assert restored == summary

    # Extra fields must be rejected (contract canary).
    import pytest as _pytest
    from pydantic import ValidationError
    with _pytest.raises(ValidationError):
        FilterPrevalenceSummary.model_validate({**serialized, "unexpected_field": 1})


@pytest.mark.asyncio
async def test_filter_prevalence_numeric_parity_with_count_path() -> None:
    """filter_prevalence.matched and .denominator match build_metric_count_result.

    The parity guarantee (KTD2, plan §Key Technical Decisions): for a numeric
    filter, filter_prevalence.denominator == len(real_rows) and
    filter_prevalence.matched == len(qualifying_rows) in the count builder run
    on the same fixture.
    """
    from compass_backend.execution.count import build_metric_count_result
    from compass_backend.execution.filters import ResolvedMetricFilter
    from compass_backend.artifacts import ResultSelection, SelectedDistrict

    workdays = MetricCandidate(
        metric_id=69,
        name="Total teacher workdays",
        answer_type="numeric",
    )
    # Run the lookup executor to get filter_prevalence.
    repository = _workdays_repository()
    executor = DeterministicQueryExecutor(repository)
    outcome = await executor.execute(
        _numeric_filter_plan(
            metric_name="Total teacher workdays",
            operator="greater_than",
            value=190.0,
        )
    )
    assert isinstance(outcome, ExecutionSuccess)
    result = outcome.result
    assert isinstance(result, MetricLookupResult)
    prevalence = result.filter_prevalence
    assert prevalence is not None

    # Also run build_metric_count_result on the same rows to get the count-path
    # denominator and count.  We use the pre-narrow selection (all 5 districts).
    pre_narrow_selection = ResultSelection(
        scope="all_covered_districts",
        districts=[
            SelectedDistrict(district_id=1, district_name="Alpha", state="TX"),
            SelectedDistrict(district_id=2, district_name="Bravo", state="TX"),
            SelectedDistrict(district_id=3, district_name="Charlie", state="TX"),
            SelectedDistrict(district_id=4, district_name="Delta", state="TX"),
            SelectedDistrict(district_id=5, district_name="Echo", state="TX"),
        ],
    )
    workdays_rows = [r for r in repository.rows if r.metric_id == 69]
    resolved_filter = ResolvedMetricFilter(
        metric_id=69,
        operator="greater_than",
        value=190.0,
        value_kind="numeric",
    )
    count_result = build_metric_count_result(
        _numeric_filter_plan(
            metric_name="Total teacher workdays",
            operator="greater_than",
            value=190.0,
        ),
        [(workdays, workdays_rows)],
        selection=pre_narrow_selection,
        academic_year="2024 - 2025",
        resolved_metric_filters=[resolved_filter],
    )

    # The parity assertion: matched == count path's count, denominator == count
    # path's denominator (len(real_rows) in build_metric_count_result).
    count_row = count_result.rows[0]
    assert prevalence.matched == count_row.count, (
        f"lookup matched={prevalence.matched} != count path count={count_row.count}"
    )
    assert prevalence.denominator == count_row.denominator, (
        f"lookup denominator={prevalence.denominator} != count path denominator={count_row.denominator}"
    )
