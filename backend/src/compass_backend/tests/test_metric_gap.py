"""#1613 U4: multi-metric lookup partial-resolution at the finalizer.

When a multi-metric ``lookup`` names a metric phrase that has no catalog metric
(``confidence="none"``) while >=1 other metric resolves, ``finalize_plan``
strips the unresolvable phrase into ``deferred_metric_gap`` and marks the plan
ready -- so the executor answers the resolvable subset and the renderer
discloses the gap, instead of failing the whole plan (over-clarify) or letting
the planner silently drop it.

Anchor: case 1193 -- "How do BA starting salary, MA starting salary, and average
teacher salary compare across the largest 10 districts?" ("average teacher
salary" has no catalog metric). Per planner.md this is a multi-metric *lookup*
(side-by-side), not a composite ranking.
"""

from __future__ import annotations

from compass_backend.contracts.catalog_resolution import (
    CatalogReconciliationReport,
    CatalogResolution,
)
from compass_backend.contracts.planning import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.planning.finalizer import finalize_plan


def _lookup_plan(
    metric_names: list[str],
    *,
    operation: str = "lookup",
    requires_all_metrics: bool = False,
    requires_composite_ranking: bool = False,
) -> QueryPlan:
    return QueryPlan(
        question="How do these salaries compare across the largest 10 districts?",
        operation=operation,
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=name) for name in metric_names],
        requires_all_metrics=requires_all_metrics,
        requires_composite_ranking=requires_composite_ranking,
    )


def _report_unresolvable(*phrases: str) -> CatalogReconciliationReport:
    return CatalogReconciliationReport(
        resolutions=tuple(
            CatalogResolution(phrase=p, role_hint="metric_subject", confidence="none")
            for p in phrases
        )
    )


def test_lookup_with_one_unresolvable_metric_defers_the_gap() -> None:
    """1193 shape: 3 metrics, "average teacher salary" unresolvable, BA+MA
    resolve -> ready; the gap is deferred; plan.metrics holds the resolvable."""
    plan = _lookup_plan(
        ["BA starting salary", "MA starting salary", "average teacher salary"]
    )
    finalized = finalize_plan(plan, _report_unresolvable("average teacher salary"))
    assert finalized.is_ready
    assert finalized.deferred_metric_gap == ("average teacher salary",)
    assert [m.name for m in finalized.plan.metrics] == [
        "BA starting salary",
        "MA starting salary",
    ]


def test_lookup_single_resolvable_metric_still_answers() -> None:
    """A lookup can answer ONE metric, so a 2-metric lookup with 1 unresolvable
    still defers the gap and answers the one that resolves (unlike composite,
    which needs >=2 -- the lookup reframe removes that floor)."""
    plan = _lookup_plan(["BA starting salary", "average teacher salary"])
    finalized = finalize_plan(plan, _report_unresolvable("average teacher salary"))
    assert finalized.is_ready
    assert finalized.deferred_metric_gap == ("average teacher salary",)
    assert [m.name for m in finalized.plan.metrics] == ["BA starting salary"]


def test_lookup_all_metrics_unresolvable_clarifies() -> None:
    """No metric resolves -> nothing to answer -> keep the blockers and clarify."""
    plan = _lookup_plan(["made-up metric A", "made-up metric B"])
    finalized = finalize_plan(
        plan, _report_unresolvable("made-up metric A", "made-up metric B")
    )
    assert not finalized.is_ready
    assert finalized.deferred_metric_gap == ()


def test_requires_all_metrics_does_not_defer() -> None:
    """"Districts satisfying BOTH A and B" -- dropping one metric would silently
    change the AND semantics, so keep the blocker and clarify."""
    plan = _lookup_plan(
        ["performance pay bonuses", "hard-to-staff school pay", "made-up metric"],
        requires_all_metrics=True,
    )
    finalized = finalize_plan(plan, _report_unresolvable("made-up metric"))
    assert not finalized.is_ready
    assert finalized.deferred_metric_gap == ()


def test_composite_shape_does_not_defer() -> None:
    """A composite ("do all N separately") request keeps its blocker and
    clarifies -- the composite render path is a deferred follow-up."""
    plan = _lookup_plan(
        ["BA starting salary", "MA starting salary", "average teacher salary"],
        operation="rank",
        requires_composite_ranking=True,
    )
    finalized = finalize_plan(plan, _report_unresolvable("average teacher salary"))
    assert not finalized.is_ready
    assert finalized.deferred_metric_gap == ()


def test_all_metrics_resolve_no_gap() -> None:
    """All metrics resolve -> no gap, ready, metrics unchanged (no behavior
    change for the fully-resolvable lookup)."""
    plan = _lookup_plan(["BA starting salary", "MA starting salary"])
    finalized = finalize_plan(plan, CatalogReconciliationReport())
    assert finalized.is_ready
    assert finalized.deferred_metric_gap == ()
    assert len(finalized.plan.metrics) == 2
