"""Tests for the widened CatalogAdjudicationEntityType union.

Step 2 of the catalog-resolution-unification plan
(``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``).
The adjudicator's previous entity-type union was
``{district, metric, metric_bundle}`` — too narrow to represent picking
a ``profile_rank_field`` over a ``metric`` when the reconciler hands the
adjudicator both as candidates. This module pins:

- the contract accepts every type
  :data:`compass_backend.contracts.catalog_resolution.ResolvedEntityType`
  may return;
- the validator does not reject decisions that select the new types;
- the two Literal unions stay in sync (drift between them is a bug).
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from compass_backend.catalog.adjudication import (
    CatalogAdjudicationCandidate,
    CatalogAdjudicationDecision,
    CatalogAdjudicationEntityType,
    validate_catalog_adjudication_decision,
)
from compass_backend.contracts.catalog_resolution import ResolvedEntityType


def test_adjudicator_entity_type_union_matches_resolved_entity_type() -> None:
    """The two unions must be structurally identical.

    The adjudicator and the reconciler operate on the same candidate
    universe; any drift between their type literals means the
    adjudicator can't represent a candidate the reconciler produced
    (or vice versa). The sync check here is the lightweight form of
    the seed-vs-migration sync tests elsewhere in the codebase.
    """

    assert set(get_args(CatalogAdjudicationEntityType)) == set(
        get_args(ResolvedEntityType)
    )


@pytest.mark.parametrize(
    "entity_type",
    [
        "metric",
        "metric_bundle",
        "profile_rank_field",
        "profile_field",
        "district",
        "unsupported_concept",
        "peer_set",
        "topic",
    ],
)
def test_candidate_accepts_every_widened_entity_type(entity_type: str) -> None:
    """Every widened type must round-trip through the Pydantic contract."""

    candidate = CatalogAdjudicationCandidate(
        entity_type=entity_type,  # type: ignore[arg-type]
        candidate_id=f"{entity_type}:test",
        label=f"test {entity_type} candidate",
    )
    assert candidate.entity_type == entity_type


def test_candidate_rejects_unknown_entity_type() -> None:
    """Sanity check: the widening did not turn the Literal into ``str``.

    A typo like ``"profile_rankfield"`` (missing underscore) must still
    fail Pydantic validation — otherwise the contract gives no useful
    guarantee.
    """

    with pytest.raises(ValidationError):
        CatalogAdjudicationCandidate(
            entity_type="profile_rankfield",  # type: ignore[arg-type]
            candidate_id="profile_rank_field:enrollment",
            label="NCES enrollment",
        )


def test_validator_accepts_select_one_on_profile_rank_field() -> None:
    """SORT-NEW-02 / case 8 closure path.

    Mirrors what the reconciler will hand the adjudicator on the
    "rank by enrollment" turn: both a ``profile_rank_field`` and a
    ``profile_field`` candidate for the same NCES field. The
    adjudicator picks ``profile_rank_field`` for the sort intent.
    """

    candidates = (
        CatalogAdjudicationCandidate(
            entity_type="profile_rank_field",
            candidate_id="profile_rank_field:enrollment",
            label="NCES enrollment",
        ),
        CatalogAdjudicationCandidate(
            entity_type="profile_field",
            candidate_id="profile_field:enrollment",
            label="NCES enrollment",
        ),
    )
    decision = CatalogAdjudicationDecision(
        action="select_one",
        selected_ids=["profile_rank_field:enrollment"],
        confidence=0.95,
        rationale=(
            "User asked to rank districts by enrollment; profile_rank_field "
            "is the rankable drawer for this NCES field."
        ),
    )

    validated = validate_catalog_adjudication_decision(decision, candidates)
    assert validated.selected_ids == ["profile_rank_field:enrollment"]


def test_validator_accepts_no_match_on_unsupported_concept_only() -> None:
    """When the only candidate is an out-of-universe alias, the
    adjudicator emits ``no_match`` (the prompt instructs this; the
    contract has to allow it)."""

    candidates = (
        CatalogAdjudicationCandidate(
            entity_type="unsupported_concept",
            candidate_id="unsupported_concept:union_release_time",
            label="Union release time",
        ),
    )
    decision = CatalogAdjudicationDecision(
        action="no_match",
        selected_ids=[],
        confidence=1.0,
        rationale=(
            "Phrase resolves to a governed unsupported_concept; upstream "
            "catalog has already flagged it as outside the universe."
        ),
    )

    validated = validate_catalog_adjudication_decision(decision, candidates)
    assert validated.action == "no_match"


def test_validator_still_rejects_invented_ids_after_widening() -> None:
    """Widening did not loosen the core invariant.

    The validator must still reject ``selected_ids`` that aren't in the
    provided candidate list — this is the guard that stops the model
    from inventing catalog references.
    """

    candidates = (
        CatalogAdjudicationCandidate(
            entity_type="profile_rank_field",
            candidate_id="profile_rank_field:enrollment",
            label="NCES enrollment",
        ),
    )
    decision = CatalogAdjudicationDecision(
        action="select_one",
        selected_ids=["profile_rank_field:district_count"],  # not provided
        confidence=0.9,
        rationale="Model attempted to invent a rank field.",
    )

    with pytest.raises(Exception):  # ModelRetry, surfaced by Pydantic AI
        validate_catalog_adjudication_decision(decision, candidates)
