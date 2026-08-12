"""Tests for the new B-spine repos (PR 1).

These tests use a real connection to the staging DB (via the same Settings pattern
the project already uses). They verify the repos can read what's there (initially:
no rows for the new tables) and write a verdict row end-to-end.

Run with:
    PYTHONPATH=src uv run pytest src/compass_backend/tests/test_b_spine_repos.py --tb=short --run-live-db
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from compass_backend.config import Settings
from compass_backend.db import (
    ReadOnlyCriteriaRepository,
    ReadOnlyScenariosV2Repository,
    VerdictsRepository,
)
from compass_backend.quality.criteria import VerdictRecord
from compass_backend.tests._evidence_fixtures import default_evidence_dict

pytestmark = pytest.mark.live_db


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


@pytest.mark.asyncio
async def test_load_active_criteria_returns_list(
    request: pytest.FixtureRequest,
) -> None:
    settings = _get_settings(request)
    pool = await _make_pool(settings)
    try:
        repo = ReadOnlyCriteriaRepository(pool)
        active = await repo.load_active()
        # Initially zero (PR 5 seeds them); the call itself should not raise.
        assert isinstance(active, list)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_load_scenarios_for_sort_accuracy_returns_list(
    request: pytest.FixtureRequest,
) -> None:
    settings = _get_settings(request)
    pool = await _make_pool(settings)
    try:
        repo = ReadOnlyScenariosV2Repository(pool)
        rows = await repo.load_scenarios_for_dimension("Sort Accuracy")
        # Initially zero (PRs 2 + 3 seed them); the call itself should not raise.
        assert isinstance(rows, list)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_verdicts_write_one_and_read_back(
    request: pytest.FixtureRequest,
) -> None:
    """End-to-end: insert a verdict (with a real criterion_id) and read it back.

    This requires a criterion to exist for FK; if the criteria table is empty,
    this test is skipped. PR 5 will populate criteria; before then this test
    runs against the empty state and skips.
    """
    settings = _get_settings(request)
    pool = await _make_pool(settings)
    new_id: str | None = None
    try:
        criteria_repo = ReadOnlyCriteriaRepository(pool)
        active = await criteria_repo.load_active()
        if not active:
            pytest.skip("no criteria seeded yet — re-run after PR 5 lands")

        verdict = VerdictRecord(
            session_id=str(uuid.uuid4()),
            turn_index=0,
            criterion_id=active[0].id,
            criterion_version_hash="test-hash-v1",
            criterion_set_version="test-set-v1",
            judge_source="deterministic",
            scope="turn",
            outcome="pass",
            reason="test fixture",
            evidence=default_evidence_dict(test=True),
            triggered_by="manual_replay",
        )
        repo = VerdictsRepository(pool)
        new_id = await repo.write_one(verdict)
        assert new_id, "expected a UUID back from write_one"

        fetched = await repo.read_one(new_id)
        assert fetched is not None
        assert fetched.session_id == verdict.session_id
        assert fetched.outcome == "pass"
    finally:
        # Cleanup: delete the test row so it doesn't pollute Scorecard counts
        if new_id is not None:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM compass.verdicts WHERE id = $1::uuid", new_id
                )
        await pool.close()
