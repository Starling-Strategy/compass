"""Tests for catalog-owned resolver metadata surfaces."""

from __future__ import annotations

from types import SimpleNamespace

import logfire.testing
import pytest
from pydantic import SecretStr
from pydantic_ai import ModelRetry

from compass_backend.catalog.adjudication import CatalogAdjudicationDecision
from compass_backend.catalog import (
    CatalogAliasRecord,
    CatalogResolver,
    DistrictCandidate,
    DistrictResolution,
    MetricCandidate,
    TopicCandidate,
)
from compass_backend.config import settings as runtime_settings
from compass_backend.contracts import ChatResponse
from compass_backend.contracts.planning import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.execution import DeterministicQueryExecutor
from compass_backend.execution.types import MetricAnswerRow


class ResolutionReportingRepository:
    """Fake catalog/answer repository for resolver-report tests."""

    async def resolve_districts(
        self,
        names: list[str],
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        return DistrictResolution(unresolved=names)

    async def list_covered_districts(
        self,
        *,
        states: set[str] | None = None,
    ) -> list[DistrictCandidate]:
        return [
            DistrictCandidate(
                district_id=150,
                district_name="Dallas Independent School District",
                state="TX",
            )
        ]

    async def search_catalog_aliases(
        self,
        alias: str,
        *,
        entity_types: set[str],
    ) -> list[CatalogAliasRecord]:
        if alias.casefold() == "dallas isd" and "district" in entity_types:
            return [
                CatalogAliasRecord(
                    alias="Dallas ISD",
                    normalized_alias="dallas isd",
                    entity_type="district",
                    resolution_status="approved",
                    canonical_id="150",
                    source="test",
                    provenance="unit test",
                    review_status="approved",
                )
            ]
        if alias.casefold() == "teacher pay" and "metric_bundle" in entity_types:
            return [
                CatalogAliasRecord(
                    alias="teacher pay",
                    normalized_alias="teacher pay",
                    entity_type="metric_bundle",
                    resolution_status="ambiguous",
                    candidate_refs=[
                        {"metric_id": 89, "metric_name": "Starting salary"},
                        {"metric_id": 96, "metric_name": "Master's starting salary"},
                    ],
                    source="test",
                    provenance="unit test",
                    review_status="approved",
                )
            ]
        if (
            alias.casefold() == "union release time"
            and "unsupported_concept" in entity_types
        ):
            return [
                CatalogAliasRecord(
                    alias="union release time",
                    normalized_alias="union release time",
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
                    review_status="approved",
                )
            ]
        return []

    async def search_metrics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MetricCandidate]:
        if query.casefold() == "number of formal observations per year":
            return [
                MetricCandidate(
                    metric_id=44,
                    name="Minimum number of formal observations for tenured teachers",
                    answer_type="numeric",
                )
            ]
        if query.casefold() == (
            "minimum number of formal observations per evaluation cycle "
            "for non-tenured teachers"
        ):
            return [
                MetricCandidate(
                    metric_id=39,
                    name=query,
                    answer_type="numeric",
                )
            ]
        if query.casefold() == "starting salary":
            return [
                MetricCandidate(
                    metric_id=89,
                    name="Starting salary",
                    answer_type="numeric",
                )
            ]
        if query.casefold() == "teacher pay":
            return [
                MetricCandidate(metric_id=89, name="Starting salary"),
                MetricCandidate(metric_id=96, name="Master's starting salary"),
            ]
        return []

    async def search_topics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[TopicCandidate]:
        if "formal observation" in query.casefold():
            return [
                TopicCandidate(
                    topic_id=7,
                    topic_name="Teacher Evaluation",
                    subtopic_id=17,
                    subtopic_name="Observation",
                    question_count=4,
                )
            ]
        return []

    async def fetch_topic_metric_candidates(
        self,
        topics: list[TopicCandidate],
        *,
        limit: int = 10,
    ) -> list[MetricCandidate]:
        return [
            MetricCandidate(
                metric_id=39,
                name=(
                    "Minimum number of formal observations per evaluation cycle "
                    "for non-tenured teachers"
                ),
                answer_type="numeric",
                topic="Teacher Evaluation",
            )
        ][:limit]

    async def fetch_metrics_by_ids(
        self,
        metric_ids: list[int],
    ) -> list[MetricCandidate]:
        candidates = {
            89: MetricCandidate(metric_id=89, name="Starting salary"),
            96: MetricCandidate(metric_id=96, name="Master's starting salary"),
        }
        return [candidates[metric_id] for metric_id in metric_ids]

    async def fetch_metric_answer_rows(
        self,
        *,
        metric_id: int,
        academic_year: str,
    ) -> list[MetricAnswerRow]:
        return [
            MetricAnswerRow(
                answer_id=1,
                district_id=150,
                district_name="Dallas Independent School District",
                state="TX",
                metric_id=metric_id,
                metric_name="Starting salary",
                value=50000,
                academic_year=academic_year,
            )
        ]

    async def fetch_reviewed_district_ids(
        self,
        *,
        academic_year: str,
        district_ids: set[int],
    ) -> set[int]:
        return set(district_ids)

    async def fetch_recent_metric_answer_rows(
        self,
        *,
        metric_id: int,
        before_academic_year: str,
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        return []


class SelectMetricAdjudicator:
    """Select one metric candidate by ID for reporting tests."""

    def __init__(self, metric_id: int) -> None:
        self.metric_id = metric_id

    async def run(self, user_prompt: str, **kwargs):
        return SimpleNamespace(
            output=CatalogAdjudicationDecision(
                action="select_one",
                selected_ids=[str(self.metric_id)],
                confidence=0.8,
                rationale="unit test selected metric",
            )
        )


@pytest.mark.asyncio
async def test_resolution_report_records_approved_district_alias() -> None:
    repository = ResolutionReportingRepository()
    executor = DeterministicQueryExecutor(
        repository,
        catalog_resolver=CatalogResolver(repository, adjudicator=None),
    )

    outcome = await executor.execute(
        QueryPlan(
            question="Compare Dallas ISD starting salary.",
            operation="lookup",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Dallas ISD"],
            ),
            metrics=[MetricSpec(name="starting salary")],
        )
    )

    assert outcome.resolution_report is not None
    district_entities = [
        entity
        for entity in outcome.resolution_report.entities
        if entity.entity_type == "district"
    ]
    assert district_entities[0].status == "approved"
    assert district_entities[0].approved_ids == ["150"]


