"""Static guard for the active scenario/admin/debug cutover.

The B-spine case surface is the canonical scenario source for dashboard and
debug launch flows. These files must not grow new reads against the old
test_scenarios/golden_criteria tables.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]

SCENARIO_SURFACE_FILES = [
    ROOT / "src/compass_backend/api/scenarios.py",
    ROOT / "src/compass_backend/db/scenarios.py",
    ROOT / "src/compass_backend/contracts/scenarios.py",
    ROOT / "src/nctqai/services/compass_scenarios.py",
    ROOT / "src/nctqai/routes/compass/scenarios.py",
    ROOT / "src/nctqai/models/compass.py",
    ROOT / "src/compass_frontend/public/api/scenario.php",
    ROOT / "src/compass_frontend/public/api/scenario_auth.php",
    ROOT / "src/compass_frontend/public/api/debug.php",
    ROOT / "src/compass_frontend/public/assets/js/debug.js",
    ROOT / "src/compass_frontend/public/assets/js/app.js",
]


def test_active_scenario_surface_does_not_read_legacy_tables() -> None:
    offenders: list[str] = []

    for path in SCENARIO_SURFACE_FILES:
        text = path.read_text()
        for legacy_name in ("test_scenarios", "golden_criteria"):
            if legacy_name in text:
                offenders.append(f"{path.relative_to(ROOT)} references {legacy_name}")

    assert not offenders, "\n".join(offenders)


def test_remaining_legacy_backfill_is_one_time_and_traceable() -> None:
    migration = (
        ROOT
        / "src/compass_data_sync/migrations/031_backfill_remaining_legacy_scenarios.sql"
    )
    text = migration.read_text()

    assert "NOT EXISTS" in text
    assert "origin_test_scenario_id" in text
    assert "'type'," in text
    assert "'sort_order'," in text
    assert "FROM compass.test_scenarios ts" in text
    assert "INSERT INTO compass.cases" in text


def test_legacy_test_scenarios_303_304_resolution_is_traceable() -> None:
    migration = (
        ROOT
        / "src/compass_data_sync/migrations/077_resolve_legacy_test_scenarios_303_304.sql"
    )
    text = migration.read_text()

    assert "C11-NO-STANCE-FRAMING-C00" in text
    assert "'origin_test_scenario_id', 303" in text
    assert "'legacy_resolution', 'covered_by_existing_b_spine_case'" in text
    assert "REGR-M1-OBS-REGULARLY-EXACT" in text
    assert "REGR-M1-OBS-REGULARLY-EXACT-C00" in text
    assert "Which districts observe teachers regularly?" in text
    assert "'Filter Accuracy'" in text
    assert "'origin_test_scenario_id', 304" in text
    assert "active compass.test_scenarios rows still lack B-spine provenance" in text


def test_compass_v2_rules_surface_is_retired() -> None:
    retired_paths = [
        ROOT / "src/nctqai/routes/compass/v2/rules.py",
        ROOT / "src/nctqai/components/compass/v2/rule_row.py",
        ROOT / "src/nctqai/services/compass_v2/rules_stats.py",
    ]
    offenders = [str(path.relative_to(ROOT)) for path in retired_paths if path.exists()]
    assert not offenders, "retired v2 rules surface still present: " + ", ".join(offenders)

    v2_init = (ROOT / "src/nctqai/routes/compass/v2/__init__.py").read_text()
    assert "reg_rules" not in v2_init
    assert "/compass/v2/rules" not in v2_init
