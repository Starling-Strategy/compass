"""Consistency validation for a planner-set ``FilterSpec.kind`` (#1373 Stage 2).

When the planner sets a typed ``kind``, a ``model_validator`` enforces the
invariants that kind implies — using only the model's own fields, never tying
``kind`` to the (possibly vestigial) ``field`` string. That keeps the #1373
dual-read intact: ``kind`` is the authority and may override what ``field`` would
derive (see :func:`execution.filters.filter_kind`), so the validator must not
re-couple them.

Two invariants:

- A selection-phase kind (``state``/``region``/``enrollment``) cannot carry an
  ``anchor_value`` — anchor values compare against a *metric* value, so that is
  ``kind="metric_value"``.
- A ``metric_value`` threshold under a comparison operator must have a ``value``
  or an ``anchor_value`` to compare against; otherwise execution applies no
  filter and silently returns every district (the cardinal silent-drop sin). The
  presence operators ``is_missing``/``is_not_missing`` legitimately carry no value.

The *acceptance* tests below are load-bearing, not decorative: the planner runs
with ``retries={"output":0}``, so a ``ValidationError`` on its output does not
re-roll — it surfaces as a user-visible rescue clarification. The validator must
therefore accept every shape the planner legitimately emits.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.contracts.planning import FilterSpec

# ---------------------------------------------------------------------------
# Rejections — genuinely-broken kind/shape combinations the planner never emits.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["state", "region", "enrollment"])
def test_selection_kind_rejects_anchor_value(kind: str) -> None:
    """A selection-phase kind cannot carry an anchor_value (Rule A)."""
    with pytest.raises(ValidationError):
        FilterSpec(
            field=kind,
            operator="equals",
            anchor_value={"district": "Portland ME"},
            kind=kind,
        )


@pytest.mark.parametrize(
    "operator",
    ["equals", "greater_than", "greater_than_or_equal", "less_than", "in"],
)
def test_metric_value_without_value_or_anchor_is_rejected(operator: str) -> None:
    """A metric-value threshold with nothing to compare against is rejected so it
    cannot silently filter nothing (Rule B)."""
    with pytest.raises(ValidationError):
        FilterSpec(field="teacher workdays", operator=operator, kind="metric_value")


# ---------------------------------------------------------------------------
# Acceptances — every shape the planner legitimately emits (NO false reject).
# ---------------------------------------------------------------------------


def test_metric_value_threshold_with_value_is_accepted() -> None:
    FilterSpec(
        field="teacher workdays", operator="greater_than", value=190, kind="metric_value"
    )


def test_metric_value_anchor_only_is_accepted() -> None:
    """The production anchor shape: kind=metric_value, anchor_value, no value."""
    FilterSpec(
        field="teacher workdays",
        operator="equals",
        anchor_value={"district": "Portland", "state": "ME"},
        kind="metric_value",
    )


def test_metric_value_boolean_false_value_is_accepted() -> None:
    """value=False is a real value, not 'missing' (parental-leave offered=False)."""
    FilterSpec(
        field="Does the district offer paid parental leave?",
        operator="equals",
        value=False,
        kind="metric_value",
    )


@pytest.mark.parametrize("operator", ["is_missing", "is_not_missing"])
def test_metric_value_presence_operators_need_no_value(operator: str) -> None:
    """is_missing/is_not_missing legitimately carry no value (Rule B exemption)."""
    FilterSpec(field="teacher workdays", operator=operator, kind="metric_value")


def test_state_filter_with_value_is_accepted() -> None:
    FilterSpec(field="state", operator="equals", value="CA", kind="state")


def test_kind_does_not_have_to_match_field() -> None:
    """Dual-read: a planner-set kind overrides field-derivation, so the validator
    must NOT tie kind to the (vestigial) field string — the same probe shape
    test_filter_kind.py relies on."""
    FilterSpec(
        field="teacher workdays", operator="greater_than", value=1, kind="state"
    )


def test_unset_kind_is_never_validated() -> None:
    """kind=None defers entirely to execution's filter_kind derivation; the
    validator must no-op (legacy 'value' token, anchor filters without kind)."""
    FilterSpec(field="value", operator="greater_than")
    FilterSpec(
        field="teacher workdays",
        operator="equals",
        anchor_value={"district": "Portland Public Schools"},
    )
