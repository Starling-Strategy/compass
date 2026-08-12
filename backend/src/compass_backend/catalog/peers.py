"""Deterministic NCES-style peer scoring for covered Compass districts.

Scoring weights, locale ordering, and formula constants are read from a
governed `PeerScoringPolicy` (see `catalog/peer_scoring.py` and the
`compass.peer_scoring_policies` table in migration 034). The policy is
versioned; every peer answer discloses its `policy_version` via
`PeerSetResolution.policy_version` and the peer-comparison methodology line.
"""

from __future__ import annotations

import math

from .peer_scoring import (
    DEFAULT_PEER_SCORING_POLICY,
    PeerScoringPolicy,
    active_peer_scoring_policy,
)
from .resolution import (
    CoveredNCESProfileRow,
    PeerDistrictCandidate,
    PeerFactorScore,
    PeerSetResolution,
)


def build_peer_set(
    anchor: CoveredNCESProfileRow,
    profiles: list[CoveredNCESProfileRow],
    *,
    limit: int,
    policy: PeerScoringPolicy | None = None,
    effective_weights_override: dict[str, float] | None = None,
    include_states: list[str] | set[str] | frozenset[str] | None = None,
    exclude_states: list[str] | None = None,
) -> PeerSetResolution:
    """Return the top covered peer districts for an anchor profile.

    Args:
        anchor: The anchor district's NCES profile.
        profiles: All covered district profiles to score against.
        limit: Maximum number of peer districts to return.
        policy: Governed scoring policy (defaults to active policy).
        effective_weights_override: Pre-derived feature-set weights from
            ``effective_weights(policy, feature_set)``.  When provided, only
            factors in this dict are included in the similarity score;
            factors absent from the dict are treated as zero-weight.
            Pass ``None`` to use the full policy weights (current behavior).
        include_states: Optional positive state abbreviations to keep in the
            candidate pool before scoring.
        exclude_states: Two-letter state abbreviations to exclude from the
            candidate pool BEFORE scoring.  Semantics: "best non-CA peers,"
            not "best peers minus CA."  ``None`` means no state exclusion.
    """

    policy = policy or active_peer_scoring_policy()
    include_upper: frozenset[str] = frozenset(
        s.upper() for s in (include_states or [])
    )
    exclude_upper: frozenset[str] = frozenset(
        s.upper() for s in (exclude_states or [])
    )
    scored: list[PeerDistrictCandidate] = []
    for profile in profiles:
        if profile.district_id == anchor.district_id:
            continue
        if include_upper and (profile.state or "").upper() not in include_upper:
            continue
        # Pre-scoring state exclusion.
        if exclude_upper and (profile.state or "").upper() in exclude_upper:
            continue
        if not _has_peer_matching_data(anchor, profile, policy):
            continue
        similarity_score, factors = score_peer_similarity(
            anchor,
            profile,
            policy=policy,
            effective_weights_override=effective_weights_override,
        )
        scored.append(
            PeerDistrictCandidate(
                **profile.model_dump(),
                rank=1,
                similarity_score=similarity_score,
                factors=factors,
            )
        )

    scored.sort(key=lambda peer: (-peer.similarity_score, peer.district_name.casefold()))
    peers = [
        peer.model_copy(update={"rank": rank})
        for rank, peer in enumerate(scored[:limit], start=1)
    ]
    return PeerSetResolution(
        anchor=anchor,
        peers=peers,
        policy_version=policy.policy_version,
    )