@pytest.mark.asyncio
async def test_resolution_report_records_ambiguous_metric_phrase() -> None:
    repository = ResolutionReportingRepository()
    executor = DeterministicQueryExecutor(
        repository,
        catalog_resolver=CatalogResolver(repository, adjudicator=None),
    )

    outcome = await executor.execute(
        QueryPlan(
            question="Compare teacher pay.",
            operation="lookup",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="teacher pay")],
        )
    )

    assert outcome.resolution_report is not None
    entity = outcome.resolution_report.entities[0]
    assert entity.entity_type == "metric_bundle"
    assert entity.status == "ambiguous"
    assert [candidate.candidate_id for candidate in entity.candidates] == ["89", "96"]


@pytest.mark.asyncio
async def test_resolution_report_records_candidate_fusion_metadata() -> None:
    repository = ResolutionReportingRepository()
    executor = DeterministicQueryExecutor(
        repository,
        catalog_resolver=CatalogResolver(
            repository,
            adjudicator=None,
            candidate_fusion_enabled=True,
        ),
    )

    outcome = await executor.execute(
        QueryPlan(
            question="Compare formal observations for non-tenured teachers.",
            operation="lookup",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[
                MetricSpec(
                    name=(
                        "number of formal observations per year "
                        "for non-tenured teachers"
                    )
                )
            ],
        )
    )

    assert outcome.resolution_report is not None
    entity = outcome.resolution_report.entities[0]
    assert entity.candidates
    assert entity.candidates[0].metadata["candidate_fusion"]["sources"]
    assert "resolution_report" not in ChatResponse.model_fields


@pytest.mark.asyncio
async def test_resolution_report_records_topic_frame_without_public_shape_change() -> None:
    repository = ResolutionReportingRepository()
    executor = DeterministicQueryExecutor(
        repository,
        catalog_resolver=CatalogResolver(
            repository,
            adjudicator=SelectMetricAdjudicator(39),
            topic_narrowing_enabled=True,
        ),
    )

    outcome = await executor.execute(
        QueryPlan(
            question="Compare formal observations for non-tenured teachers.",
            operation="lookup",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[
                MetricSpec(
                    name=(
                        "number of formal observations per year "
                        "for non-tenured teachers"
                    )
                )
            ],
        )
    )

    assert outcome.resolution_report is not None
    assert outcome.result is not None
    topic_entities = [
        entity
        for entity in outcome.resolution_report.entities
        if entity.entity_type == "topic"
    ]
    assert topic_entities[0].approved_ids == ["topic:7", "subtopic:17"]
    assert topic_entities[0].resolution_method == "topic_narrowing"
    assert "resolution_report" not in ChatResponse.model_fields


