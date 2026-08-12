"""W2-M3-01 phase 3 (#859): executor emits ``CompositeRankingResult``.

Phase 1 (#852) introduced the ``CompositeRankingResult`` typed artifact.
Phase 2 (#857/#858) added the planner signal: ``requires_composite_ranking: bool``
on ``QueryPlan`` and ``PendingQueryContext``.

Phase 3 is the executor wiring: when a plan carries
``requires_composite_ranking=True`` (with the validator-enforced shape:
``operation='rank'`` and ``2 <= len(metrics) <= 8``), the deterministic
executor MUST iterate the N metric specs, build N validated
``MetricRankingResult`` children, and wrap them in a
``CompositeRankingResult`` envelope — NOT a single ``MetricRankingResult``
and NOT the multi-metric ``MetricLookupResult`` fallback.

On main (pre-phase-3), a plan with ``requires_composite_ranking=True`` +
2..8 metrics + ``operation='rank'`` falls through the multi-metric guard
at ``operations.py`` (``len(execution_metric_specs) > 1 and limit is None``)
which routes it to ``_execute_lookup`` → ``MetricLookupResult``. This test
file therefore RED-asserts the composite envelope shape and is expected to
fail until the executor branch is added.
"""

from __future__ import annotations

import pytest

from compass_backend.artifacts import CitationSource
from compass_backend.contracts import (
    MetricSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.execution import (
    DeterministicQueryExecutor,
    DistrictCandidate,
    MetricAnswerRow,
    MetricCandidate,
)
from compass_backend.tests.test_mvp_execution import (
    FakePolicyAnswerRepository,
    _metric_alias,
)


def _make_metric(metric_id: int, name: str) -> MetricCandidate:
    return MetricCandidate(metric_id=metric_id, name=name, answer_type="numeric")


def _make_row(
    *,
    district_id: int,
    district_name: str,
    metric_id: int,
    metric_name: str,
    value: str,
    state: str = "CA",
) -> MetricAnswerRow:
    return MetricAnswerRow(
        answer_id=None,
        district_id=district_id,
        district_name=district_name,
        state=state,
        metric_id=metric_id,
        metric_name=metric_name,
        value=value,
        academic_year="2024 - 2025",
        citations=[
            CitationSource(
                source_name=f"{district_name} Contract.pdf",
                source_url="https://example.org/source.pdf",
                document_type=None,
                academic_year="2024 - 2025",
                district_id=district_id,
                citation_order=1,
            )
        ],
    )


def _composite_metric_specs() -> list[MetricCandidate]:
    return [
        _make_metric(101, "BA starting salary"),
        _make_metric(102, "MA starting salary"),
        _make_metric(103, "PhD starting salary"),
        _make_metric(104, "EdD starting salary"),
    ]


def _composite_repo() -> FakePolicyAnswerRepository:
    metrics = _composite_metric_specs()
    districts = [
        DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
        DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
    ]
    rows: list[MetricAnswerRow] = []
    for d_id, d_name, base_value in [(1, "Alpha", 50000), (2, "Bravo", 60000), (3, "Charlie", 70000)]:
        for metric in metrics:
            rows.append(
                _make_row(
                    district_id=d_id,
                    district_name=d_name,
                    metric_id=metric.metric_id,
                    metric_name=metric.name,
                    value=f"${base_value + metric.metric_id:,}",
                )
            )
    aliases = [
        _metric_alias(metric.name, entity_type="metric", canonical_id=str(metric.metric_id))
        for metric in metrics
    ]
    return FakePolicyAnswerRepository(
        metrics=metrics,
        districts=districts,
        rows=rows,
        aliases=aliases,
    )


class FilteringPolicyAnswerRepository(FakePolicyAnswerRepository):
    """Fake repository that preserves per-metric rows for composite tests."""

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


def _composite_plan(*, metric_count: int = 4) -> QueryPlan:
    metrics = _composite_metric_specs()[:metric_count]
    return QueryPlan(
        operation="rank",
        question="Rank covered districts on all four lanes, separately.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=metric.name) for metric in metrics],
        requires_composite_ranking=True,
    )


@pytest.mark.asyncio
async def test_executor_emits_composite_envelope_for_four_metrics() -> None:
    """Given a plan with ``requires_composite_ranking=True`` and 4 metrics,
    the executor returns ``CompositeRankingResult`` wrapping 4 children —
    NOT ``MetricLookupResult`` (the current multi-metric fallback) and
    NOT a single ``MetricRankingResult``.
    """

    repository = _composite_repo()
    executor = DeterministicQueryExecutor(repository)
    plan = _composite_plan(metric_count=4)

    outcome = await executor.execute(plan)

    assert outcome.result is not None, (
        "executor must succeed when plan.requires_composite_ranking=True"
    )
    assert outcome.result.result_type == "composite_ranking", (
        "executor must emit a CompositeRankingResult envelope, got "
        f"{outcome.result.result_type!r}"
    )
    assert len(outcome.result.children) == 4, (
        "composite envelope must wrap exactly the N metrics the user accepted"
    )
    # Every child must be a fully validated MetricRankingResult — the artifact
    # contract (phase 1) already pins this, but we re-assert here so a future
    # refactor that yields raw rows or unwrapped lists can't pass the test.
    for child in outcome.result.children:
        assert child.result_type == "metric_ranking", (
            f"composite child must be MetricRankingResult, got {child.result_type!r}"
        )


def _composite_salary_default_repo() -> FakePolicyAnswerRepository:
    """Composite repo where 'starting teacher salary' is an ambiguous BA/MA
    bundle carrying the governed ``launch_starting_salary_default`` → metric 89,
    paired with a second plain metric so the composite has 2 distinct children.
    """

    bachelor = _make_metric(89, "Annual base salary, first-year bachelor's teacher")
    master = _make_metric(96, "Annual base salary, first-year master's teacher")
    grad = _make_metric(50, "Four-year graduation rate")
    districts = [
        DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
    ]
    rows: list[MetricAnswerRow] = []
    for d_id, d_name in [(1, "Alpha"), (2, "Bravo")]:
        rows.append(_make_row(district_id=d_id, district_name=d_name,
                              metric_id=89, metric_name=bachelor.name,
                              value=f"${50000 + d_id * 1000:,}"))
        rows.append(_make_row(district_id=d_id, district_name=d_name,
                              metric_id=50, metric_name=grad.name,
                              value=f"{80 + d_id}%"))
    return FakePolicyAnswerRepository(
        metrics=[bachelor, master, grad],
        districts=districts,
        rows=rows,
        aliases=[
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
            _metric_alias("graduation rate", entity_type="metric", canonical_id="50"),
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
async def test_composite_commits_bachelor_default_for_broad_salary_child_without_clarify() -> None:
    """WS-1 (#1248) / #1452: a composite ranking whose children include a bare
    'starting teacher salary' must COMMIT the governed bachelor's default
    (metric 89) + disclosure note for that child — not dodge the whole composite
    into a BA-vs-MA clarify, mirroring the plain rank path (#1442) and the
    lookup metric-groups loop.
    """

    repository = _composite_salary_default_repo()
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question="Rank covered districts by starting teacher salary and graduation rate, separately.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[
            MetricSpec(name="starting teacher salary"),
            MetricSpec(name="graduation rate"),
        ],
        requires_composite_ranking=True,
    )

    outcome = await executor.execute(plan)

    assert outcome.clarification is None, (
        f"Expected a committed composite, got a clarification: {outcome.message}"
    )
    assert outcome.result is not None
    assert outcome.result.result_type == "composite_ranking"
    assert len(outcome.result.children) == 2
    assert 89 in repository.fetched_metric_ids
    assert 96 not in repository.fetched_metric_ids
    assert any(
        "bachelor's-degree starting salary" in note
        for child in outcome.result.children
        for note in child.source_notes
    ), "the salary child must carry the governed default disclosure note"


@pytest.mark.asyncio
async def test_executor_emits_composite_envelope_for_two_metrics() -> None:
    """Pin the lower bound of the artifact contract (min_length=2)."""

    repository = _composite_repo()
    executor = DeterministicQueryExecutor(repository)
    plan = _composite_plan(metric_count=2)

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "composite_ranking"
    assert len(outcome.result.children) == 2


@pytest.mark.asyncio
async def test_executor_does_not_emit_composite_envelope_when_flag_false() -> None:
    """The single-metric rank path stays unchanged when the flag is False."""

    repository = FakePolicyAnswerRepository(
        rows=[
            _make_row(
                district_id=1,
                district_name="Alpha",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value="$80,000",
            ),
            _make_row(
                district_id=2,
                district_name="Bravo",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value="$70,000",
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question="Rank covered districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Average teacher starting salary")],
        # requires_composite_ranking defaults to False — no composite envelope.
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_ranking", (
        "single-metric rank path must stay on MetricRankingResult, not "
        f"composite — got {outcome.result.result_type!r}"
    )


@pytest.mark.asyncio
async def test_composite_refuses_when_dedup_collapses_children_below_two() -> None:
    """If two metric specs resolve to the same ``metric_id``, the executor
    de-duplicates and the child list collapses below the
    ``CompositeRankingResult.min_length=2`` bound. The executor must emit
    a typed ``ExecutionRefusal`` rather than violate the artifact contract.

    Mirrors the explicit guard in
    ``_execute_composite_ranking`` (operations.py): the phase 2 validator
    enforces 2..8 metrics on the plan, but resolution-time de-duplication
    can still collapse the resolved set if two aliases point at one canonical.
    """

    metric = _make_metric(101, "BA starting salary")
    repository = FakePolicyAnswerRepository(
        # Only one canonical metric: every spec resolves to metric_id=101.
        metrics=[metric],
        districts=[
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
        ],
        rows=[
            _make_row(
                district_id=1,
                district_name="Alpha",
                metric_id=metric.metric_id,
                metric_name=metric.name,
                value="$80,000",
            )
        ],
        aliases=[
            _metric_alias(
                "BA starting salary",
                entity_type="metric",
                canonical_id=str(metric.metric_id),
            ),
            _metric_alias(
                "bachelor's starting salary",
                entity_type="metric",
                canonical_id=str(metric.metric_id),
            ),
        ],
    )
    executor = DeterministicQueryExecutor(repository)
    plan = QueryPlan(
        operation="rank",
        question="Rank by BA salary, both phrasings.",
        selection=SelectionSpec(scope="all_covered_districts"),
        # Two specs that resolve to the same metric_id — phase 2 validator
        # accepts this (2 metric specs, names are distinct strings); the
        # executor must catch the collapse at resolution time.
        metrics=[
            MetricSpec(name="BA starting salary"),
            MetricSpec(name="BA starting salary"),
        ],
        requires_composite_ranking=True,
    )

    outcome = await executor.execute(plan)

    assert outcome.result is None, (
        "executor must refuse rather than violate min_length=2 on the envelope"
    )
    assert outcome.message is not None
    assert "distinct" in outcome.message.casefold()


@pytest.mark.asyncio
async def test_composite_envelope_aggregates_total_considered_from_children() -> None:
    """The ``CompositeRankingResult.aggregate_summary_fields`` validator (phase
    1) sums ``total_considered`` across children when the envelope itself has 0.

    The executor passes 0 + the validator does the rollup. This pins that the
    executor does not need to (and should not) pre-compute the aggregate.
    """

    repository = _composite_repo()
    executor = DeterministicQueryExecutor(repository)
    plan = _composite_plan(metric_count=4)

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "composite_ranking"
    expected_total = sum(child.total_considered for child in outcome.result.children)
    assert outcome.result.total_considered == expected_total
    assert expected_total > 0, (
        "fake repository must seed at least one row per metric so the "
        "envelope's aggregate is non-trivially exercised"
    )
