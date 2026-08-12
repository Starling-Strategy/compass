"""Tests for MVP 4 deterministic ranking execution."""

from __future__ import annotations

import pytest

from compass_backend.planning.profile_fields import normalize_plan_profile_ranking_intent
from compass_backend.execution import (
    DeterministicQueryExecutor,
    DistrictCandidate,
    DistrictResolution,
    MetricAnswerRow,
    MetricCandidate,
    normalize_district_name_for_resolution,
)
from compass_backend.catalog import CatalogResolver, NCESFieldCandidate
from compass_backend.artifacts import CitationSource
from compass_backend.contracts import (
    FilterSpec,
    LimitSpec,
    MetricSpec,
    PlannerTurn,
    ProfileFieldSpec,
    QueryPlan,
    SelectionSpec,
    SortSpec,
    SortStepSpec,
)
from compass_backend.catalog import CatalogAliasRecord
from compass_backend.quality import validate_result


class FakePolicyAnswerRepository:
    """Fake read-only repository for deterministic executor tests."""

    def __init__(
        self,
        *,
        metrics: list[MetricCandidate] | None = None,
        rows: list[MetricAnswerRow] | None = None,
        recent_rows: list[MetricAnswerRow] | None = None,
        districts: list[DistrictCandidate] | None = None,
        ambiguous: dict[str, list[DistrictCandidate]] | None = None,
        reviewed_district_ids: set[int] | None = None,
        aliases: list[CatalogAliasRecord] | None = None,
        renderer_notes: dict[str, str] | None = None,
    ) -> None:
        self.metrics = metrics or [
            MetricCandidate(
                metric_id=1234,
                name="Average teacher starting salary",
                answer_type="numeric",
            )
        ]
        self.rows = rows or []
        self.recent_rows = recent_rows or []
        self.districts = districts or []
        self.ambiguous = ambiguous or {}
        self.reviewed_district_ids = reviewed_district_ids
        self.aliases = aliases or []
        self.renderer_notes = renderer_notes or {}
        self.metric_queries: list[str] = []
        self.fetched_metric_ids: list[int] = []
        self.resolved_district_names: list[list[str]] = []
        self.profile_rank_rows: list[dict[str, object]] = []
        self.profile_lookup_rows: list[dict[str, object]] = []
        self.peer_profile_rows: list[dict[str, object]] = []
        self.nces_fields: list[NCESFieldCandidate] | None = None
        self.profile_rank_calls: list[
            tuple[str, int | None, str, set[str] | None, str]
        ] = []
        self.profile_lookup_calls: list[tuple[list[str], str, set[str] | None, str]] = []
        self.peer_profile_calls: list[str] = []

    async def list_covered_districts(
        self,
        *,
        states: set[str] | None = None,
    ) -> list[DistrictCandidate]:
        state_filter = {state.upper() for state in states or set()}
        if self.districts:
            candidates = self.districts
        else:
            candidates = [
                DistrictCandidate(
                    district_id=row.district_id,
                    district_name=row.district_name,
                    state=row.state,
                )
                for row in self.rows
            ]
        seen: set[int] = set()
        covered: list[DistrictCandidate] = []
        for district in candidates:
            if district.district_id in seen:
                continue
            if state_filter and (district.state or "").upper() not in state_filter:
                continue
            seen.add(district.district_id)
            covered.append(district)
        return sorted(covered, key=lambda district: district.district_name.casefold())

    async def search_metrics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MetricCandidate]:
        self.metric_queries.append(query)
        return self.metrics[:limit]

    async def fetch_metrics_by_ids(
        self,
        metric_ids: list[int],
    ) -> list[MetricCandidate]:
        self.metric_queries.append(
            "ids:" + ",".join(str(metric_id) for metric_id in metric_ids)
        )
        by_id = {metric.metric_id: metric for metric in self.metrics}
        return [by_id[metric_id] for metric_id in metric_ids if metric_id in by_id]

    async def fetch_renderer_notes(self, note_keys: list[str]):
        from compass_backend.catalog import RendererNote

        return [
            RendererNote(
                note_key=key,
                note_text=self.renderer_notes[key],
                source="SSN-235",
                provenance="Test governed renderer note",
                scenario_ids=["SSN-235"],
                review_status="approved",
                active=True,
            )
            for key in note_keys
            if key in self.renderer_notes
        ]

    async def search_catalog_aliases(
        self,
        alias: str,
        *,
        entity_types: set[str],
    ) -> list[CatalogAliasRecord]:
        normalized = alias.casefold()
        return [
            record
            for record in self.aliases
            if record.normalized_alias == normalized
            and record.entity_type in entity_types
            and record.active
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

    async def rank_districts_by_profile_field(
        self,
        field_key: str,
        *,
        limit: int | None = None,
        direction: str = "desc",
        states: set[str] | None = None,
        academic_year: str,
    ) -> list[dict[str, object]]:
        self.profile_rank_calls.append(
            (field_key, limit, direction, states, academic_year)
        )
        state_filter = {state.upper() for state in states or set()}
        rows = [
            row
            for row in self.profile_rank_rows
            if not state_filter or str(row.get("state") or "").upper() in state_filter
        ]
        reverse = direction == "desc"
        rows.sort(
            key=lambda row: (
                -float(row["value"]) if reverse else float(row["value"]),
                str(row["district_name"]).casefold(),
            )
        )
        return rows[:limit] if limit is not None else rows

    async def search_nces_fields(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[NCESFieldCandidate]:
        if self.nces_fields is not None:
            return self.nces_fields[:limit]
        return [
            NCESFieldCandidate(
                field_key="enrollment",
                label="Enrollment",
                data_type="integer",
                description="Total district enrollment.",
            )
        ][:limit]

    async def lookup_nces_profile_values(
        self,
        district_names: list[str],
        *,
        field_key: str,
        states: set[str] | None = None,
        academic_year: str,
    ) -> list[dict[str, object]]:
        self.profile_lookup_calls.append(
            (district_names, field_key, states, academic_year)
        )
        state_filter = {state.upper() for state in states or set()}
        return [
            row
            for row in self.profile_lookup_rows
            if str(row["requested_name"]) in district_names
            and row["field_key"] == field_key
            and (not state_filter or str(row.get("state") or "").upper() in state_filter)
        ]

    async def list_covered_nces_profiles(
        self,
        *,
        academic_year: str,
    ) -> list[dict[str, object]]:
        self.peer_profile_calls.append(academic_year)
        return self.peer_profile_rows


def _row(
    district_id: int,
    district_name: str,
    value: object,
    *,
    answer_id: int | None = None,
    citations: list[CitationSource] | None = None,
    metric_id: int = 1234,
    state: str = "CA",
) -> MetricAnswerRow:
    return MetricAnswerRow(
        answer_id=answer_id,
        district_id=district_id,
        district_name=district_name,
        state=state,
        metric_id=metric_id,
        metric_name="Average teacher starting salary",
        value=value,
        academic_year="2024 - 2025",
        citations=citations or [],
    )


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


def _methodology_codes(result: object) -> list[str]:
    return [ref.code for ref in result.methodology_codes]  # type: ignore[attr-defined]


def _peer_profile(
    district_id: int,
    district_name: str,
    state: str,
    *,
    enrollment: int = 50000,
    latitude: float = 39.75,
    longitude: float = -105.0,
    locale_text: str = "City: Large",
    frpl_pct: float = 50.0,
) -> dict[str, object]:
    return {
        "district_id": district_id,
        "district_name": district_name,
        "state": state,
        "city": district_name.upper(),
        "enrollment": enrollment,
        "locale_text": locale_text,
        "latitude": latitude,
        "longitude": longitude,
        "total_rev_pp": 20000.0,
        "total_exp_pp": 19000.0,
        "pupil_teacher_ratio": 15.0,
        "frpl_pct": frpl_pct,
    }


def _ranking_plan(
    *,
    limit: LimitSpec | None = None,
    scope: str = "all_covered_districts",
    districts: list[str] | None = None,
    states: list[str] | None = None,
    filters: list[FilterSpec] | None = None,
) -> QueryPlan:
    return QueryPlan(
        question="Rank covered districts by starting salary.",
        selection=SelectionSpec(
            scope=scope,
            districts=districts or [],
            states=states or [],
        ),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        filters=filters or [],
        limit=limit,
    )


def _salary_rows(count: int) -> list[MetricAnswerRow]:
    return [
        _row(
            index,
            f"District {index:02d}",
            f"${100_000 - index:,}",
            citations=[_source(f"District {index:02d} Contract.pdf", district_id=index)],
        )
        for index in range(1, count + 1)
    ]


def _observation_metrics() -> list[MetricCandidate]:
    return [
        MetricCandidate(
            metric_id=39,
            name=(
                "Minimum number of formal observations per evaluation cycle "
                "for non-tenured teachers"
            ),
            answer_type="numeric",
        ),
        MetricCandidate(
            metric_id=44,
            name=(
                "Minimum number of formal observations per evaluation cycle "
                "for tenured teachers"
            ),
            answer_type="numeric",
        ),
        MetricCandidate(
            metric_id=40,
            name=(
                "Minimum number of informal observations per evaluation cycle "
                "for non-tenured teachers"
            ),
            answer_type="numeric",
        ),
    ]


def _profile_enrollment_rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "district_id": index,
            "district_name": f"District {index:02d}",
            "state": "CA",
            "field_key": "enrollment",
            "label": "NCES enrollment",
            "value": 100_000 - index,
            "display_value": f"{100_000 - index:,}",
            "academic_year": "2024 - 2025",
            "vintage": "NCES directory year: 2024",
        }
        for index in range(1, count + 1)
    ]


def _metric_alias(
    alias: str,
    *,
    status: str = "approved",
    entity_type: str = "metric",
    canonical_id: str | None = None,
    canonical_ids: list[str] | None = None,
    candidate_refs: list[dict[str, object]] | None = None,
    metadata: dict[str, object] | None = None,
) -> CatalogAliasRecord:
    return CatalogAliasRecord(
        alias=alias,
        normalized_alias=alias.casefold(),
        entity_type=entity_type,
        resolution_status=status,
        canonical_id=canonical_id,
        canonical_ids=canonical_ids or [],
        candidate_refs=candidate_refs or [],
        source="SSN-235",
        provenance="Test governed recognition row",
        scenario_ids=["SSN-235"],
        review_status="approved",
        metadata=metadata or {},
    )


def test_normalizes_district_names_for_exact_resolution() -> None:
    assert normalize_district_name_for_resolution(
        "Alpha Public School District"
    ) == "alpha"
    assert normalize_district_name_for_resolution("  Alpha--Unified Schools ") == "alpha"
    assert (
        normalize_district_name_for_resolution("San Bernardino City Unified")
        == "san bernardino city"
    )


def test_query_plan_models_ordered_selection_and_presentation_sort_steps() -> None:
    plan = QueryPlan(
        question=(
            "Of the 10 largest districts by enrollment, which pay teachers the "
            "highest starting salary?"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="enrollment",
                direction="desc",
                key_type="profile_field",
                limit=LimitSpec(count=10, kind="top"),
            ),
            SortStepSpec(
                phase="presentation",
                field="starting salary",
                direction="desc",
                key_type="policy_metric",
            ),
        ],
    )

    assert [step.phase for step in plan.sort_steps] == [
        "selection",
        "presentation",
    ]
    assert plan.sort_steps[0].limit == LimitSpec(count=10, kind="top")
    assert plan.sort_steps[1].field == "starting salary"


@pytest.mark.asyncio
async def test_ranks_descending_before_applying_limit() -> None:
    repository = FakePolicyAnswerRepository(
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _row(
                2,
                "Bravo",
                "$70,000",
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
            _row(
                3,
                "Charlie",
                "$60,000",
                citations=[_source("Charlie Contract.pdf", district_id=3)],
            ),
        ]
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(limit=LimitSpec(count=2, kind="top"))
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Bravo", "Charlie"]
    assert [row.value for row in outcome.result.rows] == [70000.0, 60000.0]
    assert [row.rank for row in outcome.result.rows] == [1, 2]
    assert outcome.result.total_considered == 3
    assert outcome.result.excluded_count == 0
    assert _methodology_codes(outcome.result) == [
        "citation_answer_level_preferred_source_fallback",
    ]


@pytest.mark.asyncio
async def test_unbounded_metric_ranking_does_not_apply_default_limit() -> None:
    repository = FakePolicyAnswerRepository(rows=_salary_rows(12))
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_ranking_plan())

    assert outcome.result is not None
    assert len(outcome.result.rows) == 12
    assert [row.rank for row in outcome.result.rows] == list(range(1, 13))
    assert outcome.result.rows[0].district_name == "District 01"
    assert outcome.result.rows[-1].district_name == "District 12"
    assert outcome.result.total_considered == 12


@pytest.mark.asyncio
async def test_explicit_top_limit_still_limits_metric_ranking() -> None:
    repository = FakePolicyAnswerRepository(rows=_salary_rows(12))
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(limit=LimitSpec(count=10, kind="top"))
    )

    assert outcome.result is not None
    assert len(outcome.result.rows) == 10
    assert [row.rank for row in outcome.result.rows] == list(range(1, 11))
    assert outcome.result.rows[-1].district_name == "District 10"
    assert outcome.result.total_considered == 12


