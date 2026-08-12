"""Regression: categorical_value_count promotion (2026-07-01 count-crash + Fix 3).

Production sessions ``da74d522`` / ``c4b511a6`` / ``f31c1f1e`` ("How many sick
days do most districts offer?") 500'd the whole turn with:

    pydantic_core.ValidationError: 1 validation error for QueryPlan
      count_kind='categorical_value_count' requires a metric with role='grouping'

Trace: ``build_chat_response`` -> ``_promote_pending_context_for_turn`` ->
``promote_pending_context_to_plan`` (planner.py) -> ``QueryPlan(...)``.

The trap: a ``categorical_value_count`` pending context whose sole metric carried
the default ``role='primary'`` (not ``'grouping'``) *passed*
``_pending_context_is_complete`` — the completeness predicate did not inspect
metric roles — yet *failed* ``QueryPlan`` validation. Because this promotion runs
at the top of ``build_chat_response``, OUTSIDE the planner's ``retries=0`` rescue
envelope, the raise was uncaught and 500'd the turn.

**Two eras, both proved here.**

* #1829 (crash-safety net): promotion must never raise — it degrades to the
  clarify (returns ``None``). Still true for the genuinely UNRECOVERABLE shapes
  (zero metrics, or 2+ non-grouping metrics): there is no single axis to group
  by, so those degrade.
* Fix 3 (this change): the RECOVERABLE shape — exactly one non-grouping metric —
  is no longer a dead-end clarify. A categorical breakdown over one metric has
  exactly one axis, so the metric is re-roled ``role='grouping'`` (TYPED-field
  derive-recover, no prose) and the turn now promotes to a real per-value answer.

The #1829 ``try/except ValidationError`` stays as a pure safety net for the
UNEXPECTED; Fix 3 handles the expected recoverable shape by construction, and the
tightened completeness check degrades the unrecoverable shapes via clean control
flow (early ``None``) before the QueryPlan build is even attempted.
"""

from __future__ import annotations

from compass_backend.contracts.planning import (
    MetricSpec,
    PendingQueryContext,
    SelectionSpec,
)
from compass_backend.planning.planner import (
    _pending_context_is_complete,
    promote_pending_context_to_plan,
)

_QUESTION = "How many sick days do most districts offer?"


def _selection_all() -> SelectionSpec:
    return SelectionSpec(scope="all_covered_districts")


def test_categorical_value_count_single_metric_reroled_and_promotes() -> None:
    """Fix 3: the exact prod crash shape — a categorical_value_count pending
    context whose sole metric defaults to role='primary' — now RECOVERS. The
    single metric is the breakdown axis, so it is re-roled role='grouping' and
    the turn promotes to a valid plan instead of the dead-end clarify."""

    pending = PendingQueryContext(
        operation="count",
        count_kind="categorical_value_count",
        question=_QUESTION,
        selection=_selection_all(),
        metrics=[MetricSpec(name="sick leave")],  # default role="primary"
    )

    plan = promote_pending_context_to_plan(pending, question=_QUESTION)

    assert plan is not None, (
        "Fix 3: a categorical_value_count with exactly one non-grouping metric is "
        "recoverable (re-role the sole metric to grouping); promotion must yield a "
        "valid plan, not degrade to the clarify."
    )
    assert plan.operation == "count"
    assert plan.count_kind == "categorical_value_count"
    grouping = [metric for metric in plan.metrics if metric.role == "grouping"]
    assert len(grouping) == 1
    assert grouping[0].name == "sick leave"


def test_categorical_value_count_with_grouping_metric_still_promotes() -> None:
    """The happy path is unchanged: a promotable categorical count (a grouping
    metric already set) still yields an executable plan, untouched by the
    normalizer."""

    pending = PendingQueryContext(
        operation="count",
        count_kind="categorical_value_count",
        question=_QUESTION,
        selection=_selection_all(),
        metrics=[MetricSpec(name="sick leave", role="grouping")],
    )

    plan = promote_pending_context_to_plan(pending, question=_QUESTION)

    assert plan is not None
    assert plan.operation == "count"
    assert plan.count_kind == "categorical_value_count"
    grouping = [metric for metric in plan.metrics if metric.role == "grouping"]
    assert len(grouping) == 1


def test_categorical_value_count_multi_metric_degrades_to_clarify() -> None:
    """UNRECOVERABLE shape (crash-safety net, #1829): 2+ non-grouping metrics
    have no single axis to group by. Promotion must degrade to the clarify
    (None), never raise — and Fix 3's narrow exactly-one guard must NOT re-role
    an ambiguous multi-metric shape into a spurious answer."""

    pending = PendingQueryContext(
        operation="count",
        count_kind="categorical_value_count",
        question=_QUESTION,
        selection=_selection_all(),
        metrics=[
            MetricSpec(name="sick leave"),
            MetricSpec(name="personal days"),
        ],
    )

    plan = promote_pending_context_to_plan(pending, question=_QUESTION)

    assert plan is None, (
        "A categorical_value_count with 2+ non-grouping metrics cannot form a "
        "valid QueryPlan and is not recoverable by the exactly-one normalizer; "
        "promotion must degrade to the clarify (None), never raise."
    )


def test_categorical_value_count_zero_metric_degrades_to_clarify() -> None:
    """UNRECOVERABLE shape: no metric at all. Nothing to group by, nothing to
    re-role. Promotion must degrade to the clarify (None)."""

    pending = PendingQueryContext(
        operation="count",
        count_kind="categorical_value_count",
        question=_QUESTION,
        selection=_selection_all(),
        metrics=[],
    )

    plan = promote_pending_context_to_plan(pending, question=_QUESTION)

    assert plan is None


def test_multi_metric_categorical_completeness_is_false() -> None:
    """Layer 2 (Fix 3), asserted directly — the ONLY observable effect of the
    completeness tightening at this altitude.

    Both degrade routes return None from promotion (early-None via completeness
    AND #1829's try/except), so a promotion-level test cannot tell whether
    completeness did anything. This pins the asymmetry the plan retires: a
    multi-metric categorical_value_count is now INCOMPLETE (no single groupable
    axis), so it degrades during clean control flow rather than by a caught
    ValidationError at QueryPlan build."""

    pending = PendingQueryContext(
        operation="count",
        count_kind="categorical_value_count",
        question=_QUESTION,
        selection=_selection_all(),
        metrics=[
            MetricSpec(name="sick leave"),
            MetricSpec(name="personal days"),
        ],
    )

    assert _pending_context_is_complete(pending) is False, (
        "A categorical_value_count with 2+ non-grouping metrics has no single "
        "grouping axis and is not normalizable; completeness must reject it so it "
        "degrades before QueryPlan build, not via the crash-safety net."
    )


def test_single_metric_categorical_completeness_is_true() -> None:
    """Layer 2, positive: the recoverable single-metric shape stays complete
    (it is normalizable to a grouping axis), so completeness does not spuriously
    block the turn Fix 3 now answers."""

    pending = PendingQueryContext(
        operation="count",
        count_kind="categorical_value_count",
        question=_QUESTION,
        selection=_selection_all(),
        metrics=[MetricSpec(name="sick leave")],
    )

    assert _pending_context_is_complete(pending) is True
