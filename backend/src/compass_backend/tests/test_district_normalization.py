"""Tests for governed district-name normalization rules."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from compass_backend.catalog.district_normalization import (
    CatalogIntegrityError,
    DEFAULT_DISTRICT_NORMALIZATION_RULES,
    DistrictNameNormalizer,
    DistrictNormalizationRule,
    normalize_district_name_for_resolution,
)


MIGRATION_PATH = Path(
    "src/compass_data_sync/migrations/033_district_normalization_rules.sql"
)


# ---------------------------------------------------------------------------
# Behavior — must match the pre-PR-B hardcoded function byte-for-byte.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Alpha Public School District", "alpha"),
        ("Dallas ISD", "dallas"),
        ("Alpha USD", "alpha usd"),
        ("  Alpha--Unified Schools ", "alpha"),
        ("San Bernardino City Unified", "san bernardino city"),
        ("Acme & Bee Schools", "acme and bee"),
        ("MONTGOMERY COUNTY PUBLIC SCHOOLS", "montgomery county"),
        ("Howard County School District", "howard county"),
        ("Independent School District", ""),
        ("ISD", ""),
        ("   ", ""),
        ("Some Place 2024-25", "some place 2024 25"),
    ],
)
def test_normalize_district_name_for_resolution(raw: str, expected: str) -> None:
    assert normalize_district_name_for_resolution(raw) == expected


# ---------------------------------------------------------------------------
# Sync-with-migration: in-code default tuple must mirror migration 033 seed.
# ---------------------------------------------------------------------------


# Each SQL string literal allows escaped single quotes (`''`). The literal
# pattern below matches either NULL or a quoted run where any internal
# quote is doubled — i.e. `'...'` where `...` is `[^']*(?:''[^']*)*`.
_SQL_STRING = r"(?:NULL|'[^']*(?:''[^']*)*')"
# Capture the pattern + replacement sub-literals with their own match
# objects so we can decode them while still anchoring the row shape.
_FULL_ROW_RE = re.compile(
    r"\(\s*(\d+)\s*,\s*'(\w+)'\s*,\s*("
    + _SQL_STRING
    + r")\s*,\s*("
    + _SQL_STRING
    + r")\s*,\s*("
    + _SQL_STRING
    + r")\s*\)",
    re.MULTILINE,
)


def _decode_sql_literal(raw: str) -> str | None:
    if raw == "NULL":
        return None
    # Strip outer quotes; SQL escapes '' as ' — reverse that.
    return raw[1:-1].replace("''", "'")


def _parse_migration_seed_rules() -> list[DistrictNormalizationRule]:
    sql = MIGRATION_PATH.read_text()
    rules: list[DistrictNormalizationRule] = []
    for match in _FULL_ROW_RE.finditer(sql):
        order_raw, rule_type, pattern_raw, replacement_raw, notes_raw = match.groups()
        notes = _decode_sql_literal(notes_raw) or ""
        rules.append(
            DistrictNormalizationRule(
                rule_order=int(order_raw),
                rule_type=rule_type,  # type: ignore[arg-type]
                pattern=_decode_sql_literal(pattern_raw),
                replacement=_decode_sql_literal(replacement_raw),
                notes=notes,
            )
        )
    return rules


def test_district_normalization_rules_in_sync_with_migration() -> None:
    """In-code defaults must match the migration 033 INSERT rows exactly."""

    in_code = list(DEFAULT_DISTRICT_NORMALIZATION_RULES)
    in_sql = _parse_migration_seed_rules()
    assert len(in_code) == len(in_sql), (
        f"rule count drift: in-code={len(in_code)} migration={len(in_sql)}"
    )
    in_code_by_order = {rule.rule_order: rule for rule in in_code}
    for sql_rule in in_sql:
        code_rule = in_code_by_order[sql_rule.rule_order]
        assert code_rule.rule_type == sql_rule.rule_type, (
            f"rule_type drift at order={sql_rule.rule_order}: "
            f"in-code={code_rule.rule_type!r} migration={sql_rule.rule_type!r}"
        )
        assert code_rule.pattern == sql_rule.pattern, (
            f"pattern drift at order={sql_rule.rule_order}: "
            f"in-code={code_rule.pattern!r} migration={sql_rule.pattern!r}"
        )
        assert code_rule.replacement == sql_rule.replacement, (
            f"replacement drift at order={sql_rule.rule_order}: "
            f"in-code={code_rule.replacement!r} migration={sql_rule.replacement!r}"
        )
        assert code_rule.notes == sql_rule.notes, (
            f"notes drift at order={sql_rule.rule_order}"
        )


def test_district_normalization_rules_migration_declares_governed_schema() -> None:
    """Migration 033 must declare the table, check constraints, and unique index."""

    sql = MIGRATION_PATH.read_text()
    assert "CREATE TABLE IF NOT EXISTS compass.district_normalization_rules" in sql
    assert "rule_order INT NOT NULL" in sql
    assert "rule_type TEXT NOT NULL" in sql
    assert "review_status TEXT NOT NULL DEFAULT 'approved'" in sql
    assert "active BOOLEAN NOT NULL DEFAULT TRUE" in sql
    assert "district_normalization_rules_rule_type_check" in sql
    assert "district_normalization_rules_review_status_check" in sql
    assert "idx_district_normalization_rules_order_active" in sql


# ---------------------------------------------------------------------------
# Fail-closed: empty rules tuple must raise CatalogIntegrityError.
# ---------------------------------------------------------------------------


def test_normalizer_construction_fails_closed_on_empty_rules() -> None:
    with pytest.raises(CatalogIntegrityError):
        DistrictNameNormalizer(())


def test_normalizer_construction_fails_closed_when_all_rules_inactive() -> None:
    inactive = tuple(
        rule.model_copy(update={"active": False})
        for rule in DEFAULT_DISTRICT_NORMALIZATION_RULES
    )
    with pytest.raises(CatalogIntegrityError):
        DistrictNameNormalizer(inactive)


# ---------------------------------------------------------------------------
# Per-rule-type behavior smoke tests.
# ---------------------------------------------------------------------------


def test_normalizer_skips_inactive_rules() -> None:
    """Disabling the suffix-strip rules preserves the suffix in the output."""

    rules = tuple(
        rule.model_copy(update={"active": rule.rule_type != "suffix_strip"})
        for rule in DEFAULT_DISTRICT_NORMALIZATION_RULES
    )
    normalizer = DistrictNameNormalizer(rules)
    # All of lowercase / regex_strip / whitespace_collapse still apply.
    # Without suffix_strip, "Dallas ISD" keeps "isd" suffixed.
    assert normalizer("Dallas ISD") == "dallas isd"


def test_normalization_rule_rejects_invalid_replacement_for_lowercase() -> None:
    with pytest.raises(ValueError):
        DistrictNormalizationRule(
            rule_order=1,
            rule_type="lowercase",
            replacement="X",
        )


def test_normalization_rule_rejects_missing_pattern_for_replace() -> None:
    with pytest.raises(ValueError):
        DistrictNormalizationRule(
            rule_order=1,
            rule_type="replace",
            replacement="x",
        )
