"""Topic-coverage count builder — "X of Y districts have <topic> data".

Case 373 (COVER-MIGRATED-120): "How many districts have data addressing
Evaluation policies?" must answer with a single aggregate count, not rescue.
A district counts as having topic data when it has >= 1 answer-state cell in
ANY of the topic's metrics.
"""

from __future__ import annotations

from compass_backend.artifacts import ResultSelection, SelectedDistrict
from compass_backend.catalog import MetricCandidate
from compass_backend.contracts.planning import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.execution.count import (
    build_topic_coverage_count_result,
    count_plan_is_topic_coverage,
    count_shape_is_supported,
)
from compass_backend.execution.scoping import _topic_search_variants
from compass_backend.execution.types import MetricAnswerRow


def test_topic_search_variants_narrow_phrase_to_topic_name() -> None:
    """The planner echoes the user's phrase ("Evaluation policies"), but
    search_topics matches a topic name as a SUBSTRING of the query — so the
    bare topic name must appear as one of the tried queries. Regression for the
    live 'I couldn't match that to a Compass policy topic' refusal on case 373.
    """
    assert _topic_search_variants("Evaluation policies") == [
        "Evaluation policies",
        "Evaluation",
    ]
    assert _topic_search_variants("salary data") == ["salary data", "salary"]
    # multi-word topic survives, plus single-word fallbacks
    variants = _topic_search_variants("collective bargaining")
    assert variants[0] == "collective bargaining"
    assert "bargaining" in variants
    # already-bare topic is unchanged
    assert _topic_search_variants("benefits") == ["benefits"]


def _plan() -> QueryPlan:
    return QueryPlan(
        operation="count",
        count_kind="topic_coverage_count",
        question="How many districts have data addressing Evaluation policies?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Evaluation policies")],
    )


def _row(district_id: int, name: str, value: object, *, metric_id: int) -> MetricAnswerRow:
    return MetricAnswerRow(
        district_id=district_id,
        district_name=name,
        state="CA",
        metric_id=metric_id,
        metric_name=f"Evaluation metric {metric_id}",
        value=value,
        academic_year="2024 - 2025",
    )


def _selection() -> ResultSelection:
    return ResultSelection(
        scope="all_covered_districts",
        districts=[
            SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
            SelectedDistrict(district_id=2, district_name="Bravo", state="CA"),
            SelectedDistrict(district_id=3, district_name="Charlie", state="CA"),
        ],
        unresolved_districts=[],
        states=[],
    )


def test_shape_supported_requires_topic_phrase() -> None:
    assert count_plan_is_topic_coverage(_plan())
    assert count_shape_is_supported(_plan())
    no_metric = _plan().model_copy(update={"metrics": []})
    assert not count_shape_is_supported(no_metric)


def test_topic_coverage_aggregates_to_one_row() -> None:
    # Alpha + Bravo each have >=1 answer across the two metrics; Charlie has
    # none -> count = 2 of 3 covered districts.
    metric_a = MetricCandidate(metric_id=1001, name="Eval metric A", answer_type="text")
    metric_b = MetricCandidate(metric_id=1002, name="Eval metric B", answer_type="text")
    metric_rows = [
        (metric_a, [
            _row(1, "Alpha", "Yes", metric_id=1001),
            _row(2, "Bravo", None, metric_id=1001),
            _row(3, "Charlie", None, metric_id=1001),
        ]),
        (metric_b, [
            _row(1, "Alpha", None, metric_id=1002),
            _row(2, "Bravo", "No", metric_id=1002),
            _row(3, "Charlie", None, metric_id=1002),
        ]),
    ]

    result = build_topic_coverage_count_result(
        _plan(),
        metric_rows,
        selection=_selection(),
        academic_year="2024 - 2025",
        topic_label="Evaluation",
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.coverage_reason == "topic_coverage_count"
    assert row.denominator == 3
    assert row.count == 2
    assert row.qualifying_district_ids == [1, 2]
    assert "have Evaluation data" in row.display_value
    assert row.display_value == "2 of 3 covered districts have Evaluation data"


def test_resolver_delegates_topic_methods_to_repository() -> None:
    """Regression: the executor calls these on the CatalogResolver, not the
    repository — they must exist on the resolver and delegate. (A builder-only
    test missed that the concrete resolver lacked search_topics, causing a live
    AttributeError 500 on case 373.)"""
    import asyncio

    from compass_backend.catalog.resolution import CatalogResolver, TopicCandidate

    class _FakeRepo:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def search_topics(self, query, *, limit=5):  # noqa: ANN001
            self.calls.append(f"search_topics:{query}:{limit}")
            return [TopicCandidate(topic_id=1, topic_name="Evaluation")]

        async def fetch_topic_metric_candidates(self, topics, *, limit=10):  # noqa: ANN001
            self.calls.append(f"fetch:{[t.topic_name for t in topics]}:{limit}")
            return [MetricCandidate(metric_id=9, name="Eval m", answer_type="text")]

    repo = _FakeRepo()
    resolver = CatalogResolver(repo)  # type: ignore[arg-type]
    topics = asyncio.run(resolver.search_topics("Evaluation policies", limit=3))
    assert [t.topic_name for t in topics] == ["Evaluation"]
    metrics = asyncio.run(resolver.fetch_topic_metric_candidates(topics, limit=60))
    assert [m.metric_id for m in metrics] == [9]
    assert repo.calls == [
        "search_topics:Evaluation policies:3",
        "fetch:['Evaluation']:60",
    ]


def test_full_coverage_reads_all_of_universe() -> None:
    # The real case 373 shape: every covered district has eval data -> N of N.
    metric = MetricCandidate(metric_id=1001, name="Eval metric A", answer_type="text")
    metric_rows = [
        (metric, [
            _row(1, "Alpha", "Yes", metric_id=1001),
            _row(2, "Bravo", "No", metric_id=1001),
            _row(3, "Charlie", "Yes", metric_id=1001),
        ]),
    ]
    result = build_topic_coverage_count_result(
        _plan(),
        metric_rows,
        selection=_selection(),
        academic_year="2024 - 2025",
        topic_label="Evaluation",
    )
    row = result.rows[0]
    assert row.count == 3
    assert row.denominator == 3
    assert row.percent == 100.0
