"""Single-source classification of a ``FilterSpec``'s kind (#1373).

``FilterSpec.field`` is a ``str`` overloaded with four meanings — the
selection-phase tokens ``"state"``/``"region"``/``"enrollment"``, the legacy
``"value"`` count token, and an otherwise free-form metric phrase resolved
against the catalog. Before #1373 each consumer re-derived that meaning with
its own string parsing scattered across execution/{selection,operations,
count}.py, planning/finalizer.py, and quality/_validation_helpers.py.

:func:`filter_kind` is the one place that decision is made. It prefers the
typed :attr:`FilterSpec.kind` when the planner set it (dual-read, mirroring
PR #1374's ``AnchorValueSpec.state``) and otherwise derives the kind from
``field`` reproducing the old logic exactly. Routing every former check
through it means there is a single definition of "is this a metric-value
filter / a state filter / …" to reason about and to migrate when the planner
starts emitting ``kind`` directly.

Stage 2 removed the runtime-rewritten ``"metric:<id>"`` field token (#1373).
A resolved metric-value filter now keeps its free-form metric phrase in
``field`` and is still classified ``"metric_value"`` by the operator gate
below; the metric_id it resolved to is carried out-of-band on
:class:`ResolvedMetricFilter`, never re-parsed from a field string.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from compass_backend.contracts.planning import FilterKind, FilterOperator, FilterSpec

# These two sets are an internal detail of the derivation. They are SHARED with
# the SORT axis (a separate #1373 item), so they stay defined in selection.py;
# do not refactor the sort tokens/logic here (scope guard).
from .selection import _METRIC_VALUE_FILTER_OPERATORS, _STRUCTURAL_FILTER_FIELDS


@dataclasses.dataclass(frozen=True)
class ResolvedMetricFilter:
    """A threshold filter resolved to a concrete metric_id by the catalog.

    Lives in the filters module (not operations.py) so the lower count layer
    can read ``metric_id`` for per-metric row scoping without an import cycle —
    Stage 2's replacement for the old ``"metric:<id>"`` field token (#1373).
    """

    metric_id: int
    operator: FilterOperator
    value: int | float | str | bool | list[str]
    value_kind: str = "numeric"
    exclude_district_ids: frozenset[int] = frozenset()


def filter_kind(spec: FilterSpec) -> FilterKind | None:
    """Return the typed kind of ``spec`` — the single classification source.

    Resolution order:

    1. The planner-set :attr:`FilterSpec.kind` if present (trusted verbatim,
       so the planner can own the decision once it emits ``kind`` directly).
    2. Otherwise the kind DERIVED from ``field``, reproducing the pre-#1373
       scattered logic exactly:

       - A non-structural ``field`` carried by a metric-value operator is
         ``"metric_value"`` (this is exactly ``selection._filter_is_metric_value``).
         This covers both an unresolved metric phrase and one already resolved
         (Stage 2 keeps the phrase free-form rather than rewriting it).
       - The exact field tokens ``"state"`` / ``"region"`` / ``"enrollment"``
         are their own kinds (the selection-phase constraints).
       - Everything else — the legacy ``"value"`` token and the other
         structural/sort tokens (``district``, ``answer_value``, …) — returns
         ``None``: today these are neither metric-value nor a selection-phase
         constraint, and each call site treats them accordingly.

    ``None`` therefore means "no typed filter kind applies" — never confuse it
    with ``"metric_value"``.
    """

    if spec.kind is not None:
        return spec.kind

    field = spec.field.casefold().strip()

    # 1. A non-structural free-form metric phrase under a metric-value operator
    #    -> metric_value. Mirrors _filter_is_metric_value.
    if (
        field not in _STRUCTURAL_FILTER_FIELDS
        and spec.operator in _METRIC_VALUE_FILTER_OPERATORS
    ):
        return "metric_value"

    # 2. Exact selection-phase tokens.
    if field == "state":
        return "state"
    if field == "region":
        return "region"
    if field == "enrollment":
        return "enrollment"

    # 3. Legacy "value" + other structural/sort tokens: not a typed filter kind.
    return None


def operator_symbol(operator: str) -> str:
    """Map a :class:`FilterOperator` token to its rendered comparison symbol.

    Unknown operators pass through unchanged (the ``.get`` fallback). Single
    source for the count renderer and its validator twin (#1419).
    """

    return {
        "equals": "=",
        "not_equals": "!=",
        "greater_than": ">",
        "greater_than_or_equal": ">=",
        "less_than": "<",
        "less_than_or_equal": "<=",
    }.get(operator, operator)


def filter_statement(filters: Iterable[FilterSpec]) -> str:
    """Render the human-readable filter clause for a count denominator.

    State filters are skipped via the single-source :func:`filter_kind`
    classification (#1373) — a planner-set ``kind='state'`` on a non-"state"
    field token is excluded here just as it is from the count itself. The
    remaining metric-value filters render as ``value <op> <value>`` joined by
    " and ". Returns ``"reviewed current value"`` when nothing remains.

    This is the single source the count renderer
    (:func:`execution.count`) and the quality validator twin
    (:func:`quality._validation_helpers._expected_count_filter_statement`) both
    call, so the validator never trips a false ``filter_statement_mismatch``
    (#1419).
    """

    statements = []
    for filter_spec in filters:
        if filter_kind(filter_spec) == "state":
            continue
        statements.append(
            f"value {operator_symbol(filter_spec.operator)} {filter_spec.value}"
        )
    return " and ".join(statements) if statements else "reviewed current value"


_LEGACY_RESERVED_FILTER_FIELD = "value"


def filter_field_is_reserved(spec: FilterSpec) -> bool:
    """Return True for filter fields the catalog will never resolve.

    The selection-phase ``state``/``region``/``enrollment`` kinds plus the legacy
    ``"value"`` count token; only metric-value filter phrases stay
    catalog-checkable. This is the single source for the reserved-set that the
    finalizer reconciler and recognition extraction each used to duplicate.

    ``region`` belongs here (#1216): a ``kind="region"`` filter's ``field`` token
    is the literal word ``"region"`` (the region phrase rides in ``value`` and is
    resolved by ``reference.regions.expand_state_or_region_values``, not the
    catalog). Treating it as catalog-checkable made every region-filtered
    ``lookup``/``count``/``existence`` plan raise an ``UnresolvedPhraseBlocker``
    on ``"region"`` -> ``ModelRetry`` -> rescue, while ``rank`` (which expresses
    the same region via ``selection.states``) slipped through — the
    "Do any districts in the South offer differentiated pay?" rescue.
    ``state_filters_are_supported`` already classifies region as structural.
    """

    if filter_kind(spec) in {"state", "region", "enrollment"}:
        return True
    return spec.field == _LEGACY_RESERVED_FILTER_FIELD
