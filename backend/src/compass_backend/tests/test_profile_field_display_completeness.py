"""Guard: every allowlisted NCES/profile field must be lookup-able end to end.

The FRPL bug (#1720 follow-up) was a silent drift: `frpl_pct` was added to the
NCES allowlist and to the rankable profile fields, but NOT to
`_NCES_FIELD_DISPLAY`. `lookup_nces_profile_values` early-returns ``[]`` when a
field has no display format, so the turn refused with "could not resolve sourced
NCES/profile values" for a field whose data is present for 99.4% of districts.

This fast guard catches the whole class at test time: a new allowlisted field
that lacks a display format can never ship a silent refusal again. The
companion live-DB test (`test_live_db_smoke`) proves the value actually resolves
from its real source table.
"""

from __future__ import annotations

from compass_backend.catalog.nces_allowlist import DEFAULT_NCES_ALLOWLIST
from compass_backend.db.catalog import _NCES_FIELD_DISPLAY, _NAVIGATOR_PROFILE_FIELDS


def test_every_active_allowlist_field_has_a_display_format() -> None:
    missing = [
        entry.field_key
        for entry in DEFAULT_NCES_ALLOWLIST
        if entry.active and entry.field_key not in _NCES_FIELD_DISPLAY
    ]
    assert not missing, (
        "Allowlisted profile fields missing a _NCES_FIELD_DISPLAY entry "
        f"(lookup_nces_profile_values would early-return []): {missing}"
    )


def test_navigator_only_fields_are_allowlisted_and_displayable() -> None:
    # Every navigator-only field must still be a real allowlist field with a
    # display format, or lookup_nces_profile_values can't return it.
    allowlist_keys = {e.field_key for e in DEFAULT_NCES_ALLOWLIST if e.active}
    for field_key in _NAVIGATOR_PROFILE_FIELDS:
        assert field_key in allowlist_keys, f"{field_key} not in active allowlist"
        assert field_key in _NCES_FIELD_DISPLAY, f"{field_key} missing display format"
