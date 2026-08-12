"""Typed registry mapping `MethodologyCode` → user-facing prose.

The registry is exhaustive over the `MethodologyCode` Literal. A missing
entry surfaces as a `KeyError` at render time and as a failure in
`test_methodology_registry_is_exhaustive` — both signals that a new code
was added to the Literal without a registry entry.

This module replaces the prior chained-equality dispatch in
`composer._methodology_line` (~22 `if code == "X":` branches). The CLAUDE.md
"No prose-dispatch below the planner" doctrine names that dispatch as the
known scanner-gap residue; promoting it to a typed registry closes the gap.
"""

from __future__ import annotations

from typing import Callable, get_args

from compass_backend.artifacts import MethodologyCode, MethodologyRef


def _profile_rank_line(ref: MethodologyRef) -> str:
    profile_field = ref.metadata.get("profile_field", "").strip()
    if profile_field:
        return f"District order uses district profile data: {profile_field}."
    return "District order uses district profile data."


def _peer_policy_disclosure_line(ref: MethodologyRef) -> str:
    policy_version = ref.metadata.get("policy_version", "").strip()
    if not policy_version:
        return "Peer scoring uses the governed scoring policy."
    return f"Peer scoring policy: {policy_version}."


def _degree_lane_disclosure_line(ref: MethodologyRef) -> str:
    """Return lane-specific prose from the degree_lane metadata field."""
    lane = ref.metadata.get("degree_lane")
    if lane == "ba":
        return (
            "Results are restricted to the bachelor's-degree teacher salary "
            "lane as requested in the query."
        )
    if lane == "ma":
        return (
            "Results are restricted to the master's-degree teacher salary "
            "lane as requested in the query."
        )
    # Defensive fallback: metadata missing or unexpected value (should not occur
    # in practice since degree_lane is a Literal["ba", "ma"] at the contract level).
    return (
        "Results are restricted to the requested teacher degree lane "
        "as specified in the query."
    )


def _metric_best_guess_line(ref: MethodologyRef) -> str:
    """Fix 4B disclosure: name the metric Compass best-guessed for a
    materially-ambiguous phrase plus the alternates it set aside, so a
    best-guess is never silent. Reads the chosen/alternate metric labels from
    metadata (already grounded catalog names)."""

    chosen = ref.metadata.get("chosen_metric", "").strip()
    alternates = ref.metadata.get("alternate_metrics", "").strip()
    if chosen:
        base = (
            "Your question could match more than one Compass metric, so I "
            f"answered using “{chosen}.”"
        )
    else:
        base = (
            "Your question could match more than one Compass metric, so I "
            "answered using the closest match."
        )
    if alternates:
        return f"{base} Related metrics you can ask about: {alternates}."
    return base


_StaticLine = str
_DynamicLine = Callable[[MethodologyRef], str]

