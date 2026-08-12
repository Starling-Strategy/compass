"""Canonical profile-field key utilities.

User-facing profile-field aliases are governed by ``compass.catalog_aliases``
with ``entity_type='nces_field'``. This module intentionally knows only the
canonical keys the deterministic artifacts use internally.
"""

from __future__ import annotations

PROFILE_FIELD_KEYS: frozenset[str] = frozenset({
    "enrollment",
    "frpl_pct",
})

# Synthetic ``metric_id`` sentinels for the two profile fields. These negative
# ids live in the range reserved for profile-field metrics so they never collide
# with real ``compass.metrics`` rows (mirrors migration 061; see
# ``catalog/profile_rank_fields.py`` and ``execution/ranking.py``). Centralized
# here so the value lives in one place; artifacts and the profile-field
# validator import these instead of restating the literals.
PROFILE_ENROLLMENT_METRIC_ID: int = -1001
PROFILE_FRPL_METRIC_ID: int = -1002
PROFILE_SENTINEL_METRIC_IDS: frozenset[int] = frozenset({
    PROFILE_ENROLLMENT_METRIC_ID,
    PROFILE_FRPL_METRIC_ID,
})


def profile_field_key(field: str) -> str | None:
    """Return a canonical profile-field key only when one was already provided."""

    normalized = field.casefold().strip()
    if normalized in PROFILE_FIELD_KEYS:
        return normalized
    return None
