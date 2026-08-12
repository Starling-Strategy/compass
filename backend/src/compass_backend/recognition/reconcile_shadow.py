"""Planner source-field → reconciler role-hint mapping.

Maps the typed-extraction ``source_field`` slot a phrase lived in to the
:class:`~compass_backend.catalog.reconciliation.CatalogReconciler`'s
role-hint union. Consumed live by
:mod:`compass_backend.planning.catalog_pipeline` when it builds
``PhraseToReconcile`` requests from planner mentions.
"""

from __future__ import annotations

# Mapping from the planner's typed-extraction source fields to the
# reconciler's role-hint union. Deterministic — no judgment calls; the
# source_field literal is the planner-draft slot the phrase lived in,
# and the role hint is the reconciler's name for the same role.
_ROLE_HINT_BY_SOURCE_FIELD: dict[str, str] = {
    "selection.districts": "selection_district",
    "metrics.name": "metric_subject",
    "profile_fields.name": "profile_subject",
    "filters.field": "selection_filter",
    "sort.field": "sort_key",
    "sort_steps.field": "sort_key",
    "similarity.anchor_name": "peer_anchor",
}


def role_hint_for_source_field(source_field: str | None) -> str:
    """Return the :data:`PhraseRoleHint` value for a planner source field.

    Unknown or missing source fields fall through to ``"unspecified"``;
    the reconciler treats this as "no tie-break preference among
    drawers" rather than crashing. New planner slots added in the
    future should extend :data:`_ROLE_HINT_BY_SOURCE_FIELD` rather than
    relying on the default.
    """

    if source_field is None:
        return "unspecified"
    return _ROLE_HINT_BY_SOURCE_FIELD.get(source_field, "unspecified")
