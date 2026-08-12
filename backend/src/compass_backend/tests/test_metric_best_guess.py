"""Fix 4B (#1 refusal family) — execution best-guess for a materially-ambiguous
metric phrase.

The planner drafts a paraphrase ("paid sick days in the first year") that ties
across several real leave metrics. Both the planning-time adjudicator
(``_apply_adjudication``, covered in ``test_catalog_pipeline_validator.py``) and
the execution-time metric resolver used to CLARIFY on that tie → the turn
degraded to the canned rescue. Per the product decision, execution now
best-guesses the top-ranked candidate and DISCLOSES the alternates instead of
clarifying. This pins the ``_resolve_plan_metrics`` boundary (peer / count /
trend / single-metric-lookup) with a minimal fake catalog so no gateway/DB call
is needed.
"""

from __future__ import annotations

import pytest

from compass_backend.catalog import MetricCandidate
from compass_backend.catalog.resolution import MetricBundleResolution
from compass_backend.contracts.planning import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.execution import DeterministicQueryExecutor

_SICK = MetricCandidate(
    metric_id=198, name="Maximum number of annual paid sick days", answer_type="numeric"
)
_PERSONAL = MetricCandidate(
    metric_id=207,
    name="Number of paid personal leave days granted in first year",
    answer_type="numeric",
)


class _AmbiguousMetricCatalog:
    """Minimal catalog stub: the phrase resolves to a materially-ambiguous tie
    with no governed contextual default (so the Fix 4B best-guess fires)."""

    async def clarifying_observation_metric_bundle(
        self, question: str, *, numeric_only: bool, limit: int = 5
    ) -> MetricBundleResolution | None:
        return None

    async def resolve_contextual_metric_default(
        self, phrase: str, *, context_key: str, numeric_only: bool
    ) -> None:
        return None

    async def resolve_metric_bundle(
        self,
        query: str,
        *,
        numeric_only: bool = False,
        limit: int = 5,
        degree_lane: str | None = None,
    ) -> MetricBundleResolution:
        return MetricBundleResolution(
            query=query,
            resolved=[],
            candidates=[_SICK, _PERSONAL],
            ambiguous=True,
            clarification_hint="which sick-leave metric?",
        )


def _executor() -> DeterministicQueryExecutor:
    executor = DeterministicQueryExecutor.__new__(DeterministicQueryExecutor)
    executor._catalog = _AmbiguousMetricCatalog()  # type: ignore[attr-defined]
    return executor


@pytest.mark.asyncio
async def test_resolve_plan_metrics_best_guesses_ambiguous_metric() -> None:
    """The ambiguous tie promotes candidates[0] and returns the rest as
    alternates, instead of returning an ExecutionClarification."""

    plan = QueryPlan(
        operation="peer_comparison",
        question=(
            "How does Chicago Public Schools compare to its peers on paid sick "
            "days in the first year?"
        ),
        selection=SelectionSpec(scope="named_districts", districts=["Chicago"]),
        metrics=[MetricSpec(name="paid sick days in the first year")],
    )

    resolved = await _executor()._resolve_plan_metrics(plan, numeric_only=False)

    assert isinstance(resolved, tuple), (
        f"Expected a best-guess (metrics, notes, alternates), got {resolved!r}"
    )
    metrics, source_notes, alternates = resolved
    assert [m.metric_id for m in metrics] == [198]
    assert [a.metric_id for a in alternates] == [207]
    assert source_notes == []