@pytest.mark.asyncio
async def test_broad_observation_question_clarifies_before_metric_execution() -> None:
    repository = FakePolicyAnswerRepository(metrics=_observation_metrics())
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="count",
            question="How many districts watch teachers teach?",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name=_observation_metrics()[0].name)],
        )
    )

    assert outcome.result is None
    assert outcome.clarification is not None
    assert outcome.clarification.missing_fields == ["metric"]
    assert outcome.clarification.candidates == [
        metric.name for metric in _observation_metrics()
    ]
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_specific_observation_status_executes_selected_metric() -> None:
    observation_metric = _observation_metrics()[0]
    repository = FakePolicyAnswerRepository(
        metrics=[observation_metric],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[
            _row(
                1,
                "Alpha",
                "3",
                metric_id=observation_metric.metric_id,
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="count",
            question=(
                "How many districts require formal observations "
                "for non-tenured teachers?"
            ),
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name=observation_metric.name)],
        )
    )

    assert outcome.result is not None
    assert outcome.clarification is None
    assert repository.fetched_metric_ids == [39]


@pytest.mark.asyncio
async def test_colloquial_observation_question_executes_when_plan_names_specific_lane() -> None:
    observation_metric = _observation_metrics()[0]
    repository = FakePolicyAnswerRepository(
        metrics=[observation_metric],
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[
            _row(
                1,
                "Alpha",
                "3",
                metric_id=observation_metric.metric_id,
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        QueryPlan(
            operation="count",
            question=(
                "How many districts actually watch teachers teach more than "
                "a couple times a year?"
            ),
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name=observation_metric.name)],
            filters=[FilterSpec(field="value", operator="greater_than", value=2)],
        )
    )

    assert outcome.result is not None
    assert outcome.clarification is None


@pytest.mark.asyncio
async def test_catalog_surfaces_only_observation_family_ambiguity() -> None:
    """#1008: the executor's clarify decision is sourced from a typed catalog
    method that surfaces *only* observation-family ambiguity. A non-observation
    question returns None, so the executor falls through to per-metric
    resolution instead of clarifying the whole question — preserving the
    scoping the deleted execution-time prose gate (scoping.py:114) provided."""
    repository = FakePolicyAnswerRepository(metrics=_observation_metrics())
    resolver = CatalogResolver(repository)

    non_observation = await resolver.clarifying_observation_metric_bundle(
        "What is the average teacher salary?",
        numeric_only=True,
    )
    assert non_observation is None

    observation = await resolver.clarifying_observation_metric_bundle(
        "How many districts watch teachers teach?",
        numeric_only=True,
    )
    assert observation is not None
    assert observation.ambiguous is True
    assert [candidate.name for candidate in observation.candidates] == [
        metric.name for metric in _observation_metrics()
    ]


@pytest.mark.asyncio
async def test_ranks_profile_field_without_policy_metric_rows() -> None:
    repository = FakePolicyAnswerRepository(
        aliases=[
            _metric_alias(
                "enrollment size",
                entity_type="nces_field",
                canonical_id="enrollment",
            )
        ]
    )
    repository.profile_rank_rows = [
        {
            "district_id": 1,
            "district_name": "Alpha",
            "state": "CA",
            "field_key": "enrollment",
            "label": "NCES enrollment",
            "value": 30000,
            "display_value": "30,000",
            "academic_year": "2024 - 2025",
            "vintage": "NCES directory year: 2023",
        },
        {
            "district_id": 2,
            "district_name": "Bravo",
            "state": "CA",
            "field_key": "enrollment",
            "label": "NCES enrollment",
            "value": 20000,
            "display_value": "20,000",
            "academic_year": "2024 - 2025",
            "vintage": "NCES directory year: 2023",
        },
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        question="Rank districts by enrollment, largest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="enrollment")],
        sort=SortSpec(field="enrollment", direction="desc"),
        sort_steps=[
            SortStepSpec(
                phase="presentation",
                field="enrollment",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha", "Bravo"]
    assert [row.value for row in outcome.result.rows] == [30000.0, 20000.0]
    assert outcome.result.order_statement == (
        "Ranked by NCES enrollment, highest to lowest."
    )
    assert _methodology_codes(outcome.result) == [
        "profile_rank_uses_profile_field"
    ]
    assert outcome.result.source_notes == ["NCES directory year: 2023"]
    assert validate_result(plan, outcome.result, authority=outcome.authority).valid is True
    assert repository.fetched_metric_ids == []
    assert repository.profile_rank_calls == [
        ("enrollment", None, "desc", None, "2024 - 2025")
    ]


@pytest.mark.asyncio
async def test_unbounded_profile_ranking_does_not_apply_default_limit() -> None:
    repository = FakePolicyAnswerRepository()
    repository.profile_rank_rows = _profile_enrollment_rows(12)
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        question="Rank districts by enrollment, largest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="enrollment")],
        sort=SortSpec(field="enrollment", direction="desc"),
        sort_steps=[
            SortStepSpec(
                phase="presentation",
                field="enrollment",
                direction="desc",
                key_type="profile_field",
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert len(outcome.result.rows) == 12
    assert outcome.result.rows[-1].district_name == "District 12"
    assert repository.profile_rank_calls == [
        ("enrollment", None, "desc", None, "2024 - 2025")
    ]


@pytest.mark.asyncio
async def test_largest_districts_default_still_uses_default_limit() -> None:
    repository = FakePolicyAnswerRepository(rows=_salary_rows(12))
    repository.profile_rank_rows = _profile_enrollment_rows(12)
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        question="Show starting salary for the largest districts.",
        selection=SelectionSpec(scope="largest_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert len(outcome.result.rows) == 10
    assert outcome.result.rows[-1].district_name == "District 10"
    assert repository.profile_rank_calls == [
        ("enrollment", 10, "desc", None, "2024 - 2025")
    ]


@pytest.mark.asyncio
async def test_profile_lookup_returns_nces_only_district_with_source_provenance() -> None:
    repository = FakePolicyAnswerRepository(
        aliases=[
            _metric_alias(
                "enrollment size",
                entity_type="nces_field",
                canonical_id="enrollment",
            )
        ]
    )
    repository.profile_lookup_rows = [
        {
            "requested_name": "Indian river school district",
            "district_id": None,
            "district_name": "Indian River School District",
            "state": "DE",
            "field_key": "enrollment",
            "label": "Enrollment",
            "value": 10799,
            "display_value": "10,799",
            "academic_year": "2024 - 2025",
            "source": "compass.nces_districts",
            "source_label": "NCES district profile",
            "provenance": "NCES directory year: 2022",
            "covered_by_compass": False,
        }
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="profile_lookup",
        question="What is the enrollment size of Indian river school district in Delaware?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Indian river school district"],
            states=["DE"],
        ),
        profile_fields=[ProfileFieldSpec(name="enrollment size")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "profile_lookup"
    row = outcome.result.rows[0]
    assert row.district_id is None
    assert row.district_name == "Indian River School District"
    assert row.state == "DE"
    assert row.field_key == "enrollment"
    assert row.display_value == "10,799"
    assert row.source == "profile_field"
    assert row.source_label == "NCES district profile"
    assert row.provenance == "NCES directory year: 2022"
    assert row.covered_by_compass is False
    assert row.coverage_state == "covered"
    assert _methodology_codes(outcome.result) == [
        "profile_lookup_approved_field",
        "profile_lookup_compass_coverage_flag",
    ]
    assert outcome.result.source_notes == []
    assert validate_result(plan, outcome.result, authority=outcome.authority).valid
    assert repository.profile_lookup_calls == [
        (
            ["Indian river school district"],
            "enrollment",
            {"DE"},
            "2024 - 2025",
        )
    ]


@pytest.mark.asyncio
async def test_profile_lookup_inventory_returns_multiple_governed_fields() -> None:
    repository = FakePolicyAnswerRepository(
        aliases=[
            _metric_alias(
                "enrollment",
                entity_type="nces_field",
                canonical_id="enrollment",
            ),
            _metric_alias(
                "teachers_fte",
                entity_type="nces_field",
                canonical_id="teachers_fte",
            ),
            _metric_alias(
                "frpl_pct",
                entity_type="nces_field",
                canonical_id="frpl_pct",
            ),
        ],
        districts=[
            DistrictCandidate(
                district_id=101,
                district_name="Philadelphia School District",
                state="PA",
            )
        ],
    )
    repository.profile_lookup_rows = [
        {
            "requested_name": "Philadelphia School District",
            "district_id": 101,
            "district_name": "Philadelphia School District",
            "state": "PA",
            "field_key": "enrollment",
            "label": "Enrollment",
            "value": 120000,
            "display_value": "120,000",
            "academic_year": "2024 - 2025",
            "source": "compass.nces_districts",
            "source_label": "NCES district profile",
            "provenance": "NCES directory year: 2022",
            "covered_by_compass": True,
        },
        {
            "requested_name": "Philadelphia School District",
            "district_id": 101,
            "district_name": "Philadelphia School District",
            "state": "PA",
            "field_key": "teachers_fte",
            "label": "Teachers FTE",
            "value": 7000.5,
            "display_value": "7,000.5",
            "academic_year": "2024 - 2025",
            "source": "compass.nces_districts",
            "source_label": "NCES district profile",
            "provenance": "NCES directory year: 2022",
            "covered_by_compass": True,
        },
        {
            "requested_name": "Philadelphia School District",
            "district_id": 101,
            "district_name": "Philadelphia School District",
            "state": "PA",
            "field_key": "frpl_pct",
            "label": "FRPL %",
            "value": 72.4,
            "display_value": "72.4%",
            "academic_year": "2024 - 2025",
            "source": "compass.nces_districts",
            "source_label": "NCES district profile",
            "provenance": "NCES directory year: 2022",
            "covered_by_compass": True,
        },
    ]
    repository.nces_fields = [
        NCESFieldCandidate(
            field_key="enrollment",
            label="Enrollment",
            data_type="integer",
            description="Total district enrollment.",
        ),
        NCESFieldCandidate(
            field_key="teachers_fte",
            label="Teachers FTE",
            data_type="numeric",
            description="Teacher full-time equivalent count.",
        ),
        NCESFieldCandidate(
            field_key="frpl_pct",
            label="FRPL %",
            data_type="numeric",
            description="Free and reduced-price lunch percentage.",
        ),
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="profile_lookup",
        question="what data do you have about Philadelphia school district?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Philadelphia School District"],
        ),
        profile_fields=[
            ProfileFieldSpec(name="enrollment"),
            ProfileFieldSpec(name="teachers_fte"),
            ProfileFieldSpec(name="frpl_pct"),
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [row.field_key for row in outcome.result.rows] == [
        "enrollment",
        "teachers_fte",
        "frpl_pct",
    ]
    assert validate_result(plan, outcome.result, authority=outcome.authority).valid
    assert repository.profile_lookup_calls == [
        (["Philadelphia School District"], "enrollment", None, "2024 - 2025"),
        (["Philadelphia School District"], "teachers_fte", None, "2024 - 2025"),
        (["Philadelphia School District"], "frpl_pct", None, "2024 - 2025"),
    ]


@pytest.mark.asyncio
async def test_profile_lookup_refuses_unapproved_profile_field() -> None:
    repository = FakePolicyAnswerRepository()
    repository.nces_fields = []
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="profile_lookup",
        question="What is the mascot of Indian River?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Indian River School District"],
            states=["DE"],
        ),
        profile_fields=[ProfileFieldSpec(name="mascot")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is None
    assert "NCES/profile field" in outcome.message
    assert repository.profile_lookup_calls == []


@pytest.mark.asyncio
async def test_frpl_profile_selection_normalization_still_requires_catalog_authority() -> None:
    repository = FakePolicyAnswerRepository()
    repository.nces_fields = []
    executor = DeterministicQueryExecutor(repository)
    plan = normalize_plan_profile_ranking_intent(
        QueryPlan(
            question=(
                "Show me starting salary for districts with the highest "
                "free and reduced lunch share."
            ),
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="Average teacher starting salary")],
            profile_fields=[ProfileFieldSpec(name="free and reduced lunch share")],
            limit=LimitSpec(count=10, kind="top"),
        )
    )

    outcome = await executor.execute(plan)

    assert outcome.result is None
    assert "NCES/profile field" in outcome.message
    assert repository.profile_rank_calls == []


def test_profile_sort_normalization_drops_profile_metric_duplicate() -> None:
    plan = normalize_plan_profile_ranking_intent(
        QueryPlan(
            operation="rank",
            question="Show salaries for highest FRPL districts.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[
                MetricSpec(name="Average teacher starting salary"),
                MetricSpec(name="free and reduced lunch share", role="comparison"),
            ],
            sort_steps=[
                SortStepSpec(
                    phase="selection",
                    field="free and reduced lunch share",
                    direction="desc",
                    key_type="profile_field",
                )
            ],
        )
    )

    assert [metric.name for metric in plan.metrics] == [
        "Average teacher starting salary"
    ]
    assert plan.sort_steps[0].field == "free and reduced lunch share"


def test_profile_sort_normalization_promotes_profile_sort_field() -> None:
    plan = normalize_plan_profile_ranking_intent(
        QueryPlan(
            operation="rank",
            question="Show MA salaries for lowest FRPL districts.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[
                MetricSpec(
                    name="starting teacher salaries for teachers with a masters",
                    degree_lane="ma",
                ),
                MetricSpec(name="free and reduced lunch share", role="comparison"),
            ],
            sort=SortSpec(field="free and reduced lunch share", direction="asc"),
        )
    )

    assert [metric.name for metric in plan.metrics] == [
        "starting teacher salaries for teachers with a masters"
    ]
    assert plan.sort is None
    assert plan.sort_steps == [
        SortStepSpec(
            phase="selection",
            field="frpl_pct",
            direction="asc",
            key_type="profile_field",
        )
    ]


def test_profile_sort_normalization_drops_redundant_policy_presentation_step() -> None:
    plan = normalize_plan_profile_ranking_intent(
        QueryPlan(
            operation="rank",
            question="Show salaries for lowest FRPL districts.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[
                MetricSpec(
                    name="Annual base salary for a first year teacher with a master's degree",
                    degree_lane="ma",
                ),
                MetricSpec(name="free and reduced lunch percentage", role="comparison"),
            ],
            profile_fields=[
                ProfileFieldSpec(name="free and reduced lunch percentage")
            ],
            sort_steps=[
                SortStepSpec(
                    phase="selection",
                    field="free and reduced lunch percentage",
                    direction="asc",
                    key_type="profile_field",
                    limit=LimitSpec(count=10, kind="top"),
                ),
                SortStepSpec(
                    phase="presentation",
                    field="Annual base salary for a first year teacher with a master's degree",
                    direction="desc",
                    key_type="policy_metric",
                ),
            ],
        )
    )

    assert plan.sort_steps == [
        SortStepSpec(
            phase="selection",
            field="free and reduced lunch percentage",
            direction="asc",
            key_type="profile_field",
            limit=LimitSpec(count=10, kind="top"),
        )
    ]


