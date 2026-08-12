"""Governed allowlist of NCES profile fields Compass exposes.

The prior in-code tuple `_NCES_FIELDS` (at `db/catalog.py:51`) carried the
catalog of NCES fields with no paired governance migration. This module
lifts that tuple into a typed `NCESAllowlistEntry` model whose seed lives
in migration `060_compass_nces_allowlist.sql`. The sync test
`test_nces_allowlist_in_sync_with_migration` asserts code and DB cannot
drift.

The `canonical_url` field is the load-bearing addition: W2-M5-03 (#747)
renders NCES citations by pulling this URL per cited row, so the Sources
column on NCES-backed answers is non-empty (today it is, which is the
reporter's complaint in issue #747).

Doctrine — closure plan §1.2 "single source of truth": one module owns
one concept end-to-end. The DB migration is the auditable governance
surface; this module is the runtime authority paired with it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NCESAllowlistEntry(BaseModel):
    """One governed NCES profile field with its canonical citation URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    data_type: Literal["integer", "numeric", "text"]
    description: str = ""
    canonical_url: str = Field(min_length=1)
    canonical_title: str = Field(min_length=1)
    review_status: Literal["approved", "candidate", "rejected"] = "approved"
    active: bool = True


# Application order matches the prior in-code `_NCES_FIELDS` tuple. The
# canonical URLs point at the public NCES CCD/EDGE product pages that expose
# each field; these are stable across the CCD annual release cadence.
DEFAULT_NCES_ALLOWLIST: tuple[NCESAllowlistEntry, ...] = (
    NCESAllowlistEntry(
        field_key="enrollment",
        label="Enrollment",
        data_type="integer",
        description="Total district enrollment.",
        canonical_url="https://nces.ed.gov/ccd/elsi/",
        canonical_title="NCES Common Core of Data — ELSI",
    ),
    NCESAllowlistEntry(
        field_key="teachers_fte",
        label="Teachers FTE",
        data_type="numeric",
        description="Full-time equivalent teacher count.",
        canonical_url="https://nces.ed.gov/ccd/elsi/",
        canonical_title="NCES Common Core of Data — ELSI",
    ),
    NCESAllowlistEntry(
        field_key="pupil_teacher_ratio",
        label="Pupil-teacher ratio",
        data_type="numeric",
        description="District pupil-teacher ratio.",
        canonical_url="https://nces.ed.gov/ccd/elsi/",
        canonical_title="NCES Common Core of Data — ELSI",
    ),
    NCESAllowlistEntry(
        field_key="number_of_schools",
        label="Number of schools",
        data_type="integer",
        description="Number of schools in the district.",
        canonical_url="https://nces.ed.gov/ccd/elsi/",
        canonical_title="NCES Common Core of Data — ELSI",
    ),
    NCESAllowlistEntry(
        field_key="locale_text",
        label="Locale",
        data_type="text",
        description="NCES locale classification.",
        canonical_url="https://nces.ed.gov/programs/edge/Geographic/LocaleBoundaries",
        canonical_title="NCES EDGE — locale boundaries",
    ),
    NCESAllowlistEntry(
        field_key="total_rev_pp",
        label="Total revenue per pupil",
        data_type="numeric",
        description="Total revenue per pupil.",
        canonical_url="https://nces.ed.gov/ccd/f33agency.asp",
        canonical_title="NCES CCD — F-33 school district finance survey",
    ),
    NCESAllowlistEntry(
        field_key="total_exp_pp",
        label="Total expenditure per pupil",
        data_type="numeric",
        description="Total expenditure per pupil.",
        canonical_url="https://nces.ed.gov/ccd/f33agency.asp",
        canonical_title="NCES CCD — F-33 school district finance survey",
    ),
    NCESAllowlistEntry(
        field_key="frpl_pct",
        label="FRPL %",
        data_type="numeric",
        description="Free and reduced-price lunch share.",
        canonical_url="https://nces.ed.gov/ccd/elsi/",
        canonical_title="NCES Common Core of Data — ELSI",
    ),
)


def nces_allowlist_by_field_key() -> dict[str, NCESAllowlistEntry]:
    """Return the active allowlist keyed by ``field_key`` for O(1) lookup."""

    return {entry.field_key: entry for entry in DEFAULT_NCES_ALLOWLIST if entry.active}
