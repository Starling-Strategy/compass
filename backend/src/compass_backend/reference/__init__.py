"""Shared reference data for Compass: states and canonical keys.

State normalization lives here because it is deterministic reference data.
User-facing profile-field aliases live in the governed catalog boundary;
only canonical profile keys are exposed here.
"""

from __future__ import annotations

from .profile_fields import profile_field_key
from .regions import (
    CENSUS_MIDWEST_STATES,
    CENSUS_NORTHEAST_STATES,
    CENSUS_REGIONS,
    CENSUS_SOUTH_STATES,
    CENSUS_WEST_STATES,
    census_region_key,
    census_region_label,
    expand_state_or_region_values,
)
from .states import (
    STATE_NAME_TO_ABBREVIATION,
    normalize_state,
    split_state_suffix,
    state_full_name,
)
from .text import normalize_whitespace_casefold

__all__ = [
    "CENSUS_MIDWEST_STATES",
    "CENSUS_NORTHEAST_STATES",
    "CENSUS_REGIONS",
    "CENSUS_SOUTH_STATES",
    "CENSUS_WEST_STATES",
    "STATE_NAME_TO_ABBREVIATION",
    "census_region_key",
    "census_region_label",
    "expand_state_or_region_values",
    "normalize_state",
    "normalize_whitespace_casefold",
    "profile_field_key",
    "split_state_suffix",
    "state_full_name",
]
