from __future__ import annotations

from compass_backend.quality.startup_check import (
    REQUIRED_M1_CATALOG_ALIASES,
    missing_required_m1_catalog_aliases,
)


def test_missing_required_m1_catalog_aliases_passes_when_all_present() -> None:
    rows = [
        {
            "normalized_alias": required.normalized_alias,
            "entity_type": required.entity_type,
            "resolution_status": required.resolution_status,
        }
        for required in REQUIRED_M1_CATALOG_ALIASES
    ]

    assert missing_required_m1_catalog_aliases(rows) == []


def test_missing_required_m1_catalog_aliases_reports_missing_rows() -> None:
    rows = [
        {
            "normalized_alias": required.normalized_alias,
            "entity_type": required.entity_type,
            "resolution_status": required.resolution_status,
        }
        for required in REQUIRED_M1_CATALOG_ALIASES
        if required.normalized_alias != "union release time"
    ]

    missing = missing_required_m1_catalog_aliases(rows)

    assert [(row.normalized_alias, row.entity_type) for row in missing] == [
        ("union release time", "unsupported_concept")
    ]
