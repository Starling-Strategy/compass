"""Governed peer-scoring policy for deterministic NCES-style peer comparison.

Peer-scoring weights, locale ordering, and formula constants were previously
hardcoded as module-level dicts in `catalog/peers.py`. This module lifts them
into a typed, versioned `PeerScoringPolicy` Pydantic model. The same values
are seeded to `compass.peer_scoring_policies` (migration 034) so they live
alongside `compass.catalog_aliases`, `compass.renderer_notes`, and
`compass.district_normalization_rules` as governance-visible authority.

Versioning matters here more than for any other governed surface so far:
peer scores are user-facing claims with weights that may legitimately evolve.
The `policy_version` field is surfaced through `PeerSetResolution` and the
peer-comparison methodology line so every peer answer discloses which
weighted policy produced the ranking.

`DEFAULT_PEER_SCORING_POLICY` is the runtime authority for now; the
migration table is the auditable governance surface. The
`test_peer_scoring_policy_in_sync_with_migration` test asserts they
cannot drift.

Doctrine: per CLAUDE.md and `docs/plans/2026-05-06-compass-repo-end-state.md`
("peer-scoring weights need explicit reviewed policy/version metadata"),
hardcoded weights are a `deletable` boundary leak. This module + the migration
close that leak.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .district_normalization import CatalogIntegrityError


class PeerScoringFormulas(BaseModel):
    """Tunable constants used by deterministic peer factor scoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    geo_decay_half_life_km: float = Field(gt=0.0)
    locale_category_penalty: float = Field(ge=0.0, le=1.0)
    locale_size_penalty: float = Field(ge=0.0, le=1.0)
    km_per_mile: float = Field(gt=0.0)
    earth_radius_km: float = Field(gt=0.0)


class PeerScoringPolicy(BaseModel):
    """One versioned reviewed peer-scoring policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = Field(min_length=1)
    weights: dict[str, float]
    locale_categories: dict[str, int]
    locale_sizes: dict[str, int]
    formulas: PeerScoringFormulas
    review_status: Literal["approved", "candidate", "rejected"] = "approved"
    active: bool = True
    notes: str = ""

    @model_validator(mode="after")
    def _validate_weights_sum_to_one(self) -> "PeerScoringPolicy":
        if not self.weights:
            raise ValueError("weights must be non-empty")
        total = sum(self.weights.values())
        # Allow tiny floating-point drift; the v1 seed sums to exactly 1.0.
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"weights must sum to 1.0 (got {total:.6f})"
            )
        for name, weight in self.weights.items():
            if weight < 0.0:
                raise ValueError(
                    f"weight {name!r} must be non-negative (got {weight})"
                )
        if not self.locale_categories:
            raise ValueError("locale_categories must be non-empty")
        if not self.locale_sizes:
            raise ValueError("locale_sizes must be non-empty")
        return self


# v2 policy makes peer comparison demography-first for M2.
# Bumping the policy goes through a new row + new policy_version string;
# the in-code default and the seed should both reflect the active policy.
DEFAULT_PEER_SCORING_POLICY = PeerScoringPolicy(
    policy_version="nces-similarity-v2-demography-2026-05",
    weights={
        "enrollment_size": 0.35,
        "locale_type": 0.25,
        "frpl_percentage": 0.15,
        "pupil_teacher_ratio": 0.08,
        "revenue_per_pupil": 0.06,
        "expenditure_per_pupil": 0.06,
        "geographic_proximity": 0.03,
        "same_state": 0.02,
    },
    locale_categories={"City": 0, "Suburb": 1, "Town": 2, "Rural": 3},
    locale_sizes={
        "Large": 0,
        "Fringe": 0,
        "Mid-size": 1,
        "Midsize": 1,
        "Distant": 1,
        "Small": 2,
        "Remote": 2,
    },
    formulas=PeerScoringFormulas(
        geo_decay_half_life_km=500.0,
        locale_category_penalty=0.25,
        locale_size_penalty=0.10,
        km_per_mile=1.60934,
        earth_radius_km=6371.0,
    ),
    notes=(
        "M2 demography-first policy: enrollment, urbanicity, and FRPL are "
        "primary; geography and same-state are secondary."
    ),
)


# ---------------------------------------------------------------------------
# Feature-set weight derivation (PR 2B — similarity operation)
# ---------------------------------------------------------------------------

_FEATURE_SET_INCLUDED_WEIGHTS: dict[str, frozenset[str]] = {
    "all": frozenset(
        {
            "geographic_proximity",
            "enrollment_size",
            "locale_type",
            "revenue_per_pupil",
            "pupil_teacher_ratio",
            "expenditure_per_pupil",
            "frpl_percentage",
            "same_state",
        }
    ),
    # Scale-respecting enrollment focus: include fiscal and staffing factors
    # that co-vary with district size.  Geographic proximity and state
    # membership are excluded so the peer set crosses region boundaries.
    "enrollment": frozenset(
        {
            "enrollment_size",
            "revenue_per_pupil",
            "pupil_teacher_ratio",
            "expenditure_per_pupil",
        }
    ),
    # FRPL focus always co-weights enrollment_size so that a 10,000-student
    # district matched to a 1M-student district on FRPL share alone is
    # avoided.  "Scale-respecting feature focus."
    "frpl": frozenset(
        {
            "frpl_percentage",
            "enrollment_size",
        }
    ),
    # Locale focus includes enrollment to avoid scale mismatch.
    "locale": frozenset(
        {
            "locale_type",
            "enrollment_size",
        }
    ),
}


def effective_weights(
    policy: PeerScoringPolicy,
    feature_set: str,
) -> dict[str, float]:
    """Return renormalized weights for the active policy restricted to ``feature_set``.

    Factors not in the included set are zeroed out and the remaining weights
    are renormalized to sum to 1.0 so the weighted sum in ``score_peer_similarity``
    is correct without modification.

    ``feature_set='all'`` returns the full policy weights unchanged (no copy).

    The ``_FEATURE_SET_INCLUDED_WEIGHTS`` map must cover every factor in the
    governed policy; ``test_effective_weights_feature_sets_intersect_active_policy_weights``
    guards this constraint so drift is caught in CI.
    """

    if feature_set == "all":
        return dict(policy.weights)

    included = _FEATURE_SET_INCLUDED_WEIGHTS.get(feature_set)
    if included is None:
        # Unknown feature_set — fall back to full weights rather than silently
        # breaking scoring.  Callers validate feature_set at the contract layer.
        return dict(policy.weights)

    raw = {k: v for k, v in policy.weights.items() if k in included}
    total = sum(raw.values())
    if total <= 0:
        # Degenerate case: no computable weight — return full policy weights.
        return dict(policy.weights)
    return {k: v / total for k, v in raw.items()}


def active_peer_scoring_policy() -> PeerScoringPolicy:
    """Return the governed active peer scoring policy.

    Today this returns the in-code default. A future PR can switch the
    runtime to load from `compass.peer_scoring_policies` at startup; the
    sync-with-migration test guarantees the in-code and DB rows stay in
    lockstep, so the swap will be a one-line wiring change.
    """

    if not DEFAULT_PEER_SCORING_POLICY.active:
        raise CatalogIntegrityError(
            "no active peer scoring policy available"
        )
    return DEFAULT_PEER_SCORING_POLICY