_METHODOLOGY_REGISTRY: dict[MethodologyCode, _StaticLine | _DynamicLine] = {
    # --- Citation / source ---
    # Reworded (Track 3.3): was "Sources are attached from answer-level evidence..."
    "citation_answer_level_preferred_source_fallback": (
        "Sources cite the specific document for each row when available."
    ),
    # --- Ranking / profile ordering ---
    "profile_rank_uses_profile_field": _profile_rank_line,
    # --- Lookup ---
    "lookup_default_district_order": (
        "Rows are ordered by district name unless a supported sort is requested."
    ),
    "ranked_lookup_selection_order": (
        "Rows follow the selected ranking order, with comparison metrics shown for each district."
    ),
    # --- Intersection / lookup criteria ---
    "intersection_requires_all_criteria": (
        "Districts are included only when they satisfy every requested criterion."
    ),
    # Reworded (Track 3.3): was "Each criterion accepts any current reviewed positive value..."
    "intersection_accepts_any_current_positive_value": (
        "Each criterion is met by any current positive value."
    ),
    # --- Count ---
    # Reworded (Track 3.3): was "Denominators include current covered rows with reviewed data."
    "count_denominator_current_reviewed_rows": (
        "Denominator counts only districts with a current reviewed answer for this metric."
    ),
    # Reworded (Track 3.3): was "Counts use the resolved covered-district selection."
    "covered_universe_selection_count": (
        "Count reflects all covered districts in the Compass policy universe."
    ),
    "topic_coverage_count": (
        "Count reflects covered districts with at least one reviewed answer in "
        "the requested topic."
    ),
    # Reworded (Track 3.3): was "Grouped current-year categorical values from resolved catalog IDs."
    "categorical_count_grouped_current_values": (
        "Grouped by the categorical answer for the current year."
    ),
    "categorical_count_missing_unavailable_separate": (
        "Missing and unavailable values are separate from reviewed categories."
    ),
    # --- Trend ---
    "trend_chronological_coverage_gaps": (
        "Rows are chronological and include explicit coverage gaps."
    ),
    # Reworded (Track 3.3): was "Deltas are computed only from covered artifact row values."
    "trend_deltas_from_artifact_values": (
        "Year-over-year deltas use only reviewed values; missing years do not contribute."
    ),
    # --- Profile lookup ---
    # Reworded (Track 3.3): was "Profile rows use approved NCES/profile field authority."
    # profile_lookup_nces_source dropped (Track 3.3) — duplicative after this reword.
    "profile_lookup_approved_field": (
        "Values come from NCES district profile data."
    ),
    # Reworded (Track 3.3): was "covered_by_compass=false means the district is available only..."
    "profile_lookup_compass_coverage_flag": (
        "Some districts appear in NCES profile data but are not part of "
        "the Compass policy review universe."
    ),
    # --- Peer comparison / similarity ---
    "peer_selection_nces_profiles": (
        "Peer districts were selected from covered Compass districts with "
        "linked NCES profiles."
    ),
    "peer_score_method": (
        "Peer score is demography-first: enrollment, urbanicity, and FRPL "
        "are primary; finance/staffing and distance/state are secondary "
        "signals."
    ),
    # Reworded (Track 3.3): was "Policy cells use resolved Compass answers..."
    "peer_policy_cells_with_citations": (
        "Policy cells include source citations where available."
    ),
    # audience="internal" — emitted for Logfire audit trail; composer filters it.
    "peer_scoring_policy_disclosure": _peer_policy_disclosure_line,
    # Reworded (Track 3.3): was "Peer districts were selected using deterministic NCES-style
    # similarity scoring across geography, enrollment, locale, fiscal, staffing, and FRPL factors."
    # Not dropped: emitted only when feature_set="all" (different path from peer_score_method).
    "peer_selection_rationale": (
        "Peer districts were identified using deterministic similarity scoring "
        "across all NCES-style dimensions."
    ),
    "peer_metric_coverage_screen_applied": (
        "Peer comparison was limited to peer districts with current covered "
        "data for the requested metric or metric bundle."
    ),
    # --- Threshold / filter transparency (PR 2A) ---
    "metric_value_filter_applied": (
        "A metric-value threshold filter was applied before the result was "
        "rendered."
    ),
    # --- Categorical inclusion/exclusion filter transparency (#1339) ---
    "categorical_value_filter_applied": (
        "The answer was limited to districts whose reviewed value matches the "
        "requested categories; rows marked Not applicable, not yet reviewed, "
        "or with no matching category were excluded."
    ),
    # Legacy registry entry retained for historical persisted turns only.
    # New successful execution paths must apply filters or refuse before render.
    "metric_value_filter_not_applied": (
        "A metric-value threshold filter was specified but is not enforced on "
        "trend or peer-comparison results. Threshold filtering for these "
        "operation types is not yet supported; re-run the query as a lookup or "
        "ranking to apply the threshold."
    ),
    "anchor_value_filter_applied": (
        "The filter used the anchor district's reviewed value, then applied "
        "that value to the comparison districts."
    ),
    # --- Similarity / peer-set discovery (PR 2B / Track 3.2) ---
    # Prose is operation-neutral — correct for both peer_comparison and similarity operations.
    "similarity_feature_set_override": (
        "Peer scoring weights were biased toward the requested feature set; "
        "factors outside that set were excluded and the remaining weights "
        "renormalized."
    ),
    "similarity_exclude_states_applied": (
        "Peer candidates from the specified states were excluded before scoring."
    ),
    "similarity_post_filter_narrowed_peer_set": (
        "A metric-value threshold filter was applied after peer-set selection; "
        "the returned set may be smaller than the requested limit."
    ),
    # --- Degree lane (PR 2C) ---
    # Uses a callable so the prose names the specific lane from ref.metadata.
    "degree_lane_applied": _degree_lane_disclosure_line,
    # --- Metric best-guess disclosure (Fix 4B, #1 refusal family) ---
    # Names the metric chosen for a materially-ambiguous phrase + the alternates.
    "metric_best_guess_disclosure": _metric_best_guess_line,
}


def methodology_line_for(ref: MethodologyRef) -> str:
    """Resolve a `MethodologyRef` into its user-facing prose line."""

    entry = _METHODOLOGY_REGISTRY[ref.code]
    if callable(entry):
        return entry(ref)
    return entry


def registered_methodology_codes() -> frozenset[MethodologyCode]:
    """Set of methodology codes the registry currently maps."""

    return frozenset(_METHODOLOGY_REGISTRY.keys())


def all_methodology_codes() -> frozenset[MethodologyCode]:
    """Set of every methodology code declared on the `MethodologyCode` Literal."""

    return frozenset(get_args(MethodologyCode))
