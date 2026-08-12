"""Tests for the canonical `compass_backend.reference` lookup tables."""

from __future__ import annotations

from compass_backend.reference import (
    CENSUS_MIDWEST_STATES,
    CENSUS_NORTHEAST_STATES,
    CENSUS_REGIONS,
    CENSUS_SOUTH_STATES,
    CENSUS_WEST_STATES,
    STATE_NAME_TO_ABBREVIATION,
    census_region_key,
    census_region_label,
    expand_state_or_region_values,
    normalize_state,
    profile_field_key,
)


def test_state_table_covers_50_states_plus_dc() -> None:
    """50 states + DC; no territories. Behavior unchanged from prior duplicates."""

    assert len(STATE_NAME_TO_ABBREVIATION) == 51
    assert STATE_NAME_TO_ABBREVIATION["district of columbia"] == "DC"
    assert STATE_NAME_TO_ABBREVIATION["california"] == "CA"
    assert STATE_NAME_TO_ABBREVIATION["new york"] == "NY"


def test_normalize_state_handles_full_name_case_insensitive() -> None:
    assert normalize_state("California") == "CA"
    assert normalize_state("california") == "CA"
    assert normalize_state("CALIFORNIA") == "CA"
    assert normalize_state("  California  ") == "CA"


def test_normalize_state_passes_through_abbreviations_upper_cased() -> None:
    assert normalize_state("ca") == "CA"
    assert normalize_state("CA") == "CA"
    assert normalize_state("Tx") == "TX"


def test_normalize_state_returns_empty_for_missing_values() -> None:
    assert normalize_state(None) == ""
    assert normalize_state("") == ""
    assert normalize_state("   ") == ""


def test_normalize_state_uppercases_unknown_values() -> None:
    """Unknown states fall through to upper-case so callers can detect them."""

    assert normalize_state("Puerto Rico") == "PUERTO RICO"
    assert normalize_state("guam") == "GUAM"


def test_census_south_region_expands_to_governed_states() -> None:
    assert CENSUS_SOUTH_STATES == (
        "AL",
        "AR",
        "DC",
        "DE",
        "FL",
        "GA",
        "KY",
        "LA",
        "MD",
        "MS",
        "NC",
        "OK",
        "SC",
        "TN",
        "TX",
        "VA",
        "WV",
    )
    assert expand_state_or_region_values(["South"]) == set(CENSUS_SOUTH_STATES)
    assert expand_state_or_region_values(["southern"]) == set(CENSUS_SOUTH_STATES)
    assert expand_state_or_region_values(["CA", "South"]) == {
        "CA",
        *CENSUS_SOUTH_STATES,
    }


def test_southeast_subregion_maps_to_census_south() -> None:
    """#1686: an ungoverned "the Southeast" sub-region resolves to the governed
    Census South (it is a subset) instead of dead-ending as an unresolved phrase
    and tripping the planner's rescue fallback."""
    for phrase in ("southeast", "the southeast", "south-east", "Southeastern", "the South East"):
        assert census_region_key(phrase) == "south", phrase
        assert expand_state_or_region_values([phrase]) == set(CENSUS_SOUTH_STATES), phrase
    assert census_region_label("the Southeast") == "the South"


def test_four_census_regions_cover_50_states_plus_dc_without_overlap() -> None:
    """The four governed regions partition all 51 jurisdictions exactly."""
    members = [s for states in CENSUS_REGIONS.values() for s in states]
    assert len(members) == 51
    assert len(set(members)) == 51  # no state in two regions
    assert set(CENSUS_REGIONS) == {"northeast", "midwest", "south", "west"}


def test_all_regions_expand_and_label() -> None:
    assert expand_state_or_region_values(["the Northeast"]) == set(CENSUS_NORTHEAST_STATES)
    assert expand_state_or_region_values(["midwest"]) == set(CENSUS_MIDWEST_STATES)
    assert expand_state_or_region_values(["the West"]) == set(CENSUS_WEST_STATES)
    assert census_region_key("Northeastern") == "northeast"
    assert census_region_label("the midwest") == "the Midwest"
    assert census_region_label("the West") == "the West"
    # well-known region members land in the right bucket
    assert "NY" in CENSUS_NORTHEAST_STATES and "IL" in CENSUS_MIDWEST_STATES
    assert "CA" in CENSUS_WEST_STATES and "TX" in CENSUS_SOUTH_STATES


def test_ungoverned_region_phrase_is_not_resolved() -> None:
    assert census_region_key("Appalachia") is None
    assert census_region_label("Pacific Northwest") is None
    # ungoverned phrase contributes no governed states (normalize_state passes
    # the raw token through upper-cased; it just isn't a region expansion).
    assert expand_state_or_region_values(["Appalachia"]) == {"APPALACHIA"}


def test_profile_field_key_accepts_only_canonical_keys() -> None:
    assert profile_field_key("enrollment") == "enrollment"
    assert profile_field_key("frpl_pct") == "frpl_pct"


def test_profile_field_key_does_not_authorize_aliases() -> None:
    for alias in (
        "student_enrollment",
        "students",
        "enrollment-size",
        "Enrollment Size",
        "NCES enrollment",
        "frpl",
        "frpl %",
        "FRPL Percent",
        "free and reduced lunch",
        "free-reduced-lunch-share",
        "Free and Reduced Price Lunch Percent",
    ):
        assert profile_field_key(alias) is None, alias


def test_profile_field_key_returns_none_for_unknown_field() -> None:
    assert profile_field_key("graduation_rate") is None
    assert profile_field_key("") is None
    assert profile_field_key("teacher_salary") is None