def score_peer_similarity(
    anchor: CoveredNCESProfileRow,
    candidate: CoveredNCESProfileRow,
    *,
    policy: PeerScoringPolicy | None = None,
    effective_weights_override: dict[str, float] | None = None,
) -> tuple[float, dict[str, PeerFactorScore]]:
    """Score one covered district against an anchor with transparent factors.

    Args:
        anchor: The anchor district's NCES profile.
        candidate: The candidate district's NCES profile.
        policy: Governed scoring policy (defaults to active policy).
        effective_weights_override: When provided, only factors in this dict
            contribute to the weighted sum.  Factors absent from the dict are
            skipped entirely (not counted as missing/0.5).  The dict's values
            must already be renormalized (sum to 1.0).
    """

    policy = policy or active_peer_scoring_policy()
    # Use override weights if provided; fall back to full policy weights.
    weights = effective_weights_override if effective_weights_override is not None else policy.weights

    factors: dict[str, PeerFactorScore] = {}
    computable_weight = 0.0
    for factor_name, weight in weights.items():
        scored = _score_peer_factor(factor_name, anchor, candidate, policy)
        if scored is None:
            factors[factor_name] = PeerFactorScore(
                score=0.5,
                explanation="data unavailable",
            )
            continue
        score, explanation = scored
        factors[factor_name] = PeerFactorScore(
            score=round(score, 2),
            explanation=explanation,
        )
        computable_weight += weight

    if computable_weight == 0:
        return 0.5, factors

    total = 0.0
    for factor_name, weight in weights.items():
        factor = factors.get(factor_name)
        if factor is None or factor.explanation == "data unavailable":
            continue
        total += factor.score * (weight / computable_weight)
    return round(total, 2), factors


def peer_reason(peer: PeerDistrictCandidate) -> str:
    """Return a compact deterministic reason for peer inclusion."""

    parts: list[str] = []
    for factor_name, label in (
        ("enrollment_size", "Similar enrollment"),
        ("locale_type", "Similar urbanicity"),
        ("frpl_percentage", "Similar FRPL share"),
    ):
        factor = peer.factors.get(factor_name)
        if factor is not None and factor.explanation != "data unavailable":
            parts.append(f"{label} ({factor.explanation})")

    secondary = [
        (name, factor)
        for name, factor in peer.factors.items()
        if factor.explanation != "data unavailable"
        and name not in {"enrollment_size", "locale_type", "frpl_percentage"}
        and not (name == "same_state" and factor.score <= 0)
    ]
    secondary.sort(key=lambda item: (-item[1].score, _factor_reason_order(item[0])))
    for factor_name, factor in secondary:
        if len(parts) >= 4:
            break
        if len(parts) >= 3 and factor_name in {
            "geographic_proximity",
            "same_state",
        }:
            continue
        parts.append(f"{_secondary_reason_label(factor_name)} ({factor.explanation})")

    return "; ".join(parts) or "Covered NCES peer profile selected by similarity score."


def _secondary_reason_label(factor_name: str) -> str:
    labels = {
        "pupil_teacher_ratio": "Similar staffing ratio",
        "revenue_per_pupil": "Similar revenue per pupil",
        "expenditure_per_pupil": "Similar spending per pupil",
        "geographic_proximity": "Secondary distance signal",
        "same_state": "Secondary state signal",
    }
    return labels.get(factor_name, "Similar profile factor")


def _factor_reason_order(factor_name: str) -> int:
    order = {
        "enrollment_size": 0,
        "frpl_percentage": 1,
        "locale_type": 2,
        "pupil_teacher_ratio": 3,
        "revenue_per_pupil": 4,
        "expenditure_per_pupil": 5,
        "geographic_proximity": 6,
        "same_state": 7,
    }
    return order.get(factor_name, 99)


