from __future__ import annotations

import pytest

from compass_backend.catalog import (
    CatalogAliasRecord,
    CatalogRecallService,
    DistrictCandidate,
    DistrictResolution,
    GlossaryTermCandidate,
    MetricCandidate,
    NCESFieldCandidate,
    SourceDocumentCandidate,
    TopicCandidate,
    normalize_district_name_for_resolution,
)


class FakeRecallRepository:
    def __init__(self) -> None:
        self.aliases = [
            CatalogAliasRecord(
                alias="Philadelphia",
                normalized_alias=normalize_district_name_for_resolution(
                    "Philadelphia"
                ),
                entity_type="district",
                resolution_status="approved",
                canonical_id="133",
                source="test",
                provenance="unit test",
                scenario_ids=["golden-22"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="benefits information",
                normalized_alias=normalize_district_name_for_resolution(
                    "benefits information"
                ),
                entity_type="metric_bundle",
                resolution_status="approved",
                canonical_ids=["232", "233", "234", "235"],
                source="test",
                provenance="unit test",
                scenario_ids=["golden-22"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="Starting salary",
                normalized_alias=normalize_district_name_for_resolution(
                    "Starting salary"
                ),
                entity_type="metric",
                resolution_status="approved",
                canonical_id="89",
                source="test",
                provenance="unit test",
                scenario_ids=["golden-24"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="Starting pay",
                normalized_alias=normalize_district_name_for_resolution("Starting pay"),
                entity_type="metric",
                resolution_status="approved",
                canonical_id="89",
                source="test",
                provenance="unit test",
                scenario_ids=["golden-24"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="School year length",
                normalized_alias=normalize_district_name_for_resolution(
                    "School year length"
                ),
                entity_type="metric",
                resolution_status="ambiguous",
                candidate_refs=[
                    {
                        "metric_id": 69,
                        "metric_name": "Total contracted workdays per academic year",
                    },
                    {
                        "metric_id": 70,
                        "metric_name": (
                            "Contracted student-teacher contact days per academic "
                            "year in elementary school"
                        ),
                    },
                ],
                source="test",
                provenance="unit test",
                scenario_ids=["golden-24"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="Sick leave policy",
                normalized_alias=normalize_district_name_for_resolution(
                    "Sick leave policy"
                ),
                entity_type="metric_bundle",
                resolution_status="approved",
                canonical_ids=["198", "201", "202"],
                source="test",
                provenance="unit test",
                scenario_ids=["m1-rescue"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="special ed stipend",
                normalized_alias=normalize_district_name_for_resolution(
                    "special ed stipend"
                ),
                entity_type="metric",
                resolution_status="approved",
                canonical_id="182",
                source="test",
                provenance="unit test",
                scenario_ids=["m1-recall"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="ELL stipend",
                normalized_alias=normalize_district_name_for_resolution(
                    "ELL stipend"
                ),
                entity_type="metric",
                resolution_status="approved",
                canonical_id="184",
                source="test",
                provenance="unit test",
                scenario_ids=["m1-recall"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="health insurance premiums",
                normalized_alias=normalize_district_name_for_resolution(
                    "health insurance premiums"
                ),
                entity_type="metric",
                resolution_status="approved",
                canonical_id="233",
                source="test",
                provenance="unit test",
                scenario_ids=["m1-recall"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="formal observations",
                normalized_alias=normalize_district_name_for_resolution(
                    "formal observations"
                ),
                entity_type="metric",
                resolution_status="ambiguous",
                candidate_refs=[
                    {
                        "metric_id": 39,
                        "metric_name": (
                            "Minimum number of formal observations per "
                            "evaluation cycle for non-tenured teachers"
                        ),
                    }
                ],
                source="test",
                provenance="unit test",
                scenario_ids=["m1-recall"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="teacher strikes",
                normalized_alias=normalize_district_name_for_resolution(
                    "teacher strikes"
                ),
                entity_type="metric",
                resolution_status="approved",
                canonical_id="262",
                source="test",
                provenance="unit test",
                scenario_ids=["m1-recall"],
                review_status="approved",
            ),
            CatalogAliasRecord(
                alias="union release time",
                normalized_alias=normalize_district_name_for_resolution(
                    "union release time"
                ),
                entity_type="unsupported_concept",
                resolution_status="out_of_universe",
                canonical_id="union_release_time",
                metadata={
                    "message": (
                        "Compass does not yet have a governed metric for "
                        "union release time."
                    )
                },
                source="test",
                provenance="unit test",
                scenario_ids=["m1-rescue"],
                review_status="approved",
            ),
        ]
        self.metrics = [
            MetricCandidate(
                metric_id=89,
                name="Starting salary",
                answer_type="numeric",
                topic="Compensation",
            ),
            MetricCandidate(
                metric_id=69,
                name="Total contracted workdays per academic year",
                answer_type="numeric",
                topic="Teacher time",
            ),
            MetricCandidate(
                metric_id=96,
                name=(
                    "Annual base salary for a first year teacher with a "
                    "master's degree"
                ),
                answer_type="numeric",
                topic="Compensation",
            ),
            MetricCandidate(
                metric_id=182,
                name="Minimum amount of additional annual pay for special education teachers",
                answer_type="numeric",
                topic="Differentiated compensation",
            ),
            MetricCandidate(
                metric_id=184,
                name="Minimum amount of additional annual pay for English learner teachers",
                answer_type="numeric",
                topic="Differentiated compensation",
            ),
            MetricCandidate(
                metric_id=233,
                name="Health insurance premium contribution for employees",
                answer_type="numeric",
                topic="Benefits",
            ),
            MetricCandidate(
                metric_id=262,
                name="Legality of teacher strikes",
                answer_type="categorical",
                topic="Collective bargaining",
            ),
        ]
        self.districts = [
            DistrictCandidate(
                district_id=1,
                district_name="Denver Public Schools",
                state="CO",
                city="Denver",
                match_reason="name",
            )
        ]
        self.profile_fields = [
            NCESFieldCandidate(
                field_key="frpl_pct",
                label="FRPL %",
                data_type="numeric",
                description="Free and reduced-price lunch share.",
            )
        ]
        self.topics = [
            TopicCandidate(
                topic_id=5,
                topic_name="Benefits",
                subtopic_id=8,
                subtopic_name="Health insurance",
                question_count=3,
            )
        ]
        self.glossary_terms = [
            GlossaryTermCandidate(
                term_id="pathfinder",
                term="District Policy Pathfinder",
                definition="The covered Compass district universe.",
            )
        ]
        self.source_documents = [
            SourceDocumentCandidate(
                source_id=10,
                title="Collective bargaining agreement",
                district_id=1,
                document_type="contract",
                academic_year="2024 - 2025",
            )
        ]

    async def search_metrics(self, query: str, *, limit: int = 5):
        normalized = query.casefold()
        if normalized == "starting salary":
            return [metric for metric in self.metrics if metric.metric_id == 89][:limit]
        if "days teachers work" in normalized or "workdays" in normalized:
            return [metric for metric in self.metrics if metric.metric_id == 69][:limit]
        if "master" in normalized and "degree" in normalized:
            return [metric for metric in self.metrics if metric.metric_id == 96][:limit]
        if "teacher strikes" in normalized:
            return [metric for metric in self.metrics if metric.metric_id == 262][:limit]
        return self.metrics[:limit] if "salary" in normalized else []

    async def fetch_metrics_by_ids(self, metric_ids: list[int]):
        return [metric for metric in self.metrics if metric.metric_id in metric_ids]

    async def search_catalog_aliases(self, alias: str, *, entity_types: set[str]):
        normalized = normalize_district_name_for_resolution(alias)
        return [
            row
            for row in self.aliases
            if row.entity_type in entity_types and row.normalized_alias == normalized
        ]

    async def fetch_recall_aliases(self, entity_types: set[str]):
        return [
            row
            for row in self.aliases
            if row.entity_type in entity_types
            and row.active
            and row.review_status == "approved"
        ]

    async def fetch_renderer_notes(self, note_keys: list[str]):
        return []

    async def resolve_districts(self, names: list[str], *, states: set[str] | None = None):
        return DistrictResolution()

    async def list_covered_districts(self, *, states: set[str] | None = None):
        return self.districts

    async def search_district_candidates(
        self,
        query: str,
        *,
        states: set[str] | None = None,
        limit: int = 8,
    ):
        return self.districts[:limit] if "denver" in query.casefold() else []

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 5,
        academic_year: str,
    ):
        return self.districts[:limit]

    async def select_districts_by_enrollment_range(
        self,
        *,
        states: set[str] | None = None,
        min_enrollment: int | None = None,
        max_enrollment: int | None = None,
        academic_year: str,
    ):
        return self.districts

    async def rank_districts_by_profile_field(
        self,
        field_key: str,
        *,
        limit: int | None = None,
        direction: str = "desc",
        states: set[str] | None = None,
        academic_year: str,
    ):
        return []

    async def lookup_nces_profile_values(
        self,
        district_names: list[str],
        *,
        field_key: str,
        states: set[str] | None = None,
        academic_year: str,
    ):
        return []

    async def list_covered_nces_profiles(self, *, academic_year: str):
        return []

    async def search_source_documents(
        self,
        query: str,
        *,
        district_ids: set[int] | None = None,
        limit: int = 5,
    ):
        return (
            self.source_documents[:limit] if "contract" in query.casefold() else []
        )

    async def search_topics(self, query: str, *, limit: int = 5):
        return self.topics[:limit] if "health" in query.casefold() else []

    async def fetch_topic_metric_candidates(
        self,
        topics: list[TopicCandidate],
        *,
        limit: int = 10,
    ):
        return []

    async def fetch_topic_content_links(
        self,
        topics: list[TopicCandidate],
        *,
        limit: int = 10,
    ):
        return []

    async def search_glossary_terms(self, query: str, *, limit: int = 5):
        return (
            self.glossary_terms[:limit] if "pathfinder" in query.casefold() else []
        )

    async def search_nces_fields(self, query: str, *, limit: int = 5):
        normalized = query.casefold()
        if "frpl" in normalized or (
            "free" in normalized and "reduced" in normalized and "lunch" in normalized
        ):
            return self.profile_fields[:limit]
        return []


class StateSuffixFakeRepository(FakeRecallRepository):
    """Fake with same-name districts in two states; honors the states filter.

    Mirrors the real repository contract (``db/catalog.py
    search_district_candidates``): a non-empty ``states`` set restricts rows to
    those states. Records every district search call so tests can assert which
    query string and state filter actually reached the repository (#1513).
    """

    def __init__(self) -> None:
        super().__init__()
        self.districts = [
            DistrictCandidate(
                district_id=4101,
                district_name="Portland Public Schools",
                state="OR",
                city="Portland",
                match_reason="name",
            ),
            DistrictCandidate(
                district_id=2301,
                district_name="Portland Public Schools",
                state="ME",
                city="Portland",
                match_reason="name",
            ),
            DistrictCandidate(
                district_id=3601,
                district_name="Port Washington School District",
                state="NY",
                city="Port Washington",
                match_reason="name",
            ),
        ]
        self.district_search_calls: list[tuple[str, set[str] | None]] = []

    async def search_district_candidates(
        self,
        query: str,
        *,
        states: set[str] | None = None,
        limit: int = 8,
    ):
        self.district_search_calls.append(
            (query, set(states) if states is not None else None)
        )
        normalized = query.casefold()
        matches = [
            district
            for district in self.districts
            if normalized in district.district_name.casefold()
            or district.district_name.casefold() in normalized
        ]
        if states:
            matches = [
                district for district in matches if district.state in states
            ]
        return matches[:limit]


def _district_refs(cards) -> set[str]:
    return {
        card.entity_ref
        for card in cards
        if card.entity_type == "district" and card.entity_ref
    }


@pytest.mark.asyncio
async def test_recall_state_suffix_narrows_district_candidates() -> None:
    """'Name, ST' — the canonical handle Compass renders — narrows to ST (#1513)."""

    repository = StateSuffixFakeRepository()
    service = CatalogRecallService(repository)

    report = await service.recall(
        "Portland Public Schools, OR",
        entity_types={"district"},
    )

    assert _district_refs(report.candidates) == {"district:4101"}
    for batch in report.batches:
        assert "district:2301" not in _district_refs(batch.candidates)
    # Every district lookup (direct + ngram fanout) carried the suffix state.
    assert repository.district_search_calls
    for _, states in repository.district_search_calls:
        assert states == {"OR"}

    # Ranking decision: the direct batch's district card ranks against the
    # remainder — the string the repository actually matched — not the
    # suffixed phrase.
    direct_batch = report.batches[0]
    direct_card = next(
        card for card in direct_batch.candidates if card.entity_type == "district"
    )
    assert direct_card.input_phrase == "Portland Public Schools"
    assert direct_card.metadata["matched_query"] == "Portland Public Schools"
    assert direct_card.metadata["origin_query"] == "Portland Public Schools, OR"


@pytest.mark.asyncio
async def test_recall_state_suffix_whitespace_form_narrows_district_candidates() -> None:
    repository = StateSuffixFakeRepository()
    service = CatalogRecallService(repository)

    report = await service.recall(
        "Portland Public Schools OR",
        entity_types={"district"},
    )

    assert _district_refs(report.candidates) == {"district:4101"}
    for batch in report.batches:
        assert "district:2301" not in _district_refs(batch.candidates)


@pytest.mark.asyncio
async def test_recall_bare_district_name_keeps_all_state_candidates() -> None:
    repository = StateSuffixFakeRepository()
    service = CatalogRecallService(repository)

    report = await service.recall(
        "Portland Public Schools",
        entity_types={"district"},
    )

    assert _district_refs(report.candidates) == {
        "district:4101",
        "district:2301",
    }
    # No suffix split → no state filter was injected anywhere.
    for _, states in repository.district_search_calls:
        assert states is None


@pytest.mark.asyncio
async def test_recall_false_state_split_falls_back_to_raw_phrase() -> None:
    """'Port Washington' splits to 'Port' + WA, finds nothing, and recovers."""

    repository = StateSuffixFakeRepository()
    service = CatalogRecallService(repository)

    report = await service.recall(
        "Port Washington",
        entity_types={"district"},
    )

    # The zero-hit probe ran under the split, then the raw phrase recovered
    # the real candidate — nothing was lost to the false split.
    assert repository.district_search_calls[0] == ("Port", {"WA"})
    assert ("Port Washington", None) in repository.district_search_calls
    assert _district_refs(report.candidates) == {"district:3601"}


@pytest.mark.asyncio
async def test_recall_bare_state_and_metric_only_queries_unaffected() -> None:
    # A bare state is never split (split_state_suffix refuses it): the raw
    # query reaches the repository with the caller's (absent) state filter.
    repository = StateSuffixFakeRepository()
    service = CatalogRecallService(repository)
    await service.recall("OR", entity_types={"district"})
    assert repository.district_search_calls == [("OR", None)]

    # Metric-only recall never touches the district lookup at all.
    repository = StateSuffixFakeRepository()
    service = CatalogRecallService(repository)
    report = await service.recall("Starting salary", entity_types={"metric"})
    assert repository.district_search_calls == []
    assert [card.label for card in report.candidates] == ["Starting salary"]


@pytest.mark.asyncio
async def test_recall_caller_states_compose_union_with_suffix_state() -> None:
    repository = StateSuffixFakeRepository()
    service = CatalogRecallService(repository)

    report = await service.recall(
        "Portland Public Schools, OR",
        entity_types={"district"},
        states={"ME"},
    )

    # Caller filter and suffix state union: both Portlands stay reachable.
    assert repository.district_search_calls[0] == (
        "Portland Public Schools",
        {"ME", "OR"},
    )
    assert _district_refs(report.candidates) == {
        "district:4101",
        "district:2301",
    }


def test_split_state_suffix_authority_is_reference_states() -> None:
    """The execution module re-exports the one authority in reference.states."""

    from compass_backend.execution.referent_resolution import (
        split_state_suffix as execution_split,
    )
    from compass_backend.reference import split_state_suffix as reference_split

    assert execution_split is reference_split


def test_split_state_suffix_two_token_state_beats_trailing_token() -> None:
    """Multi-word state names are tried before the single trailing token.

    West Virginia is the one US state whose final token alone also names a
    state: "Charleston West Virginia" must anchor Charleston in WV, never
    "Charleston West" in VA. A bare multi-word state name has no district
    name left to anchor on and is returned unchanged, like a bare
    abbreviation.
    """

    from compass_backend.reference import split_state_suffix

    assert split_state_suffix("Charleston West Virginia") == ("Charleston", {"WV"})
    assert split_state_suffix("Charleston, West Virginia") == ("Charleston", {"WV"})
    assert split_state_suffix("West Virginia") == ("West Virginia", set())
    assert split_state_suffix("Buffalo New York") == ("Buffalo", {"NY"})


@pytest.mark.asyncio
async def test_recall_merges_alias_and_search_without_execution_authority() -> None:
    service = CatalogRecallService(FakeRecallRepository())

    report = await service.recall(
        "Starting salary",
        entity_types={"metric"},
        limit=5,
    )

    assert len(report.batches) == 1
    assert [card.label for card in report.candidates] == ["Starting salary"]
    assert report.candidates[0].source_methods == ["alias", "metric_search"]
    assert report.candidates[0].entity_ref == "metric:89"
    assert report.candidates[0].debug_ref == "alias_metric:starting salary:89"

    model_context = report.candidates[0].to_model_context()
    assert "entity_ref" not in model_context
    assert "debug_ref" not in model_context
    assert "metadata" not in model_context
    assert "89" not in str(model_context)


@pytest.mark.asyncio
async def test_recall_hydrates_metric_alias_to_official_candidate_card() -> None:
    service = CatalogRecallService(FakeRecallRepository())

    report = await service.recall(
        "Starting pay",
        entity_types={"metric"},
        limit=5,
    )

    assert [card.label for card in report.candidates] == ["Starting salary"]
    assert report.candidates[0].source_methods == ["alias"]

    model_context = report.candidates[0].to_model_context()
    assert model_context["input_phrase"] == "Starting pay"
    assert model_context["label"] == "Starting salary"
    assert "entity_ref" not in model_context
    assert "89" not in str(model_context)


@pytest.mark.asyncio
async def test_recall_scores_ambiguous_metric_alias_candidate_refs() -> None:
    service = CatalogRecallService(FakeRecallRepository())

    report = await service.recall(
        "School year length",
        entity_types={"metric"},
        limit=5,
    )

    candidate_refs = {card.entity_ref for card in report.candidates}
    assert "metric:69" in candidate_refs
    assert "metric:70" in candidate_refs


@pytest.mark.asyncio
async def test_recall_scores_metric_bundle_without_model_facing_ids() -> None:
    service = CatalogRecallService(FakeRecallRepository())

    report = await service.recall(
        "Sick leave policy",
        entity_types={"metric_bundle"},
        limit=5,
    )

    assert [card.entity_ref for card in report.candidates] == [
        "metric_bundle:198,201,202"
    ]
    model_context = report.candidates[0].to_model_context()
    assert "entity_ref" not in model_context
    assert "198" not in str(model_context)


@pytest.mark.asyncio
async def test_recall_returns_multiple_official_entity_types() -> None:
    service = CatalogRecallService(FakeRecallRepository())

    report = await service.recall(
        "Denver FRPL health Pathfinder contract",
        entity_types={
            "district",
            "profile_field",
            "topic",
            "glossary_term",
            "source_document",
        },
    )

    entity_labels = {(card.entity_type, card.label) for card in report.candidates}
    assert ("district", "Denver Public Schools") in entity_labels
    assert ("profile_field", "FRPL %") in entity_labels
    assert ("topic", "Health insurance") in entity_labels
    assert ("glossary_term", "District Policy Pathfinder") in entity_labels
    assert ("source_document", "Collective bargaining agreement") in entity_labels


@pytest.mark.asyncio
async def test_recall_can_surface_unsupported_concept_card() -> None:
    service = CatalogRecallService(FakeRecallRepository())

    report = await service.recall(
        "union release time",
        entity_types={"unsupported_concept"},
    )

    assert len(report.candidates) == 1
    card = report.candidates[0]
    assert card.entity_type == "unsupported_concept"
    assert card.entity_ref == "unsupported_concept:union_release_time"
    assert card.source_methods == ["alias", "unsupported_concept"]
    assert "does not yet have" in card.plain_definition


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "entity_types", "expected_refs"),
    [
        (
            "I am from Philadelphia, show me ten similar districts and their benefits information.",
            {"district", "metric_bundle"},
            {"district:133", "metric_bundle:232,233,234,235"},
        ),
        (
            "Show me the 10 highest starting salaries and how many days teachers work in those districts.",
            {"metric"},
            {"metric:89", "metric:69"},
        ),
        (
            "Compare starting salaries for teachers with a master's degree in the 20 largest districts.",
            {"metric"},
            {"metric:96"},
        ),
        (
            "Which districts pay additional stipends for both special ed and ELL teachers?",
            {"metric"},
            {"metric:182", "metric:184"},
        ),
        (
            "Show me starting teacher salaries for districts with the highest free-and-reduced lunch share.",
            {"profile_field", "metric"},
            {"profile_field:frpl_pct", "metric:89"},
        ),
        (
            "Which districts have more than 190 teacher workdays?",
            {"metric"},
            {"metric:69"},
        ),
        (
            "Which districts cover the most of teachers health insurance premiums?",
            {"metric"},
            {"metric:233"},
        ),
        (
            "Which districts require teachers to be formally observed more than a couple times a year?",
            {"metric"},
            {"metric:39"},
        ),
        (
            "Compare sick leave policy for similar districts.",
            {"metric_bundle"},
            {"metric_bundle:198,201,202"},
        ),
        (
            "Which states allow teacher strikes?",
            {"metric"},
            {"metric:262"},
        ),
        (
            "Do districts provide union release time?",
            {"unsupported_concept"},
            {"unsupported_concept:union_release_time"},
        ),
    ],
)
async def test_full_prompt_recall_fanout_surfaces_m1_expected_candidates(
    prompt: str,
    entity_types: set[str],
    expected_refs: set[str],
) -> None:
    service = CatalogRecallService(FakeRecallRepository())

    report = await service.recall(
        prompt,
        entity_types=entity_types,
        limit=25,
    )

    candidate_refs = {card.entity_ref for card in report.candidates[:25]}
    assert expected_refs <= candidate_refs
    assert any(batch.source == "direct" for batch in report.batches)
    assert any(
        batch.source in {"ngram", "catalog_alias_overlap"}
        for batch in report.batches
    )
    for expected_ref in expected_refs:
        matched = next(card for card in report.candidates if card.entity_ref == expected_ref)
        assert matched.metadata["origin_query"] == prompt
        assert matched.metadata["matched_query"]
        assert matched.metadata["batch_source"] in {
            "direct",
            "ngram",
            "catalog_alias_overlap",
        }
