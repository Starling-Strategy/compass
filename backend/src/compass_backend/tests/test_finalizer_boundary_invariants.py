"""Finalizer-boundary invariant guards (Refs #1315).

``finalize_plan`` canonicalizes the planner's draft: it folds a top-level
``plan.sort`` into a ``presentation``-phase ``SortStepSpec`` and clears
``plan.sort = None``; execution/quality then read the canonical form through the
``execution.selection`` accessors. These guards pin the *ends* invariant
end-to-end — draft intent -> finalize -> accessor — in ONE place, so a future
stale read of the emptied ``plan.sort`` trips a test instead of silently
reverting to a default. That silent revert is exactly the failure mode behind
the S41/S107 miss ("ranked lowest first" rendered "highest to lowest") and the
lookup-sort cluster ("sort by value" rendered alphabetical-by-district).

This module is the home for new finalizer-boundary cases: add a row to
``SORT_INTENT_CASES`` whenever a new surface reads a finalizer-moved field.

A brittle source-scan ("no raw ``plan.sort`` read in execution/quality") was
considered and rejected: many legitimate readers guard *both* representations
(``plan.sort is None and not plan.sort_steps``) or read ``plan.sort`` as the
pre-finalize draft branch, so a static ban produces false positives. The
behavioral round-trip below is the robust guard; the forward rule for new code
lives in ``src/compass_backend/execution/AGENTS.md``.
"""

from __future__ import annotations

import pytest

from compass_backend.contracts.catalog_resolution import CatalogReconciliationReport
from compass_backend.contracts.planning import (
    MetricSpec,
    QueryPlan,
    SelectionSpec,
    SortSpec,
)
from compass_backend.execution.selection import direction, sort_kind
from compass_backend.planning.finalizer import finalize_plan


def _finalize(draft: QueryPlan) -> QueryPlan:
    return finalize_plan(draft, CatalogReconciliationReport()).plan


def _lookup(metrics: list[str], sort_field: str, sort_dir: str) -> QueryPlan:
    return QueryPlan(
        question="finalizer-boundary invariant fixture",
        operation="lookup",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name=name) for name in metrics],
        sort=SortSpec(field=sort_field, direction=sort_dir),
    )


def _ranked_compare(sort_dir: str) -> QueryPlan:
    return QueryPlan(
        question="finalizer-boundary invariant fixture",
        operation="rank",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["San Bernardino", "Capistrano", "Los Angeles"],
        ),
        metrics=[MetricSpec(name="starting teacher salary")],
        sort=SortSpec(field="starting teacher salary", direction=sort_dir),
    )


# (label, draft, expected direction(), expected sort_kind())
SORT_INTENT_CASES = [
    ("lookup_value_desc", _lookup(["starting teacher salary", "class size"], "starting teacher salary", "desc"), "desc", "value"),
    ("lookup_value_asc", _lookup(["starting teacher salary"], "starting teacher salary", "asc"), "asc", "value"),
    ("lookup_district_desc", _lookup(["starting teacher salary"], "district", "desc"), "desc", "district"),
    ("rank_compare_lowest_first", _ranked_compare("asc"), "asc", "value"),
    ("rank_compare_highest_first", _ranked_compare("desc"), "desc", "value"),
]


@pytest.mark.parametrize(
    "label,draft,expected_direction,expected_sort_kind",
    SORT_INTENT_CASES,
    ids=[case[0] for case in SORT_INTENT_CASES],
)
def test_sort_intent_survives_finalize(
    label: str,
    draft: QueryPlan,
    expected_direction: str,
    expected_sort_kind: str,
) -> None:
    """The draft's sort intent must survive the finalizer on the canonical
    accessors — never silently revert to the conventional default."""

    # Precondition: the finalizer always empties plan.sort. This is the fact
    # that makes a stale `plan.sort` read dangerous; pin it so the guard's
    # premise can't quietly change.
    finalized = _finalize(draft)
    assert finalized.sort is None
    assert finalized.sort_steps, "the folded sort must land in sort_steps"

    assert direction(finalized) == expected_direction
    assert sort_kind(finalized) == expected_sort_kind


def test_no_sort_finalizes_to_district_default_not_a_phantom_direction() -> None:
    """A plan with no requested sort still clears to the conventional district
    order — the default is intentional here, not a lost signal."""

    draft = QueryPlan(
        question="finalizer-boundary invariant fixture",
        operation="lookup",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="starting teacher salary")],
    )
    finalized = _finalize(draft)
    assert finalized.sort is None
    assert sort_kind(finalized) == "district"