def _score_peer_factor(
    factor_name: str,
    anchor: CoveredNCESProfileRow,
    candidate: CoveredNCESProfileRow,
    policy: PeerScoringPolicy,
) -> tuple[float, str] | None:
    if factor_name == "geographic_proximity":
        if (
            anchor.latitude is None
            or anchor.longitude is None
            or candidate.latitude is None
            or candidate.longitude is None
        ):
            return None
        distance_km = _haversine_km(
            anchor.latitude,
            anchor.longitude,
            candidate.latitude,
            candidate.longitude,
            earth_radius_km=policy.formulas.earth_radius_km,
        )
        distance_miles = round(distance_km / policy.formulas.km_per_mile)
        half_life = policy.formulas.geo_decay_half_life_km
        return 1.0 / (1.0 + distance_km / half_life), f"~{distance_miles} miles apart"

    if factor_name == "enrollment_size":
        if (
            anchor.enrollment is None
            or candidate.enrollment is None
            or anchor.enrollment <= 0
            or candidate.enrollment <= 0
        ):
            return None
        log_ratio = abs(math.log(anchor.enrollment) - math.log(candidate.enrollment))
        return (
            1.0 / (1.0 + log_ratio),
            f"{anchor.enrollment:,} vs {candidate.enrollment:,} students",
        )

    if factor_name == "locale_type":
        anchor_locale = _parse_locale(anchor.locale_text, policy)
        candidate_locale = _parse_locale(candidate.locale_text, policy)
        if anchor_locale is None or candidate_locale is None:
            return None
        category_distance = abs(anchor_locale[0] - candidate_locale[0])
        size_distance = abs(anchor_locale[1] - candidate_locale[1])
        score = max(
            0.0,
            1.0
            - (
                category_distance * policy.formulas.locale_category_penalty
                + size_distance * policy.formulas.locale_size_penalty
            ),
        )
        return score, f"{anchor.locale_text} vs {candidate.locale_text}"

    if factor_name == "revenue_per_pupil":
        score = _score_ratio(anchor.total_rev_pp, candidate.total_rev_pp)
        if score is None:
            return None
        return (
            score,
            f"${anchor.total_rev_pp:,.0f} vs ${candidate.total_rev_pp:,.0f} per pupil",
        )

    if factor_name == "pupil_teacher_ratio":
        score = _score_ratio(anchor.pupil_teacher_ratio, candidate.pupil_teacher_ratio)
        if score is None:
            return None
        return (
            score,
            (
                f"{anchor.pupil_teacher_ratio:.0f}:1 vs "
                f"{candidate.pupil_teacher_ratio:.0f}:1 pupil-teacher ratio"
            ),
        )

    if factor_name == "expenditure_per_pupil":
        score = _score_ratio(anchor.total_exp_pp, candidate.total_exp_pp)
        if score is None:
            return None
        return (
            score,
            (
                f"${anchor.total_exp_pp:,.0f} vs "
                f"${candidate.total_exp_pp:,.0f} per pupil spending"
            ),
        )

    if factor_name == "frpl_percentage":
        score = _score_ratio(anchor.frpl_pct, candidate.frpl_pct)
        if score is None:
            return None
        return score, f"{anchor.frpl_pct:.0f}% vs {candidate.frpl_pct:.0f}% FRPL"

    if factor_name == "same_state":
        if not anchor.state or not candidate.state:
            return None
        if anchor.state.upper() == candidate.state.upper():
            return 1.0, f"Both in {anchor.state.upper()}"
        return 0.0, f"{anchor.state.upper()} vs {candidate.state.upper()}"

    return None


def _has_peer_matching_data(
    anchor: CoveredNCESProfileRow,
    candidate: CoveredNCESProfileRow,
    policy: PeerScoringPolicy,
) -> bool:
    """Return whether a candidate has usable peer evidence beyond covered state."""

    return any(
        _score_peer_factor(factor_name, anchor, candidate, policy) is not None
        for factor_name in policy.weights
        if factor_name != "same_state"
    )


def _parse_locale(
    locale_text: str | None, policy: PeerScoringPolicy
) -> tuple[int, int] | None:
    if not locale_text or ":" not in locale_text:
        return None
    category, size = [part.strip() for part in locale_text.split(":", 1)]
    category_order = policy.locale_categories.get(category)
    size_order = policy.locale_sizes.get(size)
    if category_order is None or size_order is None:
        return None
    return category_order, size_order


def _haversine_km(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    *,
    earth_radius_km: float = DEFAULT_PEER_SCORING_POLICY.formulas.earth_radius_km,
) -> float:
    lat1, lon1, lat2, lon2 = (math.radians(value) for value in (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


def _score_ratio(left: float | None, right: float | None) -> float | None:
    if left is None or right is None or left <= 0 or right <= 0:
        return None
    return min(left, right) / max(left, right)