@pytest.mark.asyncio
async def test_peer_comparison_uses_deterministic_peer_profiles_and_policy_citations() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[
            MetricCandidate(
                metric_id=198,
                name="Maximum number of annual paid sick days",
                answer_type="numeric",
            )
        ],
        aliases=[
            CatalogAliasRecord(
                alias="sick leave",
                normalized_alias="sick leave",
                entity_type="metric_bundle",
                resolution_status="approved",
                canonical_ids=["198"],
                source="M2-730",
                provenance="Reviewed broad sick leave bundle.",
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="sick leave policy",
                normalized_alias="sick leave policy",
                entity_type="metric_bundle",
                resolution_status="approved",
                canonical_ids=["198"],
                source="SSN-235",
                provenance="Reviewed sick leave bundle.",
                review_status="approved",
            )
        ],
        districts=[
            DistrictCandidate(
                district_id=26,
                district_name="Denver Public Schools",
                state="CO",
            )
        ],
        rows=[
            _row(
                26,
                "Denver Public Schools",
                "10",
                answer_id=260198,
                metric_id=198,
                state="CO",
                citations=[
                    _source(
                        "Denver Public Schools. CO. (2024-2025). Agreement. p. 10.",
                        district_id=26,
                    )
                ],
            ),
            _row(
                29,
                "Jeffco Public Schools",
                "9",
                answer_id=290198,
                metric_id=198,
                state="CO",
                citations=[
                    _source(
                        "Jeffco Public Schools. CO. (2024-2025). Agreement. p. 5.",
                        district_id=29,
                    )
                ],
            ),
        ],
    )
    repository.peer_profile_rows = [
        {
            "district_id": 26,
            "district_name": "Denver Public Schools",
            "state": "CO",
            "city": "DENVER",
            "enrollment": 87883,
            "locale_text": "City: Large",
            "frpl_pct": 62.0,
            "latitude": 39.745750,
            "longitude": -104.985751,
            "total_rev_pp": 18381.59,
            "total_exp_pp": 18137.07,
            "pupil_teacher_ratio": 14.8,
        },
        {
            "district_id": 29,
            "district_name": "Jeffco Public Schools",
            "state": "CO",
            "city": "GOLDEN",
            "enrollment": 75327,
            "locale_text": "Suburb: Large",
            "frpl_pct": 57.0,
            "latitude": 39.740,
            "longitude": -105.220,
            "total_rev_pp": 23900.00,
            "total_exp_pp": 21000.00,
            "pupil_teacher_ratio": 16.6,
        },
    ]
    executor = DeterministicQueryExecutor(repository, default_limit=1)
    plan = QueryPlan(
        operation="peer_comparison",
        question=(
            "I work in Denver Public Schools. Who are our peer districts and "
            "how do our sick leave policies compare?"
        ),
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="sick leave")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "peer_comparison"
    assert [district.district_name for district in outcome.result.selection.districts] == [
        "Denver Public Schools",
        "Jeffco Public Schools",
    ]
    assert [(row.district_name, row.peer_role, row.peer_rank) for row in outcome.result.rows] == [
        ("Denver Public Schools", "anchor", None),
        ("Jeffco Public Schools", "peer", 1),
    ]
    assert [
        (row.district_name, row.peer_enrollment, row.peer_urbanicity)
        for row in outcome.result.rows
    ] == [
        ("Denver Public Schools", 87883, "City: Large"),
        ("Jeffco Public Schools", 75327, "Suburb: Large"),
    ]
    assert outcome.result.rows[1].peer_selection_method == "nces_similarity"
    assert outcome.result.rows[1].peer_reason.startswith("Similar enrollment")
    assert "urbanicity" in outcome.result.rows[1].peer_reason
    assert "FRPL" in outcome.result.rows[1].peer_reason
    assert "miles apart" not in outcome.result.rows[1].peer_reason
    assert "Both in" not in outcome.result.rows[1].peer_reason
    assert outcome.result.rows[0].citation_markers
    # Charts are suppressed for peer comparisons with fewer than 3 numeric points
    assert outcome.result.chart is None
    assert _methodology_codes(outcome.result) == [
        "peer_selection_nces_profiles",
        "peer_score_method",
        "peer_scoring_policy_disclosure",
        "peer_policy_cells_with_citations",
        "peer_metric_coverage_screen_applied",
    ]
    # The new policy-disclosure code carries the active policy_version
    # so the rendered methodology line cites the governed scoring policy.
    policy_refs = [
        ref for ref in outcome.result.methodology_codes
        if ref.code == "peer_scoring_policy_disclosure"
    ]
    assert len(policy_refs) == 1
    assert policy_refs[0].metadata.get("policy_version") == (
        "nces-similarity-v2-demography-2026-05"
    )
    assert outcome.result.source_notes == []
    assert validate_result(plan, outcome.result, authority=outcome.authority).valid


