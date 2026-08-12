"""#1830 — a rank operation must not offer, or loop on, a non-rankable field.

Production session ``841f346c`` reproduced an infinite clarify loop:

    turn 1  "Show me the 5 districts that offer the most planning time"
            → clarify offering 6 rankable numeric metrics PLUS 2 non-rankable
              categorical/policy fields ("permits/requires/prohibits…",
              "specifies a minimum or maximum…").
    turn 2  user picks a categorical field
            → the executor re-offers *the exact same string* as its only
              clarify candidate → a self-referential loop with no exit.

Two boundaries own the fix, both exercised here against stub catalogs:

(a) ``grounded_metric_options`` (the turn-1 candidate list): when the pending
    operation is a numeric ranking, the categorical fields must be filtered out
    so only rankable metrics are offered.
(b) ``_OperationsMixin._resolve_rank_primary_metric_with_default`` (the turn-2
    execution escape): a clarify whose candidate list is a subset of the
    just-chosen phrase is a guaranteed loop — it must never be emitted; the
    executor states plainly it cannot rank by that field instead.
"""

from __future__ import annotations

import asyncio

from compass_backend.catalog import (
    MetricBundleResolution,
    MetricCandidate,
    MetricResolution,
)
from compass_backend.contracts import ClarificationRequest
from compass_backend.contracts.planning import (
    LimitSpec,
    MetricSpec,
    PendingQueryContext,
    QueryPlan,
    SelectionSpec,
    SortSpec,
)
from compass_backend.execution import operations
from compass_backend.execution.operations import _OperationsMixin
from compass_backend.execution.types import (
    ExecutionClarification,
    ExecutionRefusal,
)
from compass_backend.orchestration.clarification_grounding import (
    grounded_metric_options,
)


# The real "planning time" bundle shape: 6 rankable numeric metrics (minutes /
# days) plus the 2 non-rankable categorical/policy fields that fed the loop.
_NUMERIC_PLANNING_TIME = [
    MetricCandidate(
        metric_id=57,
        name="Minimum elementary teacher planning time per week (in minutes)",
        answer_type="numeric",
        topic="Planning time",
    ),
    MetricCandidate(
        metric_id=58,
        name="Minimum middle school teacher planning time per week (in minutes)",
        answer_type="numeric",
        topic="Planning time",
    ),
    MetricCandidate(
        metric_id=59,
        name="Minimum high school teacher planning time per week (in minutes)",
        answer_type="numeric",
        topic="Planning time",
    ),
    MetricCandidate(
        metric_id=60,
        name="Minimum elementary teacher planning time per day (in minutes)",
        answer_type="numeric",
        topic="Planning time",
    ),
    MetricCandidate(
        metric_id=61,
        name="Minimum middle school teacher planning time per day (in minutes)",
        answer_type="numeric",
        topic="Planning time",
    ),
    MetricCandidate(
        metric_id=62,
        name="Minimum high school teacher planning time per day (in minutes)",
        answer_type="numeric",
        topic="Planning time",
    ),
]

_CATEGORICAL_PLANNING_TIME = [
    MetricCandidate(
        metric_id=63,
        name="The district permits, requires, or prohibits collaborative planning time",
        answer_type="categorical",
        topic="Planning time",
    ),
    MetricCandidate(
        metric_id=64,
        name="The district specifies a minimum or maximum amount of planning time",
        answer_type="categorical",
        topic="Planning time",
    ),
]

_ALL_PLANNING_TIME = _NUMERIC_PLANNING_TIME + _CATEGORICAL_PLANNING_TIME

_CATEGORICAL_PHRASE = (
    "The district permits, requires, or prohibits collaborative planning time"
)


class _FakeMetricCatalog:
    """Stub of ``resolve_metric_bundle`` for the turn-1 grounding seam.

    Mirrors the real resolver's contract: ``.candidates`` is the RAW code-search
    set (it is not itself numeric-filtered — the ``numeric_only`` flag governs
    ``.resolved`` / adjudication), which is exactly why the grounding seam must
    apply the ``answer_type`` filter itself.
    """

    def __init__(self, candidates: list[MetricCandidate]):
        self._candidates = candidates

    async def resolve_metric_bundle(  # noqa: ANN001
        self, query, *, numeric_only=False, limit=5, degree_lane=None
    ):
        return MetricBundleResolution(
            query=query, resolved=[], candidates=list(self._candidates)
        )


def _rank_clarification() -> ClarificationRequest:
    return ClarificationRequest(
        question="Planning time breaks down a few ways — which to rank by?",
        missing_fields=["metric"],
        candidates=["planning time"],
        pending_context=PendingQueryContext(operation="rank"),
    )


