"""Focused tests for M3 #1013 validation→clarification pivot.

When `_validate_and_render_success` sees a multi-metric plan whose validation
fails, it now pivots to a typed ClarificationRequest instead of letting the
"I found a deterministic result, but validation failed before rendering"
prose propagate to the user. Both branches of the predicate
`_validation_failure_offers_clarification` are covered: the multi-metric
branch must fire, the single-metric / valid branches must NOT fire.

Run with:
    PYTHONPATH=src uv run pytest \\
        src/compass_backend/tests/test_validation_clarification_pivot.py --tb=short
"""

from __future__ import annotations

from compass_backend.contracts.planning import MetricSpec, QueryPlan, SelectionSpec
from compass_backend.contracts.validation import (
    ValidationFinding,
    ValidationReport,
)
from compass_backend.orchestration.chat import (
    _validation_failure_offers_clarification,
)


def _plan(metrics: list[MetricSpec]) -> QueryPlan:
    return QueryPlan(
        operation="rank",
        question="show me starting salary for BA and MA teachers",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=metrics,
    )


def _report(valid: bool, codes: list[str]) -> ValidationReport:
    return ValidationReport(
        valid=valid,
        findings=[
            ValidationFinding(
                code=code,
                message=f"{code} test",
                dimension="metric",
                severity="error",
            )
            for code in codes
        ],
    )


def test_pivot_fires_on_multi_metric_validation_failure() -> None:
    plan = _plan(
        metrics=[
            MetricSpec(name="BA starting salary"),
            MetricSpec(name="MA starting salary"),
        ]
    )
    report = _report(valid=False, codes=["unsupported_metric_plan"])
    assert _validation_failure_offers_clarification(plan, report) is True


def test_pivot_skips_when_validation_passed() -> None:
    plan = _plan(
        metrics=[
            MetricSpec(name="BA starting salary"),
            MetricSpec(name="MA starting salary"),
        ]
    )
    report = _report(valid=True, codes=[])
    assert _validation_failure_offers_clarification(plan, report) is False


def test_pivot_skips_on_single_metric_failure() -> None:
    """Single-metric failures have no useful render choice to offer — the
    generic prose stays as a debugging surface."""
    plan = _plan(metrics=[MetricSpec(name="starting teacher salary")])
    report = _report(valid=False, codes=["metric_row_mismatch"])
    assert _validation_failure_offers_clarification(plan, report) is False


def test_pivot_ignores_grouping_metrics_for_threshold() -> None:
    """A plan with one primary metric plus a grouping metric is still
    single-effective-metric — pivot must not fire."""
    plan = _plan(
        metrics=[
            MetricSpec(name="starting teacher salary"),
            MetricSpec(name="sick leave policy type", role="grouping"),
        ]
    )
    report = _report(valid=False, codes=["unsupported_metric_plan"])
    assert _validation_failure_offers_clarification(plan, report) is False


def test_pivot_fires_when_two_primary_three_grouping() -> None:
    """Three or more metrics with at least 2 primaries — the pivot's
    intersection / union / separate-tables choice is meaningful."""
    plan = _plan(
        metrics=[
            MetricSpec(name="BA starting salary"),
            MetricSpec(name="MA starting salary"),
            MetricSpec(name="ending teacher salary"),
        ]
    )
    report = _report(valid=False, codes=["metric_row_mismatch"])
    assert _validation_failure_offers_clarification(plan, report) is True