@pytest.mark.asyncio
async def test_peer_comparison_formats_scenario_291_salary_values_as_whole_dollars() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[
            MetricCandidate(
                metric_id=112,
                name=(
                    "Maximum base salary for a teacher with a bachelor's degree, "
                    "based on new hire schedule"
                ),
                answer_type="numeric",
                topic="Salary",
            )
        ],
        aliases=[
            CatalogAliasRecord(
                alias="Maximum teacher salary",
                normalized_alias="maximum teacher salary",
                entity_type="metric_bundle",
                resolution_status="approved",
                canonical_ids=["112"],
                source="scenario-291",
                provenance="Reviewed maximum salary phrase.",
                review_status="approved",
            )
        ],
        districts=[
            DistrictCandidate(
                district_id=18,
                district_name="San Bernardino City Unified School District",
                state="CA",
            )
        ],
        rows=[
            _row(
                18,
                "San Bernardino City Unified School District",
                "$136,734.00",
                answer_id=180112,
                metric_id=112,
                state="CA",
                citations=[_source("San Bernardino Salary Schedule.pdf", district_id=18)],
            ),
            _row(
                221,
                "Fresno Unified School District",
                "130000.00",
                answer_id=221112,
                metric_id=112,
                state="CA",
                citations=[_source("Fresno Salary Schedule.pdf", district_id=221)],
            ),
        ],
    )
    repository.peer_profile_rows = [
        {
            "district_id": 18,
            "district_name": "San Bernardino City Unified School District",
            "state": "CA",
            "city": "SAN BERNARDINO",
            "enrollment": 47838,
            "locale_text": "City: Midsize",
            "latitude": 34.1083,
            "longitude": -117.2898,
            "total_rev_pp": 21400.00,
            "total_exp_pp": 20500.00,
            "pupil_teacher_ratio": 22.0,
            "frpl_pct": 84.0,
        },
        {
            "district_id": 221,
            "district_name": "Fresno Unified School District",
            "state": "CA",
            "city": "FRESNO",
            "enrollment": 69995,
            "locale_text": "City: Large",
            "latitude": 36.7468,
            "longitude": -119.7726,
            "total_rev_pp": 22300.00,
            "total_exp_pp": 20900.00,
            "pupil_teacher_ratio": 21.0,
            "frpl_pct": 86.0,
        },
    ]
    executor = DeterministicQueryExecutor(repository, default_limit=1)
    plan = QueryPlan(
        operation="peer_comparison",
        question=(
            "What is the maximum teacher salary in San Bernardino City Unified "
            "and comparable districts?"
        ),
        selection=SelectionSpec(
            scope="named_districts",
            districts=["San Bernardino City Unified School District"],
            states=["CA"],
        ),
        metrics=[MetricSpec(name="Maximum teacher salary")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "peer_comparison"
    assert [row.display_value for row in outcome.result.rows] == [
        "$136,734",
        "$130,000",
    ]
    assert [row.coverage_display for row in outcome.result.rows] == [
        "$136,734",
        "$130,000",
    ]
    assert [row.value for row in outcome.result.rows] == [136734, 130000]
    assert [row["display_value"] for row in outcome.result.csv_export.rows] == [
        "$136,734",
        "$130,000",
    ]
    assert [row["value"] for row in outcome.result.csv_export.rows] == [
        136734,
        130000,
    ]


@pytest.mark.asyncio
async def test_peer_comparison_screens_unavailable_peers_before_final_limit() -> None:
    """Metric-bearing peer comparison should drop N/A, INA, and not-reviewed peers."""

    repository = FakePolicyAnswerRepository(
        metrics=[
            MetricCandidate(
                metric_id=214,
                name="Total paid sick and personal leave days",
                answer_type="numeric",
                topic="Leave",
            )
        ],
        districts=[
            DistrictCandidate(
                district_id=18,
                district_name="San Bernardino City Unified School District",
                state="CA",
            )
        ],
        reviewed_district_ids={18, 9, 14, 11, 8, 24, 81, 151, 185},
        rows=[
            _row(
                18,
                "San Bernardino City Unified School District",
                "10",
                metric_id=214,
                state="CA",
            ),
            _row(9, "Corona-Norco Unified School District", "N/A", metric_id=214, state="CA"),
            _row(
                14,
                "Long Beach Unified School District",
                "Issue not addressed in the documents reviewed.",
                metric_id=214,
                state="CA",
            ),
            _row(24, "Aurora Public Schools", "12", metric_id=214, state="CO"),
            _row(81, "Detroit Public Schools Community District", "10", metric_id=214, state="MI"),
            _row(151, "El Paso Independent School District", "13", metric_id=214, state="TX"),
            _row(185, "Milwaukee Public Schools", "12.50", metric_id=214, state="WI"),
        ],
    )
    repository.peer_profile_rows = [
        _peer_profile(18, "San Bernardino City Unified School District", "CA", latitude=34.10, longitude=-117.30),
        _peer_profile(9, "Corona-Norco Unified School District", "CA", latitude=34.00, longitude=-117.55),
        _peer_profile(14, "Long Beach Unified School District", "CA", latitude=33.80, longitude=-118.16),
        _peer_profile(11, "Fresno Unified School District", "CA", latitude=36.75, longitude=-119.77),
        _peer_profile(8, "Capistrano Unified School District", "CA", latitude=33.50, longitude=-117.66),
        _peer_profile(24, "Aurora Public Schools", "CO", latitude=39.73, longitude=-104.83),
        _peer_profile(81, "Detroit Public Schools Community District", "MI", latitude=42.33, longitude=-83.05),
        _peer_profile(151, "El Paso Independent School District", "TX", latitude=31.76, longitude=-106.49),
        _peer_profile(185, "Milwaukee Public Schools", "WI", latitude=43.04, longitude=-87.91),
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="peer_comparison",
        question="I work in San Bernardino. Who are our peers and how do sick leave policies compare?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["San Bernardino City Unified School District"],
        ),
        metrics=[MetricSpec(name="Total paid sick and personal leave days")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    peer_rows = [row for row in outcome.result.rows if row.peer_role == "peer"]
    assert {row.district_name for row in peer_rows} == {
        "Aurora Public Schools",
        "Detroit Public Schools Community District",
        "El Paso Independent School District",
        "Milwaukee Public Schools",
    }
    assert [row.peer_rank for row in peer_rows] == [1, 2, 3, 4]
    assert {row.coverage_state for row in peer_rows} == {"covered"}
    assert {
        "Corona-Norco Unified School District",
        "Long Beach Unified School District",
        "Fresno Unified School District",
        "Capistrano Unified School District",
    }.isdisjoint({
        row.district_name for row in outcome.result.rows
    })
    assert outcome.result.excluded_count == 4
    assert "peer_metric_coverage_screen_applied" in _methodology_codes(outcome.result)


@pytest.mark.asyncio
async def test_peer_comparison_limits_same_state_peers_when_out_of_state_options_match() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[MetricCandidate(metric_id=198, name="Sick leave days", answer_type="numeric")],
        districts=[DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO")],
        rows=[
            _row(26, "Denver Public Schools", "10", metric_id=198, state="CO"),
            _row(24, "Aurora Public Schools", "12", metric_id=198, state="CO"),
            _row(25, "Cherry Creek School District", "13", metric_id=198, state="CO"),
            _row(29, "Jeffco Public Schools", "9", metric_id=198, state="CO"),
            _row(27, "Douglas County School District", "9", metric_id=198, state="CO"),
            _row(96, "Albuquerque Public Schools", "10", metric_id=198, state="NM"),
            _row(153, "Fort Worth Independent School District", "10", metric_id=198, state="TX"),
            _row(185, "Milwaukee Public Schools", "10", metric_id=198, state="WI"),
        ],
    )
    repository.peer_profile_rows = [
        _peer_profile(26, "Denver Public Schools", "CO", enrollment=87883, latitude=39.75, longitude=-104.99),
        _peer_profile(24, "Aurora Public Schools", "CO", enrollment=38135, latitude=39.73, longitude=-104.83),
        _peer_profile(25, "Cherry Creek School District", "CO", enrollment=52392, latitude=39.64, longitude=-104.88),
        _peer_profile(29, "Jeffco Public Schools", "CO", enrollment=75327, latitude=39.74, longitude=-105.22, locale_text="Suburb: Large"),
        _peer_profile(27, "Douglas County School District", "CO", enrollment=62341, latitude=39.37, longitude=-104.86, locale_text="Suburb: Large"),
        _peer_profile(96, "Albuquerque Public Schools", "NM", enrollment=80000, latitude=38.00, longitude=-103.00),
        _peer_profile(153, "Fort Worth Independent School District", "TX", enrollment=80000, latitude=38.00, longitude=-103.00),
        _peer_profile(185, "Milwaukee Public Schools", "WI", enrollment=82000, latitude=38.00, longitude=-103.00),
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's peer districts and how do sick leave policies compare?",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="Sick leave days")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    peer_rows = [row for row in outcome.result.rows if row.peer_role == "peer"]
    assert len(peer_rows) == 5
    assert sum(1 for row in peer_rows if row.state == "CO") == 2
    assert sum(1 for row in peer_rows if row.state != "CO") >= 3
    assert {row.state for row in peer_rows} >= {"NM", "TX", "WI"}
    coverage_ref = next(
        ref
        for ref in outcome.result.methodology_codes
        if ref.code == "peer_metric_coverage_screen_applied"
    )
    assert coverage_ref.metadata["same_state_cap"] == "2"
    assert coverage_ref.metadata["same_state_cap_applied"] == "false"
    assert coverage_ref.metadata["diversity_replacement_count"] == "0"
    assert coverage_ref.metadata["diversity_score_delta"] == "0.08"


@pytest.mark.asyncio
async def test_peer_comparison_does_not_replace_same_state_peers_with_distant_matches() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[MetricCandidate(metric_id=198, name="Sick leave days", answer_type="numeric")],
        districts=[DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO")],
        rows=[
            _row(26, "Denver Public Schools", "10", metric_id=198, state="CO"),
            _row(24, "Aurora Public Schools", "12", metric_id=198, state="CO"),
            _row(25, "Cherry Creek School District", "13", metric_id=198, state="CO"),
            _row(29, "Jeffco Public Schools", "9", metric_id=198, state="CO"),
            _row(27, "Douglas County School District", "9", metric_id=198, state="CO"),
            _row(28, "Boulder Valley School District", "11", metric_id=198, state="CO"),
            _row(153, "Fort Worth Independent School District", "10", metric_id=198, state="TX"),
        ],
    )
    repository.peer_profile_rows = [
        _peer_profile(26, "Denver Public Schools", "CO", enrollment=87883, latitude=39.75, longitude=-104.99),
        _peer_profile(24, "Aurora Public Schools", "CO", enrollment=38135, latitude=39.73, longitude=-104.83),
        _peer_profile(25, "Cherry Creek School District", "CO", enrollment=52392, latitude=39.64, longitude=-104.88),
        _peer_profile(29, "Jeffco Public Schools", "CO", enrollment=75327, latitude=39.74, longitude=-105.22, locale_text="Suburb: Large"),
        _peer_profile(27, "Douglas County School District", "CO", enrollment=62341, latitude=39.37, longitude=-104.86, locale_text="Suburb: Large"),
        _peer_profile(28, "Boulder Valley School District", "CO", enrollment=28000, latitude=40.02, longitude=-105.27),
        _peer_profile(153, "Fort Worth Independent School District", "TX", enrollment=5000, latitude=25.00, longitude=-80.00, locale_text="Town: Distant", frpl_pct=95.0),
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's peer districts and how do sick leave policies compare?",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="Sick leave days")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    peer_rows = [row for row in outcome.result.rows if row.peer_role == "peer"]
    assert len(peer_rows) == 5
    assert {row.state for row in peer_rows} == {"CO"}
    coverage_ref = next(
        ref
        for ref in outcome.result.methodology_codes
        if ref.code == "peer_metric_coverage_screen_applied"
    )
    assert coverage_ref.metadata["same_state_cap_applied"] == "false"
    assert coverage_ref.metadata["diversity_replacement_count"] == "0"
    assert coverage_ref.metadata["diversity_score_delta"] == "0.08"


@pytest.mark.asyncio
async def test_peer_comparison_peer_overrides_bypass_default_same_state_cap() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[MetricCandidate(metric_id=198, name="Sick leave days", answer_type="numeric")],
        districts=[DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO")],
        rows=[
            _row(26, "Denver Public Schools", "10", metric_id=198, state="CO"),
            _row(24, "Aurora Public Schools", "12", metric_id=198, state="CO"),
            _row(25, "Cherry Creek School District", "13", metric_id=198, state="CO"),
            _row(29, "Jeffco Public Schools", "9", metric_id=198, state="CO"),
            _row(27, "Douglas County School District", "9", metric_id=198, state="CO"),
            _row(96, "Albuquerque Public Schools", "10", metric_id=198, state="NM"),
            _row(153, "Fort Worth Independent School District", "10", metric_id=198, state="TX"),
        ],
    )
    repository.peer_profile_rows = [
        _peer_profile(26, "Denver Public Schools", "CO", enrollment=87883, latitude=39.75, longitude=-104.99),
        _peer_profile(24, "Aurora Public Schools", "CO", enrollment=38135, latitude=39.73, longitude=-104.83),
        _peer_profile(25, "Cherry Creek School District", "CO", enrollment=52392, latitude=39.64, longitude=-104.88),
        _peer_profile(29, "Jeffco Public Schools", "CO", enrollment=75327, latitude=39.74, longitude=-105.22, locale_text="Suburb: Large"),
        _peer_profile(27, "Douglas County School District", "CO", enrollment=62341, latitude=39.37, longitude=-104.86, locale_text="Suburb: Large"),
        _peer_profile(
            96,
            "Albuquerque Public Schools",
            "NM",
            enrollment=5000,
            latitude=38.00,
            longitude=-103.00,
            locale_text="Town: Distant",
            frpl_pct=90.0,
        ),
        _peer_profile(
            153,
            "Fort Worth Independent School District",
            "TX",
            enrollment=6000,
            latitude=38.00,
            longitude=-103.00,
            locale_text="Town: Distant",
            frpl_pct=90.0,
        ),
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's peer districts by enrollment and how do sick leave policies compare?",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="Sick leave days")],
        peer_overrides={"feature_set": "all"},
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    peer_rows = [row for row in outcome.result.rows if row.peer_role == "peer"]
    assert sum(1 for row in peer_rows if row.state == "CO") == 4


@pytest.mark.asyncio
async def test_peer_comparison_state_filter_bypasses_default_same_state_cap() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[MetricCandidate(metric_id=198, name="Sick leave days", answer_type="numeric")],
        districts=[DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO")],
        rows=[
            _row(26, "Denver Public Schools", "10", metric_id=198, state="CO"),
            _row(24, "Aurora Public Schools", "12", metric_id=198, state="CO"),
            _row(25, "Cherry Creek School District", "13", metric_id=198, state="CO"),
            _row(29, "Jeffco Public Schools", "9", metric_id=198, state="CO"),
            _row(27, "Douglas County School District", "9", metric_id=198, state="CO"),
            _row(96, "Albuquerque Public Schools", "10", metric_id=198, state="NM"),
            _row(153, "Fort Worth Independent School District", "10", metric_id=198, state="TX"),
        ],
    )
    repository.peer_profile_rows = [
        _peer_profile(26, "Denver Public Schools", "CO", enrollment=87883, latitude=39.75, longitude=-104.99),
        _peer_profile(24, "Aurora Public Schools", "CO", enrollment=38135, latitude=39.73, longitude=-104.83),
        _peer_profile(25, "Cherry Creek School District", "CO", enrollment=52392, latitude=39.64, longitude=-104.88),
        _peer_profile(29, "Jeffco Public Schools", "CO", enrollment=75327, latitude=39.74, longitude=-105.22, locale_text="Suburb: Large"),
        _peer_profile(27, "Douglas County School District", "CO", enrollment=62341, latitude=39.37, longitude=-104.86, locale_text="Suburb: Large"),
        _peer_profile(96, "Albuquerque Public Schools", "NM", enrollment=80000, latitude=38.00, longitude=-103.00),
        _peer_profile(153, "Fort Worth Independent School District", "TX", enrollment=80000, latitude=38.00, longitude=-103.00),
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's Colorado peer districts and how do sick leave policies compare?",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="Sick leave days")],
        filters=[FilterSpec(field="state", operator="equals", value="CO")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    peer_rows = [row for row in outcome.result.rows if row.peer_role == "peer"]
    assert sum(1 for row in peer_rows if row.state == "CO") == 4
    coverage_ref = next(
        ref
        for ref in outcome.result.methodology_codes
        if ref.code == "peer_metric_coverage_screen_applied"
    )
    assert coverage_ref.metadata["same_state_cap_applied"] == "false"


@pytest.mark.asyncio
async def test_peer_comparison_returns_fewer_peers_when_only_two_are_comparison_ready() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[MetricCandidate(metric_id=198, name="Sick leave days", answer_type="numeric")],
        districts=[DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO")],
        reviewed_district_ids={26, 24, 25, 29, 96},
        rows=[
            _row(26, "Denver Public Schools", "10", metric_id=198, state="CO"),
            _row(24, "Aurora Public Schools", "12", metric_id=198, state="CO"),
            _row(25, "Cherry Creek School District", "13", metric_id=198, state="CO"),
        ],
    )
    repository.peer_profile_rows = [
        _peer_profile(26, "Denver Public Schools", "CO"),
        _peer_profile(24, "Aurora Public Schools", "CO", latitude=39.73, longitude=-104.83),
        _peer_profile(25, "Cherry Creek School District", "CO", latitude=39.64, longitude=-104.88),
        _peer_profile(29, "Jeffco Public Schools", "CO", latitude=39.74, longitude=-105.22),
        _peer_profile(96, "Albuquerque Public Schools", "NM", latitude=35.08, longitude=-106.65),
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's peers and how do sick leave policies compare?",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="Sick leave days")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    peer_rows = [row for row in outcome.result.rows if row.peer_role == "peer"]
    assert [row.district_name for row in peer_rows] == [
        "Aurora Public Schools",
        "Cherry Creek School District",
    ]
    assert outcome.result.excluded_count == 2


@pytest.mark.asyncio
async def test_peer_comparison_keeps_anchor_when_anchor_lacks_current_covered_data() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[MetricCandidate(metric_id=198, name="Sick leave days", answer_type="numeric")],
        districts=[DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO")],
        reviewed_district_ids={26, 24, 25},
        rows=[
            _row(24, "Aurora Public Schools", "12", metric_id=198, state="CO"),
            _row(25, "Cherry Creek School District", "13", metric_id=198, state="CO"),
        ],
    )
    repository.peer_profile_rows = [
        _peer_profile(26, "Denver Public Schools", "CO"),
        _peer_profile(24, "Aurora Public Schools", "CO", latitude=39.73, longitude=-104.83),
        _peer_profile(25, "Cherry Creek School District", "CO", latitude=39.64, longitude=-104.88),
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's peers and how do sick leave policies compare?",
        selection=SelectionSpec(scope="named_districts", districts=["Denver Public Schools"]),
        metrics=[MetricSpec(name="Sick leave days")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    anchor_rows = [row for row in outcome.result.rows if row.peer_role == "anchor"]
    assert len(anchor_rows) == 1
    assert anchor_rows[0].district_name == "Denver Public Schools"
    assert anchor_rows[0].coverage_state == "not_reviewed"
    assert [row.district_name for row in outcome.result.rows if row.peer_role == "peer"] == [
        "Aurora Public Schools",
        "Cherry Creek School District",
    ]


@pytest.mark.asyncio
async def test_peer_comparison_refuses_when_profiles_have_no_peer_matching_data() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[
            MetricCandidate(
                metric_id=198,
                name="Maximum number of annual paid sick days",
                answer_type="numeric",
            )
        ],
        districts=[
            DistrictCandidate(
                district_id=26,
                district_name="Denver Public Schools",
                state="CO",
            )
        ],
    )
    repository.peer_profile_rows = [
        {
            "district_id": 26,
            "district_name": "Denver Public Schools",
            "state": "CO",
        },
        {
            "district_id": 29,
            "district_name": "Jeffco Public Schools",
            "state": "CO",
        },
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's comparable districts for sick leave?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="Maximum number of annual paid sick days")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is None
    assert "deterministic covered NCES peer set" in outcome.message


@pytest.mark.asyncio
async def test_peer_comparison_refuses_when_peer_authority_is_unavailable() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[
            MetricCandidate(
                metric_id=198,
                name="Maximum number of annual paid sick days",
                answer_type="numeric",
            )
        ],
        districts=[
            DistrictCandidate(
                district_id=26,
                district_name="Denver Public Schools",
                state="CO",
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="peer_comparison",
        question="Who are Denver's peers for sick leave?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="Maximum number of annual paid sick days")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is None
    assert "deterministic covered NCES peer set" in outcome.message
    assert repository.peer_profile_calls == ["2024 - 2025"]


@pytest.mark.asyncio
async def test_selection_sort_step_limits_profile_rows_before_salary_ranking() -> None:
    repository = FakePolicyAnswerRepository(
        aliases=[
            _metric_alias(
                "student enrollment",
                entity_type="nces_field",
                canonical_id="enrollment",
            )
        ],
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _row(
                2,
                "Bravo",
                "$70,000",
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
            _row(
                3,
                "Charlie",
                "$60,000",
                citations=[_source("Charlie Contract.pdf", district_id=3)],
            ),
        ]
    )
    repository.profile_rank_rows = [
        {
            "district_id": 1,
            "district_name": "Alpha",
            "state": "CA",
            "field_key": "enrollment",
            "label": "NCES enrollment",
            "value": 30000,
            "display_value": "30,000",
            "academic_year": "2024 - 2025",
            "vintage": "NCES directory year: 2023",
        },
        {
            "district_id": 2,
            "district_name": "Bravo",
            "state": "CA",
            "field_key": "enrollment",
            "label": "NCES enrollment",
            "value": 20000,
            "display_value": "20,000",
            "academic_year": "2024 - 2025",
            "vintage": "NCES directory year: 2023",
        },
        {
            "district_id": 3,
            "district_name": "Charlie",
            "state": "CA",
            "field_key": "enrollment",
            "label": "NCES enrollment",
            "value": 10000,
            "display_value": "10,000",
            "academic_year": "2024 - 2025",
            "vintage": "NCES directory year: 2023",
        },
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        question="Of the 2 largest districts by enrollment, which pay highest?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="Average teacher starting salary"),
            MetricSpec(name="student enrollment", role="grouping"),
        ],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="enrollment",
                direction="desc",
                key_type="profile_field",
                limit=LimitSpec(count=2, kind="top"),
            ),
            SortStepSpec(
                phase="presentation",
                field="Average teacher starting salary",
                direction="desc",
                key_type="policy_metric",
            ),
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Bravo", "Alpha"]
    assert [row.value for row in outcome.result.rows] == [70000.0, 50000.0]
    assert validate_result(plan, outcome.result, authority=outcome.authority).valid
    assert repository.profile_rank_calls == [
        ("enrollment", 2, "desc", None, "2024 - 2025")
    ]


@pytest.mark.asyncio
async def test_profile_field_selection_displays_policy_metric_in_profile_order() -> None:
    repository = FakePolicyAnswerRepository(
        aliases=[
            _metric_alias(
                "free-and-reduced lunch share",
                entity_type="nces_field",
                canonical_id="frpl_pct",
            )
        ],
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _row(
                2,
                "Bravo",
                "$70,000",
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
        ]
    )
    repository.nces_fields = [
        NCESFieldCandidate(
            field_key="frpl_pct",
            label="FRPL %",
            data_type="numeric",
            description="Free and reduced-price lunch share.",
        )
    ]
    repository.profile_rank_rows = [
        {
            "district_id": 3,
            "district_name": "Charlie",
            "state": "CA",
            "field_key": "frpl_pct",
            "label": "FRPL %",
            "value": 90.0,
            "display_value": "90%",
            "academic_year": "2024 - 2025",
            "vintage": "NCES release year: 2023",
        },
        {
            "district_id": 1,
            "district_name": "Alpha",
            "state": "CA",
            "field_key": "frpl_pct",
            "label": "FRPL %",
            "value": 80.0,
            "display_value": "80%",
            "academic_year": "2024 - 2025",
            "vintage": "NCES release year: 2023",
        },
        {
            "district_id": 2,
            "district_name": "Bravo",
            "state": "CA",
            "field_key": "frpl_pct",
            "label": "FRPL %",
            "value": 70.0,
            "display_value": "70%",
            "academic_year": "2024 - 2025",
            "vintage": "NCES release year: 2023",
        },
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        question=(
            "Show me starting teacher salaries for districts with the highest "
            "free-and-reduced lunch share."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="Average teacher starting salary"),
            MetricSpec(name="free-and-reduced lunch share", role="grouping"),
        ],
        sort=SortSpec(field="free-and-reduced lunch share", direction="desc"),
        limit=LimitSpec(count=3, kind="top"),
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="free-and-reduced lunch share",
                direction="desc",
                key_type="profile_field",
                limit=LimitSpec(count=3, kind="top"),
            ),
            SortStepSpec(
                phase="presentation",
                field="free-and-reduced lunch share",
                direction="desc",
                key_type="profile_field",
            ),
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == [
        "Charlie",
        "Alpha",
        "Bravo",
    ]
    assert [row.sort_display_value for row in outcome.result.rows] == [
        "90%",
        "80%",
        "70%",
    ]
    assert [row.display_value for row in outcome.result.rows] == [
        "NCTQ hasn't reviewed Charlie (2024 - 2025 data not yet reviewed).",
        "$50,000",
        "$70,000",
    ]
    assert [row.metric_name for row in outcome.result.rows] == [
        "Average teacher starting salary",
        "Average teacher starting salary",
        "Average teacher starting salary",
    ]
    assert outcome.result.order_statement == (
        "Ranked by FRPL %, highest to lowest; displayed "
        "Average teacher starting salary."
    )
    assert validate_result(plan, outcome.result, authority=outcome.authority).valid
    assert repository.profile_rank_calls == [
        ("frpl_pct", 3, "desc", None, "2024 - 2025")
    ]


@pytest.mark.asyncio
async def test_frpl_selection_defaults_broad_starting_salary_to_bachelor_display_metric() -> None:
    bachelor_metric = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    master_metric = MetricCandidate(
        metric_id=96,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
    )
    repository = FakePolicyAnswerRepository(
        metrics=[bachelor_metric, master_metric],
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                metric_id=89,
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _row(
                2,
                "Bravo",
                "$70,000",
                metric_id=89,
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
        ],
        aliases=[
            _metric_alias(
                "starting teacher salary",
                status="ambiguous",
                candidate_refs=[
                    {
                        "metric_id": 89,
                        "metric_name": bachelor_metric.name,
                    },
                    {
                        "metric_id": 96,
                        "metric_name": master_metric.name,
                    },
                ],
                metadata={
                    "contextual_defaults": {
                        "frpl_profile_sort_display": {
                            "metric_id": 89,
                            "note_keys": [
                                "bachelor_starting_salary_default_lane"
                            ],
                        }
                    }
                },
            ),
            _metric_alias(
                "free-and-reduced lunch share",
                entity_type="nces_field",
                canonical_id="frpl_pct",
            ),
        ],
        renderer_notes={
            "bachelor_starting_salary_default_lane": (
                "Used bachelor's-degree starting salary for first-year teachers "
                "because no degree lane was specified. Ask for master's starting "
                "salary to use the master's-degree lane."
            )
        },
    )
    repository.nces_fields = [
        NCESFieldCandidate(
            field_key="frpl_pct",
            label="FRPL %",
            data_type="numeric",
            description="Free and reduced-price lunch share.",
        )
    ]
    repository.profile_rank_rows = [
        {
            "district_id": 1,
            "district_name": "Alpha",
            "state": "CA",
            "field_key": "frpl_pct",
            "label": "FRPL %",
            "value": 80.0,
            "display_value": "80%",
            "academic_year": "2024 - 2025",
            "vintage": "NCES release year: 2023",
        },
        {
            "district_id": 2,
            "district_name": "Bravo",
            "state": "CA",
            "field_key": "frpl_pct",
            "label": "FRPL %",
            "value": 70.0,
            "display_value": "70%",
            "academic_year": "2024 - 2025",
            "vintage": "NCES release year: 2023",
        },
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        question=(
            "Show starting teacher salary for districts with the highest "
            "free-and-reduced lunch share."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting teacher salary")],
        profile_fields=[ProfileFieldSpec(name="free-and-reduced lunch share")],
        limit=LimitSpec(count=2, kind="top"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [row.metric_id for row in outcome.result.rows] == [89, 89]
    assert [row.metric_name for row in outcome.result.rows] == [
        bachelor_metric.name,
        bachelor_metric.name,
    ]
    assert repository.fetched_metric_ids == [89]
    assert any("bachelor's-degree starting salary" in note for note in outcome.result.source_notes)


def _broad_salary_default_repository() -> "FakePolicyAnswerRepository":
    """Repository where 'starting teacher salary' is an ambiguous BA/MA bundle
    that carries the governed ``launch_starting_salary_default`` → metric 89.

    Mirrors the production seed (seed_catalog_aliases.sql Bucket 7b: the broad
    salary alias is deliberately ``ambiguous`` for standalone execution, while
    the launch context key authorizes the bachelor's lane plus its disclosure
    note). Used by the WS-1 (#1248) commit-default scoping tests below.
    """

    bachelor_metric = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    master_metric = MetricCandidate(
        metric_id=96,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
    )
    return FakePolicyAnswerRepository(
        metrics=[bachelor_metric, master_metric],
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                metric_id=89,
                state="CA",
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _row(
                2,
                "Bravo",
                "$70,000",
                metric_id=89,
                state="CA",
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
        ],
        aliases=[
            _metric_alias(
                "starting teacher salary",
                status="ambiguous",
                entity_type="metric_bundle",
                candidate_refs=[
                    {"metric_id": 89, "metric_name": bachelor_metric.name},
                    {"metric_id": 96, "metric_name": master_metric.name},
                ],
                metadata={
                    "contextual_defaults": {
                        "launch_starting_salary_default": {
                            "metric_id": 89,
                            "note_keys": ["bachelor_starting_salary_default_lane"],
                        }
                    }
                },
            ),
        ],
        renderer_notes={
            "bachelor_starting_salary_default_lane": (
                "Used bachelor's-degree starting salary for first-year teachers "
                "because no degree lane was specified. Ask for master's starting "
                "salary to use the master's-degree lane."
            )
        },
    )


@pytest.mark.asyncio
async def test_lookup_broad_starting_salary_commits_bachelor_default_without_clarify() -> None:
    """WS-1 (#1248): a `lookup` for broad 'starting teacher salary' with no degree
    lane must COMMIT the governed launch default (metric 89, bachelor's) and
    surface its disclosure note — not dodge into a BA-vs-MA clarify.

    The rank/profile-sort paths already commit this default; this proves the
    lookup / metric-groups path (`_resolve_plan_metric_groups`) now does too,
    and that the note reaches ``result.source_notes`` end to end.
    """

    repository = _broad_salary_default_repository()
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="What is the starting teacher salary in covered districts?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting teacher salary")],
    )

    outcome = await executor.execute(plan)

    assert outcome.clarification is None, (
        f"Expected a committed answer, got a clarification: {outcome.message}"
    )
    assert outcome.result is not None
    assert {row.metric_id for row in outcome.result.rows} == {89}
    assert 96 not in repository.fetched_metric_ids
    assert any(
        "bachelor's-degree starting salary" in note
        for note in outcome.result.source_notes
    )


@pytest.mark.asyncio
async def test_resolve_plan_metrics_commits_bachelor_default_for_broad_salary() -> None:
    """WS-1 (#1248): `_resolve_plan_metrics` (the trend/peer/count metric-spec
    site) commits the governed launch default + returns its disclosure notes
    instead of returning an ExecutionClarification."""

    repository = _broad_salary_default_repository()
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="What is the starting teacher salary in covered districts?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting teacher salary")],
    )

    resolved = await executor._resolve_plan_metrics(plan, numeric_only=False)

    assert isinstance(resolved, tuple), (
        "Expected (metrics, source_notes, alternates), got "
        f"{type(resolved).__name__}: {resolved}"
    )
    metrics, source_notes, alternates = resolved
    assert [metric.metric_id for metric in metrics] == [89]
    assert any("bachelor's-degree starting salary" in note for note in source_notes)
    # Contextual-default path returns before the Fix 4B best-guess, so no alternates.
    assert alternates == []


@pytest.mark.asyncio
async def test_rank_chart_broad_starting_salary_commits_bachelor_default_without_clarify() -> None:
    """WS-1 (#1248) / case 470 regression (Fixes #1438): a `rank` (chart) of
    broad 'starting teacher salary' with no degree lane must COMMIT the governed
    launch default (metric 89, bachelor's) and surface its disclosure note — not
    dodge into a BA-vs-MA clarify.

    The plain single-metric ranking path (`_execute_ranking`) was the one rank
    surface that still clarified: it resolved the primary metric inline instead
    of through `_resolve_rank_primary_metric_with_default` (which applies the
    governed default). Debug case_id=470 / SORT-MIGRATED-281.
    """

    repository = _broad_salary_default_repository()
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question=(
            "Show me a chart of starting teacher salaries across districts, "
            "ranked from highest to lowest."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting teacher salary")],
        sort=SortSpec(field="starting teacher salary", direction="desc"),
    )

    outcome = await executor.execute(plan)

    assert outcome.clarification is None, (
        f"Expected a committed ranked chart, got a clarification: {outcome.message}"
    )
    assert outcome.result is not None
    assert 89 in repository.fetched_metric_ids
    assert 96 not in repository.fetched_metric_ids
    assert any(
        "bachelor's-degree starting salary" in note
        for note in outcome.result.source_notes
    )


@pytest.mark.asyncio
async def test_lookup_ambiguous_salary_without_launch_default_still_clarifies() -> None:
    """WS-1 boundary guard (#1248): the commit hatch fires ONLY when the alias
    carries the governed ``launch_starting_salary_default`` context key. An
    ambiguous salary bundle WITHOUT that default (the bare 'teacher salary'
    max-BA-vs-MA case the seed keeps ambiguous on purpose) must STILL clarify —
    never silently commit a metric."""

    bachelor_metric = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    max_bachelor_metric = MetricCandidate(
        metric_id=112,
        name="Maximum base salary for a teacher with a bachelor's degree",
        answer_type="numeric",
    )
    repository = FakePolicyAnswerRepository(
        metrics=[bachelor_metric, max_bachelor_metric],
        aliases=[
            _metric_alias(
                "teacher salary",
                status="ambiguous",
                entity_type="metric_bundle",
                candidate_refs=[
                    {"metric_id": 89, "metric_name": bachelor_metric.name},
                    {"metric_id": 112, "metric_name": max_bachelor_metric.name},
                ],
            ),
        ],
        rows=[_row(1, "Alpha", "$50,000", metric_id=89)],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="lookup",
        question="What is the teacher salary in covered districts?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="teacher salary")],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is None
    assert outcome.clarification is not None
    assert outcome.clarification.missing_fields == ["metric"]
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_limited_ranked_lookup_commits_bachelor_default_for_broad_salary_comparison() -> None:
    """WS-1 (#1248) / #1452: a limited ranked lookup (rank by primary metric +
    show a comparison metric) whose comparison column is a bare 'starting
    teacher salary' must COMMIT the governed bachelor's default (metric 89) +
    disclosure note — not dodge into a BA-vs-MA clarify. The secondary-metric
    loop resolved inline and skipped the default that the primary path applies.
    """

    grad = MetricCandidate(metric_id=50, name="Four-year graduation rate", answer_type="numeric")
    bachelor = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    master = MetricCandidate(
        metric_id=96,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
    )
    repository = FakePolicyAnswerRepository(
        metrics=[grad, bachelor, master],
        rows=[
            _row(1, "Alpha", "82%", metric_id=50, state="CA"),
            _row(2, "Bravo", "88%", metric_id=50, state="CA"),
            _row(1, "Alpha", "$50,000", metric_id=89, state="CA"),
            _row(2, "Bravo", "$60,000", metric_id=89, state="CA"),
        ],
        aliases=[
            _metric_alias("graduation rate", entity_type="metric", canonical_id="50"),
            _metric_alias(
                "starting teacher salary",
                status="ambiguous",
                entity_type="metric_bundle",
                candidate_refs=[
                    {"metric_id": 89, "metric_name": bachelor.name},
                    {"metric_id": 96, "metric_name": master.name},
                ],
                metadata={
                    "contextual_defaults": {
                        "launch_starting_salary_default": {
                            "metric_id": 89,
                            "note_keys": ["bachelor_starting_salary_default_lane"],
                        }
                    }
                },
            ),
        ],
        renderer_notes={
            "bachelor_starting_salary_default_lane": (
                "Used bachelor's-degree starting salary for first-year teachers "
                "because no degree lane was specified. Ask for master's starting "
                "salary to use the master's-degree lane."
            )
        },
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question="Top 2 districts by graduation rate, with their starting teacher salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="graduation rate"),
            MetricSpec(name="starting teacher salary", role="comparison"),
        ],
        sort=SortSpec(field="graduation rate", direction="desc"),
        limit=LimitSpec(count=2, kind="top"),
    )

    outcome = await executor.execute(plan)

    assert outcome.clarification is None, (
        f"Expected a committed ranked lookup, got a clarification: {outcome.message}"
    )
    assert outcome.result is not None
    assert 89 in repository.fetched_metric_ids
    assert 96 not in repository.fetched_metric_ids
    assert any(
        "bachelor's-degree starting salary" in note
        for note in outcome.result.source_notes
    )


@pytest.mark.asyncio
async def test_frpl_selection_preserves_explicit_master_salary_lane() -> None:
    bachelor_metric = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    master_metric = MetricCandidate(
        metric_id=96,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
    )
    repository = FakePolicyAnswerRepository(
        metrics=[bachelor_metric, master_metric],
        rows=[
            _row(
                1,
                "Alpha",
                "$58,000",
                metric_id=96,
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _row(
                2,
                "Bravo",
                "$72,000",
                metric_id=96,
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
        ],
        aliases=[
            _metric_alias(
                "starting teacher salary",
                status="ambiguous",
                candidate_refs=[
                    {
                        "metric_id": 89,
                        "metric_name": bachelor_metric.name,
                    },
                    {
                        "metric_id": 96,
                        "metric_name": master_metric.name,
                    },
                ],
            ),
            _metric_alias(
                "free-and-reduced lunch share",
                entity_type="nces_field",
                canonical_id="frpl_pct",
            ),
        ],
    )
    repository.nces_fields = [
        NCESFieldCandidate(
            field_key="frpl_pct",
            label="FRPL %",
            data_type="numeric",
            description="Free and reduced-price lunch share.",
        )
    ]
    repository.profile_rank_rows = [
        {
            "district_id": 1,
            "district_name": "Alpha",
            "state": "CA",
            "field_key": "frpl_pct",
            "label": "FRPL %",
            "value": 80.0,
            "display_value": "80%",
            "academic_year": "2024 - 2025",
            "vintage": "NCES release year: 2023",
        },
        {
            "district_id": 2,
            "district_name": "Bravo",
            "state": "CA",
            "field_key": "frpl_pct",
            "label": "FRPL %",
            "value": 70.0,
            "display_value": "70%",
            "academic_year": "2024 - 2025",
            "vintage": "NCES release year: 2023",
        },
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        question=(
            "Show me starting teacher salaries for teachers with a masters for "
            "districts with the lowest free-and-reduced lunch share"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting teacher salary", degree_lane="ma")],
        profile_fields=[ProfileFieldSpec(name="free-and-reduced lunch share")],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="free-and-reduced lunch share",
                direction="asc",
                key_type="profile_field",
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert repository.fetched_metric_ids == [96]
    assert [row.metric_id for row in outcome.result.rows] == [96, 96]
    assert [row.metric_name for row in outcome.result.rows] == [
        master_metric.name,
        master_metric.name,
    ]
    lane_refs = [
        ref
        for ref in outcome.result.methodology_codes
        if ref.code == "degree_lane_applied"
    ]
    assert len(lane_refs) == 1
    assert lane_refs[0].metadata == {"degree_lane": "ma"}


@pytest.mark.asyncio
async def test_largest_districts_selection_defaults_broad_starting_salary_to_bachelor_display_metric() -> None:
    bachelor_metric = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    master_metric = MetricCandidate(
        metric_id=96,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
    )
    repository = FakePolicyAnswerRepository(
        metrics=[bachelor_metric, master_metric],
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                metric_id=89,
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _row(
                2,
                "Bravo",
                "$70,000",
                metric_id=89,
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
        ],
        aliases=[
            _metric_alias(
                "starting teacher salary",
                status="ambiguous",
                candidate_refs=[
                    {
                        "metric_id": 89,
                        "metric_name": bachelor_metric.name,
                    },
                    {
                        "metric_id": 96,
                        "metric_name": master_metric.name,
                    },
                ],
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
            _metric_alias(
                "enrollment",
                entity_type="nces_field",
                canonical_id="enrollment",
            ),
        ],
        renderer_notes={
            "bachelor_starting_salary_default_lane": (
                "Used bachelor's-degree starting salary for first-year teachers "
                "because no degree lane was specified. Ask for master's starting "
                "salary to use the master's-degree lane."
            )
        },
    )
    repository.nces_fields = [
        NCESFieldCandidate(
            field_key="enrollment",
            label="Enrollment",
            data_type="integer",
            description="Total district enrollment.",
        )
    ]
    repository.profile_rank_rows = [
        {
            "district_id": 1,
            "district_name": "Alpha",
            "state": "CA",
            "field_key": "enrollment",
            "label": "Enrollment",
            "value": 80000,
            "display_value": "80,000",
            "academic_year": "2024 - 2025",
            "vintage": "NCES directory year: 2023",
        },
        {
            "district_id": 2,
            "district_name": "Bravo",
            "state": "CA",
            "field_key": "enrollment",
            "label": "Enrollment",
            "value": 70000,
            "display_value": "70,000",
            "academic_year": "2024 - 2025",
            "vintage": "NCES directory year: 2023",
        },
    ]
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        question="Show starting teacher salary for districts with the largest enrollment.",
        selection=SelectionSpec(scope="largest_districts"),
        metrics=[MetricSpec(name="starting teacher salary")],
        limit=LimitSpec(count=2, kind="top"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert [row.metric_id for row in outcome.result.rows] == [89, 89]
    assert repository.fetched_metric_ids == [89]
    assert any("bachelor's-degree starting salary" in note for note in outcome.result.source_notes)


@pytest.mark.asyncio
async def test_metric_value_filter_broad_salary_commits_bachelor_default_without_clarify() -> None:
    """GAP 1 (#1454): a rank plan whose metric-value filter field is the broad
    'starting teacher salary' phrase (no degree_lane) must commit the governed
    launch default (metric 89) and execute — not clarify with the BA/MA bundle.

    The fix lives in ``_resolve_metric_value_filters`` (operations.py): the
    contextual-default check that ``_resolve_rank_primary_metric_with_default``
    already applies must run BEFORE the ``metric_resolution.ambiguous`` clarify
    branch so the filter resolves silently to metric 89.

    The primary metric ('Average teacher starting salary') is a distinct phrase
    that resolves cleanly to metric 1234; the salary filter phrase is the only
    ambiguous token.  This isolates the filter path as the failure site.
    """
    bachelor_metric = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    master_metric = MetricCandidate(
        metric_id=96,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
    )
    primary_metric = MetricCandidate(
        metric_id=1234,
        name="Average teacher starting salary",
        answer_type="numeric",
    )
    repository = FakePolicyAnswerRepository(
        metrics=[primary_metric, bachelor_metric, master_metric],
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                metric_id=1234,
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _row(
                2,
                "Bravo",
                "$70,000",
                metric_id=1234,
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
            _row(1, "Alpha", "$50,000", metric_id=89),
            _row(2, "Bravo", "$70,000", metric_id=89),
        ],
        aliases=[
            _metric_alias(
                "Average teacher starting salary",
                canonical_id="1234",
            ),
            _metric_alias(
                "starting teacher salary",
                status="ambiguous",
                entity_type="metric_bundle",
                candidate_refs=[
                    {"metric_id": 89, "metric_name": bachelor_metric.name},
                    {"metric_id": 96, "metric_name": master_metric.name},
                ],
                metadata={
                    "contextual_defaults": {
                        "launch_starting_salary_default": {
                            "metric_id": 89,
                            "note_keys": ["bachelor_starting_salary_default_lane"],
                        }
                    }
                },
            ),
        ],
        renderer_notes={
            "bachelor_starting_salary_default_lane": (
                "Used bachelor's-degree starting salary for first-year teachers "
                "because no degree lane was specified. Ask for master's starting "
                "salary to use the master's-degree lane."
            )
        },
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question="Rank districts that pay at least $40,000 in starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        filters=[
            FilterSpec(
                field="starting teacher salary",
                operator="greater_than",
                value=40000,
            )
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.clarification is None, (
        f"Expected a committed answer, got a clarification: {outcome.message}"
    )
    assert outcome.result is not None
    # The filter must silently resolve to metric 89 (bachelor's) — metric 96
    # (master's) must never be fetched.
    assert 96 not in repository.fetched_metric_ids


@pytest.mark.asyncio
async def test_limited_ranked_lookup_emits_degree_lane_applied_methodology_code() -> None:
    """GAP 2 (#1454): _execute_limited_ranked_lookup must emit a
    ``degree_lane_applied`` MethodologyRef when the primary metric spec
    carries an explicit degree_lane — mirroring the plain rank path.

    A two-metric plan with LimitSpec triggers ``_execute_limited_ranked_lookup``
    (len(execution_metric_specs) > 1 and plan.limit is not None).  The primary
    metric spec has ``degree_lane="ma"``; after execution the result's
    ``methodology_codes`` must contain ``code="degree_lane_applied"`` with
    ``metadata={"degree_lane": "ma"}``.
    """
    ma_metric = MetricCandidate(
        metric_id=96,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
    )
    comparison_metric = MetricCandidate(
        metric_id=1234,
        name="Average teacher starting salary",
        answer_type="numeric",
    )
    repository = FakePolicyAnswerRepository(
        metrics=[ma_metric, comparison_metric],
        rows=[
            _row(
                1,
                "Alpha",
                "$80,000",
                metric_id=96,
                citations=[_source("Alpha Contract.pdf", district_id=1)],
            ),
            _row(
                2,
                "Bravo",
                "$75,000",
                metric_id=96,
                citations=[_source("Bravo Contract.pdf", district_id=2)],
            ),
            _row(1, "Alpha", "$55,000", metric_id=1234),
            _row(2, "Bravo", "$50,000", metric_id=1234),
        ],
        aliases=[
            _metric_alias(
                "starting teacher salary master's",
                canonical_id="96",
            ),
            _metric_alias(
                "Average teacher starting salary",
                canonical_id="1234",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question="Top 2 districts by MA starting salary, with base salary shown.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting teacher salary master's", degree_lane="ma"),
            MetricSpec(name="Average teacher starting salary", role="comparison"),
        ],
        limit=LimitSpec(count=2, kind="top"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None, f"Expected a result, got: {outcome.message}"
    lane_refs = [
        ref
        for ref in outcome.result.methodology_codes
        if ref.code == "degree_lane_applied"
    ]
    assert len(lane_refs) == 1, (
        f"Expected one degree_lane_applied code, got {lane_refs}"
    )
    assert lane_refs[0].metadata == {"degree_lane": "ma"}


@pytest.mark.asyncio
async def test_ranks_ascending_for_bottom_limit() -> None:
    repository = FakePolicyAnswerRepository(
        rows=[
            _row(1, "Alpha", "$50,000"),
            _row(2, "Bravo", "$70,000"),
            _row(3, "Charlie", "$60,000"),
        ]
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(limit=LimitSpec(count=2, kind="bottom"))
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha", "Charlie"]
    assert "lowest to highest" in outcome.result.order_statement


@pytest.mark.asyncio
async def test_excludes_unavailable_and_non_numeric_rows() -> None:
    repository = FakePolicyAnswerRepository(
        rows=[
            _row(1, "Alpha", "$50,000"),
            _row(2, "Bravo", "INA"),
            _row(3, "Charlie", "not published"),
            _row(4, "Delta", None),
        ]
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_ranking_plan())

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha"]
    assert outcome.result.excluded_count == 3


@pytest.mark.asyncio
async def test_ranking_counts_not_reviewed_rows_in_coverage_frame() -> None:
    repository = FakePolicyAnswerRepository(
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            _row(1, "Alpha", "$50,000"),
            _row(2, "Bravo", "INA"),
        ],
        reviewed_district_ids={1, 2, 3},
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_ranking_plan())

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha"]
    assert outcome.result.total_considered == 3
    assert outcome.result.excluded_count == 2
    assert outcome.result.coverage_frame is not None
    assert outcome.result.coverage_frame.universe_count == 3
    assert outcome.result.coverage_frame.addressed_count == 2
    assert outcome.result.coverage_frame.real_data_count == 1
    assert outcome.result.coverage_frame.not_reviewed_count == 1


@pytest.mark.asyncio
async def test_ranking_excludes_stale_recent_rows_and_counts_sparse_coverage() -> None:
    repository = FakePolicyAnswerRepository(
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[_row(1, "Alpha", "$50,000")],
        recent_rows=[
            _row(2, "Bravo", "$70,000").model_copy(
                update={"academic_year": "2023 - 2024"}
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(limit=LimitSpec(count=3, kind="top"))
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha"]
    assert outcome.result.excluded_count == 2
    assert outcome.result.coverage_frame is not None
    assert outcome.result.coverage_frame.not_reviewed_count == 2
    assert outcome.result.coverage_frame.sparse is True
    assert outcome.result.coverage_frame.sparse_disclosure == (
        "Sparse coverage: 1 of 3 in-scope cells have current reviewed data."
    )
    assert [disclosure.district_name for disclosure in outcome.result.coverage_disclosures] == [
        "Bravo",
        "Charlie",
    ]
    # #1514 D7 — the canonical "last reviewed" narrative sentence.
    assert (
        "NCTQ last reviewed Bravo for Average teacher starting salary in "
        "2023 - 2024; the value then was $70,000."
    ) == outcome.result.coverage_disclosures[0].display
    assert outcome.result.coverage_disclosures[0].prior_academic_year == "2023 - 2024"
    assert outcome.result.coverage_disclosures[0].prior_display_value == "$70,000"
    assert not any(
        note.startswith("NCTQ hasn't reviewed")
        for note in outcome.result.source_notes
    )
    # Charts are suppressed for ranking results with fewer than 3 numeric points
    assert outcome.result.chart is None


@pytest.mark.asyncio
async def test_rejects_unsupported_shape_without_crashing() -> None:
    repository = FakePolicyAnswerRepository(rows=[_row(1, "Alpha", "$50,000")])
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(
            filters=[
                FilterSpec(
                    field="district_name",
                    operator="contains",
                    value="Alpha",
                )
            ]
        )
    )

    assert outcome.result is None
    assert "governed data" in outcome.message
    assert "supported query shape" not in outcome.message


@pytest.mark.asyncio
async def test_ranking_ambiguous_metric_family_returns_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compass_backend.execution._helpers import _metric_clarification

    async def _fake_compose_fallback(
        metric_phrase: str,
        *,
        operation: str,
        candidates: list,
        adjudicator_hint: str | None = None,
        stylist_agent: object = None,
    ):
        return _metric_clarification(
            metric_phrase, operation=operation, candidates=candidates
        )

    monkeypatch.setattr(
        "compass_backend.execution.operations.compose_clarify_question_async",
        _fake_compose_fallback,
    )
    monkeypatch.setattr(
        "compass_backend.execution.scoping.compose_clarify_question_async",
        _fake_compose_fallback,
    )

    repository = FakePolicyAnswerRepository(
        metrics=[
            MetricCandidate(
                metric_id=57,
                name="Minimum amount of elementary teacher planning time per week (in minutes)",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=58,
                name="Minimum amount of middle school teacher planning time per week (in minutes)",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=59,
                name="Minimum amount of high school teacher planning time per week (in minutes)",
                answer_type="numeric",
            ),
        ],
        aliases=[
            _metric_alias(
                "planning time",
                status="ambiguous",
                entity_type="metric_bundle",
                candidate_refs=[
                    {"metric_id": 57},
                    {"metric_id": 58},
                    {"metric_id": 59},
                ],
            )
        ],
        rows=[_row(1, "Alpha", "300", metric_id=57)],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(
            limit=LimitSpec(count=5, kind="top"),
        ).model_copy(
            update={
                "question": "Show me the 5 districts that offer the most planning time.",
                "metrics": [MetricSpec(name="planning time")],
            }
        )
    )

    assert outcome.result is None
    assert outcome.clarification is not None
    assert outcome.clarification.missing_fields == ["metric"]
    assert outcome.clarification.candidates == [
        "Minimum amount of elementary teacher planning time per week (in minutes)",
        "Minimum amount of middle school teacher planning time per week (in minutes)",
        "Minimum amount of high school teacher planning time per week (in minutes)",
    ]
    assert outcome.message == (
        'I found a few Compass metrics that could match "planning time". '
        "Do you mean one of these?"
    )
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_ranking_approved_nonnumeric_metric_bundle_returns_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compass_backend.execution._helpers import _metric_clarification

    async def _fake_compose_fallback(
        metric_phrase: str,
        *,
        operation: str,
        candidates: list,
        adjudicator_hint: str | None = None,
        stylist_agent: object = None,
    ):
        return _metric_clarification(
            metric_phrase, operation=operation, candidates=candidates
        )

    monkeypatch.setattr(
        "compass_backend.execution.operations.compose_clarify_question_async",
        _fake_compose_fallback,
    )
    monkeypatch.setattr(
        "compass_backend.execution.scoping.compose_clarify_question_async",
        _fake_compose_fallback,
    )

    repository = FakePolicyAnswerRepository(
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
                    "Maximum portion of the employee's dependents' health "
                    "insurance premium paid by the employer"
                ),
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=235,
                name="Dollar cap for portion of health insurance premium covered by employer",
                answer_type="text",
            ),
        ],
        aliases=[
            _metric_alias(
                "benefits",
                entity_type="metric_bundle",
                canonical_ids=["232", "233", "234", "235"],
            )
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(
            limit=LimitSpec(count=10, kind="top"),
        ).model_copy(
            update={
                "question": (
                    "What are the ten districts that provide the most "
                    "meaningful benefits?"
                ),
                "metrics": [MetricSpec(name="benefits")],
                "sort": SortSpec(field="benefits", direction="desc"),
            }
        )
    )

    assert outcome.result is None
    assert outcome.clarification is not None
    assert outcome.clarification.missing_fields == ["metric"]
    assert outcome.clarification.candidates == [
        "Does the district cover 100% of employees' health insurance premium?",
        "What percent of the employees' health insurance premium does the district cover?",
        (
            "Maximum portion of the employee's dependents' health "
            "insurance premium paid by the employer"
        ),
        "Dollar cap for portion of health insurance premium covered by employer",
    ]
    assert outcome.message == (
        'I found a few Compass metrics that could match "benefits". '
        "Do you mean one of these?"
    )
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_ranking_specific_planning_time_alias_executes() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[
            MetricCandidate(
                metric_id=57,
                name="Minimum amount of elementary teacher planning time per week (in minutes)",
                answer_type="numeric",
            )
        ],
        aliases=[
            _metric_alias(
                "elementary planning time",
                canonical_id="57",
            )
        ],
        rows=[
            _row(1, "Alpha", "200", metric_id=57),
            _row(2, "Bravo", "300", metric_id=57),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(limit=LimitSpec(count=1, kind="top")).model_copy(
            update={
                "question": "Show me the district with the most elementary planning time.",
                "metrics": [MetricSpec(name="elementary planning time")],
            }
        )
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Bravo"]
    assert repository.fetched_metric_ids == [57]


@pytest.mark.asyncio
async def test_state_selection_filters_rows_before_ranking_and_limit() -> None:
    repository = FakePolicyAnswerRepository(
        rows=[
            _row(1, "Alpha", "$50,000", state="CA"),
            _row(2, "Bravo", "$90,000", state="NY"),
            _row(3, "Charlie", "$60,000", state="CA"),
        ]
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(
            scope="state",
            states=["CA"],
            limit=LimitSpec(count=2, kind="top"),
        )
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Charlie", "Alpha"]
    assert {row.state for row in outcome.result.rows} == {"CA"}
    assert outcome.result.total_considered == 2
    assert outcome.result.excluded_count == 0
    assert "CA" in outcome.result.order_statement


@pytest.mark.asyncio
async def test_state_filter_equals_filters_rows_before_ranking() -> None:
    repository = FakePolicyAnswerRepository(
        rows=[
            _row(1, "Alpha", "$50,000", state="CA"),
            _row(2, "Bravo", "$90,000", state="NY"),
            _row(3, "Charlie", "INA", state="CA"),
        ]
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(
            filters=[FilterSpec(field="state", operator="equals", value="CA")],
        )
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha"]
    assert outcome.result.total_considered == 2
    assert outcome.result.excluded_count == 1


@pytest.mark.asyncio
async def test_named_district_selection_filters_rows_before_ranking() -> None:
    repository = FakePolicyAnswerRepository(
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ],
        rows=[
            _row(1, "Alpha", "$50,000", state="CA"),
            _row(2, "Bravo", "$90,000", state="CA"),
            _row(3, "Charlie", "$60,000", state="CA"),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(
            scope="named_districts",
            districts=["Alpha", "Charlie"],
            limit=LimitSpec(count=2, kind="top"),
        )
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Charlie", "Alpha"]
    assert outcome.result.selection is not None
    assert outcome.result.selection.scope == "named_districts"
    assert [
        district.district_id for district in outcome.result.selection.districts
    ] == [1, 3]
    assert outcome.result.total_considered == 2
    assert repository.resolved_district_names == [["Alpha", "Charlie"]]


@pytest.mark.asyncio
async def test_named_district_selection_resolves_normalized_names() -> None:
    repository = FakePolicyAnswerRepository(
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[_row(1, "Alpha", "$50,000", state="CA")],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(
            scope="named_districts",
            districts=["alpha public school district"],
        )
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha"]


@pytest.mark.asyncio
async def test_unknown_named_district_returns_no_result_without_metric_fetch() -> None:
    repository = FakePolicyAnswerRepository(
        districts=[DistrictCandidate(district_id=1, district_name="Alpha", state="CA")],
        rows=[_row(1, "Alpha", "$50,000", state="CA")],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(scope="named_districts", districts=["Missing"])
    )

    assert outcome.result is None
    assert "could not resolve" in outcome.message
    assert repository.metric_queries == []
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_ambiguous_named_district_returns_no_result_without_metric_fetch() -> None:
    repository = FakePolicyAnswerRepository(
        ambiguous={
            "Portland": [
                DistrictCandidate(district_id=10, district_name="Portland", state="OR"),
                DistrictCandidate(district_id=11, district_name="Portland", state="ME"),
            ]
        },
        rows=[
            _row(10, "Portland", "$50,000", state="OR"),
            _row(11, "Portland", "$60,000", state="ME"),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(scope="named_districts", districts=["Portland"])
    )

    assert outcome.result is None
    assert outcome.clarification is not None
    assert outcome.clarification.missing_fields == ["district"]
    assert outcome.clarification.candidates == ["Portland, OR", "Portland, ME"]
    assert outcome.message == (
        'I found more than one covered district matching "Portland". '
        "Which district do you mean?"
    )
    assert "deterministic execution" not in outcome.message
    assert repository.metric_queries == []
    assert repository.fetched_metric_ids == []


@pytest.mark.asyncio
async def test_state_filter_narrows_named_district_resolution() -> None:
    repository = FakePolicyAnswerRepository(
        ambiguous={
            "Portland": [
                DistrictCandidate(district_id=10, district_name="Portland", state="OR"),
                DistrictCandidate(district_id=11, district_name="Portland", state="ME"),
            ]
        },
        rows=[
            _row(10, "Portland", "$50,000", state="OR"),
            _row(11, "Portland", "$60,000", state="ME"),
        ],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(
            scope="named_districts",
            districts=["Portland"],
            filters=[FilterSpec(field="state", operator="equals", value="OR")],
        )
    )

    assert outcome.result is not None
    assert [row.district_id for row in outcome.result.rows] == [10]
    assert outcome.result.selection is not None
    assert outcome.result.selection.states == ["OR"]


@pytest.mark.asyncio
async def test_resolves_metric_name_without_hardcoded_ids() -> None:
    repository = FakePolicyAnswerRepository(
        metrics=[
            MetricCandidate(
                metric_id=9876,
                name="Average teacher starting salary",
                answer_type="numeric",
            )
        ],
        rows=[_row(1, "Alpha", "$50,000", metric_id=9876)],
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_ranking_plan())

    assert outcome.result is not None
    assert repository.metric_queries == ["Average teacher starting salary"]
    assert repository.fetched_metric_ids == [9876]
    assert outcome.result.rows[0].metric_id == 9876


@pytest.mark.asyncio
async def test_ranked_rows_include_citation_markers_after_limit() -> None:
    repository = FakePolicyAnswerRepository(
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                answer_id=101,
                citations=[
                    _source(
                        "Alpha District. CA. (2024-2025). Contract. p. 1.",
                        source_url="https://example.org/alpha.pdf",
                        district_id=1,
                    )
                ],
            ),
            _row(
                2,
                "Bravo",
                "$70,000",
                answer_id=102,
                citations=[
                    _source(
                        "Bravo District. CA. (2024-2025). Contract. p. 2.",
                        source_url="https://example.org/bravo.pdf",
                        district_id=2,
                    )
                ],
            ),
            _row(
                3,
                "Charlie",
                "$60,000",
                answer_id=103,
                citations=[
                    _source(
                        "Charlie District. CA. (2024-2025). Contract. p. 3.",
                        source_url="https://example.org/charlie.pdf",
                        district_id=3,
                    )
                ],
            ),
        ]
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(
        _ranking_plan(limit=LimitSpec(count=2, kind="top"))
    )

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Bravo", "Charlie"]
    assert [row.citation_markers for row in outcome.result.rows] == [[1], [2]]
    assert [citation.marker for citation in outcome.result.citations] == [1, 2]
    assert [citation.district_id for citation in outcome.result.citations] == [2, 3]
    assert all("Alpha" not in citation.title for citation in outcome.result.citations)


@pytest.mark.asyncio
async def test_excluded_rows_do_not_contribute_citations() -> None:
    repository = FakePolicyAnswerRepository(
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                answer_id=101,
                citations=[
                    _source(
                        "Alpha District. CA. (2024-2025). Contract. p. 1.",
                        district_id=1,
                    )
                ],
            ),
            _row(
                2,
                "Bravo",
                "INA",
                answer_id=102,
                citations=[
                    _source(
                        "Bravo District. CA. (2024-2025). Contract. p. 2.",
                        district_id=2,
                    )
                ],
            ),
        ]
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_ranking_plan())

    assert outcome.result is not None
    assert [row.district_name for row in outcome.result.rows] == ["Alpha"]
    assert [row.citation_markers for row in outcome.result.rows] == [[1]]
    assert [citation.district_id for citation in outcome.result.citations] == [1]


@pytest.mark.asyncio
async def test_duplicate_citation_sources_share_one_marker() -> None:
    duplicate_source = _source(
        "Alpha District. CA. (2024-2025). Contract. p. 1.",
        source_url="https://example.org/alpha.pdf",
        district_id=1,
    )
    repository = FakePolicyAnswerRepository(
        rows=[
            _row(
                1,
                "Alpha",
                "$50,000",
                answer_id=101,
                citations=[duplicate_source, duplicate_source.model_copy()],
            )
        ]
    )
    executor = DeterministicQueryExecutor(repository)

    outcome = await executor.execute(_ranking_plan())

    assert outcome.result is not None
    assert outcome.result.rows[0].citation_markers == [1]
    assert [citation.marker for citation in outcome.result.citations] == [1]


def test_execute_route_contract_is_still_typed() -> None:
    turn = PlannerTurn(route="execute", confidence=0.9, query_plan=_ranking_plan())

    assert turn.query_plan is not None
    assert turn.query_plan.metrics[0].name == "Average teacher starting salary"


# ---------------------------------------------------------------------------
# PR 2B: similarity operation executor tests
# ---------------------------------------------------------------------------


def _peer_profiles_for_similarity() -> list[dict[str, object]]:
    """Two covered NCES profiles: Denver (anchor) + Jeffco (peer)."""

    return [
        {
            "district_id": 26,
            "district_name": "Denver Public Schools",
            "state": "CO",
            "city": "DENVER",
            "enrollment": 87883,
            "locale_text": "City: Large",
            "latitude": 39.745750,
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


def _similarity_plan(
    anchor: str = "Denver Public Schools",
    feature_set: str = "all",
    exclude_states: list[str] | None = None,
    limit: int = 10,
) -> QueryPlan:
    from compass_backend.contracts import SimilarityQuerySpec

    return QueryPlan(
        operation="similarity",
        question=f"Find peers to {anchor}.",
        selection=SelectionSpec(scope="named_districts", districts=[anchor]),
        similarity=SimilarityQuerySpec(
            anchor_name=anchor,
            feature_set=feature_set,
            exclude_states=exclude_states or [],
            limit=limit,
        ),
    )


@pytest.mark.asyncio
async def test_similarity_returns_peers_only_artifact() -> None:
    """Basic similarity plan returns a PeerComparisonResult with peer rows."""

    repository = FakePolicyAnswerRepository(
        districts=[
            DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO"),
        ],
    )
    repository.peer_profile_rows = _peer_profiles_for_similarity()
    executor = DeterministicQueryExecutor(repository, default_limit=5)

    outcome = await executor.execute(_similarity_plan(limit=5))

    assert outcome.result is not None
    assert outcome.result.result_type == "peer_comparison"
    # Anchor + at least 1 peer
    roles = [row.peer_role for row in outcome.result.rows]
    assert "anchor" in roles
    assert "peer" in roles
    # No policy metric — metric_id sentinel is 0
    assert all(row.metric_id == 0 for row in outcome.result.rows)
    assert all(row.metric_name == "similarity" for row in outcome.result.rows)
    # Methodology codes
    codes = [ref.code for ref in outcome.result.methodology_codes]
    assert "peer_selection_nces_profiles" in codes
    assert "peer_selection_rationale" in codes
    assert "similarity_feature_set_override" not in codes, (
        "similarity_feature_set_override must NOT fire when feature_set == 'all'"
    )


@pytest.mark.asyncio
async def test_similarity_with_exclude_states_drops_in_state_peers() -> None:
    """exclude_states='CO' means no CO peers should appear."""

    repository = FakePolicyAnswerRepository(
        districts=[
            DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO"),
        ],
    )
    # Add a CA district that would score well
    ca_profiles = [
        *_peer_profiles_for_similarity(),
        {
            "district_id": 50,
            "district_name": "Los Angeles Unified",
            "state": "CA",
            "city": "LOS ANGELES",
            "enrollment": 600000,
            "locale_text": "City: Large",
            "latitude": 34.05,
            "longitude": -118.24,
            "total_rev_pp": 15000.00,
            "total_exp_pp": 14000.00,
            "pupil_teacher_ratio": 22.5,
            "frpl_pct": 79.0,
        },
    ]
    repository.peer_profile_rows = ca_profiles
    executor = DeterministicQueryExecutor(repository, default_limit=5)

    outcome = await executor.execute(_similarity_plan(exclude_states=["CO"], limit=5))

    assert outcome.result is not None
    peer_states = {
        row.state for row in outcome.result.rows if row.peer_role == "peer"
    }
    # No CO peers (only CA or other states allowed through)
    assert "CO" not in peer_states


@pytest.mark.asyncio
async def test_similarity_with_feature_set_frpl_has_frpl_code() -> None:
    """feature_set='frpl' produces similarity_feature_set_override methodology code."""

    repository = FakePolicyAnswerRepository(
        districts=[
            DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO"),
        ],
    )
    repository.peer_profile_rows = _peer_profiles_for_similarity()
    executor = DeterministicQueryExecutor(repository, default_limit=5)

    outcome = await executor.execute(_similarity_plan(feature_set="frpl", limit=5))

    assert outcome.result is not None
    codes = [ref.code for ref in outcome.result.methodology_codes]
    assert "similarity_feature_set_override" in codes
    assert "peer_selection_rationale" not in codes, (
        "peer_selection_rationale must NOT fire when feature_set != 'all' "
        "(contradiction with similarity_feature_set_override). See Round 2 Fix 2."
    )


@pytest.mark.asyncio
async def test_similarity_with_unresolvable_anchor_refuses() -> None:
    """Unresolvable anchor produces ExecutionRefusal with clear message."""

    repository = FakePolicyAnswerRepository(
        districts=[
            DistrictCandidate(district_id=26, district_name="Denver Public Schools", state="CO"),
        ],
    )
    repository.peer_profile_rows = _peer_profiles_for_similarity()
    executor = DeterministicQueryExecutor(repository, default_limit=5)

    outcome = await executor.execute(_similarity_plan(anchor="Completely Unknown District"))

    assert outcome.result is None
    assert "NCES peer set" in outcome.message or "deterministic" in outcome.message.lower()


class _RaisingPolicyAnswerRepository(FakePolicyAnswerRepository):
    """Repository whose row fetch raises mid-execution (e.g. asyncpg pool loss).

    The other executor tests only exercise success / refusal / clarification
    outcomes. This covers the fourth path: an unexpected error raised from the
    repository while a supported plan is executing.
    """

    async def fetch_metric_answer_rows(
        self,
        *,
        metric_id: int,
        academic_year: str,
    ) -> list[MetricAnswerRow]:
        raise ConnectionError("synthetic asyncpg pool exhausted")


@pytest.mark.asyncio
async def test_executor_propagates_repository_exception_without_masking() -> None:
    """A repository error during execution must surface, not be swallowed.

    The executor deliberately does not catch unexpected repository errors: the
    turn boundary (`process_chat_turn`) converts a raised exception into a
    well-formed TurnErrorEnvelope + trace-tagged 500. The grounding-critical
    invariant locked here is that the executor never masks such a failure into
    a bogus ExecutionSuccess (fabricated rows) or a silent ExecutionRefusal —
    it propagates the original exception so the error path can run. If a future
    refactor added a blanket `except Exception` that returned a fake outcome,
    this test fails.
    """

    repository = _RaisingPolicyAnswerRepository(rows=_salary_rows(3))
    executor = DeterministicQueryExecutor(repository)

    with pytest.raises(ConnectionError, match="asyncpg pool exhausted"):
        await executor.execute(_ranking_plan())
