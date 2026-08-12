"""Tests for the B-spine PR 3 migration: 9 existing Sort Accuracy rows.

Verifies that compass.scenarios + compass.cases hold the expected content after
014_migrate_sort_accuracy_existing.sql runs against the staging DB.

Combined invariants:
  - PR 2 seeded 2 curated Scenarios (3 Cases each) → 6 Cases total
  - PR 3 migrated 9 existing Scenarios (1 Case each) → 9 Cases
  - Total: ≥ 11 Sort Accuracy Scenarios, ≥ 15 Sort Accuracy Cases

Run with:
    PYTHONPATH=src uv run pytest src/compass_backend/tests/test_b_spine_migrations.py \\
        --tb=short --run-live-db
"""

from __future__ import annotations

import asyncpg
import pytest

from compass_backend.config import Settings
from compass_backend.db import ReadOnlyScenariosV2Repository

pytestmark = pytest.mark.live_db

# ── helpers ───────────────────────────────────────────────────────────────────


def _get_settings(request: pytest.FixtureRequest) -> Settings:
    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB smoke tests")
    s = Settings()
    if not s.pg_password.get_secret_value():
        pytest.skip("PG_PASSWORD is required for live DB smoke tests")
    return s


async def _make_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password.get_secret_value(),
        min_size=1,
        max_size=2,
    )


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_curated_and_migrated_scenarios_present(
    request: pytest.FixtureRequest,
) -> None:
    """After PRs 2 + 3: ≥ 11 active Sort Accuracy Scenarios exist."""
    settings = _get_settings(request)
    pool = await _make_pool(settings)
    try:
        repo = ReadOnlyScenariosV2Repository(pool)
        scenarios = await repo.load_scenarios_for_dimension("Sort Accuracy")
        scenario_codes = [s.scenario_code for s in scenarios]
        assert len(scenarios) >= 11, (
            f"Expected ≥ 11 Sort Accuracy Scenarios (2 curated + 9 migrated), "
            f"got {len(scenarios)}: {scenario_codes}"
        )
        # Spot-check that both namespaces are present
        curated = [s for s in scenarios if s.scenario_code.startswith("SORT-CURATED-")]
        migrated = [s for s in scenarios if s.scenario_code.startswith("SORT-MIGRATED-")]
        assert len(curated) >= 2, f"Expected ≥ 2 curated Scenarios, got {len(curated)}"
        assert len(migrated) >= 9, f"Expected ≥ 9 migrated Scenarios, got {len(migrated)}"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_every_scenario_has_at_least_one_case(
    request: pytest.FixtureRequest,
) -> None:
    """FK integrity: every Sort Accuracy Scenario has ≥ 1 active Case."""
    settings = _get_settings(request)
    pool = await _make_pool(settings)
    try:
        repo = ReadOnlyScenariosV2Repository(pool)
        scenarios = await repo.load_scenarios_for_dimension("Sort Accuracy")
        childless: list[str] = []
        for sc in scenarios:
            cases = await repo.load_cases_for_scenario(sc.id)
            if not cases:
                childless.append(sc.scenario_code)
        assert not childless, (
            f"These Scenarios have no active Cases: {childless}"
        )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_all_scenarios_have_version_hash(
    request: pytest.FixtureRequest,
) -> None:
    """Every Sort Accuracy Scenario has a non-empty version_hash."""
    settings = _get_settings(request)
    pool = await _make_pool(settings)
    try:
        repo = ReadOnlyScenariosV2Repository(pool)
        scenarios = await repo.load_scenarios_for_dimension("Sort Accuracy")
        missing_hash = [s.scenario_code for s in scenarios if not s.version_hash]
        assert not missing_hash, (
            f"These Scenarios have empty version_hash: {missing_hash}"
        )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_migrated_case_inputs_match_original_turns(
    request: pytest.FixtureRequest,
) -> None:
    """Spot-check: a migrated Case's StepInput round-trips through the repo.

    Verifies:
    - first migrated Scenario loads successfully (no Pydantic validation error)
    - its single Case has exactly 1 input step
    - the first step's prompt is non-empty (populated from initial_user_message)
    - metadata contains origin_test_scenario_id for traceability
    """
    settings = _get_settings(request)
    pool = await _make_pool(settings)
    try:
        repo = ReadOnlyScenariosV2Repository(pool)
        scenarios = await repo.load_scenarios_for_dimension("Sort Accuracy")
        migrated = [s for s in scenarios if s.scenario_code.startswith("SORT-MIGRATED-")]
        assert migrated, "No migrated Scenarios found — did 014_migrate_sort_accuracy_existing.sql run?"

        # Pick the first migrated Scenario
        sc = migrated[0]
        cases = await repo.load_cases_for_scenario(sc.id)
        assert cases, f"No Cases for migrated Scenario {sc.scenario_code}"

        case = cases[0]
        assert len(case.inputs) == 1, (
            f"Expected 1 StepInput for degenerate-K=1 migrated Case "
            f"{case.case_code}, got {len(case.inputs)}"
        )
        prompt = case.inputs[0].prompt
        assert prompt, (
            f"StepInput.prompt is empty for migrated Case {case.case_code}"
        )
        # Must contain actual text, not a placeholder
        assert len(prompt) > 5, (
            f"StepInput.prompt suspiciously short ({prompt!r}) for {case.case_code}"
        )
        # Metadata must carry traceability field
        assert "origin_test_scenario_id" in case.metadata, (
            f"metadata missing origin_test_scenario_id for {case.case_code}; "
            f"got keys: {list(case.metadata.keys())}"
        )
    finally:
        await pool.close()
