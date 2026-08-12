"""Tests for the governed peer-scoring policy."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from compass_backend.artifacts import MethodologyRef
from compass_backend.catalog.district_normalization import CatalogIntegrityError
from compass_backend.catalog.peer_scoring import (
    DEFAULT_PEER_SCORING_POLICY,
    PeerScoringFormulas,
    PeerScoringPolicy,
    _FEATURE_SET_INCLUDED_WEIGHTS,
    active_peer_scoring_policy,
    effective_weights,
)
from compass_backend.rendering.methodology import methodology_line_for


SCHEMA_MIGRATION_PATH = Path(
    "src/compass_data_sync/migrations/034_peer_scoring_policies.sql"
)
ACTIVE_POLICY_MIGRATION_PATH = Path(
    "src/compass_data_sync/migrations/058_m2_demography_peer_scoring_policy.sql"
)


# ---------------------------------------------------------------------------
# Sync-with-migration: in-code default must mirror the active policy seed.
# ---------------------------------------------------------------------------


def _extract_seed_jsonb(sql: str, field_marker: str) -> dict:
    """Pull one JSONB literal from migration 034's INSERT statement.

    The INSERT uses ::jsonb cast literals — find each one in order
    relative to its position in the VALUES (...) tuple.
    """

    # Match `'... json ...'::jsonb` non-greedily after the field_marker.
    pattern = re.compile(
        re.escape(field_marker) + r"\s*(\{[\s\S]*?\})\s*'::jsonb",
        re.MULTILINE,
    )
    match = pattern.search(sql)
    assert match is not None, f"could not find JSONB literal after {field_marker!r}"
    return json.loads(match.group(1))


def _parse_seeded_policy() -> PeerScoringPolicy:
    sql = ACTIVE_POLICY_MIGRATION_PATH.read_text()
    # Locate the version-text literal: 'nces-similarity-...'
    version_match = re.search(r"'(nces-similarity-[^']+)'", sql)
    assert version_match is not None
    policy_version = version_match.group(1)

    # Each ::jsonb literal starts with a leading single-quote we use as anchor.
    # We scan in order: weights, locale_categories, locale_sizes, formulas.
    jsonb_pattern = re.compile(r"'(\{[\s\S]*?\})'::jsonb")
    jsonb_blobs = [json.loads(m.group(1)) for m in jsonb_pattern.finditer(sql)]
    assert len(jsonb_blobs) == 4, (
        f"expected 4 ::jsonb literals in seed, found {len(jsonb_blobs)}"
    )
    weights, locale_categories, locale_sizes, formulas_raw = jsonb_blobs

    return PeerScoringPolicy(
        policy_version=policy_version,
        weights=weights,
        locale_categories=locale_categories,
        locale_sizes=locale_sizes,
        formulas=PeerScoringFormulas(**formulas_raw),
    )


def test_peer_scoring_policy_in_sync_with_migration() -> None:
    """In-code default must match the active migration seed exactly."""

    in_code = DEFAULT_PEER_SCORING_POLICY
    in_sql = _parse_seeded_policy()
    assert in_code.policy_version == in_sql.policy_version
    assert in_code.weights == in_sql.weights
    assert in_code.locale_categories == in_sql.locale_categories
    assert in_code.locale_sizes == in_sql.locale_sizes
    assert in_code.formulas == in_sql.formulas


def test_peer_scoring_policy_migration_declares_governed_schema() -> None:
    """Migration 034 must declare the table + check constraint + one-active index."""

    sql = SCHEMA_MIGRATION_PATH.read_text()
    assert "CREATE TABLE IF NOT EXISTS compass.peer_scoring_policies" in sql
    assert "policy_version TEXT NOT NULL UNIQUE" in sql
    assert "weights JSONB NOT NULL" in sql
    assert "locale_categories JSONB NOT NULL" in sql
    assert "locale_sizes JSONB NOT NULL" in sql
    assert "formulas JSONB NOT NULL" in sql
    assert "review_status TEXT NOT NULL DEFAULT 'approved'" in sql
    assert "active BOOLEAN NOT NULL DEFAULT TRUE" in sql
    assert "peer_scoring_policies_review_status_check" in sql
    assert "idx_peer_scoring_policies_one_active" in sql


# ---------------------------------------------------------------------------
# Validation: weights must sum to 1; locale dicts must be non-empty.
# ---------------------------------------------------------------------------


def _policy_kwargs(**overrides):
    """Helper that returns kwargs equivalent to DEFAULT_PEER_SCORING_POLICY."""

    base = dict(
        policy_version=DEFAULT_PEER_SCORING_POLICY.policy_version,
        weights=dict(DEFAULT_PEER_SCORING_POLICY.weights),
        locale_categories=dict(DEFAULT_PEER_SCORING_POLICY.locale_categories),
        locale_sizes=dict(DEFAULT_PEER_SCORING_POLICY.locale_sizes),
        formulas=DEFAULT_PEER_SCORING_POLICY.formulas,
        notes=DEFAULT_PEER_SCORING_POLICY.notes,
    )
    base.update(overrides)
    return base


def test_policy_rejects_weights_not_summing_to_one() -> None:
    bad_weights = dict(DEFAULT_PEER_SCORING_POLICY.weights)
    bad_weights["same_state"] = 0.50  # sum > 1.0
    with pytest.raises(ValueError, match="weights must sum to 1.0"):
        PeerScoringPolicy(**_policy_kwargs(weights=bad_weights))


def test_policy_rejects_negative_weight() -> None:
    bad_weights = dict(DEFAULT_PEER_SCORING_POLICY.weights)
    bad_weights["frpl_percentage"] = -0.04  # delta -0.19
    bad_weights["same_state"] = 0.21       # delta +0.19, keeps sum == 1.0
    with pytest.raises(ValueError, match="must be non-negative"):
        PeerScoringPolicy(**_policy_kwargs(weights=bad_weights))


def test_policy_rejects_empty_locale_categories() -> None:
    with pytest.raises(ValueError, match="locale_categories must be non-empty"):
        PeerScoringPolicy(**_policy_kwargs(locale_categories={}))


# ---------------------------------------------------------------------------
# Fail-closed: active policy lookup raises when policy is marked inactive.
# ---------------------------------------------------------------------------


def test_active_peer_scoring_policy_returns_default() -> None:
    policy = active_peer_scoring_policy()
    assert policy is DEFAULT_PEER_SCORING_POLICY
    assert policy.policy_version == "nces-similarity-v2-demography-2026-05"


def test_default_policy_is_demography_first() -> None:
    weights = DEFAULT_PEER_SCORING_POLICY.weights
    assert weights["enrollment_size"] == 0.35
    assert weights["locale_type"] == 0.25
    assert weights["frpl_percentage"] == 0.15
    assert weights["geographic_proximity"] == 0.03
    assert weights["same_state"] == 0.02
    assert (
        weights["enrollment_size"]
        + weights["locale_type"]
        + weights["frpl_percentage"]
    ) > 0.70


def test_default_policy_is_active() -> None:
    assert DEFAULT_PEER_SCORING_POLICY.active is True


# ---------------------------------------------------------------------------
# Methodology line: the new code interpolates the policy_version.
# ---------------------------------------------------------------------------


def test_peer_scoring_policy_disclosure_methodology_line() -> None:
    line = methodology_line_for(
        MethodologyRef(
            code="peer_scoring_policy_disclosure",
            metadata={"policy_version": "nces-similarity-v2-demography-2026-05"},
        )
    )
    assert line == "Peer scoring policy: nces-similarity-v2-demography-2026-05."


def test_peer_scoring_policy_disclosure_methodology_line_no_metadata() -> None:
    line = methodology_line_for(
        MethodologyRef(code="peer_scoring_policy_disclosure")
    )
    assert line == "Peer scoring uses the governed scoring policy."


# ---------------------------------------------------------------------------
# CatalogIntegrityError is the same fail-closed exception used elsewhere.
# ---------------------------------------------------------------------------


def test_catalog_integrity_error_is_subclass_of_runtime() -> None:
    """Sanity check: peer_scoring.py reuses the same fail-closed error."""

    assert issubclass(CatalogIntegrityError, RuntimeError)


# ---------------------------------------------------------------------------
# PR 2B: effective_weights — feature-set weight derivation.
# ---------------------------------------------------------------------------


def test_effective_weights_all_returns_full_policy() -> None:
    """feature_set='all' returns the complete policy weights unchanged."""

    policy = DEFAULT_PEER_SCORING_POLICY
    w = effective_weights(policy, "all")
    assert w == dict(policy.weights)


def test_effective_weights_enrollment_zeros_geo_and_state() -> None:
    """feature_set='enrollment' excludes geographic_proximity and same_state."""

    policy = DEFAULT_PEER_SCORING_POLICY
    w = effective_weights(policy, "enrollment")
    assert "geographic_proximity" not in w
    assert "same_state" not in w
    assert "frpl_percentage" not in w
    assert "locale_type" not in w
    assert "enrollment_size" in w
    assert "revenue_per_pupil" in w
    assert "pupil_teacher_ratio" in w
    assert "expenditure_per_pupil" in w


def test_effective_weights_frpl_keeps_enrollment_co_driver() -> None:
    """feature_set='frpl' co-weights enrollment_size (scale-respecting focus)."""

    policy = DEFAULT_PEER_SCORING_POLICY
    w = effective_weights(policy, "frpl")
    assert "frpl_percentage" in w
    assert "enrollment_size" in w
    assert "geographic_proximity" not in w
    assert "same_state" not in w
    assert "locale_type" not in w


def test_effective_weights_renormalizes_to_one() -> None:
    """Every non-'all' feature_set produces weights that sum to 1.0."""

    policy = DEFAULT_PEER_SCORING_POLICY
    for feature_set in ("enrollment", "frpl", "locale"):
        w = effective_weights(policy, feature_set)
        total = sum(w.values())
        assert abs(total - 1.0) < 1e-6, (
            f"feature_set={feature_set!r}: sum={total:.8f} (expected 1.0)"
        )


def test_effective_weights_feature_sets_intersect_active_policy_weights() -> None:
    """Every _FEATURE_SET_INCLUDED_WEIGHTS key must appear in the active policy.

    This guards against drift: if the migration bumps the policy to add a new
    factor, the feature-set map must be updated too.  The test fails
    immediately rather than silently dropping unknown factors.
    """

    policy = DEFAULT_PEER_SCORING_POLICY
    policy_keys = frozenset(policy.weights.keys())
    for feature_set, included in _FEATURE_SET_INCLUDED_WEIGHTS.items():
        unknown = included - policy_keys
        assert not unknown, (
            f"_FEATURE_SET_INCLUDED_WEIGHTS[{feature_set!r}] references "
            f"unknown policy factors: {unknown!r}. "
            "Update the map to match the governed policy weights."
        )
