"""Parity tests for the single-source ``filter_kind()`` derivation (#1373).

``FilterSpec.field`` is a ``str`` overloaded with four meanings —
``"state"``/``"region"``/``"enrollment"`` (selection-phase), the legacy
``"value"`` count token, and an otherwise free-form metric phrase — with the
*kind* re-derived by scattered string parsing across execution/{selection,
operations,count}.py, planning/finalizer.py, and quality/_validation_helpers.py.

Stage 1 types the *decision* (which kind) in ``FilterSpec.kind`` and routes
every former check through one ``filter_kind()`` helper. Stage 2 removed the
runtime-rewritten ``"metric:<id>"`` field token (resolved metric-value filters
keep their free-form phrase), so that form no longer reaches ``filter_kind``.
These tests pin the helper's output to the per-field classification for every
field form the system actually produces.

The oracle below reproduces the logic *independently* of the helper under test
(it does not import ``filter_kind``); the parity test asserts the helper agrees
with the oracle for the full cross-product of field forms × operators.
"""

from __future__ import annotations

import pytest

from compass_backend.contracts.planning import FilterKind, FilterOperator, FilterSpec
from compass_backend.execution.filters import filter_kind

# ---------------------------------------------------------------------------
# Reference oracle — today's scattered classification, reproduced inline.
# ---------------------------------------------------------------------------

# Structural artifact/selection field tokens (selection._STRUCTURAL_FILTER_FIELDS).
_OLD_STRUCTURAL_FIELDS = frozenset(
    {
        "answer_value",
        "district",
        "district_name",
        "display_value",
        "enrollment",
        "metric_value",
        "name",
        "region",
        "state",
        "value",
    }
)

# Operators that target a metric value (selection._METRIC_VALUE_FILTER_OPERATORS).
_OLD_METRIC_VALUE_OPERATORS = frozenset(
    {
        "equals",
        "not_equals",
        "greater_than",
        "greater_than_or_equal",
        "less_than",
        "less_than_or_equal",
        "contains",
        "in",
        "not_in",
    }
)

ALL_OPERATORS: tuple[FilterOperator, ...] = (
    "equals",
    "not_equals",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "in",
    "not_in",
    "contains",
    "is_missing",
    "is_not_missing",
)


def _old_is_metric_value(spec: FilterSpec) -> bool:
    """Reproduce selection._filter_is_metric_value (post-#1373 Stage 2).

    The ``"metric:<id>"`` short-circuit is gone: a resolved metric-value filter
    keeps its free-form phrase, which still classifies metric_value via the
    non-structural-field-under-metric-value-operator gate below.
    """

    field = spec.field.casefold().strip()
    if field in _OLD_STRUCTURAL_FIELDS:
        return False
    if spec.operator not in _OLD_METRIC_VALUE_OPERATORS:
        return False
    return True


def _old_kind(spec: FilterSpec) -> FilterKind | None:
    """Reproduce the *combined* pre-#1373 classification used across sites.

    - metric-value (selection._filter_is_metric_value) -> "metric_value"
    - exact "state"/"region"/"enrollment" field tokens -> those kinds
    - everything else (legacy "value", other structural/sort tokens) -> None
      (today these are NOT metric-value, NOT state/region/enrollment).
    """

    if _old_is_metric_value(spec):
        return "metric_value"
    field = spec.field.casefold().strip()
    if field == "state":
        return "state"
    if field == "region":
        return "region"
    if field == "enrollment":
        return "enrollment"
    return None


# ---------------------------------------------------------------------------
# Field forms covering every meaning ``field`` is overloaded with.
# ---------------------------------------------------------------------------

FIELD_FORMS: tuple[str, ...] = (
    # selection-phase structural tokens
    "state",
    "STATE",  # case/whitespace variant still classifies as state
    " state ",
    "region",
    "enrollment",
    # legacy count back-compat token
    "value",
    # other structural/sort tokens that are neither metric_value nor s/r/e
    "answer_value",
    "district",
    "district_name",
    "display_value",
    "metric_value",
    "name",
    # free-form metric phrases (catalog-resolved)
    "teacher workdays",
    "starting salary",
    "Who is eligible for paid parental leave?",
)


@pytest.mark.parametrize("field", FIELD_FORMS)
@pytest.mark.parametrize("operator", ALL_OPERATORS)
def test_filter_kind_matches_old_classification(
    field: str, operator: FilterOperator
) -> None:
    """filter_kind() == the pre-#1373 derived classification for every form."""

    spec = FilterSpec(field=field, operator=operator, value=1)
    assert filter_kind(spec) == _old_kind(spec)


def test_filter_kind_prefers_typed_kind_when_set() -> None:
    """A planner-set ``kind`` is trusted verbatim, overriding field derivation."""

    # A metric phrase the deriver would call metric_value, but typed as state.
    spec = FilterSpec(
        field="teacher workdays", operator="greater_than", value=1, kind="state"
    )
    assert filter_kind(spec) == "state"

    # The legacy "value" token (deriver -> None) typed explicitly as metric_value.
    spec2 = FilterSpec(field="value", operator="greater_than", value=1, kind="metric_value")
    assert filter_kind(spec2) == "metric_value"


def test_filter_kind_phrase_with_unsupported_operator_is_not_metric_value() -> None:
    """A free-form phrase under is_missing is NOT metric_value (matches today)."""

    spec = FilterSpec(field="teacher workdays", operator="is_missing")
    # Not metric_value, and not a structural s/r/e token -> None (unclassified).
    assert filter_kind(spec) is None


def test_filter_field_is_reserved_matches_legacy_reserved_set() -> None:
    """filter_field_is_reserved() reserves the structural selection-phase kinds
    {"state","region","enrollment"} plus the legacy "value" count token; only
    metric phrases stay catalog-checkable; a planner-set kind is honored
    (dual-read). region joined the reserved set in #1216 (see region case below)."""
    from compass_backend.execution.filters import filter_field_is_reserved

    def reserved(field, operator="equals", kind=None):
        return filter_field_is_reserved(
            FilterSpec(field=field, operator=operator, value="x", kind=kind)
        )

    # Reserved — the catalog will never resolve these:
    assert reserved("state") is True
    assert reserved("enrollment", operator="greater_than") is True
    assert reserved("value", operator="greater_than") is True
    # #1216 — a kind="region" filter's field token "region" is structural (the
    # region phrase rides in `value`, resolved by reference.regions, never the
    # catalog). Previously False, which made every region-filtered
    # lookup/count/existence plan block on "region" -> rescue.
    assert reserved("region", kind="region") is True
    # NOT reserved (stay catalog-checkable) — real metric phrases:
    assert reserved("teacher workdays", operator="greater_than") is False
    assert reserved("district") is False
    # Planner-set kind is honored: a metric phrase typed as state is reserved.
    assert reserved("teacher workdays", operator="greater_than", kind="state") is True
