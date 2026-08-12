"""Governed regional scopes used by deterministic Compass selection.

The four U.S. Census Bureau regions (Northeast, Midwest, South, West) cover all
50 states + DC with no overlaps and no gaps. This is the single governed source
of truth for any "in the South" / "Midwestern districts" refinement — both the
data query path (``execution/selection.py``) and the policy-guidance exemplar
renderer expand region phrases through ``expand_state_or_region_values`` so the
same phrase always means the same state set everywhere.
"""

from __future__ import annotations

from .text import normalize_whitespace_casefold

from .states import normalize_state

# U.S. Census Bureau regions. Tuples are sorted for stable display/test order.
CENSUS_NORTHEAST_STATES: tuple[str, ...] = (
    "CT",
    "MA",
    "ME",
    "NH",
    "NJ",
    "NY",
    "PA",
    "RI",
    "VT",
)
CENSUS_MIDWEST_STATES: tuple[str, ...] = (
    "IA",
    "IL",
    "IN",
    "KS",
    "MI",
    "MN",
    "MO",
    "ND",
    "NE",
    "OH",
    "SD",
    "WI",
)
CENSUS_SOUTH_STATES: tuple[str, ...] = (
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
CENSUS_WEST_STATES: tuple[str, ...] = (
    "AK",
    "AZ",
    "CA",
    "CO",
    "HI",
    "ID",
    "MT",
    "NM",
    "NV",
    "OR",
    "UT",
    "WA",
    "WY",
)

# Canonical region key -> member states.
CENSUS_REGIONS: dict[str, tuple[str, ...]] = {
    "northeast": CENSUS_NORTHEAST_STATES,
    "midwest": CENSUS_MIDWEST_STATES,
    "south": CENSUS_SOUTH_STATES,
    "west": CENSUS_WEST_STATES,
}

# Human-readable framing for each canonical region ("in the South").
_REGION_DISPLAY: dict[str, str] = {
    "northeast": "the Northeast",
    "midwest": "the Midwest",
    "south": "the South",
    "west": "the West",
}

# Every accepted phrasing -> canonical region key. Normalized (casefolded,
# whitespace-collapsed) before lookup, so callers don't pre-clean.
_REGION_ALIASES: dict[str, str] = {
    # Northeast
    "northeast": "northeast",
    "north east": "northeast",
    "north-east": "northeast",
    "northeastern": "northeast",
    "the northeast": "northeast",
    # Midwest
    "midwest": "midwest",
    "mid west": "midwest",
    "mid-west": "midwest",
    "midwestern": "midwest",
    "the midwest": "midwest",
    # South
    "south": "south",
    "southern": "south",
    "the south": "south",
    "census south": "south",
    # The Southeast is a subset of the governed Census South — map it to South
    # rather than letting an ungoverned "the Southeast" region phrase dead-end
    # as an unresolved phrase and trip the planner rescue fallback (#1686).
    "southeast": "south",
    "south east": "south",
    "south-east": "south",
    "southeastern": "south",
    "the southeast": "south",
    "the south east": "south",
    # West
    "west": "west",
    "western": "west",
    "the west": "west",
}

# Derived: alias phrase -> member states.
_REGION_STATES: dict[str, tuple[str, ...]] = {
    alias: CENSUS_REGIONS[key] for alias, key in _REGION_ALIASES.items()
}


def _normalize_phrase(value: object) -> str:
    return normalize_whitespace_casefold(str(value))


def census_region_key(value: object) -> str | None:
    """Return the canonical region key for a phrase, or None if ungoverned.

    e.g. "the South" / "Southern" -> "south"; "Pacific Northwest" -> None.
    """

    return _REGION_ALIASES.get(_normalize_phrase(value))


def census_region_label(value: object) -> str | None:
    """Return display framing ("the South") for a governed region phrase.

    Returns None when the phrase isn't a governed Census region, so callers can
    fall back rather than print an ungoverned label.
    """

    key = census_region_key(value)
    return _REGION_DISPLAY[key] if key is not None else None


def expand_state_or_region_values(values: list[str] | tuple[str, ...] | set[str]) -> set[str]:
    """Return normalized state abbreviations after expanding governed regions."""

    states: set[str] = set()
    for value in values:
        region_states = _REGION_STATES.get(_normalize_phrase(value))
        if region_states is not None:
            states.update(region_states)
            continue
        states.add(normalize_state(str(value)))
    return {state for state in states if state}
