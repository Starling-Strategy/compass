"""Tests for the Universal Catalog Resolution foundation."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel

from compass_backend.catalog.adjudication import (
    CatalogAdjudicationCandidate,
    CatalogAdjudicationDecision,
    CatalogAdjudicationDeps,
    create_catalog_adjudicator_agent,
    validate_catalog_adjudication_decision,
)
from compass_backend.catalog import (
    CandidateCard,
    CatalogAliasRecord,
    CatalogResolver,
    DistrictCandidate,
    DistrictResolution,
    GlossaryTermCandidate,
    NCESFieldCandidate,
    MetricCandidate,
    SourceDocumentCandidate,
    RecallBatch,
    RecallReport,
    TopicCandidate,
    normalize_district_name_for_resolution,
)
from compass_backend.contracts import (
    ClarificationRequest,
    LimitSpec,
    MetricSpec,
    PendingQueryContext,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    SortSpec,
    SortStepSpec,
)
from compass_backend.execution import DeterministicQueryExecutor, MetricAnswerRow
from compass_backend.planning.cross_box import review_cross_box_planner_turn


class FakeCatalogRepository:
    """Fake catalog repository for resolver tests."""

    def __init__(
        self,
        *,
        metrics: list[MetricCandidate] | None = None,
        districts: DistrictResolution | None = None,
        aliases: list[CatalogAliasRecord] | None = None,
        metric_results_by_query: dict[str, list[MetricCandidate]] | None = None,
        district_candidate_results_by_query: dict[str, list[DistrictCandidate]]
        | None = None,
        topic_metric_results: list[MetricCandidate] | None = None,
        nces_fields: list[NCESFieldCandidate] | None = None,
        renderer_notes: dict[str, str] | None = None,
    ) -> None:
        self.metrics = metrics or []
        self.districts = districts or DistrictResolution()
        self.aliases = aliases or []
        self.metric_results_by_query = metric_results_by_query
        self.district_candidate_results_by_query = (
            district_candidate_results_by_query or {}
        )
        self.topic_metric_results = topic_metric_results or []
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
        self.metric_query_limits: list[int] = []
        self.district_queries: list[list[str]] = []
        self.district_candidate_queries: list[tuple[str, set[str] | None, int]] = []
        self.alias_queries: list[tuple[str, tuple[str, ...]]] = []
        self.state_scopes: list[set[str] | None] = []
        self.source_queries: list[str] = []
        self.topic_queries: list[str] = []
        self.glossary_queries: list[str] = []
        self.nces_queries: list[str] = []
        self.largest_district_calls: list[tuple[set[str] | None, int, str]] = []
        self.renderer_note_queries: list[list[str]] = []

    async def search_metrics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MetricCandidate]:
        self.metric_queries.append(query)
        self.metric_query_limits.append(limit)
        if self.metric_results_by_query is not None:
            return self.metric_results_by_query.get(query, [])[:limit]
        return self.metrics[:limit]

    async def fetch_metrics_by_ids(
        self,
        metric_ids: list[int],
    ) -> list[MetricCandidate]:
        self.metric_queries.append(f"ids:{','.join(str(value) for value in metric_ids)}")
        by_id = {metric.metric_id: metric for metric in self.metrics}
        return [by_id[metric_id] for metric_id in metric_ids if metric_id in by_id]

    async def fetch_renderer_notes(self, note_keys: list[str]):
        self.renderer_note_queries.append(note_keys)
        from compass_backend.catalog import RendererNote

        return [
            RendererNote(
                note_key=key,
                note_text=self.renderer_notes[key],
                source="SSN-235",
                provenance="test fixture",
                scenario_ids=["79"],
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
        self.alias_queries.append((alias, tuple(sorted(entity_types))))
        normalized = alias.casefold()
        return [
            record
            for record in self.aliases
            if record.normalized_alias == normalized
            and record.entity_type in entity_types
            and record.active
        ]

    async def resolve_districts(
        self,
        names: list[str],
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        self.district_queries.append(names)
        self.state_scopes.append(states)
        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        ambiguous: dict[str, list[DistrictCandidate]] = {}
        for name in names:
            normalized = normalize_district_name_for_resolution(name)
            matches = [
                district
                for district in self.districts.resolved
                if normalize_district_name_for_resolution(district.district_name)
                == normalized
                and (
                    not states
                    or (district.state is not None and district.state in states)
                )
            ]
            if len(matches) == 1:
                resolved.append(matches[0])
            elif len(matches) > 1:
                ambiguous[name] = matches
            else:
                unresolved.append(name)
        return DistrictResolution(
            resolved=resolved,
            unresolved=unresolved,
            ambiguous=ambiguous,
        )

    async def search_district_candidates(
        self,
        query: str,
        *,
        states: set[str] | None = None,
        limit: int = 8,
    ) -> list[DistrictCandidate]:
        self.district_candidate_queries.append((query, states, limit))
        state_filter = {state.upper() for state in states or set()}
        return [
            district
            for district in self.district_candidate_results_by_query.get(query, [])
            if not state_filter or (district.state or "").upper() in state_filter
        ][:limit]

    async def list_covered_districts(
        self,
        *,
        states: set[str] | None = None,
    ) -> list[DistrictCandidate]:
        self.state_scopes.append(states)
        return self.districts.resolved

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 5,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        self.largest_district_calls.append((states, limit, academic_year))
        return self.districts.resolved[:limit]

    async def search_source_documents(
        self,
        query: str,
        *,
        district_ids: set[int] | None = None,
        limit: int = 5,
    ) -> list[SourceDocumentCandidate]:
        self.source_queries.append(query)
        return [
            SourceDocumentCandidate(
                source_id=100,
                title="Alpha Contract, 2024-2025",
                district_id=10,
                document_type="Contract",
                academic_year="2024 - 2025",
                url="https://example.org/alpha-contract.pdf",
            )
        ][:limit]

    async def search_topics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[TopicCandidate]:
        self.topic_queries.append(query)
        return [
            TopicCandidate(
                topic_id=1,
                topic_name="Teacher Compensation",
                subtopic_id=11,
                subtopic_name="Salary",
                question_count=12,
            )
        ][:limit]

    async def fetch_topic_metric_candidates(
        self,
        topics: list[TopicCandidate],
        *,
        limit: int = 10,
    ) -> list[MetricCandidate]:
        return self.topic_metric_results[:limit]

    async def search_glossary_terms(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[GlossaryTermCandidate]:
        self.glossary_queries.append(query)
        return [
            GlossaryTermCandidate(
                term_id="collective bargaining",
                term="Collective bargaining",
                definition="Negotiation between employers and employees.",
                context="District policy glossary",
            )
        ][:limit]

    async def search_nces_fields(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[NCESFieldCandidate]:
        self.nces_queries.append(query)
        return self.nces_fields[:limit]


class FakeResolverRecallService:
    """Fake recall service for resolver integration tests."""

    def __init__(self, cards: list[CandidateCard]) -> None:
        self.cards = cards
        self.calls: list[dict[str, object]] = []

    async def recall(
        self,
        query: str,
        *,
        entity_types=None,
        limit: int = 10,
        states: set[str] | None = None,
        expand_prompt: bool = True,
    ) -> RecallReport:
        requested = tuple(entity_types or ("metric",))
        self.calls.append(
            {
                "query": query,
                "entity_types": requested,
                "limit": limit,
                "states": states,
                "expand_prompt": expand_prompt,
            }
        )
        return RecallReport(
            query=query,
            batches=[
                RecallBatch(
                    query=query,
                    entity_types=list(requested),
                    candidates=[
                        card
                        for card in self.cards
                        if card.entity_type in requested
                    ],
                    limit=limit,
                )
            ],
        )


class FakeAnswerRepository:
    """Fake ranking answer repository that has no catalog lookup methods."""

    def __init__(self, rows: list[MetricAnswerRow]) -> None:
        self.rows = rows
        self.fetched_metric_ids: list[int] = []

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


def _row(district_id: int, district_name: str, value: object) -> MetricAnswerRow:
    return MetricAnswerRow(
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=1234,
        metric_name="Average teacher starting salary",
        value=value,
        academic_year="2024 - 2025",
    )


def _ranking_plan() -> QueryPlan:
    return QueryPlan(
        question="Rank Alpha by BA starting salary.",
        selection=SelectionSpec(scope="named_districts", districts=["Alpha"]),
        metrics=[MetricSpec(name="BA starting salary")],
        limit=LimitSpec(count=5, kind="top"),
    )


def test_catalog_adjudication_rejects_ids_outside_candidates() -> None:
    candidates = [
        CatalogAdjudicationCandidate(
            entity_type="metric",
            candidate_id="89",
            label="Annual base salary for a first year teacher",
        )
    ]
    decision = CatalogAdjudicationDecision(
        action="select_one",
        selected_ids=["999"],
        confidence=0.61,
        rationale="The user asked for starting teacher salary.",
    )

    with pytest.raises(ModelRetry, match="outside the provided candidates"):
        validate_catalog_adjudication_decision(decision, candidates)


@pytest.mark.asyncio
async def test_catalog_resolver_uses_recall_for_metric_candidates() -> None:
    recall = FakeResolverRecallService(
        [
            CandidateCard(
                input_phrase="starting salary",
                entity_type="metric",
                label="starting salary",
                source_methods=["metric_search"],
                entity_ref="metric:89",
                metadata={"answer_type": "numeric", "topic": "Compensation"},
            )
        ]
    )
    repository = FakeCatalogRepository(metrics=[])
    resolver = CatalogResolver(
        repository,
        adjudicator=None,
        recall_service=recall,  # type: ignore[arg-type]
    )

    resolution = await resolver.resolve_primary_metric("starting salary")

    assert resolution.ok is True
    assert resolution.resolved is not None
    assert resolution.resolved.metric_id == 89
    assert repository.metric_queries == []
    assert recall.calls[0]["expand_prompt"] is False
    report = resolver.drain_recall_report()
    assert report is not None
    assert report.candidates[0].entity_ref == "metric:89"


@pytest.mark.asyncio
async def test_catalog_resolver_treats_alias_recall_as_advisory_only() -> None:
    recall = FakeResolverRecallService(
        [
            CandidateCard(
                input_phrase="starting salary",
                entity_type="metric",
                label="starting salary",
                source_methods=["alias"],
                entity_ref="metric:89",
            )
        ]
    )
    repository = FakeCatalogRepository(metrics=[])
    resolver = CatalogResolver(
        repository,
        adjudicator=None,
        recall_service=recall,  # type: ignore[arg-type]
    )

    resolution = await resolver.resolve_primary_metric("starting salary")

    assert resolution.ok is False
    assert resolution.resolved is None
    assert resolution.candidates == []
    assert resolver.drain_recall_report() is not None


@pytest.mark.asyncio
async def test_catalog_resolver_uses_recall_for_district_candidates() -> None:
    # This asserts the recall service (not the repository) is the candidate
    # source; the candidate then stays ambiguous because Denver(26) is NOT in
    # the covered universe here (the repository's covered index is empty), so
    # the #1563/#1566 single-covered short-circuit does not fire. A recall
    # candidate that IS covered would now resolve directly — that policy is
    # covered by the DC/Cleveland shape tests below.
    recall = FakeResolverRecallService(
        [
            CandidateCard(
                input_phrase="Denver",
                entity_type="district",
                label="Denver Public Schools",
                source_methods=["district_search"],
                entity_ref="district:26",
                metadata={"state": "CO", "match_reason": "name"},
            )
        ]
    )
    repository = FakeCatalogRepository(metrics=[])
    resolver = CatalogResolver(
        repository,
        adjudicator=None,
        recall_service=recall,  # type: ignore[arg-type]
    )

    resolution = await resolver.resolve_district("Denver")

    assert resolution.ok is False
    assert resolution.resolved == []
    assert [district.district_id for district in resolution.ambiguous["Denver"]] == [26]
    assert repository.district_candidate_queries == []


@pytest.mark.asyncio
async def test_catalog_resolver_uses_recall_for_profile_field_candidates() -> None:
    recall = FakeResolverRecallService(
        [
            CandidateCard(
                input_phrase="FRPL %",
                entity_type="profile_field",
                label="FRPL %",
                plain_definition="Free and reduced-price lunch share.",
                source_methods=["profile_field_search"],
                entity_ref="profile_field:frpl_pct",
                metadata={"data_type": "numeric"},
            )
        ]
    )
    repository = FakeCatalogRepository(metrics=[], nces_fields=[])
    resolver = CatalogResolver(
        repository,
        adjudicator=None,
        recall_service=recall,  # type: ignore[arg-type]
    )

    resolution = await resolver.resolve_profile_field_authority("FRPL %")

    assert resolution.ok is True
    assert resolution.resolved is not None
    assert resolution.resolved.field_key == "frpl_pct"
    assert repository.nces_queries == []


@pytest.mark.asyncio
async def test_catalog_adjudicator_clarifies_ambiguous_teacher_pay_candidates() -> None:
    decision = CatalogAdjudicationDecision(
        action="clarify",
        selected_ids=[],
        clarification_question=(
            "Do you mean starting salary, maximum salary, or extra-duty pay?"
        ),
        confidence=0.38,
        rationale="Teacher pay could refer to several provided metrics.",
    )
    agent = create_catalog_adjudicator_agent(
        TestModel(custom_output_args=decision.model_dump(mode="json"))
    )

    result = await agent.run(
        "teacher pay",
        deps=CatalogAdjudicationDeps(
            query="teacher pay",
            candidates=(
                CatalogAdjudicationCandidate(
                    entity_type="metric",
                    candidate_id="89",
                    label="Annual base salary for a first year teacher",
                ),
                CatalogAdjudicationCandidate(
                    entity_type="metric",
                    candidate_id="90",
                    label="Maximum annual teacher salary",
                ),
                CatalogAdjudicationCandidate(
                    entity_type="metric",
                    candidate_id="171",
                    label="Minimum annual performance pay bonus, if eligible",
                ),
            ),
        ),
    )

    assert result.output.action == "clarify"
    assert result.output.selected_ids == []
    assert result.output.clarification_question is not None
    assert "starting salary" in result.output.clarification_question


def test_catalog_normalizes_district_names_without_alias_or_fuzzy_matching() -> None:
    assert normalize_district_name_for_resolution(
        "Alpha Public School District"
    ) == "alpha"
    assert normalize_district_name_for_resolution("Dallas ISD") == "dallas"
    assert normalize_district_name_for_resolution("Alpha USD") == "alpha usd"


def test_catalog_adjudication_allows_district_candidates_but_rejects_invented_ids() -> None:
    candidates = [
        CatalogAdjudicationCandidate(
            entity_type="district",
            candidate_id="26",
            label="Denver Public Schools",
        )
    ]
    decision = CatalogAdjudicationDecision(
        action="select_one",
        selected_ids=["999"],
        confidence=0.91,
        rationale="Invented district ID should be rejected.",
    )

    with pytest.raises(ModelRetry, match="outside the provided candidates"):
        validate_catalog_adjudication_decision(decision, candidates)


def _approved_alias(
    alias: str,
    *,
    entity_type: str,
    canonical_id: str | None = None,
    canonical_ids: list[str] | None = None,
    candidate_refs: list[dict[str, object]] | None = None,
) -> CatalogAliasRecord:
    return CatalogAliasRecord(
        alias=alias,
        normalized_alias=alias.casefold(),
        entity_type=entity_type,
        resolution_status="approved",
        canonical_id=canonical_id,
        canonical_ids=canonical_ids or [],
        candidate_refs=candidate_refs or [],
        source="SSN-235",
        provenance="PR #488 reviewed evidence",
        scenario_ids=["SSN-235"],
        review_status="approved",
    )


def pending_context_from_plan(
    plan: QueryPlan,
    *,
    question: str,
) -> ClarificationRequest:
    return ClarificationRequest(
        question=question,
        missing_fields=["metric"],
        candidates=[],
        pending_context=PendingQueryContext(
            operation=plan.operation,
            question=plan.question,
            selection=plan.selection,
            metrics=plan.metrics,
            profile_fields=plan.profile_fields,
            filters=plan.filters,
            sort=plan.sort,
            limit=plan.limit,
            output=plan.output,
            temporal=plan.temporal,
            missing_fields=["metric"],
        ),
    )


def _ambiguous_alias(
    alias: str,
    *,
    entity_type: str,
    candidate_refs: list[dict[str, object]],
    metadata: dict[str, object] | None = None,
) -> CatalogAliasRecord:
    return CatalogAliasRecord(
        alias=alias,
        normalized_alias=alias.casefold(),
        entity_type=entity_type,
        resolution_status="ambiguous",
        candidate_refs=candidate_refs,
        metadata=metadata or {},
        source="SSN-235",
        provenance="PR #488 reviewed ambiguity",
        scenario_ids=["SSN-235"],
        review_status="approved",
    )


def _catalog_adjudicator(
    decision: CatalogAdjudicationDecision,
):
    return create_catalog_adjudicator_agent(
        TestModel(custom_output_args=decision.model_dump(mode="json"))
    )


class _UncheckedCatalogAdjudicator:
    """Test double that bypasses the agent validator to exercise resolver gating."""

    def __init__(self, decision: CatalogAdjudicationDecision) -> None:
        self._decision = decision

    async def run(self, user_prompt: str, **kwargs):
        return _UncheckedCatalogAdjudicationResult(self._decision)


class _UncheckedCatalogAdjudicationResult:
    def __init__(self, decision: CatalogAdjudicationDecision) -> None:
        self.output = decision


class RecordingCatalogAdjudicator:
    """Adjudicator stub that selects the first provided candidate."""

    def __init__(self) -> None:
        self.candidate_ids: list[str] = []

    async def run(self, user_prompt: str, **kwargs):
        deps = kwargs["deps"]
        self.candidate_ids = [
            candidate.candidate_id for candidate in deps.candidates
        ]
        return SimpleNamespace(
            output=CatalogAdjudicationDecision(
                action="select_one",
                selected_ids=[self.candidate_ids[0]],
                confidence=0.8,
                rationale="unit test selects first candidate",
            )
        )


class SelectingCatalogAdjudicator:
    """Adjudicator stub that selects a configured candidate ID."""

    def __init__(self, selected_id: str) -> None:
        self.selected_id = selected_id
        self.candidate_ids: list[str] = []

    async def run(self, user_prompt: str, **kwargs):
        deps = kwargs["deps"]
        self.candidate_ids = [
            candidate.candidate_id for candidate in deps.candidates
        ]
        return SimpleNamespace(
            output=CatalogAdjudicationDecision(
                action="select_one",
                selected_ids=[self.selected_id],
                confidence=0.88,
                rationale="unit test selects configured candidate",
            )
        )


@pytest.mark.asyncio
async def test_catalog_resolves_primary_metric_to_metric_id() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=1234,
                name="Average teacher starting salary",
                answer_type="numeric",
            )
        ]
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=None,
    )

    resolution = await resolver.resolve_primary_metric(
        "Average teacher starting salary"
    )

    assert resolution.resolved is not None
    assert resolution.resolved.metric_id == 1234
    assert resolution.resolved.name == "Average teacher starting salary"
    assert resolution.candidates == repository.metrics
    assert repository.metric_queries == ["Average teacher starting salary"]


@pytest.mark.asyncio
async def test_topic_narrowing_promotes_linked_metrics_before_adjudication() -> None:
    salary = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
        topic="Teacher Compensation",
    )
    benefits = MetricCandidate(
        metric_id=130,
        name="Health insurance premium contribution",
        answer_type="numeric",
        topic="Benefits",
    )
    repository = FakeCatalogRepository(
        metrics=[salary, benefits],
        metric_results_by_query={"teacher compensation": [benefits]},
        topic_metric_results=[salary],
    )
    adjudicator = RecordingCatalogAdjudicator()
    resolver = CatalogResolver(
        repository,
        adjudicator=adjudicator,
        topic_narrowing_enabled=True,
    )

    resolution = await resolver.resolve_metric_bundle(
        "teacher compensation",
        numeric_only=True,
    )

    assert [metric.metric_id for metric in resolution.resolved] == [89]
    assert adjudicator.candidate_ids[:2] == ["89", "130"]
    assert resolution.topic_frame is not None
    assert resolution.topic_frame.authority is not None
    assert resolution.topic_frame.authority.metric_ids == [89]
    assert repository.topic_queries == ["teacher compensation"]
    assert resolution.candidates[0].metadata["topic_frame"]["linked"] is True


@pytest.mark.asyncio
async def test_topic_narrowing_does_not_override_approved_metric_alias() -> None:
    salary = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
        topic="Teacher Compensation",
    )
    master_salary = MetricCandidate(
        metric_id=96,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
        topic="Teacher Compensation",
    )
    repository = FakeCatalogRepository(
        metrics=[salary, master_salary],
        aliases=[
            CatalogAliasRecord(
                alias="starting salary",
                normalized_alias="starting salary",
                entity_type="metric",
                resolution_status="approved",
                canonical_id="89",
                source="unit",
                provenance="unit test",
                review_status="approved",
            )
        ],
        topic_metric_results=[master_salary],
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=None,
        topic_narrowing_enabled=True,
    )

    resolution = await resolver.resolve_metric_bundle(
        "starting salary",
        numeric_only=True,
    )

    assert [metric.metric_id for metric in resolution.resolved] == [89]
    assert repository.topic_queries == []
    assert resolution.topic_frame is None


@pytest.mark.asyncio
async def test_catalog_resolves_single_exact_metric_name_not_first_loose_candidate() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=1111,
                name="Collective bargaining agreement notes",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=4321,
                name="Collective bargaining status",
                answer_type="text",
            ),
        ]
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_primary_metric(
        "Collective bargaining status",
        numeric_only=False,
    )

    assert resolution.resolved is not None
    assert resolution.resolved.metric_id == 4321


@pytest.mark.asyncio
async def test_catalog_metric_candidates_are_not_execution_authority() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=4321,
                name="Narrative teacher compensation policy",
                answer_type="text",
            )
        ]
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=_catalog_adjudicator(
            CatalogAdjudicationDecision(
                action="no_match",
                selected_ids=[],
                confidence=0.36,
                rationale="The candidate is not a numeric teacher compensation metric.",
            )
        ),
    )

    resolution = await resolver.resolve_primary_metric("teacher compensation")

    assert resolution.resolved is None
    assert [candidate.metric_id for candidate in resolution.candidates] == [4321]


@pytest.mark.asyncio
async def test_catalog_ambiguous_metric_candidates_do_not_silently_execute_first() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=1001,
                name="Teacher compensation notes",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=1002,
                name="Teacher compensation amount",
                answer_type="numeric",
            ),
        ]
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=_catalog_adjudicator(
            CatalogAdjudicationDecision(
                action="clarify",
                selected_ids=[],
                clarification_question=(
                    "Which teacher compensation metric do you want to use?"
                ),
                confidence=0.42,
                rationale="Both provided candidates plausibly match the phrase.",
            )
        ),
    )

    resolution = await resolver.resolve_primary_metric(
        "teacher compensation",
        numeric_only=True,
    )

    assert resolution.resolved is None
    assert resolution.ambiguous is True
    assert [candidate.metric_id for candidate in resolution.candidates] == [1001, 1002]


@pytest.mark.asyncio
async def test_catalog_adjudicated_candidate_choice_resolves_primary_metric() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=39,
                name=(
                    "Minimum number of formal observations per evaluation cycle "
                    "for non-tenured teachers"
                ),
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=40,
                name="Teacher observation narrative policy",
                answer_type="text",
            ),
        ]
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=_catalog_adjudicator(
            CatalogAdjudicationDecision(
                action="select_one",
                selected_ids=["39"],
                confidence=0.82,
                rationale="The numeric formal observation count matches the phrase.",
            )
        ),
    )

    resolution = await resolver.resolve_primary_metric(
        "number of teacher observations per year",
        numeric_only=True,
    )

    assert resolution.ok is True
    assert resolution.resolved is not None
    assert resolution.resolved.metric_id == 39
    assert [candidate.metric_id for candidate in resolution.candidates] == [39, 40]
    assert not any(query.startswith("ids:") for query in repository.metric_queries)


@pytest.mark.asyncio
async def test_catalog_adjudication_rejects_out_of_candidate_primary_choice() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=39,
                name="Classroom observation frequency",
                answer_type="numeric",
            )
        ]
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=_UncheckedCatalogAdjudicator(
            CatalogAdjudicationDecision(
                action="select_one",
                selected_ids=["999"],
                confidence=0.71,
                rationale="Invented ID should never authorize execution.",
            )
        ),
    )

    resolution = await resolver.resolve_primary_metric(
        "teacher observation count",
        numeric_only=True,
    )

    assert resolution.ok is False
    assert resolution.resolved is None
    assert resolution.ambiguous is True
    assert [candidate.metric_id for candidate in resolution.candidates] == [39]


@pytest.mark.asyncio
async def test_catalog_adjudication_ambiguous_decision_preserves_clarification() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=1001,
                name="Teacher compensation notes",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=1002,
                name="Teacher compensation amount",
                answer_type="numeric",
            ),
        ]
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=_catalog_adjudicator(
            CatalogAdjudicationDecision(
                action="clarify",
                selected_ids=[],
                clarification_question="Which teacher compensation metric do you mean?",
                confidence=0.34,
                rationale="The phrase matches multiple provided candidates.",
            )
        ),
    )

    resolution = await resolver.resolve_metric_bundle(
        "teacher compensation",
        numeric_only=False,
    )

    assert resolution.ok is False
    assert resolution.resolved == []
    assert resolution.ambiguous is True
    assert [candidate.metric_id for candidate in resolution.candidates] == [1001, 1002]


@pytest.mark.asyncio
async def test_catalog_adjudicated_bundle_choice_resolves_candidate_ids_only() -> None:
    repository = FakeCatalogRepository(
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
        ]
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=_catalog_adjudicator(
            CatalogAdjudicationDecision(
                action="select_bundle",
                selected_ids=["171", "172"],
                confidence=0.79,
                rationale="Both provided bonus amount metrics match the bundle phrase.",
            )
        ),
    )

    resolution = await resolver.resolve_metric_bundle(
        "minimum and maximum annual performance pay bonuses",
        numeric_only=True,
    )

    assert resolution.ok is True
    assert [metric.metric_id for metric in resolution.resolved] == [171, 172]
    assert [candidate.metric_id for candidate in resolution.candidates] == [171, 172]


@pytest.mark.asyncio
async def test_catalog_adjudicated_bundle_choice_cannot_invent_metric_ids() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=171,
                name="Minimum annual performance pay bonus, if eligible",
                answer_type="numeric",
            )
        ]
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=_UncheckedCatalogAdjudicator(
            CatalogAdjudicationDecision(
                action="select_bundle",
                selected_ids=["171", "999"],
                confidence=0.74,
                rationale="Invented IDs must not become execution authority.",
            )
        ),
    )

    resolution = await resolver.resolve_metric_bundle(
        "performance pay bonus amounts",
        numeric_only=True,
    )

    assert resolution.ok is False
    assert resolution.resolved == []
    assert resolution.ambiguous is True
    assert [candidate.metric_id for candidate in resolution.candidates] == [171]


@pytest.mark.parametrize(
    "phrase",
    [
        "number of required formal observations per year for non-tenured teachers",
        "number of formal classroom observations per year",
        "number of teacher observations per year",
        "number of formal observations per year",
    ],
)
@pytest.mark.asyncio
async def test_catalog_formal_observation_phrases_generate_bounded_candidates(
    phrase: str,
) -> None:
    formal_observation_metric = MetricCandidate(
        metric_id=39,
        name=(
            "Minimum number of formal observations per evaluation cycle "
            "for non-tenured teachers"
        ),
        answer_type="numeric",
    )
    repository = FakeCatalogRepository(
        metrics=[formal_observation_metric],
        metric_results_by_query={
            formal_observation_metric.name: [formal_observation_metric],
        },
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=None,
    )

    resolution = await resolver.resolve_metric_bundle(phrase, numeric_only=True)

    assert resolution.ok is False
    assert resolution.resolved == []
    assert [candidate.metric_id for candidate in resolution.candidates] == [39]
    assert phrase in repository.metric_queries
    assert formal_observation_metric.name in repository.metric_queries


@pytest.mark.asyncio
async def test_catalog_candidate_fusion_flag_off_keeps_existing_search_limits() -> None:
    phrase = "number of formal observations per year"
    formal_observation_metric = MetricCandidate(
        metric_id=39,
        name=(
            "Minimum number of formal observations per evaluation cycle "
            "for non-tenured teachers"
        ),
        answer_type="numeric",
    )
    repository = FakeCatalogRepository(
        metric_results_by_query={
            phrase: [formal_observation_metric],
            formal_observation_metric.name: [formal_observation_metric],
        },
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=None,
    )

    candidates = await resolver._search_metric_candidates(phrase, limit=2)

    assert [candidate.metric_id for candidate in candidates] == [39]
    assert repository.metric_queries == [phrase, formal_observation_metric.name]
    assert repository.metric_query_limits == [2, 2]
    assert candidates[0].metadata == {}


@pytest.mark.asyncio
async def test_catalog_candidate_fusion_fetches_wider_pool_and_adds_metadata() -> None:
    phrase = "number of formal observations per year"
    formal_observation_metric = MetricCandidate(
        metric_id=39,
        name=(
            "Minimum number of formal observations per evaluation cycle "
            "for non-tenured teachers"
        ),
        answer_type="numeric",
    )
    repository = FakeCatalogRepository(
        metric_results_by_query={
            phrase: [
                MetricCandidate(metric_id=44, name="Formal observations for tenured teachers"),
            ],
            formal_observation_metric.name: [formal_observation_metric],
        },
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=None,
        candidate_fusion_enabled=True,
    )

    candidates = await resolver._search_metric_candidates(phrase, limit=2)

    assert [candidate.metric_id for candidate in candidates] == [39, 44]
    assert repository.metric_queries == [phrase, formal_observation_metric.name]
    assert repository.metric_query_limits == [15, 15]
    assert candidates[0].metadata["candidate_fusion"]["sources"] == [
        {
            "query": formal_observation_metric.name,
            "source": "catalog_search",
            "rank": 1,
        }
    ]


@pytest.mark.asyncio
async def test_catalog_watch_teachers_teach_clarifies_observation_metrics() -> None:
    phrase = "watch teachers teach"
    formal_non_tenured = MetricCandidate(
        metric_id=39,
        name=(
            "Minimum number of formal observations per evaluation cycle "
            "for non-tenured teachers"
        ),
        answer_type="numeric",
    )
    formal_tenured = MetricCandidate(
        metric_id=44,
        name=(
            "Minimum number of formal observations per evaluation cycle "
            "for tenured teachers"
        ),
        answer_type="numeric",
    )
    informal_non_tenured = MetricCandidate(
        metric_id=40,
        name=(
            "Minimum number of informal observations per evaluation cycle "
            "for non-tenured teachers"
        ),
        answer_type="numeric",
    )
    formal_query = formal_non_tenured.name
    repository = FakeCatalogRepository(
        metric_results_by_query={
            phrase: [],
            formal_query: [formal_non_tenured, formal_tenured, informal_non_tenured],
        },
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_metric_bundle(phrase, numeric_only=False)

    assert resolution.ok is False
    assert resolution.ambiguous is True
    assert [candidate.metric_id for candidate in resolution.candidates] == [39, 44, 40]
    assert repository.metric_queries == [phrase, formal_query]


@pytest.mark.asyncio
async def test_catalog_specific_observation_status_can_resolve_single_metric() -> None:
    phrase = "formal observations for non-tenured teachers"
    formal_non_tenured = MetricCandidate(
        metric_id=39,
        name=(
            "Minimum number of formal observations per evaluation cycle "
            "for non-tenured teachers"
        ),
        answer_type="numeric",
    )
    formal_tenured = MetricCandidate(
        metric_id=44,
        name=(
            "Minimum number of formal observations per evaluation cycle "
            "for tenured teachers"
        ),
        answer_type="numeric",
    )
    repository = FakeCatalogRepository(
        metric_results_by_query={
            phrase: [formal_non_tenured, formal_tenured],
            formal_non_tenured.name: [formal_non_tenured, formal_tenured],
        },
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=_catalog_adjudicator(
            CatalogAdjudicationDecision(
                action="select_one",
                selected_ids=["39"],
                confidence=0.9,
                rationale="The phrase specifies non-tenured formal observations.",
            )
        ),
    )

    resolution = await resolver.resolve_metric_bundle(phrase, numeric_only=True)

    assert resolution.ok is True
    assert [metric.metric_id for metric in resolution.resolved] == [39]


@pytest.mark.asyncio
async def test_catalog_unrelated_phrase_stays_unresolved_after_observation_guard() -> None:
    repository = FakeCatalogRepository(metric_results_by_query={"favorite color": []})
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_metric_bundle(
        "favorite color",
        numeric_only=False,
    )

    assert resolution.ok is False
    assert resolution.resolved == []
    assert resolution.candidates == []
    assert resolution.ambiguous is False


@pytest.mark.asyncio
async def test_catalog_ambiguous_metric_alias_returns_clarification_candidates() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=55,
                name="The district permits, requires, or prohibits collaborative planning time",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=56,
                name="The district specifies a minimum or maximum amount of collaborative planning time",
                answer_type="text",
            ),
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
            _ambiguous_alias(
                "planning time",
                entity_type="metric_bundle",
                candidate_refs=[
                    {"metric_id": 55, "metric_name": "Planning time policy"},
                    {"metric_id": 56, "metric_name": "Planning time min/max policy"},
                    {
                        "metric_id": 57,
                        "metric_name": "Elementary teacher planning time per week",
                    },
                    {
                        "metric_id": 58,
                        "metric_name": "Middle school teacher planning time per week",
                    },
                    {
                        "metric_id": 59,
                        "metric_name": "High school teacher planning time per week",
                    },
                ],
            )
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_primary_metric(
        "planning time",
        numeric_only=True,
    )

    assert resolution.resolved is None
    assert resolution.ambiguous is True
    assert [candidate.metric_id for candidate in resolution.candidates] == [57, 58, 59]
    assert repository.metric_queries == ["planning time", "ids:55,56,57,58,59"]


@pytest.mark.asyncio
async def test_catalog_keeps_broad_teacher_salary_alias_ambiguous() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=89,
                name="Annual base salary for a first year teacher with a bachelor's degree",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=96,
                name="Annual base salary for a first year teacher with a master's degree",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=112,
                name=(
                    "Maximum base salary for a teacher with a bachelor's degree, "
                    "based on new hire schedule"
                ),
                answer_type="numeric",
            ),
        ],
        aliases=[
            _ambiguous_alias(
                "teacher salary",
                entity_type="metric",
                candidate_refs=[
                    {"metric_id": 89},
                    {"metric_id": 96},
                    {"metric_id": 112},
                ],
            )
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_primary_metric(
        "teacher salary",
        numeric_only=True,
    )

    assert resolution.resolved is None
    assert resolution.ambiguous is True
    assert [candidate.metric_id for candidate in resolution.candidates] == [
        89,
        96,
        112,
    ]


@pytest.mark.asyncio
async def test_catalog_resolves_contextual_bachelor_salary_default_from_metadata() -> None:
    repository = FakeCatalogRepository(
        metrics=[
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
        aliases=[
            _ambiguous_alias(
                "starting salary",
                entity_type="metric",
                candidate_refs=[
                    {
                        "metric_id": 89,
                        "metric_name": (
                            "Annual base salary for a first year teacher with "
                            "a bachelor's degree"
                        ),
                    },
                    {
                        "metric_id": 96,
                        "metric_name": (
                            "Annual base salary for a first year teacher with "
                            "a master's degree"
                        ),
                    },
                ],
                metadata={
                    "contextual_defaults": {
                        "frpl_profile_sort_display": {
                            "metric_id": 89,
                            "note_keys": [
                                "bachelor_starting_salary_default_lane",
                            ],
                        }
                    }
                },
            )
        ],
        renderer_notes={
            "bachelor_starting_salary_default_lane": (
                "Used bachelor's-degree starting salary for first-year teachers "
                "because no degree lane was specified. Ask for master's starting "
                "salary to use the master's-degree lane."
            )
        },
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    contextual_default = await resolver.resolve_contextual_metric_default(
        "starting salary",
        context_key="frpl_profile_sort_display",
        numeric_only=False,
    )

    assert contextual_default is not None
    assert contextual_default.metric.metric_id == 89
    assert [note.note_key for note in contextual_default.renderer_notes] == [
        "bachelor_starting_salary_default_lane"
    ]
    assert repository.renderer_note_queries == [
        ["bachelor_starting_salary_default_lane"]
    ]


@pytest.mark.asyncio
async def test_catalog_resolves_launch_starting_salary_default_from_seed_alias() -> None:
    repository = FakeCatalogRepository(
        metrics=[
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
        aliases=[
            _ambiguous_alias(
                "starting salary",
                entity_type="metric",
                candidate_refs=[
                    {"metric_id": 89},
                    {"metric_id": 96},
                ],
                metadata={
                    "contextual_defaults": {
                        "launch_starting_salary_default": {
                            "metric_id": 89,
                            "note_keys": [
                                "bachelor_starting_salary_default_lane",
                            ],
                        }
                    }
                },
            )
        ],
        renderer_notes={
            "bachelor_starting_salary_default_lane": (
                "Used first-year BA starting salary because no degree lane "
                "was specified."
            )
        },
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    contextual_default = await resolver.resolve_contextual_metric_default(
        "starting salary",
        context_key="launch_starting_salary_default",
        numeric_only=False,
    )

    assert contextual_default is not None
    assert contextual_default.metric.metric_id == 89
    assert contextual_default.alias.source == "SSN-235"


@pytest.mark.asyncio
async def test_catalog_does_not_use_launch_default_when_seed_alias_lacks_context() -> None:
    repository = FakeCatalogRepository(
        metrics=[
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
        aliases=[
            _ambiguous_alias(
                "starting salary",
                entity_type="metric",
                candidate_refs=[
                    {"metric_id": 89},
                    {"metric_id": 96},
                ],
                metadata={
                    "contextual_defaults": {
                        "frpl_profile_sort_display": {
                            "metric_id": 89,
                            "note_keys": [
                                "bachelor_starting_salary_default_lane",
                            ],
                        }
                    }
                },
            )
        ],
        renderer_notes={
            "bachelor_starting_salary_default_lane": (
                "Used first-year BA starting salary because no degree lane "
                "was specified."
            )
        },
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    contextual_default = await resolver.resolve_contextual_metric_default(
        "starting salary",
        context_key="launch_starting_salary_default",
        numeric_only=True,
    )

    assert contextual_default is None


@pytest.mark.asyncio
async def test_catalog_resolves_broad_sick_leave_to_policy_bundle_from_seed_alias() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(metric_id=198, name="Maximum number of annual paid sick days"),
            MetricCandidate(
                metric_id=201,
                name="Whether sick leave increases with service",
            ),
            MetricCandidate(
                metric_id=202,
                name="Whether unused sick leave can carry over",
            ),
        ],
        aliases=[
            _approved_alias(
                "sick leave",
                entity_type="metric_bundle",
                canonical_ids=["198", "201", "202"],
            )
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_metric_bundle("sick leave")

    assert [metric.metric_id for metric in resolution.resolved] == [198, 201, 202]
    assert resolution.bundle_key == "sick_leave"


@pytest.mark.asyncio
async def test_catalog_resolves_launch_benefits_bundle_from_seed_alias() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(metric_id=232, name="Full premium coverage"),
            MetricCandidate(metric_id=233, name="Employee premium percent"),
            MetricCandidate(metric_id=234, name="Dependent premium percent"),
            MetricCandidate(metric_id=235, name="Premium dollar cap"),
        ],
        aliases=[
            _approved_alias(
                "benefits",
                entity_type="metric_bundle",
                canonical_ids=["232", "233", "234", "235"],
            )
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_metric_bundle("benefits")

    assert [metric.metric_id for metric in resolution.resolved] == [
        232,
        233,
        234,
        235,
    ]
    assert resolution.bundle_key == "benefits"


@pytest.mark.asyncio
async def test_catalog_resolves_launch_health_premium_filter_alias_from_seed() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=232,
                name="Does the district cover 100% of employees health premium?",
                answer_type="text",
            )
        ],
        aliases=[
            _approved_alias(
                "employee health insurance premium coverage",
                entity_type="metric",
                canonical_id="232",
            )
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_metric_bundle(
        "employee health insurance premium coverage",
        numeric_only=False,
    )

    assert [metric.metric_id for metric in resolution.resolved] == [232]


@pytest.mark.asyncio
async def test_catalog_contextual_default_fails_closed_without_renderer_note() -> None:
    repository = FakeCatalogRepository(
        metrics=[
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
        aliases=[
            _ambiguous_alias(
                "starting salary",
                entity_type="metric",
                candidate_refs=[
                    {"metric_id": 89},
                    {"metric_id": 96},
                ],
                metadata={
                    "contextual_defaults": {
                        "frpl_profile_sort_display": {
                            "metric_id": 89,
                            "note_keys": ["missing_renderer_note"],
                        }
                    }
                },
            )
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    contextual_default = await resolver.resolve_contextual_metric_default(
        "starting salary",
        context_key="frpl_profile_sort_display",
        numeric_only=False,
    )

    assert contextual_default is None


@pytest.mark.asyncio
async def test_catalog_specific_planning_time_alias_resolves_to_metric_id() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=57,
                name="Minimum amount of elementary teacher planning time per week (in minutes)",
                answer_type="numeric",
            )
        ],
        aliases=[
            _approved_alias(
                "elementary planning time",
                entity_type="metric",
                canonical_id="57",
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_primary_metric(
        "elementary planning time",
        numeric_only=True,
    )

    assert resolution.resolved is not None
    assert resolution.resolved.metric_id == 57
    assert resolution.ambiguous is False
    assert repository.metric_queries == ["elementary planning time", "ids:57"]


@pytest.mark.asyncio
async def test_catalog_resolves_classroom_watching_alias_variant_to_formal_observation_metric() -> None:
    repository = FakeCatalogRepository(
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
        aliases=[
            _approved_alias(
                "teachers watched in classroom regularly",
                entity_type="metric",
                canonical_id="39",
            )
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_primary_metric(
        "teachers being watched in the classroom regularly",
        numeric_only=True,
    )

    assert resolution.ok is True
    assert resolution.resolved is not None
    assert resolution.resolved.metric_id == 39
    assert repository.metric_queries == [
        "teachers being watched in the classroom regularly",
        (
            "Minimum number of formal observations per evaluation cycle "
            "for non-tenured teachers"
        ),
        "ids:39",
    ]


@pytest.mark.asyncio
async def test_catalog_clarifies_school_year_length_metric_family() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=69,
                name="Total contracted workdays per academic year",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=70,
                name=(
                    "Contracted student-teacher contact days per academic "
                    "year in elementary school"
                ),
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=254,
                name=(
                    "Contracted student-teacher contact days per academic "
                    "year in middle school"
                ),
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=255,
                name=(
                    "Contracted student-teacher contact days per academic "
                    "year in high school"
                ),
                answer_type="numeric",
            ),
        ],
        aliases=[
            _ambiguous_alias(
                "school year length",
                entity_type="metric",
                candidate_refs=[
                    {"metric_id": 69},
                    {"metric_id": 70},
                    {"metric_id": 254},
                    {"metric_id": 255},
                ],
            )
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_primary_metric(
        "school year length",
        numeric_only=True,
    )

    assert resolution.ambiguous is True
    assert resolution.resolved is None
    assert [candidate.metric_id for candidate in resolution.candidates] == [
        69,
        70,
        254,
        255,
    ]


@pytest.mark.asyncio
async def test_catalog_resolves_planner_emitted_classroom_observation_alias() -> None:
    repository = FakeCatalogRepository(
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
        aliases=[
            _approved_alias(
                "number of required classroom observations per year",
                entity_type="metric",
                canonical_id="39",
            )
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_primary_metric(
        "number of required classroom observations per year",
        numeric_only=True,
    )

    assert resolution.ok is True
    assert resolution.resolved is not None
    assert resolution.resolved.metric_id == 39


@pytest.mark.asyncio
async def test_catalog_resolves_subject_stipend_aliases_to_additional_pay_metrics() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=185,
                name="Minimum amount of additional annual pay for science teachers",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=186,
                name="Minimum amount of additional annual pay for math teachers",
                answer_type="numeric",
            ),
        ],
        aliases=[
            _approved_alias(
                "math teacher stipend",
                entity_type="metric",
                canonical_id="186",
            ),
            _approved_alias(
                "science teacher stipend",
                entity_type="metric",
                canonical_id="185",
            ),
        ],
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    math_resolution = await resolver.resolve_primary_metric("math teacher stipend")
    science_resolution = await resolver.resolve_primary_metric(
        "science teacher stipend"
    )

    assert math_resolution.ok is True
    assert math_resolution.resolved is not None
    assert math_resolution.resolved.metric_id == 186
    assert science_resolution.ok is True
    assert science_resolution.resolved is not None
    assert science_resolution.resolved.metric_id == 185
    assert repository.metric_queries == [
        "math teacher stipend",
        "ids:186",
        "science teacher stipend",
        "ids:185",
    ]


@pytest.mark.asyncio
async def test_catalog_uses_db_backed_district_alias_authority() -> None:
    repository = FakeCatalogRepository(
        districts=DistrictResolution(
            resolved=[
                DistrictCandidate(
                    district_id=15,
                    district_name="Los Angeles Unified School District",
                    state="CA",
                ),
                DistrictCandidate(
                    district_id=40,
                    district_name="Miami-Dade County Public Schools",
                    state="FL",
                ),
            ]
        ),
        aliases=[
            _approved_alias(
                "LAUSD",
                entity_type="district",
                canonical_id="15",
            ),
            _approved_alias(
                "Miami",
                entity_type="district",
                canonical_id="40",
            ),
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_districts(["LAUSD", "Miami"])

    assert resolution.ok is True
    assert [district.district_id for district in resolution.resolved] == [15, 40]
    assert repository.alias_queries == [
        ("LAUSD", ("district",)),
        ("Miami", ("district",)),
    ]


@pytest.mark.asyncio
async def test_catalog_ambiguous_district_alias_returns_candidates() -> None:
    repository = FakeCatalogRepository(
        aliases=[
            _ambiguous_alias(
                "DPS",
                entity_type="district",
                candidate_refs=[
                    {
                        "district_id": 26,
                        "district_name": "Denver Public Schools",
                        "state": "CO",
                    },
                    {
                        "district_id": 81,
                        "district_name": "Detroit Public Schools Community District",
                        "state": "MI",
                    },
                ],
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_districts(["DPS"])

    assert resolution.ok is False
    assert "DPS" in resolution.ambiguous
    assert [district.district_id for district in resolution.ambiguous["DPS"]] == [
        26,
        81,
    ]


@pytest.mark.asyncio
async def test_catalog_single_covered_district_candidate_resolves_directly() -> None:
    # #1563 / #1566: a fuzzy phrase whose ONLY covered-universe match is a
    # single district must resolve directly, not bucket into a degenerate
    # one-option clarification ("I found more than one covered district
    # matching ... - DC, DC"). This test previously asserted the inverse
    # (named `..._requires_clarification`); the spec was intentionally
    # reversed by #1563/#1566 — a single covered match is unambiguous and
    # should answer.
    philadelphia = DistrictCandidate(
        district_id=50,
        district_name="School District of Philadelphia",
        state="PA",
    )
    repository = FakeCatalogRepository(
        districts=DistrictResolution(resolved=[philadelphia]),
        district_candidate_results_by_query={
            "I am from Philadelphia": [philadelphia],
        },
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_districts(["I am from Philadelphia"])

    assert resolution.ok is True
    assert resolution.ambiguous == {}
    assert [district.district_id for district in resolution.resolved] == [50]
    assert repository.district_candidate_queries == [
        ("I am from Philadelphia", None, 8)
    ]


@pytest.mark.asyncio
async def test_catalog_leaves_dps_candidate_generation_ambiguous() -> None:
    denver = DistrictCandidate(
        district_id=26,
        district_name="Denver Public Schools",
        state="CO",
    )
    detroit = DistrictCandidate(
        district_id=81,
        district_name="Detroit Public Schools Community District",
        state="MI",
    )
    repository = FakeCatalogRepository(
        districts=DistrictResolution(resolved=[denver, detroit]),
        district_candidate_results_by_query={"DPS": [denver, detroit]},
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_districts(["DPS"])

    assert resolution.ok is False
    assert [district.district_id for district in resolution.ambiguous["DPS"]] == [
        26,
        81,
    ]


@pytest.mark.asyncio
async def test_catalog_does_not_use_adjudicator_as_district_execution_authority() -> None:
    portland_me = DistrictCandidate(
        district_id=120,
        district_name="Portland Public Schools",
        state="ME",
    )
    portland_or = DistrictCandidate(
        district_id=121,
        district_name="Portland Public Schools",
        state="OR",
    )
    # #1513: a state-suffixed phrase ("Portland ME") now resolves exactly
    # under its state before the candidate-search path, so it no longer
    # exercises this test's subject. A stateless fuzzy phrase still reaches
    # the candidate search — exactly the case where the adjudicator must NOT
    # pick.
    repository = FakeCatalogRepository(
        districts=DistrictResolution(resolved=[portland_me, portland_or]),
        district_candidate_results_by_query={
            "Portland area schools": [portland_me, portland_or],
        },
    )
    adjudicator = SelectingCatalogAdjudicator("120")
    resolver = CatalogResolver(repository, adjudicator=adjudicator)

    resolution = await resolver.resolve_districts(["Portland area schools"])

    assert resolution.ok is False
    assert resolution.resolved == []
    assert [
        district.district_id
        for district in resolution.ambiguous["Portland area schools"]
    ] == [
        120,
        121,
    ]
    assert adjudicator.candidate_ids == []


@pytest.mark.asyncio
async def test_catalog_dc_shape_single_covered_match_resolves_directly() -> None:
    """#1563: 'DC Public Schools' has exactly one covered district.

    The DC-shape reproduction: a phrase that maps to a single covered
    district (District of Columbia Public Schools, id=35) must resolve
    directly instead of producing the degenerate one-option clarification
    seen in case_id 157 ("I found more than one covered district matching
    'DC Public Schools'. ... - DC, DC"). The promoted row comes from the
    canonical covered index (``list_covered_districts``), not the raw fuzzy
    candidate, so a drifted candidate name can never leak into the answer.
    """

    dc = DistrictCandidate(
        district_id=35,
        district_name="District of Columbia Public Schools",
        state="DC",
    )
    repository = FakeCatalogRepository(
        districts=DistrictResolution(resolved=[dc]),
        district_candidate_results_by_query={
            "DC Public Schools": [dc],
        },
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_districts(["DC Public Schools"])

    assert resolution.ok is True
    assert resolution.ambiguous == {}
    assert [district.district_id for district in resolution.resolved] == [35]
    assert [district.district_name for district in resolution.resolved] == [
        "District of Columbia Public Schools"
    ]


@pytest.mark.asyncio
async def test_catalog_cleveland_shape_one_covered_among_many_resolves() -> None:
    """#1566: filter fuzzy candidates to the covered universe before deciding.

    The fuzzy candidate search can return several hits where only ONE is in
    the covered universe (the Cleveland County symptom: a non-covered
    Oklahoma record surfaced alongside the covered NC district). The resolver
    must promote the single covered match and never clarify on the
    non-covered noise. Here ``cleveland_nc`` is covered (in
    ``list_covered_districts``); ``cleveland_ok`` is NOT — it is returned by
    the fuzzy search only. Result: resolve to NC, no clarification.
    """

    cleveland_nc = DistrictCandidate(
        district_id=200,
        district_name="Cleveland County Schools",
        state="NC",
    )
    cleveland_ok = DistrictCandidate(
        district_id=999,
        district_name="Cleveland",
        state="OK",
    )
    repository = FakeCatalogRepository(
        # Only the NC district is covered (present in list_covered_districts).
        districts=DistrictResolution(resolved=[cleveland_nc]),
        district_candidate_results_by_query={
            "Cleveland County": [cleveland_nc, cleveland_ok],
        },
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_districts(["Cleveland County"])

    assert resolution.ok is True
    assert resolution.ambiguous == {}
    assert [district.district_id for district in resolution.resolved] == [200]


@pytest.mark.asyncio
async def test_catalog_single_non_covered_candidate_stays_ambiguous() -> None:
    """A lone fuzzy candidate that is NOT covered keeps the prior behavior.

    Grounding guard: the short-circuit only promotes a candidate whose
    district_id is in the covered universe. A single fuzzy hit that is absent
    from ``list_covered_districts`` must NOT be invented into a resolved
    district — it falls through to the existing ``ambiguous`` bucket exactly
    as before the #1563/#1566 fix, so coverage is never fabricated.
    """

    non_covered = DistrictCandidate(
        district_id=999,
        district_name="Cleveland",
        state="OK",
    )
    repository = FakeCatalogRepository(
        # The covered universe does NOT contain district 999.
        districts=DistrictResolution(resolved=[]),
        district_candidate_results_by_query={
            "Cleveland County": [non_covered],
        },
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_districts(["Cleveland County"])

    assert resolution.ok is False
    assert resolution.resolved == []
    assert [
        district.district_id
        for district in resolution.ambiguous["Cleveland County"]
    ] == [999]


@pytest.mark.asyncio
async def test_resolve_districts_state_suffix_resolves_exactly_without_clarify() -> None:
    """#1513: a typed "Name, ST" handle re-resolves exactly under its state.

    "Portland Public Schools, OR" — the format Compass itself renders in
    chips and clarify bullets — must resolve to the OR district instead of
    bucketing into ambiguous (the one-option reconfirm). The short form
    "Portland ME" resolves too: district-name normalization strips the
    org-type words, so the remainder exact-matches under the explicit state.
    Only a unique EXACT/normalized match under the state is promoted: a
    state-suffixed phrase whose remainder has no exact match still falls
    through to the candidate-search ambiguity path, so nothing is silently
    substituted.
    """

    portland_me = DistrictCandidate(
        district_id=120,
        district_name="Portland Public Schools",
        state="ME",
    )
    portland_or = DistrictCandidate(
        district_id=121,
        district_name="Portland Public Schools",
        state="OR",
    )
    repository = FakeCatalogRepository(
        districts=DistrictResolution(resolved=[portland_me, portland_or]),
        district_candidate_results_by_query={
            "Cleveland County OK": [portland_me, portland_or],
        },
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_districts(["Portland Public Schools, OR"])
    assert resolution.ok is True
    assert [district.district_id for district in resolution.resolved] == [121]
    assert resolution.ambiguous == {}

    short_form = await resolver.resolve_districts(["Portland ME"])
    assert short_form.ok is True
    assert [district.district_id for district in short_form.resolved] == [120]

    fuzzy = await resolver.resolve_districts(["Cleveland County OK"])
    assert fuzzy.ok is False
    assert [
        district.district_id for district in fuzzy.ambiguous["Cleveland County OK"]
    ] == [
        120,
        121,
    ]


@pytest.mark.asyncio
async def test_catalog_uses_db_backed_metric_alias_authority() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=174,
                name=(
                    "District offers additional pay for teachers with National "
                    "Board Certification"
                ),
                answer_type="text",
            )
        ],
        aliases=[
            _approved_alias(
                "National Board Certification bonus",
                entity_type="metric",
                canonical_id="174",
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_primary_metric(
        "National Board Certification bonus",
        numeric_only=False,
    )

    assert resolution.resolved is not None
    assert resolution.resolved.metric_id == 174
    assert repository.metric_queries == [
        "National Board Certification bonus",
        "ids:174",
    ]


@pytest.mark.asyncio
async def test_catalog_adjudication_skeleton_does_not_change_exact_or_alias_resolution() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=4321,
                name="Collective bargaining status",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=174,
                name=(
                    "District offers additional pay for teachers with National "
                    "Board Certification"
                ),
                answer_type="text",
            ),
        ],
        aliases=[
            _approved_alias(
                "National Board Certification bonus",
                entity_type="metric",
                canonical_id="174",
            )
        ],
    )
    resolver = CatalogResolver(repository)

    exact = await resolver.resolve_primary_metric(
        "Collective bargaining status",
        numeric_only=False,
    )
    alias = await resolver.resolve_primary_metric(
        "National Board Certification bonus",
        numeric_only=False,
    )

    assert exact.resolved is not None
    assert exact.resolved.metric_id == 4321
    assert alias.resolved is not None
    assert alias.resolved.metric_id == 174


@pytest.mark.asyncio
async def test_catalog_clarifies_broad_teacher_salary_against_nearby_salary_metrics() -> None:
    repository = FakeCatalogRepository(
        metrics=[
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
            MetricCandidate(
                metric_id=112,
                name=(
                    "Maximum base salary for a teacher with a bachelor's degree, "
                    "based on new hire schedule"
                ),
                answer_type="numeric",
            ),
        ],
        aliases=[
            _ambiguous_alias(
                "teacher salary",
                entity_type="metric_bundle",
                candidate_refs=[
                    {"metric_id": 89},
                    {"metric_id": 96},
                    {"metric_id": 112},
                ],
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_metric_bundle("teacher salary", numeric_only=True)

    assert resolution.ok is False
    assert resolution.ambiguous is True
    assert [candidate.metric_id for candidate in resolution.candidates] == [89, 96, 112]


@pytest.mark.asyncio
async def test_catalog_resolves_peer_maximum_teacher_salary_alias() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=112,
                name=(
                    "Maximum base salary for a teacher with a bachelor's degree, "
                    "based on new hire schedule"
                ),
                answer_type="numeric",
            ),
        ],
        aliases=[
            _approved_alias(
                "Maximum teacher salary",
                entity_type="metric",
                canonical_id="112",
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_primary_metric(
        "maximum teacher salary",
        numeric_only=True,
    )

    assert resolution.ok is True
    assert resolution.resolved is not None
    assert resolution.resolved.metric_id == 112


@pytest.mark.asyncio
async def test_catalog_resolves_explicit_salary_degree_lanes() -> None:
    repository = FakeCatalogRepository(
        metrics=[
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
        aliases=[
            _approved_alias(
                "starting salaries for teachers with a bachelor's degree",
                entity_type="metric",
                canonical_id="89",
            ),
            _approved_alias(
                "BA starting salary",
                entity_type="metric",
                canonical_id="89",
            ),
            _approved_alias(
                "starting salary for teachers with a master's degree",
                entity_type="metric",
                canonical_id="96",
            ),
            _approved_alias(
                "MA starting salary",
                entity_type="metric",
                canonical_id="96",
            ),
        ],
    )
    resolver = CatalogResolver(repository)

    ba_resolution = await resolver.resolve_primary_metric(
        "starting salaries for teachers with a bachelor's degree",
        numeric_only=True,
    )
    ma_resolution = await resolver.resolve_primary_metric(
        "starting salary for teachers with a master's degree",
        numeric_only=True,
    )

    assert ba_resolution.ok is True
    assert ba_resolution.resolved is not None
    assert ba_resolution.resolved.metric_id == 89
    assert ma_resolution.ok is True
    assert ma_resolution.resolved is not None
    assert ma_resolution.resolved.metric_id == 96


@pytest.mark.asyncio
async def test_catalog_uses_db_backed_metric_bundle_authority() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=198,
                name="Maximum number of annual paid sick days",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=201,
                name="Paid sick day allowance increases with years of service in the district",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=202,
                name="Unused sick days can be carried over to the following year",
                answer_type="text",
            ),
        ],
        aliases=[
            _approved_alias(
                "sick leave policy",
                entity_type="metric_bundle",
                canonical_ids=["198", "201", "202"],
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_metric_bundle(
        "sick leave policy",
        numeric_only=False,
    )

    assert resolution.ok is True
    assert resolution.bundle_key == "sick_leave_policy"
    assert [metric.metric_id for metric in resolution.resolved] == [198, 201, 202]
    assert repository.metric_queries == [
        "sick leave policy",
        "ids:198,201,202",
    ]


@pytest.mark.asyncio
async def test_catalog_uses_reviewed_teacher_evaluation_difference_alias() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=37,
                name="Frequency of summative evaluation rating for non-tenured teachers",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=39,
                name="Minimum number of formal observations per evaluation cycle for non-tenured teachers",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=40,
                name="Minimum number of informal observations per evaluation cycle for non-tenured teachers",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=42,
                name="Frequency of summative evaluation rating for tenured teachers",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=44,
                name="Minimum number of formal observations per evaluation cycle for tenured teachers",
                answer_type="numeric",
            ),
            MetricCandidate(
                metric_id=45,
                name="Minimum number of informal observations per evaluation cycle for tenured teachers",
                answer_type="numeric",
            ),
        ],
        aliases=[
            _approved_alias(
                "teacher evaluations differently",
                entity_type="metric_bundle",
                canonical_ids=["37", "39", "40", "42", "44", "45"],
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_metric_bundle(
        "teacher evaluations differently",
        numeric_only=False,
    )

    assert resolution.ok is True
    assert resolution.bundle_key == "teacher_evaluations_differently"
    assert [metric.metric_id for metric in resolution.resolved] == [
        37,
        39,
        40,
        42,
        44,
        45,
    ]


@pytest.mark.asyncio
async def test_executor_consumes_catalog_resolved_ids_not_free_text() -> None:
    catalog_repository = FakeCatalogRepository(
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
        aliases=[
            _approved_alias(
                "BA starting salary",
                entity_type="metric",
                canonical_id="89",
            )
        ],
        districts=DistrictResolution(
            resolved=[
                DistrictCandidate(district_id=10, district_name="Alpha", state="CA")
            ]
        ),
    )
    answer_repository = FakeAnswerRepository(
        rows=[
            _row(10, "Alpha", "$50,000"),
            _row(11, "Bravo", "$90,000"),
        ]
    )
    executor = DeterministicQueryExecutor(
        answer_repository,
        catalog_resolver=CatalogResolver(catalog_repository),
    )

    outcome = await executor.execute(_ranking_plan())

    assert outcome.result is not None
    assert [row.district_id for row in outcome.result.rows] == [10]
    assert answer_repository.fetched_metric_ids == [89]
    assert catalog_repository.metric_queries == [
        "BA starting salary",
        "ids:89",
    ]
    assert catalog_repository.district_queries == [["Alpha"]]


@pytest.mark.asyncio
async def test_profile_field_alias_resolves_through_catalog_authority() -> None:
    repository = FakeCatalogRepository(
        aliases=[
            _approved_alias(
                "free and reduced lunch share",
                entity_type="nces_field",
                canonical_id="frpl_pct",
            )
        ],
        nces_fields=[
            NCESFieldCandidate(
                field_key="frpl_pct",
                label="FRPL %",
                data_type="numeric",
                description="Free and reduced-price lunch share.",
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_profile_field_authority(
        "free and reduced lunch share"
    )

    assert resolution.ok
    assert resolution.resolved is not None
    assert resolution.resolved.field_key == "frpl_pct"
    assert resolution.resolution_method == "alias"
    assert repository.alias_queries == [
        ("free and reduced lunch share", ("nces_field",))
    ]
    assert repository.nces_queries == ["frpl_pct"]


@pytest.mark.asyncio
async def test_profile_field_exact_key_or_label_resolves_without_alias() -> None:
    repository = FakeCatalogRepository(
        nces_fields=[
            NCESFieldCandidate(
                field_key="frpl_pct",
                label="FRPL %",
                data_type="numeric",
                description="Free and reduced-price lunch share.",
            )
        ],
    )
    resolver = CatalogResolver(repository)

    by_key = await resolver.resolve_profile_field_authority("frpl_pct")
    by_label = await resolver.resolve_profile_field_authority("FRPL %")

    assert by_key.ok
    assert by_key.resolved is not None
    assert by_key.resolved.field_key == "frpl_pct"
    assert by_key.resolution_method == "exact"
    assert by_label.ok
    assert by_label.resolved is not None
    assert by_label.resolved.field_key == "frpl_pct"
    assert by_label.resolution_method == "exact"


@pytest.mark.asyncio
async def test_profile_field_candidate_search_does_not_authorize_loose_match() -> None:
    repository = FakeCatalogRepository(
        nces_fields=[
            NCESFieldCandidate(
                field_key="frpl_pct",
                label="FRPL %",
                data_type="numeric",
                description="Free and reduced-price lunch share.",
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_profile_field_authority("lunch share")

    assert not resolution.ok
    assert resolution.resolved is None
    assert [candidate.field_key for candidate in resolution.candidates] == ["frpl_pct"]


@pytest.mark.asyncio
async def test_cross_box_repair_moves_profile_metric_to_selection_sort_step() -> None:
    salary = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    repository = FakeCatalogRepository(
        metrics=[salary],
        metric_results_by_query={
            "starting teacher salaries": [],
            "enrollment": [],
        },
        aliases=[
            _approved_alias(
                "starting teacher salaries",
                entity_type="metric",
                canonical_id="89",
            ),
            _approved_alias(
                "enrollment",
                entity_type="nces_field",
                canonical_id="enrollment",
            ),
        ],
        nces_fields=[
            NCESFieldCandidate(
                field_key="enrollment",
                label="Enrollment",
                data_type="integer",
                description="Total district enrollment.",
            )
        ],
    )
    resolver = CatalogResolver(repository)
    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="rank",
            question=(
                "Show me starting teacher salaries for districts with the "
                "largest enrollment size"
            ),
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[
                MetricSpec(name="starting teacher salaries"),
                MetricSpec(name="enrollment", role="comparison"),
            ],
            sort=SortSpec(field="enrollment", direction="desc"),
            limit=LimitSpec(count=10, kind="top"),
        ),
    )

    repaired = await review_cross_box_planner_turn(turn, resolver)

    assert repaired.route == "execute"
    assert repaired.query_plan is not None
    assert [metric.name for metric in repaired.query_plan.metrics] == [
        "starting teacher salaries"
    ]
    assert repaired.query_plan.sort is None
    assert repaired.query_plan.sort_steps == [
        SortStepSpec(
            phase="selection",
            field="enrollment",
            direction="desc",
            key_type="profile_field",
            limit=LimitSpec(count=10, kind="top"),
        )
    ]


@pytest.mark.asyncio
async def test_cross_box_repair_promotes_clarify_pending_context_when_metric_is_profile_field() -> None:
    salary = MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )
    repository = FakeCatalogRepository(
        metrics=[salary],
        metric_results_by_query={
            "starting teacher salaries": [],
            "free and reduced lunch percentage": [],
        },
        aliases=[
            _approved_alias(
                "starting teacher salaries",
                entity_type="metric",
                canonical_id="89",
            ),
            _approved_alias(
                "free and reduced lunch percentage",
                entity_type="nces_field",
                canonical_id="frpl_pct",
            ),
        ],
        nces_fields=[
            NCESFieldCandidate(
                field_key="frpl_pct",
                label="FRPL %",
                data_type="numeric",
                description="Free and reduced-price lunch share.",
            )
        ],
    )
    resolver = CatalogResolver(repository)
    pending_plan = QueryPlan(
        operation="rank",
        question=(
            "show me starting teacher salaries for districts with the highest "
            "free-and-reduced lunch share"
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting teacher salaries"),
            MetricSpec(name="free and reduced lunch percentage", role="comparison"),
        ],
        sort=SortSpec(field="free and reduced lunch percentage", direction="desc"),
        limit=LimitSpec(count=10, kind="top"),
    )
    turn = PlannerTurn(
        route="clarify",
        confidence=0.72,
        clarification=pending_context_from_plan(
            pending_plan,
            question="Compass could not find a governed metric for free and reduced lunch percentage.",
        ),
    )

    repaired = await review_cross_box_planner_turn(turn, resolver)

    assert repaired.route == "execute"
    assert repaired.query_plan is not None
    assert [metric.name for metric in repaired.query_plan.metrics] == [
        "starting teacher salaries"
    ]
    assert repaired.query_plan.sort_steps == [
        SortStepSpec(
            phase="selection",
            field="free and reduced lunch percentage",
            direction="desc",
            key_type="profile_field",
            limit=LimitSpec(count=10, kind="top"),
        )
    ]


@pytest.mark.asyncio
async def test_cross_box_repair_asks_clarification_when_phrase_is_valid_in_multiple_boxes() -> None:
    metric = MetricCandidate(metric_id=301, name="Enrollment policy", answer_type="text")
    repository = FakeCatalogRepository(
        metrics=[metric],
        metric_results_by_query={"enrollment": []},
        aliases=[
            _approved_alias(
                "enrollment",
                entity_type="metric",
                canonical_id="301",
            ),
            _approved_alias(
                "enrollment",
                entity_type="nces_field",
                canonical_id="enrollment",
            ),
        ],
        nces_fields=[
            NCESFieldCandidate(
                field_key="enrollment",
                label="Enrollment",
                data_type="integer",
                description="Total district enrollment.",
            )
        ],
    )
    resolver = CatalogResolver(repository)
    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank districts by enrollment.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="enrollment")],
            sort=SortSpec(field="enrollment", direction="desc"),
            limit=LimitSpec(count=10, kind="top"),
        ),
    )

    repaired = await review_cross_box_planner_turn(turn, resolver)

    assert repaired.route == "clarify"
    assert repaired.clarification is not None
    assert repaired.clarification.missing_fields == ["metric"]
    assert "Enrollment policy" in repaired.clarification.candidates
    assert "Enrollment" in repaired.clarification.candidates


@pytest.mark.asyncio
async def test_catalog_adjudication_does_not_authorize_db_search_candidates_by_itself() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=4321,
                name="Narrative teacher compensation policy",
                answer_type="text",
            )
        ]
    )
    resolver = CatalogResolver(repository, adjudicator=None)

    resolution = await resolver.resolve_primary_metric(
        "teacher compensation",
        numeric_only=False,
    )
    decision = validate_catalog_adjudication_decision(
        CatalogAdjudicationDecision(
            action="select_one",
            selected_ids=["4321"],
            confidence=0.52,
            rationale="The searched candidate is about teacher compensation.",
        ),
        [
            CatalogAdjudicationCandidate(
                entity_type="metric",
                candidate_id="4321",
                label="Narrative teacher compensation policy",
            )
        ],
    )

    assert resolution.ok is False
    assert resolution.resolved is None
    assert decision.selected_ids == ["4321"]
    assert repository.metric_queries == ["teacher compensation"]
    assert not any(query.startswith("ids:") for query in repository.metric_queries)


@pytest.mark.asyncio
async def test_catalog_resolver_selects_largest_districts_as_catalog_authority() -> None:
    repository = FakeCatalogRepository(
        districts=DistrictResolution(
            resolved=[
                DistrictCandidate(district_id=10, district_name="Alpha", state="TX"),
                DistrictCandidate(district_id=11, district_name="Bravo", state="TX"),
            ]
        )
    )
    resolver = CatalogResolver(repository)

    districts = await resolver.select_largest_districts(
        states={"TX"},
        limit=2,
        academic_year="2024 - 2025",
    )

    assert [district.district_id for district in districts] == [10, 11]
    assert repository.largest_district_calls == [({"TX"}, 2, "2024 - 2025")]


@pytest.mark.asyncio
async def test_catalog_resolves_reviewed_metric_bundle_to_ordered_metric_ids() -> None:
    repository = FakeCatalogRepository(
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
            _approved_alias(
                "minimum and maximum annual performance pay bonuses",
                entity_type="metric_bundle",
                canonical_ids=["171", "172"],
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_metric_bundle(
        "minimum and maximum annual performance pay bonuses",
        numeric_only=False,
    )

    assert resolution.ok is True
    assert resolution.bundle_key == "minimum_and_maximum_annual_performance_pay_bonuses"
    assert [metric.metric_id for metric in resolution.resolved] == [171, 172]
    assert repository.metric_queries == [
        "minimum and maximum annual performance pay bonuses",
        "ids:171,172",
    ]


@pytest.mark.asyncio
async def test_catalog_uses_reviewed_metric_alias_before_loose_text_match() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(
                metric_id=1,
                name="Method for evaluation feedback for non-tenured teachers",
                answer_type="text",
            ),
            MetricCandidate(
                metric_id=42,
                name="Frequency of summative evaluation rating for tenured teachers",
                answer_type="text",
            ),
        ],
        aliases=[
            _approved_alias(
                "tenure policy",
                entity_type="metric",
                canonical_id="42",
            )
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_metric_bundle(
        "tenure policy",
        numeric_only=False,
    )

    assert resolution.ok is True
    assert resolution.bundle_key == "tenure_policy"
    assert [metric.metric_id for metric in resolution.resolved] == [42]
    assert repository.metric_queries == [
        "tenure policy",
        "ids:42",
    ]


@pytest.mark.asyncio
async def test_catalog_resolves_reviewed_district_aliases_without_fuzzy_matching() -> None:
    repository = FakeCatalogRepository(
        districts=DistrictResolution(
            resolved=[
                DistrictCandidate(
                    district_id=60,
                    district_name="Chicago Public Schools",
                    state="IL",
                ),
                DistrictCandidate(
                    district_id=26,
                    district_name="Denver Public Schools",
                    state="CO",
                ),
            ]
        ),
        aliases=[
            _approved_alias("Chicago", entity_type="district", canonical_id="60"),
            _approved_alias("Denver", entity_type="district", canonical_id="26"),
        ],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_districts(["Chicago", "Denver"])

    assert resolution.ok is True
    assert [district.district_id for district in resolution.resolved] == [60, 26]
    assert repository.district_queries == [["Chicago", "Denver"]]


@pytest.mark.asyncio
async def test_resolve_district_singular_mirrors_metrics_flow() -> None:
    """`resolve_district(phrase)` is the singular convenience method paralleling `resolve_primary_metric`."""

    boston = DistrictCandidate(district_id=42, district_name="Boston Public Schools", state="MA")
    repository = FakeCatalogRepository(
        districts=DistrictResolution(resolved=[boston]),
        aliases=[_approved_alias("boston public schools", entity_type="district", canonical_id="42")],
    )
    resolver = CatalogResolver(repository)

    resolution = await resolver.resolve_district("Boston Public Schools")

    assert isinstance(resolution, DistrictResolution)
    assert [d.district_id for d in resolution.resolved] == [42]


# ---------------------------------------------------------------------------
# Wave 2 Bucket 7b — Silent-revert regression tests on the SQL alias seed.
#
# These read the actual SQL seed file at test time and assert the doctrine
# state of specific rows. They are intentionally string-level (not behavioral)
# because behavioral tests using FakeCatalogRepository would still pass even
# if someone reverted the seed — the protection here is against silent SQL
# drift back to the pre-7b "approved + BA" state.
# ---------------------------------------------------------------------------

_SEED_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src/compass_data_sync/seed_catalog_aliases.sql"
)

# The 11 broad-salary alias phrases that Wave 2 Bucket 7b flipped from
# approved+BA to ambiguous+NULL. Each MUST contain both BA (89) and MA (96)
# candidates in candidate_refs so the adjudicator can clarify the salary lane.
_BUCKET_7B_BROAD_SALARY_PHRASES = (
    "starting salary / starting pay",
    "starting salaries",
    "starting pay",
    "teacher starting salary",
    "starting teacher salary",
    "starting teacher salaries",
    "starting salary for first-year teachers",
    "first-year teacher salary",
    "beginning teacher pay",
    "beginning teacher salary",
    "first year teacher pay",
)

# Explicit-lane aliases that MUST stay approved + canonical_id matching their
# lane. Wave 2 Bucket 7b only ambiguated BROAD phrases; these explicit
# mentions ("BA salary", "MA salary") are doctrine-correct as approved.
_BUCKET_7B_EXPLICIT_BA_PHRASES = (
    "starting salary for teachers with a bachelor degree",
    "BA starting salary",
    "BA starting salaries",
    "starting teacher salary bachelors degree",
)
_BUCKET_7B_EXPLICIT_MA_PHRASES = (
    "MA starting salary",
)
_STRIKE_LEGALITY_ALIASES = (
    "strike legality",
    "teacher strike legality",
    "legality of teacher strikes",
    "states allow strikes",
    "which states allow strikes",
    "states permit teacher strikes",
)
_M1_REQUIRED_LAUNCH_ALIASES = (
    ("starting salary / starting pay", "'metric', 'ambiguous'"),
    ("benefits", "'metric_bundle', 'approved'"),
    ("employee health insurance premium coverage", "'metric', 'approved'"),
    ("health insurance premiums", "'metric', 'approved'"),
    ("school year length", "'metric', 'ambiguous'"),
)


def _seed_row_for_alias(alias: str) -> str:
    """Return the single seed-file row whose first column is exactly ``alias``."""
    content = _SEED_PATH.read_text()
    lines = [
        line
        for line in content.split("\n")
        if line.lstrip().startswith(f"('{alias}',")
    ]
    assert len(lines) == 1, (
        f"Expected exactly 1 seed row for alias {alias!r}; found {len(lines)}. "
        f"Either the alias was renamed or the seed file structure changed."
    )
    return lines[0]


def test_seed_aliases_broad_salary_phrases_stay_ambiguous() -> None:
    """The 11 broad-salary aliases from Wave 2 Bucket 7b must stay ambiguous.

    Silent-revert protection: if any of these rows flips back to
    ``'approved'`` + ``canonical_id='89'``, broad salary phrases would
    again silently narrow to the BA metric instead of clarifying. That
    violates PLANNER_INSTRUCTIONS ("Do not silently narrow broad salary or
    pay phrases to BA starting salary"). See PR #646 for the original flip.
    """
    for phrase in _BUCKET_7B_BROAD_SALARY_PHRASES:
        row = _seed_row_for_alias(phrase)
        assert ", 'ambiguous', NULL," in row, (
            f"Seed row for {phrase!r} must be ('ambiguous', NULL); silent-revert "
            f"to approved+BA would violate the no-silent-narrowing doctrine. "
            f"Row: {row[:200]!r}"
        )
        assert '"metric_id":89' in row, (
            f"Seed row for {phrase!r} must list BA (metric 89) as a candidate "
            f"so the adjudicator can offer it. Row: {row[:200]!r}"
        )
        assert '"metric_id":96' in row, (
            f"Seed row for {phrase!r} must list MA (metric 96) as a candidate "
            f"so the adjudicator can offer it. Row: {row[:200]!r}"
        )


def test_seed_aliases_explicit_lane_phrases_stay_approved() -> None:
    """Explicit BA/MA alias phrases must stay approved+canonical so the
    resolver picks the right metric deterministically without clarification.

    Wave 2 Bucket 7b only ambiguated BROAD phrases ("starting salary"); it
    intentionally did NOT touch explicit lane mentions ("BA starting salary").
    This test catches the opposite-direction silent revert — accidentally
    ambiguating an explicit-lane phrase would force unnecessary clarification.
    """
    for phrase in _BUCKET_7B_EXPLICIT_BA_PHRASES:
        row = _seed_row_for_alias(phrase)
        assert ", 'approved', '89'," in row, (
            f"Seed row for explicit-BA phrase {phrase!r} must be "
            f"('approved', '89'); accidentally ambiguating an explicit-lane "
            f"alias forces unnecessary clarification. Row: {row[:200]!r}"
        )
    for phrase in _BUCKET_7B_EXPLICIT_MA_PHRASES:
        row = _seed_row_for_alias(phrase)
        assert ", 'approved', '96'," in row, (
            f"Seed row for explicit-MA phrase {phrase!r} must be "
            f"('approved', '96'). Row: {row[:200]!r}"
        )


def test_seed_aliases_authorize_strike_legality_metric() -> None:
    for phrase in _STRIKE_LEGALITY_ALIASES:
        row = _seed_row_for_alias(phrase)
        assert ", 'metric', 'approved', '262'," in row, (
            f"Seed row for {phrase!r} must approve metric 262 for the "
            f"strike-legality follow-up. Row: {row[:220]!r}"
        )
        assert "ARRAY['361']" in row, (
            f"Seed row for {phrase!r} must cite scenario 361 provenance. "
            f"Row: {row[:220]!r}"
        )


def test_seed_aliases_include_required_m1_launch_aliases_and_blockers() -> None:
    for phrase, expected_status in _M1_REQUIRED_LAUNCH_ALIASES:
        row = _seed_row_for_alias(phrase)
        assert expected_status in row, (
            f"Seed row for {phrase!r} must be governed as {expected_status}. "
            f"Row: {row[:260]!r}"
        )
    seed = _SEED_PATH.read_text()
    assert "'union release time'," in seed
    assert "'Do districts provide union release time?'," in seed
    assert "'unsupported_concept'," in seed
    assert "'out_of_universe'," in seed
    assert "union_release_time" in seed
    assert "Compass does not yet have a governed metric" in seed


# ---------------------------------------------------------------------------
# select_with_alternates adjudication outcome
# ---------------------------------------------------------------------------


def test_select_with_alternates_accepts_primary_plus_alternates():
    candidates = (
        CatalogAdjudicationCandidate(
            entity_type="metric", candidate_id="m_ba", label="first-year BA base salary"
        ),
        CatalogAdjudicationCandidate(
            entity_type="metric", candidate_id="m_ma", label="first-year MA base salary"
        ),
        CatalogAdjudicationCandidate(
            entity_type="metric", candidate_id="m_max", label="max base salary, new-hire schedule"
        ),
    )
    decision = CatalogAdjudicationDecision(
        action="select_with_alternates",
        selected_ids=["m_ba"],
        alternate_ids=["m_ma", "m_max"],
        confidence=0.85,
        rationale="bachelor's first-year base salary is the conventional default for bare teacher-pay prompts.",
    )
    validated = validate_catalog_adjudication_decision(decision, candidates)
    assert validated.action == "select_with_alternates"
    assert validated.selected_ids == ["m_ba"]
    assert validated.alternate_ids == ["m_ma", "m_max"]


def test_select_with_alternates_rejects_empty_alternates():
    candidates = (
        CatalogAdjudicationCandidate(entity_type="metric", candidate_id="m_ba", label="ba"),
    )
    decision = CatalogAdjudicationDecision(
        action="select_with_alternates",
        selected_ids=["m_ba"],
        alternate_ids=[],
        confidence=0.9,
        rationale="needs alternates.",
    )
    with pytest.raises(ModelRetry, match="alternate_ids with at least one"):
        validate_catalog_adjudication_decision(decision, candidates)


def test_select_with_alternates_rejects_overlap_between_primary_and_alternates():
    candidates = (
        CatalogAdjudicationCandidate(entity_type="metric", candidate_id="m_ba", label="ba"),
        CatalogAdjudicationCandidate(entity_type="metric", candidate_id="m_ma", label="ma"),
    )
    decision = CatalogAdjudicationDecision(
        action="select_with_alternates",
        selected_ids=["m_ba"],
        alternate_ids=["m_ba", "m_ma"],
        confidence=0.9,
        rationale="overlap forbidden.",
    )
    with pytest.raises(ModelRetry, match="overlap|disjoint"):
        validate_catalog_adjudication_decision(decision, candidates)


def test_alternate_ids_rejected_when_action_is_not_select_with_alternates():
    candidates = (
        CatalogAdjudicationCandidate(entity_type="metric", candidate_id="m_ba", label="ba"),
        CatalogAdjudicationCandidate(entity_type="metric", candidate_id="m_ma", label="ma"),
    )
    decision = CatalogAdjudicationDecision(
        action="select_one",
        selected_ids=["m_ba"],
        alternate_ids=["m_ma"],
        confidence=0.95,
        rationale="alternate_ids must be empty for select_one.",
    )
    with pytest.raises(ModelRetry, match="alternate_ids may only be populated"):
        validate_catalog_adjudication_decision(decision, candidates)


# ---------------------------------------------------------------------------
# MetricBundleResolution carries alternate_candidates for select_with_alternates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metric_bundle_resolution_carries_alternates_on_select_with_alternates() -> None:
    repository = FakeCatalogRepository(
        metrics=[
            MetricCandidate(metric_id=10, name="first-year teacher base salary, bachelor's"),
            MetricCandidate(metric_id=11, name="first-year teacher base salary, master's"),
            MetricCandidate(metric_id=12, name="max teacher base salary, new-hire schedule"),
        ]
    )
    resolver = CatalogResolver(
        repository,
        adjudicator=_catalog_adjudicator(
            CatalogAdjudicationDecision(
                action="select_with_alternates",
                selected_ids=["10"],
                alternate_ids=["11", "12"],
                confidence=0.85,
                rationale="first-year BA base salary is the conventional default.",
            )
        ),
    )

    result = await resolver.resolve_metric_bundle("teacher salary")

    assert result.ambiguous is False
    assert [m.metric_id for m in result.resolved] == [10]
    assert [m.metric_id for m in result.alternate_candidates] == [11, 12]


@pytest.mark.asyncio
async def test_metric_bundle_resolution_carries_clarification_hint_on_clarify() -> None:
    """When the catalog adjudicator emits action='clarify', the resolver
    threads the adjudicator's clarification_question into
    MetricBundleResolution.clarification_hint."""

    candidates = [
        MetricCandidate(metric_id=10, name="Principal base salary"),
        MetricCandidate(metric_id=11, name="Teacher-leader stipend"),
    ]

    class _FakeAdjudicator:
        async def run(self, prompt, *, deps):
            class _Result:
                output = CatalogAdjudicationDecision(
                    action="clarify",
                    selected_ids=[],
                    clarification_question="Admin pay or teacher-leader pay?",
                    confidence=0.6,
                    rationale="Materially different concepts.",
                )

            return _Result()

    repository = FakeCatalogRepository(metrics=candidates)
    resolver = CatalogResolver(
        repository=repository,
        adjudicator=_FakeAdjudicator(),
    )
    result = await resolver.resolve_metric_bundle("principal pay")
    assert result.ambiguous is True
    assert result.resolved == []
    assert result.clarification_hint == "Admin pay or teacher-leader pay?"


@pytest.mark.asyncio
async def test_metric_resolution_inherits_clarification_hint_from_bundle() -> None:
    """resolve_primary_metric propagates clarification_hint from the
    underlying MetricBundleResolution into MetricResolution."""

    candidates = [
        MetricCandidate(metric_id=10, name="Principal base salary"),
        MetricCandidate(metric_id=11, name="Teacher-leader stipend"),
    ]

    class _FakeAdjudicator:
        async def run(self, prompt, *, deps):
            class _Result:
                output = CatalogAdjudicationDecision(
                    action="clarify",
                    selected_ids=[],
                    clarification_question="Admin or teacher-leader?",
                    confidence=0.6,
                    rationale="ambiguous.",
                )

            return _Result()

    repository = FakeCatalogRepository(metrics=candidates)
    resolver = CatalogResolver(
        repository=repository,
        adjudicator=_FakeAdjudicator(),
    )
    result = await resolver.resolve_primary_metric("principal pay")
    assert result.ambiguous is True
    assert result.clarification_hint == "Admin or teacher-leader?"
