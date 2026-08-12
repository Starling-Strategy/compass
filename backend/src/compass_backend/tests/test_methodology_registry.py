"""Tests for the typed methodology-code registry."""

from __future__ import annotations

import pytest

from compass_backend.artifacts import MethodologyRef
from compass_backend.rendering.methodology import (
    all_methodology_codes,
    methodology_line_for,
    registered_methodology_codes,
)


def test_methodology_registry_is_exhaustive_over_literal() -> None:
    """Every `MethodologyCode` literal value must have a registry entry."""

    missing = all_methodology_codes() - registered_methodology_codes()
    extra = registered_methodology_codes() - all_methodology_codes()
    assert not missing, (
        "MethodologyCode literal values without a registry entry: "
        f"{sorted(missing)}"
    )
    assert not extra, (
        "Registry entries not declared on the MethodologyCode literal: "
        f"{sorted(extra)}"
    )


@pytest.mark.parametrize("code", sorted(all_methodology_codes()))
def test_methodology_line_for_every_code_returns_non_empty_string(code: str) -> None:
    """Each code must resolve to a non-empty prose line."""

    line = methodology_line_for(MethodologyRef(code=code))
    assert isinstance(line, str)
    assert line.strip(), f"methodology_line_for({code!r}) returned empty"


def test_methodology_line_profile_rank_uses_profile_field_metadata() -> None:
    """The dynamic profile-rank entry interpolates `metadata['profile_field']`."""

    with_field = methodology_line_for(
        MethodologyRef(
            code="profile_rank_uses_profile_field",
            metadata={"profile_field": "Total enrollment"},
        )
    )
    assert with_field == "District order uses district profile data: Total enrollment."

    without_field = methodology_line_for(
        MethodologyRef(code="profile_rank_uses_profile_field")
    )
    assert without_field == "District order uses district profile data."

    blank_field = methodology_line_for(
        MethodologyRef(
            code="profile_rank_uses_profile_field",
            metadata={"profile_field": "   "},
        )
    )
    assert blank_field == "District order uses district profile data."


def test_methodology_line_metric_best_guess_names_chosen_and_alternates() -> None:
    """Fix 4B: the disclosure names the chosen metric AND lists the alternates
    so a best-guess is never silent."""

    line = methodology_line_for(
        MethodologyRef(
            code="metric_best_guess_disclosure",
            metadata={
                "chosen_metric": "Maximum number of annual paid sick days",
                "alternate_metrics": (
                    "Unused sick days can be carried over to the following year; "
                    "Number of paid personal leave days granted in first year"
                ),
            },
        )
    )
    assert "Maximum number of annual paid sick days" in line
    assert "Related metrics you can ask about:" in line
    assert "paid personal leave days" in line

    # Degrades gracefully when metadata is absent (defensive path).
    bare = methodology_line_for(MethodologyRef(code="metric_best_guess_disclosure"))
    assert bare.strip()
    assert "Related metrics" not in bare


_DROPPED_CODES = [
    "ranking_current_reviewed_numeric_answers",
    "ranking_excludes_unrankable_rows",
    "displayed_values_resolved_catalog_ids",
    "lookup_resolved_catalog_ids",
    "count_resolved_catalog_ids",
    "trend_resolved_metric_district_ids",
    "profile_lookup_nces_source",
]

_REWORDED_CODES: dict[str, str] = {
    "citation_answer_level_preferred_source_fallback": (
        "Sources cite the specific document for each row when available."
    ),
    "intersection_accepts_any_current_positive_value": (
        "Each criterion is met by any current positive value."
    ),
    "count_denominator_current_reviewed_rows": (
        "Denominator counts only districts with a current reviewed answer for this metric."
    ),
    "covered_universe_selection_count": (
        "Count reflects all covered districts in the Compass policy universe."
    ),
    "categorical_count_grouped_current_values": (
        "Grouped by the categorical answer for the current year."
    ),
    "trend_deltas_from_artifact_values": (
        "Year-over-year deltas use only reviewed values; missing years do not contribute."
    ),
    "profile_lookup_approved_field": (
        "Values come from NCES district profile data."
    ),
    "profile_lookup_compass_coverage_flag": (
        "Some districts appear in NCES profile data but are not part of "
        "the Compass policy review universe."
    ),
    "peer_policy_cells_with_citations": (
        "Policy cells include source citations where available."
    ),
    "peer_selection_rationale": (
        "Peer districts were identified using deterministic similarity scoring "
        "across all NCES-style dimensions."
    ),
}


