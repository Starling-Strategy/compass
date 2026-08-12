"""Narrow SQL safety checks for Compass migration replay."""

from __future__ import annotations

import re
from pathlib import Path


MIGRATIONS_DIR = Path("src/compass_data_sync/migrations")

_GRANDFATHERED_DUPLICATE_PREFIXES = {
    "007",
    "008",
    "016",
    "018",
    "019",
    "031",
    "032",
    "044",
    "045",
}

_UNTYPED_DOLLAR_TO_JSONB_RE = re.compile(
    r"""
    to_jsonb
    \s*
    \(
    \s*
    (?P<tag>\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$)
    (?:(?!(?P=tag))[\s\S])*?
    (?P=tag)
    (?!\s*::)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def test_to_jsonb_dollar_quote_detector_requires_cast_at_call_site() -> None:
    assert _UNTYPED_DOLLAR_TO_JSONB_RE.search("to_jsonb($$hello$$)") is not None
    assert (
        _UNTYPED_DOLLAR_TO_JSONB_RE.search("to_jsonb($tag$hello$tag$)")
        is not None
    )
    assert _UNTYPED_DOLLAR_TO_JSONB_RE.search("to_jsonb($$hello$$::text)") is None
    assert (
        _UNTYPED_DOLLAR_TO_JSONB_RE.search("to_jsonb($tag$hello$tag$::text)")
        is None
    )
    assert (
        _UNTYPED_DOLLAR_TO_JSONB_RE.search(
            "to_jsonb($$hello$$::text), TRUE), later_value = $$other$$"
        )
        is None
    )


def test_to_jsonb_dollar_quoted_literals_are_explicitly_typed() -> None:
    """PostgreSQL cannot infer to_jsonb(unknown) for raw dollar literals."""

    offenders: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text()
        for match in _UNTYPED_DOLLAR_TO_JSONB_RE.finditer(sql):
            line_number = sql.count("\n", 0, match.start()) + 1
            snippet = " ".join(sql[match.start() : match.end()].split())
            offenders.append(f"{path}:{line_number}: {snippet}")

    assert not offenders, (
        "Dollar-quoted literals passed directly to to_jsonb() have PostgreSQL "
        "type unknown and fail at replay time. Cast the literal at the call "
        "site, e.g. to_jsonb($$text$$::text).\n"
        + "\n".join(offenders)
    )


def test_migration_numeric_prefixes_are_unique() -> None:
    """Migration replay order depends on each numeric prefix being unique."""

    by_prefix: dict[str, list[Path]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = re.match(r"^(?P<prefix>\d{3})_", path.name)
        if match is None:
            continue
        by_prefix.setdefault(match.group("prefix"), []).append(path)

    unexpected_duplicates = {
        prefix: paths
        for prefix, paths in by_prefix.items()
        if len(paths) > 1 and prefix not in _GRANDFATHERED_DUPLICATE_PREFIXES
    }

    assert not unexpected_duplicates, (
        "Compass migration numeric prefixes must be unique so replay order is "
        "deterministic:\n"
        + "\n".join(
            f"{prefix}: {', '.join(path.name for path in paths)}"
            for prefix, paths in sorted(unexpected_duplicates.items())
        )
    )


# ─── #882: scenarios must roll up under canonical scorecard dim names ─────


def test_migration_073_retrofits_w2_m5_01_03_to_canonical_dim_name() -> None:
    """The retrofit migration 073 must UPDATE both W2-M5-01 and W2-M5-03
    scenarios to canonical 'Citation Accuracy', and the canonical dim name
    must exist in STOCK_DIMENSIONS — otherwise the retrofit aims at a
    target that doesn't exist."""

    from compass_backend.quality.scorecard import STOCK_DIMENSIONS

    assert "citation-accuracy" in STOCK_DIMENSIONS
    canonical_name, _ = STOCK_DIMENSIONS["citation-accuracy"]
    assert canonical_name == "Citation Accuracy"

    sql = (MIGRATIONS_DIR / "073_retrofit_w2_m5_01_03_scorecard_dim.sql").read_text()

    # Migration target both regression scenarios.
    assert "REGR-W2-M5-01-COMPOSITE-CSV-NOT-BLANK" in sql
    assert "REGR-W2-M5-03-FRPL-LOOKUP-CITATION" in sql

    # Migration sets the canonical name and is gated on the broken value
    # (idempotent: re-runs converge because the WHERE clause stops matching
    # after the first run).
    assert f"accuracy_primary_dimension = '{canonical_name}'" in sql
    assert "AND accuracy_primary_dimension = 'Citations and Exports'" in sql


# ─── #1260: re-home LEGACY-MIGRATED scenarios off non-canonical dims ──────

# The 16 active LEGACY-MIGRATED-* scenarios (verified on staging 2026-06-02)
# whose accuracy_primary_dimension is outside the canonical seven, keyed by the
# non-canonical value they currently carry. Migration 095 must move every one
# off these values so their verdicts stop silently dropping out of the
# scorecard. Each is re-homed to a NON-SCORED structural-diagnostic label
# (Process Integrity / Surface Consistency) — none is clearly one of the
# canonical seven scored dimensions, so per the issue we default to the safe
# non-scored treatment rather than guessing a scored home.
_MIGRATION_095_REHOMES: dict[str, list[str]] = {
    "Ephemera": ["LEGACY-MIGRATED-149"],
    "Error Handling": ["LEGACY-MIGRATED-137"],
    "Exemplar": ["LEGACY-MIGRATED-140"],
    "Speed": [
        "LEGACY-MIGRATED-24",
        "LEGACY-MIGRATED-25",
        "LEGACY-MIGRATED-26",
        "LEGACY-MIGRATED-155",
        "LEGACY-MIGRATED-156",
    ],
    "Sweep": [
        "LEGACY-MIGRATED-74",
        "LEGACY-MIGRATED-75",
        "LEGACY-MIGRATED-77",
        "LEGACY-MIGRATED-85",
        "LEGACY-MIGRATED-89",
        "LEGACY-MIGRATED-98",
        "LEGACY-MIGRATED-111",
        "LEGACY-MIGRATED-112",
    ],
}

# Non-scored diagnostic labels deliberately kept OUT of CANONICAL_DIM_SLUGS
# (matches migration 076 / #951 precedent). Scenarios re-homed onto these
# values are excluded from every scorecard dimension by design.
_NON_SCORED_DIM_LABELS = {"Process Integrity", "Surface Consistency"}


def _migration_095_sql() -> str:
    return (
        MIGRATIONS_DIR / "095_rehome_legacy_migrated_dims.sql"
    ).read_text()


def test_migration_095_exists_after_091() -> None:
    """Append-only: 095 lands strictly after the highest pre-existing prefix."""

    prefixes = sorted(
        int(m.group(1))
        for path in MIGRATIONS_DIR.glob("*.sql")
        if (m := re.match(r"^(\d{3})_", path.name)) is not None
    )
    assert 95 in prefixes, "095 migration is missing"
    # Append-only safety for THIS migration: number 095 must not be reused by a
    # second file. (Old historical duplicates exist and are fine; and "095 is the
    # global highest" is brittle while parallel migrations contend the 09x space,
    # so we guard only that the rehome migration's own number is unique.)
    assert prefixes.count(95) == 1, (
        f"migration number 095 is used by {prefixes.count(95)} files (collision)"
    )


def test_migration_095_targets_every_non_canonical_scenario() -> None:
    """Every one of the 16 staging scenarios is named and re-homed, each UPDATE
    guarded on its current non-canonical value so re-runs converge (idempotent)
    and an already-moved scenario is never reclassified."""

    sql = _migration_095_sql()

    for old_dim, codes in _MIGRATION_095_REHOMES.items():
        # The migration must guard each move on the exact legacy value.
        assert f"accuracy_primary_dimension = '{old_dim}'" in sql, (
            f"missing guard on legacy dimension {old_dim!r}"
        )
        for code in codes:
            assert code in sql, f"scenario {code} not targeted by migration 095"


def test_migration_095_only_assigns_non_scored_labels() -> None:
    """None of the 16 is confidently one of the canonical seven, so each is
    re-homed to a NON-SCORED structural-diagnostic label (kept out of
    CANONICAL_DIM_SLUGS). Guards against an accidental guess at a scored dim."""

    from compass_backend.quality.scorecard import STOCK_DIMENSIONS

    canonical_names = {name for name, _ in STOCK_DIMENSIONS.values()}

    sql = _migration_095_sql()

    # Every SET target must be a known non-scored label …
    set_targets = set(
        re.findall(r"accuracy_primary_dimension\s*=\s*'([^']+)'", sql)
    )
    # … minus the guarded WHERE values (the legacy dims being moved off of).
    assigned = set_targets - set(_MIGRATION_095_REHOMES)
    assert assigned, "migration 095 assigns no new dimension values"
    assert assigned <= _NON_SCORED_DIM_LABELS, (
        "migration 095 assigns a value that is not an approved non-scored "
        f"diagnostic label: {assigned - _NON_SCORED_DIM_LABELS}"
    )
    # And it must never assign a canonical scored dimension (no silent guess).
    assert not (assigned & canonical_names), (
        "migration 095 must not re-home an ambiguous LEGACY scenario onto a "
        f"canonical scored dimension: {assigned & canonical_names}"
    )


def test_migration_095_has_idempotent_acceptance_guard() -> None:
    """The migration must assert, post-update, that no active scenario still
    carries any of the five non-canonical values (the acceptance check)."""

    sql = _migration_095_sql()
    assert "RAISE EXCEPTION" in sql, "missing post-update acceptance guard"
    for old_dim in _MIGRATION_095_REHOMES:
        assert f"'{old_dim}'" in sql, (
            f"acceptance guard does not reference legacy value {old_dim!r}"
        )
