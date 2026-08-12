"""Governed catalog of profile fields Compass can rank covered districts by.

The prior in-code dict `_PROFILE_RANK_FIELDS` (at `db/catalog.py:170`)
carried the rank-by-profile-field surface with no paired governance
migration. This module lifts it into a typed `ProfileRankField` model
whose seed lives in migration `061_compass_profile_rank_fields.sql`. The
sync test `test_profile_rank_fields_in_sync_with_migration` asserts code
and DB cannot drift.

`UnsupportedRankField` is the typed blocker that replaces the prior
silent empty-list return at `db/catalog.py:957`. Plan §7 W2-M5-00a:

    Unsupported rank field returns typed UnsupportedRankField blocker
    (not empty list) at db/catalog.py:957.

The empty-list behavior collapsed two cases into one: a real
zero-coverage result and an unsupported-shape request. The typed
exception lets the executor emit a useful clarification (and, more
importantly, lets tests assert the distinction).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UnsupportedRankField(Exception):
    """Raised when ``rank_districts_by_profile_field`` gets a field_key that
    is not in the governed allowlist.

    The catalog/execution boundary uses this as a typed blocker so the
    executor can convert it into ``ExecutionRefusal`` rather than treat
    an unknown field as a zero-row result.
    """

    def __init__(self, *, field_key: str) -> None:
        super().__init__(
            f"profile rank field not in governed allowlist: {field_key!r}"
        )
        self.field_key = field_key


class ProfileRankField(BaseModel):
    """One governed profile field Compass can sort covered districts by."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_key: str = Field(min_length=1)
    column_name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    metric_id: int
    display: Literal["integer", "percent", "number", "currency", "text"]
    vintage_label: str = Field(min_length=1)
    review_status: Literal["approved", "candidate", "rejected"] = "approved"
    active: bool = True


# Mirrors the seed in migration 061. metric_id uses the negative-id range
# reserved for synthetic profile-field metrics so it never collides with
# real `compass.metrics` rows. The sync test asserts code/DB lockstep.
DEFAULT_PROFILE_RANK_FIELDS: tuple[ProfileRankField, ...] = (
    ProfileRankField(
        field_key="enrollment",
        column_name="enrollment",
        label="NCES enrollment",
        metric_id=-1001,
        display="integer",
        vintage_label="NCES directory year",
    ),
    ProfileRankField(
        field_key="frpl_pct",
        column_name="frpl_pct",
        label="FRPL %",
        metric_id=-1002,
        display="percent",
        vintage_label="FRPL year",
    ),
)


def profile_rank_field_by_key() -> dict[str, ProfileRankField]:
    """Return active profile rank fields keyed by ``field_key`` for O(1) lookup."""

    return {
        field.field_key: field
        for field in DEFAULT_PROFILE_RANK_FIELDS
        if field.active
    }
