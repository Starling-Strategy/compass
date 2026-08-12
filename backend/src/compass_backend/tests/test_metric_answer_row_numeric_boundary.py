"""Characterize the parse-once boundary on ``MetricAnswerRow.numeric_value``.

A metric row's stringly-typed ``value`` (a rendered cell like ``"45,602"`` or
``"12.5%"``) is parsed exactly ONCE, at row construction, into the typed
``numeric_value`` field — via the single string->number authority
``execution._text_utils.parse_compass_numeric_value``. Every numeric consumer
(filter / count / ranking / lookup-sort / trend) reads that typed field instead
of re-parsing ``value``; before this boundary each re-parsed independently and a
bare ``float("45,602")`` raised the ValueError that 500'd the turn (#1772).

This test pins the boundary contract: for a battery of representative cell
shapes, ``MetricAnswerRow(value=X).numeric_value`` equals
``parse_compass_numeric_value(X)``. If the validator ever diverges from the
authority, this fails.
"""

from __future__ import annotations

import pytest

from compass_backend.db.rows import MetricAnswerRow
from compass_backend.execution._text_utils import parse_compass_numeric_value

# Representative metric-cell shapes: comma-grouped integer, currency with cents,
# percent, capped/"at-least" display ("100+"), an affirmative categorical (no
# number), empty, missing, and a negative.
_VALUE_BATTERY: list[str | None] = [
    "45,602",
    "$1,234.56",
    "12.5%",
    "100+",
    "Yes",
    "",
    None,
    "-5",
]


def _row(value: str | int | float | None) -> MetricAnswerRow:
    return MetricAnswerRow(
        district_id=1,
        district_name="Test District",
        metric_id=42,
        metric_name="Test metric",
        value=value,
        academic_year="2024-25",
    )


@pytest.mark.parametrize("value", _VALUE_BATTERY)
def test_numeric_value_matches_canonical_parser(value: str | None) -> None:
    """The boundary field equals the single authority for every battery input."""

    row = _row(value)
    assert row.numeric_value == parse_compass_numeric_value(value)


def test_comma_currency_does_not_raise_at_construction() -> None:
    """A comma-formatted currency cell parses to a float, never a ValueError.

    This is the #1772 crash shape: a bare ``float("45,602")`` raises; the row
    boundary parses it once into 45602.0 and consumers read that.
    """

    assert _row("45,602").numeric_value == 45602.0


def test_explicit_numeric_value_is_not_overwritten() -> None:
    """An explicitly supplied numeric_value is preserved (validator only fills None)."""

    row = MetricAnswerRow(
        district_id=1,
        district_name="Test District",
        metric_id=42,
        metric_name="Test metric",
        value="45,602",
        numeric_value=999.0,
        academic_year="2024-25",
    )
    assert row.numeric_value == 999.0


def test_numeric_value_survives_model_dump_round_trip() -> None:
    """Serialize -> reconstruct preserves the parsed numeric (no silent re-derive drift)."""

    row = _row("12.5%")
    rebuilt = MetricAnswerRow.model_validate(row.model_dump())
    assert rebuilt.numeric_value == row.numeric_value == 12.5
