"""Pins for the #1419 single-source helper extractions (Milestone 14).

Two byte-identical twin pairs were consolidated into one source each:

* ``execution.count._filter_statement`` / ``_operator_symbol`` and their
  ``quality._validation_helpers`` twins now live as the PUBLIC
  ``execution.filters.filter_statement`` / ``operator_symbol``.
* ``rendering.writer`` / ``rendering.composer``'s private ``_selection_label``
  and ``_sort_metric_name`` now live as ``rendering.shared.selection_label`` /
  ``sort_metric_name``.

These tests pin the exact behavior the twins had, so the consolidation is a
provable zero-behavior-change refactor.
"""

from __future__ import annotations

from types import SimpleNamespace

from compass_backend.contracts.planning import (
    FilterSpec,
    MetricSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.execution.filters import filter_statement, operator_symbol
from compass_backend.rendering.shared import selection_label, sort_metric_name


# ─── Task A: operator_symbol ─────────────────────────────────────────────────


def test_operator_symbol_maps_every_known_operator() -> None:
    assert operator_symbol("equals") == "="
    assert operator_symbol("not_equals") == "!="
    assert operator_symbol("greater_than") == ">"
    assert operator_symbol("greater_than_or_equal") == ">="
    assert operator_symbol("less_than") == "<"
    assert operator_symbol("less_than_or_equal") == "<="


def test_operator_symbol_passes_unmapped_operator_through() -> None:
    # The dict uses ``.get(operator, operator)`` — an unknown token returns
    # itself rather than raising. This preserves the twins' fallback behavior.
    assert operator_symbol("between") == "between"


# ─── Task A: filter_statement ────────────────────────────────────────────────


def test_filter_statement_empty_returns_reviewed_current_value() -> None:
    assert filter_statement([]) == "reviewed current value"


def test_filter_statement_single_metric_value_filter() -> None:
    filters = [
        FilterSpec(field="teacher workdays", operator="greater_than", value=190)
    ]
    assert filter_statement(filters) == "value > 190"


def test_filter_statement_joins_two_filters_with_and() -> None:
    filters = [
        FilterSpec(field="teacher workdays", operator="greater_than", value=190),
        FilterSpec(field="teacher salary", operator="less_than", value=50000),
    ]
    assert filter_statement(filters) == "value > 190 and value < 50000"


def test_filter_statement_skips_typed_state_kind_filter() -> None:
    # A state filter expressed via the TYPED kind on a non-"state" field token
    # is classified by ``filter_kind`` and excluded from the rendered
    # denominator statement — only the metric threshold survives.
    filters = [
        FilterSpec(field="home state", operator="equals", value="CA", kind="state"),
        FilterSpec(field="teacher workdays", operator="greater_than", value=190),
    ]
    rendered = filter_statement(filters)
    assert rendered == "value > 190"
    assert "home state" not in rendered


# ─── Task B: selection_label ─────────────────────────────────────────────────


_RANK_METRIC = MetricSpec(name="Average teacher starting salary")


def test_selection_label_named_districts_reads_selected_districts() -> None:
    plan = QueryPlan(
        operation="rank",
        question="rank districts",
        selection=SelectionSpec(
            scope="named_districts", districts=["Denver Public Schools"]
        ),
        metrics=[_RANK_METRIC],
    )
    assert selection_label(plan) == "selected districts"


def test_selection_label_largest_districts_reads_selected_districts() -> None:
    plan = QueryPlan(
        operation="rank",
        question="rank districts",
        selection=SelectionSpec(scope="largest_districts"),
        metrics=[_RANK_METRIC],
    )
    assert selection_label(plan) == "selected districts"


def test_selection_label_no_state_reads_covered_districts() -> None:
    plan = QueryPlan(
        operation="rank",
        question="rank districts",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[_RANK_METRIC],
    )
    assert selection_label(plan) == "covered districts"


def test_selection_label_with_states_lists_them() -> None:
    plan = QueryPlan(
        operation="rank",
        question="rank districts in California",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[_RANK_METRIC],
    )
    assert selection_label(plan) == "covered districts in CA"


# ─── Task B: sort_metric_name ────────────────────────────────────────────────


def test_sort_metric_name_returns_first_non_empty_row_value() -> None:
    result = SimpleNamespace(
        rows=[
            SimpleNamespace(sort_metric_name=None),
            SimpleNamespace(sort_metric_name="Teacher workdays"),
        ]
    )
    assert sort_metric_name(result) == "Teacher workdays"


def test_sort_metric_name_returns_none_when_no_row_carries_a_value() -> None:
    result = SimpleNamespace(rows=[SimpleNamespace(sort_metric_name=None)])
    assert sort_metric_name(result) is None


def test_sort_metric_name_tolerates_row_without_attribute() -> None:
    # composer's defensive ``getattr(row, "sort_metric_name", None)`` body is the
    # shared one, so a row lacking the attribute is treated as no value.
    result = SimpleNamespace(rows=[SimpleNamespace()])
    assert sort_metric_name(result) is None