@pytest.mark.parametrize("dropped_code", _DROPPED_CODES)
def test_dropped_methodology_codes_no_longer_in_literal(dropped_code: str) -> None:
    """Dropped codes must not appear in the MethodologyCode Literal."""

    assert dropped_code not in all_methodology_codes(), (
        f"{dropped_code!r} is still declared on MethodologyCode — it should have been dropped"
    )


@pytest.mark.parametrize("code,expected_prose", sorted(_REWORDED_CODES.items()))
def test_reworded_methodology_codes_emit_new_prose(code: str, expected_prose: str) -> None:
    """Reworded codes must resolve to the new trimmed prose."""

    from compass_backend.artifacts import MethodologyRef

    line = methodology_line_for(MethodologyRef(code=code))
    assert line == expected_prose, (
        f"methodology_line_for({code!r}) returned {line!r}; expected {expected_prose!r}"
    )


def test_methodology_ref_audience_default_is_user() -> None:
    """MethodologyRef.audience defaults to 'user' when not specified."""

    ref = MethodologyRef(code="lookup_default_district_order")
    assert ref.audience == "user"


def test_peer_scoring_policy_disclosure_emitted_with_internal_audience_and_not_rendered() -> None:
    """peer_scoring_policy_disclosure must be emitted with audience='internal' in peer.py
    and therefore not appear in rendered methodology lines.
    """
    # This test validates the contract at the artifact level: peer_scoring_policy_disclosure
    # must accept audience='internal' (not rejected by Pydantic) and must be filtered
    # by the composer.
    from compass_backend.rendering.composer import methodology_lines_for_result
    from compass_backend.artifacts.results import MetricRankingResult, ResultSelection

    internal_ref = MethodologyRef(
        code="peer_scoring_policy_disclosure",
        audience="internal",
        metadata={"policy_version": "v1"},
    )

    result = MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[],
        citations=[],
        total_considered=0,
        excluded_count=0,
        order_statement="Ranked by metric, highest to lowest.",
        methodology_codes=[internal_ref],
    )

    lines = methodology_lines_for_result(result)
    assert not any("Peer scoring" in line for line in lines), (
        "peer_scoring_policy_disclosure with audience='internal' should not appear in rendered lines"
    )


def test_methodology_ref_audience_internal_skipped_by_composer() -> None:
    """Composer skips refs with audience='internal'; user refs are rendered."""

    from compass_backend.rendering.composer import methodology_lines_for_result
    from compass_backend.artifacts.results import MetricRankingResult, ResultSelection

    internal_ref = MethodologyRef(
        code="peer_scoring_policy_disclosure",
        audience="internal",
        metadata={"policy_version": "v1"},
    )
    user_ref = MethodologyRef(code="lookup_default_district_order")

    result = MetricRankingResult(
        selection=ResultSelection(scope="all_covered_districts"),
        rows=[],
        citations=[],
        total_considered=0,
        excluded_count=0,
        order_statement="Ranked by metric, highest to lowest.",
        methodology_codes=[internal_ref, user_ref],
    )

    lines = methodology_lines_for_result(result)
    internal_prose = "Peer scoring policy"
    user_prose = "Rows are ordered by district name"
    assert not any(internal_prose in line for line in lines), (
        "internal-audience ref should be filtered from rendered lines"
    )
    assert any(user_prose in line for line in lines), (
        "user-audience ref should be rendered"
    )
