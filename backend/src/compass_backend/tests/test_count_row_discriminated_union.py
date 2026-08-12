"""Tests for the CountRow discriminated-union contract.

The split into ThresholdCountRow / CoveredUniverseCountRow / CategoricalCountRow
turns silent runtime mishaps (`getattr(row, "count_kind", None)` returning the
wrong default; `category=None` on a categorical row) into compile- or
parse-time errors.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from compass_backend.artifacts import (
    COUNT_ROW_TYPES,
    CategoricalCountRow,
    CountRow,
    CoveredUniverseCountRow,
    ThresholdCountRow,
)


def _threshold() -> ThresholdCountRow:
    return ThresholdCountRow(
        metric_id=10,
        metric_name="Threshold metric",
        value=3,
        display_value="3 of 5 covered districts",
        academic_year="2024 - 2025",
        count=3,
        denominator=5,
        filter_statement="value >= 3",
        coverage_state="covered",
    )


def _covered_universe() -> CoveredUniverseCountRow:
    return CoveredUniverseCountRow(
        metric_id=0,
        metric_name="Covered district universe",
        value=10,
        display_value="10 covered districts",
        academic_year="2024 - 2025",
        count=10,
        denominator=10,
        filter_statement="covered district universe",
        source="coverage_state",
        coverage_state="covered",
    )


def _categorical() -> CategoricalCountRow:
    return CategoricalCountRow(
        metric_id=20,
        metric_name="Categorical metric",
        value="Yes",
        category="Yes",
        display_value="2 of 5 covered districts",
        academic_year="2024 - 2025",
        count=2,
        denominator=5,
        filter_statement="group by reviewed value",
        coverage_state="covered",
    )


# ─── Round-trip parsing ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "factory,expected_cls",
    [
        (_threshold, ThresholdCountRow),
        (_covered_universe, CoveredUniverseCountRow),
        (_categorical, CategoricalCountRow),
    ],
)
def test_count_row_round_trip_preserves_variant(factory, expected_cls) -> None:
    """JSON dump → TypeAdapter parse picks the correct count-row variant."""

    adapter = TypeAdapter(CountRow)
    original = factory()
    parsed = adapter.validate_python(original.model_dump())
    assert isinstance(parsed, expected_cls)
    assert parsed.count_kind == original.count_kind


def test_count_row_types_tuple_contains_every_variant() -> None:
    assert set(COUNT_ROW_TYPES) == {
        ThresholdCountRow,
        CoveredUniverseCountRow,
        CategoricalCountRow,
    }


# ─── Categorical variant requires `category` ─────────────────────────────────


def test_categorical_count_row_requires_category() -> None:
    """`category: str` is required on CategoricalCountRow — no None default.

    The pre-refactor flat `CountRow.category: str | None = None` accepted
    categorical rows without a category. The split makes that impossible.
    """

    with pytest.raises(ValidationError) as exc:
        CategoricalCountRow(
            metric_id=20,
            metric_name="Categorical metric",
            display_value="2 of 5 covered districts",
            academic_year="2024 - 2025",
            count=2,
            denominator=5,
            filter_statement="group by reviewed value",
            coverage_state="covered",
            # category intentionally omitted
        )
    assert "category" in str(exc.value).lower()


def test_categorical_count_row_rejects_empty_category() -> None:
    """`category` has `min_length=1` — empty string fails."""

    with pytest.raises(ValidationError):
        CategoricalCountRow(
            metric_id=20,
            metric_name="Categorical metric",
            category="",
            display_value="2 of 5 covered districts",
            academic_year="2024 - 2025",
            count=2,
            denominator=5,
            filter_statement="group by reviewed value",
            coverage_state="covered",
        )


# ─── Discriminator hardening ─────────────────────────────────────────────────


def test_count_row_rejects_unknown_count_kind() -> None:
    """An unknown `count_kind` value fails to parse via the discriminated union."""

    adapter = TypeAdapter(CountRow)
    payload = _threshold().model_dump()
    payload["count_kind"] = "made_up_kind"
    with pytest.raises(ValidationError) as exc:
        adapter.validate_python(payload)
    assert "count_kind" in str(exc.value)


def test_threshold_count_row_has_no_category_attribute() -> None:
    """`category` exists only on CategoricalCountRow — type narrowing works.

    Pyright catches misuses statically; this runtime check pins the model
    shape so a future regression that *adds* `category` to the base class
    surfaces here.
    """

    row = _threshold()
    assert not hasattr(row, "category")


def test_covered_universe_count_row_has_no_category_attribute() -> None:
    row = _covered_universe()
    assert not hasattr(row, "category")