def _lookup_clarification() -> ClarificationRequest:
    return ClarificationRequest(
        question="Which planning-time policy do you mean?",
        missing_fields=["metric"],
        candidates=["planning time"],
        pending_context=PendingQueryContext(operation="lookup"),
    )


class TestTurn1CandidateFilter:
    def test_rank_clarify_excludes_categorical_fields(self) -> None:
        grounded = asyncio.run(
            grounded_metric_options(
                _rank_clarification(), _FakeMetricCatalog(_ALL_PLANNING_TIME)
            )
        )
        names = [option.value for option in grounded]
        # The 6 rankable numeric metrics survive.
        assert names == [c.name for c in _NUMERIC_PLANNING_TIME]
        # Neither categorical/policy field is offered as "which metric to rank by".
        assert _CATEGORICAL_PHRASE not in names
        assert all("permits, requires, or prohibits" not in n for n in names)

    def test_rank_shape_filters_even_when_operation_unset(self) -> None:
        # Robustness: a partial mid-clarify context may leave operation unset,
        # but a top/bottom limit (from "the 5 …most…") is a rank signal too — the
        # categorical fields must still be filtered so Part 1 does not silently
        # no-op if the planner omits operation.
        clar = ClarificationRequest(
            question="Which planning-time metric to rank by?",
            missing_fields=["metric"],
            candidates=["planning time"],
            pending_context=PendingQueryContext(
                limit=LimitSpec(count=5, kind="top"),
                sort=SortSpec(field="planning time", direction="desc"),
            ),
        )
        grounded = asyncio.run(
            grounded_metric_options(clar, _FakeMetricCatalog(_ALL_PLANNING_TIME))
        )
        names = [option.value for option in grounded]
        assert names == [c.name for c in _NUMERIC_PLANNING_TIME]
        assert _CATEGORICAL_PHRASE not in names

    def test_non_rank_clarify_keeps_all_metrics(self) -> None:
        # A lookup clarify legitimately offers categorical policy fields — the
        # filter must be scoped to numeric-ranking operations only.
        grounded = asyncio.run(
            grounded_metric_options(
                _lookup_clarification(), _FakeMetricCatalog(_ALL_PLANNING_TIME)
            )
        )
        names = [option.value for option in grounded]
        assert names == [c.name for c in _ALL_PLANNING_TIME]
        assert _CATEGORICAL_PHRASE in names


class _SelfReferentialCatalog:
    """Stub reproducing turn 2: the chosen categorical phrase resolves as an
    ambiguous metric whose ONLY candidate restates that same phrase."""

    def __init__(self, phrase: str):
        self._phrase = phrase

    async def resolve_primary_metric(  # noqa: ANN001
        self, query, *, numeric_only=True, limit=5, degree_lane=None
    ):
        return MetricResolution(
            query=query,
            resolved=None,
            candidates=[
                MetricCandidate(
                    metric_id=63, name=self._phrase, answer_type="categorical"
                )
            ],
            ambiguous=True,
        )

    async def resolve_contextual_metric_default(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


class _FakeSelf:
    def __init__(self, catalog):
        self._catalog = catalog


def _rank_plan() -> QueryPlan:
    return QueryPlan(
        operation="rank",
        question="Show me the 5 districts that offer the most planning time",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=_CATEGORICAL_PHRASE)],
    )


class TestTurn2ExecutionEscape:
    def test_categorical_rank_does_not_loop(self, monkeypatch) -> None:
        # Stub the clarify composer so the pre-fix path stays deterministic
        # (no gateway call); after the fix the guard returns before it is used.
        from compass_backend._clarify_helpers import _metric_clarification

        async def _stub_compose(metric_phrase, *, operation, candidates, **kwargs):
            return _metric_clarification(
                metric_phrase, operation=operation, candidates=candidates
            )

        monkeypatch.setattr(operations, "compose_clarify_question_async", _stub_compose)

        fake = _FakeSelf(_SelfReferentialCatalog(_CATEGORICAL_PHRASE))
        outcome = asyncio.run(
            _OperationsMixin._resolve_rank_primary_metric_with_default(
                fake, _rank_plan(), MetricSpec(name=_CATEGORICAL_PHRASE)
            )
        )

        # The loop-breaker: the executor must NOT re-offer the chosen phrase as a
        # clarify candidate. It states plainly it cannot rank by a categorical
        # field instead.
        if isinstance(outcome, ExecutionClarification):
            sole = [c.strip().casefold() for c in outcome.clarification.candidates]
            assert sole != [_CATEGORICAL_PHRASE.casefold()], (
                "self-referential clarify re-offered the just-chosen string — "
                "guaranteed loop"
            )
        assert isinstance(outcome, ExecutionRefusal)
        assert "rank" in outcome.message.casefold()
