"""Tests for the planner source-field → reconciler role-hint mapping.

``role_hint_for_source_field`` is consumed live by
``compass_backend.planning.catalog_pipeline`` to tag each planner mention
with the role hint the ``CatalogReconciler`` uses for tie-breaking.
"""

from __future__ import annotations

import pytest

from compass_backend.recognition.reconcile_shadow import role_hint_for_source_field


@pytest.mark.parametrize(
    ("source_field", "expected_role_hint"),
    [
        ("selection.districts", "selection_district"),
        ("metrics.name", "metric_subject"),
        ("profile_fields.name", "profile_subject"),
        ("filters.field", "selection_filter"),
        ("sort.field", "sort_key"),
        ("sort_steps.field", "sort_key"),
        ("similarity.anchor_name", "peer_anchor"),
    ],
)
def test_role_hint_mapping_covers_every_source_field(
    source_field: str, expected_role_hint: str
) -> None:
    """Every planner-extraction source_field maps to a known role hint."""

    assert role_hint_for_source_field(source_field) == expected_role_hint


def test_role_hint_falls_through_to_unspecified() -> None:
    """Unknown source fields default to ``"unspecified"``.

    The reconciler treats this as "no tie-break preference among
    drawers" rather than crashing. Future planner slots should extend
    the mapping rather than rely on this default.
    """

    assert role_hint_for_source_field("future.new.slot") == "unspecified"
    assert role_hint_for_source_field(None) == "unspecified"