@pytest.mark.asyncio
async def test_unsupported_concept_blocks_before_prompt_only_answer() -> None:
    repository = ResolutionReportingRepository()
    executor = DeterministicQueryExecutor(
        repository,
        catalog_resolver=CatalogResolver(repository, adjudicator=None),
    )

    outcome = await executor.execute(
        QueryPlan(
            question="Compare union release time.",
            operation="lookup",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="union release time")],
        )
    )

    assert outcome.result is None
    assert outcome.clarification is not None
    assert outcome.resolution_report is not None
    assert outcome.resolution_report.summary_status() == "unsupported"
    assert "union release time" in outcome.message


def test_chat_response_does_not_gain_public_resolution_field() -> None:
    assert "resolution_report" not in ChatResponse.model_fields
    assert "recognition" not in ChatResponse.model_fields


# ---------------------------------------------------------------------------
# compass.catalog.adjudicate span coverage (Tier 2.3 audit)
# ---------------------------------------------------------------------------


class _RaisingAdjudicator:
    """Adjudicator that raises ModelRetry to exercise the error outcome."""

    async def run(self, user_prompt: str, **kwargs):
        raise ModelRetry("forced retry for span coverage test")


def _enable_logfire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force settings.logfire_enabled True so compass_span emits real spans."""
    monkeypatch.setattr(
        runtime_settings,
        "logfire_token",
        SecretStr("pylf_v1_us_real_token_for_catalog_test"),
    )


@pytest.mark.asyncio
async def test_catalog_adjudicate_span_records_select_one_outcome(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compass.catalog.adjudicate carries outcome=select_one + selected_ids on a clean pick."""
    _enable_logfire(monkeypatch)
    resolver = CatalogResolver(
        ResolutionReportingRepository(),
        adjudicator=SelectMetricAdjudicator(42),
    )
    candidates = [
        MetricCandidate(metric_id=42, name="alpha"),
        MetricCandidate(metric_id=43, name="beta"),
    ]

    decision = await resolver._metric_adjudication_decision("teacher pay", candidates)
    assert decision is not None
    assert decision.action == "select_one"
    assert decision.selected_ids == ["42"]

    spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.catalog.adjudicate"
    ]
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["outcome"] == "select_one"
    assert attrs["candidate_count"] == 2
    assert attrs["winner_confidence"] == 0.8


@pytest.mark.asyncio
async def test_catalog_adjudicate_span_records_no_candidates_outcome(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compass.catalog.adjudicate carries outcome=no_candidates + does not touch the LLM."""
    _enable_logfire(monkeypatch)
    # Adjudicator should not be called; pass a stub that would fail if invoked.
    resolver = CatalogResolver(
        ResolutionReportingRepository(),
        adjudicator=_RaisingAdjudicator(),
    )

    decision = await resolver._metric_adjudication_decision("nothing", [])
    assert decision is None

    spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.catalog.adjudicate"
    ]
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["outcome"] == "no_candidates"
    assert attrs["candidate_count"] == 0


@pytest.mark.asyncio
async def test_catalog_adjudicate_span_records_error_outcome_on_modelretry(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compass.catalog.adjudicate carries outcome=error + error_type when adjudicator raises."""
    _enable_logfire(monkeypatch)
    resolver = CatalogResolver(
        ResolutionReportingRepository(),
        adjudicator=_RaisingAdjudicator(),
    )
    candidates = [MetricCandidate(metric_id=42, name="alpha")]

    decision = await resolver._metric_adjudication_decision("teacher pay", candidates)
    # On error we return a synthetic clarify decision, not None.
    assert decision is not None
    assert decision.action == "clarify"

    spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.catalog.adjudicate"
    ]
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["outcome"] == "error"
    assert attrs["error_type"] == "ModelRetry"
    assert attrs["candidate_count"] == 1
