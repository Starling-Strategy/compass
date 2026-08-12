"""Integration tests for the L2 verdict pipeline (Spec B PR 6).

These tests use the real staging DB to verify:
  1. evaluate_and_write writes ≥2 verdicts for a sort-intent turn with a table.
  2. evaluate_and_write writes ≥1 verdict even for a turn with no result artifact.

Tests hit the compass schema on staging and clean up after themselves.

Run with:
    PYTHONPATH=src uv run pytest src/compass_backend/tests/test_l2_pipeline.py \
        --tb=short --run-live-db
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import asyncpg
import pytest

from compass_backend.artifacts import (
    MetricRankingResult,
)
from compass_backend.artifacts.results import RankingRow, ResultSet
from compass_backend.config import Settings
from compass_backend.contracts.chat import ChatResponse
from compass_backend.contracts.planning import (
    DirectResponse,
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    SortSpec,
    TemporalSpec,
)
from compass_backend.contracts.session import SessionState
from compass_backend.quality.verdict_pipeline import evaluate_and_write

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


def _fake_session(session_id: str | None = None) -> SessionState:
    """Return a minimal SessionState for testing."""
    sid = session_id or str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc)
    return SessionState(
        session_id=sid,
        created_at=now,
        updated_at=now,
        turn_count=1,  # post-save count
    )


def _sort_turn() -> PlannerTurn:
    """Return a planner turn that looks like a sort/ranking request."""
    return PlannerTurn(
        route="execute",
        confidence=0.95,
        query_plan=QueryPlan(
            operation="rank",
            question="show me the top 5 districts by enrollment",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="enrollment")],
            output=OutputSpec(format="table"),
            temporal=TemporalSpec(intent="current", academic_year="2024 - 2025"),
            sort=SortSpec(field="enrollment", direction="desc"),
        ),
    )


def _direct_turn() -> PlannerTurn:
    """Return a planner turn for a direct (non-execute) response."""
    return PlannerTurn(
        route="direct",
        confidence=0.5,
        direct_response=DirectResponse(
            message="I can help you with district data. What metric are you interested in?",
            reason="User intent is vague; prompting for specifics.",
        ),
    )


def _ranking_rows(n: int = 3) -> list[RankingRow]:
    """Return minimal RankingRow stubs for the artifact snapshot."""
    return [
        RankingRow(
            district_id=i + 1,
            district_name=f"District {i + 1}",
            state="CA",
            metric_id=100,
            metric_name="enrollment",
            display_value=str(10000 - i * 1000),
            academic_year="2024 - 2025",
            rank=i + 1,
        )
        for i in range(n)
    ]


def _result_set(rows: list | None = None) -> ResultSet:
    """Return a minimal ResultSet for testing."""
    r = rows or _ranking_rows(3)
    return MetricRankingResult(        rows=r,
        citations=[],
        total_considered=50,
        excluded_count=0,
        order_statement="Sorted by enrollment descending",
    )


def _make_response(
    *,
    session_id: str | None = None,
    turn: PlannerTurn | None = None,
    result: ResultSet | None = None,
    message: str = "Here are the top districts by enrollment.",
    trace_id: str | None = None,
) -> ChatResponse:
    """Assemble a ChatResponse matching the shape build_chat_response() produces."""
    session = _fake_session(session_id)
    return ChatResponse(
        message=message,
        turn=turn or _sort_turn(),
        session=session,
        snapshot_id=str(uuid.uuid4()),
        result=result,
        validation=None,
        manifest=None,
        trace_id=trace_id or "test-trace-" + session.session_id[:8],
        logfire_url=None,
        message_ids=[],
    )


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_and_write_produces_verdicts_for_sort_turn(
    request: pytest.FixtureRequest,
) -> None:
    """A sort-intent turn with a table artifact writes ≥2 verdicts to compass.verdicts.

    Sort criteria expected: forbidden_temporal_phrases (mandatory global) +
    sort_descending + respects_user_limit (prefilters). That gives ≥ 2 before
    the AI classifier runs; AI classifier may add more.

    Written verdict IDs are cleaned up after the test.
    """
    settings = _get_settings(request)
    pool = await _make_pool(settings)
    written_ids: list[str] = []
    try:
        # Check criteria are seeded — if not, skip (PR 5 may not have run yet)
        from compass_backend.db import ReadOnlyCriteriaRepository

        repo = ReadOnlyCriteriaRepository(pool)
        active = await repo.load_active()
        if not active:
            pytest.skip("no criteria seeded — re-run after PR 5 migration lands")

        response = _make_response(
            turn=_sort_turn(),
            result=_result_set(),
        )

        # Disable AI classifier to keep the test deterministic (no LLM calls)
        written_ids = await evaluate_and_write(
            response,
            pool=pool,
            triggered_by="user_turn",
            use_ai_classifier=False,
        )

        assert len(written_ids) >= 2, (
            f"Expected ≥ 2 verdicts for a sort turn with a table; got {len(written_ids)}. "
            f"Mandatory global (forbidden_temporal_phrases) + at least one Sort prefilter "
            f"(sort_descending or respects_user_limit) should fire."
        )

        # Verify the written rows look correct
        from compass_backend.db import VerdictsRepository

        verdict_repo = VerdictsRepository(pool)
        for vid in written_ids:
            fetched = await verdict_repo.read_one(vid)
            assert fetched is not None, f"verdict {vid} not found in DB"
            assert fetched.session_id == response.session.session_id
            assert fetched.turn_index == response.session.turn_count
            assert fetched.triggered_by == "user_turn"
            assert fetched.outcome in ("pass", "fail", "error")

    finally:
        # Cleanup: remove all test-session verdicts so they don't pollute counts
        if written_ids:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM compass.verdicts WHERE id = ANY($1::uuid[])",
                    [uuid.UUID(vid) for vid in written_ids],
                )
        await pool.close()


@pytest.mark.asyncio
async def test_evaluate_and_write_handles_no_artifact(
    request: pytest.FixtureRequest,
) -> None:
    """A turn with no result artifact still writes ≥1 verdict (mandatory global fires).

    The mandatory global (forbidden_temporal_phrases) fires unconditionally.
    Even when result=None (direct response), we expect it to appear.
    """
    settings = _get_settings(request)
    pool = await _make_pool(settings)
    written_ids: list[str] = []
    try:
        from compass_backend.db import ReadOnlyCriteriaRepository

        repo = ReadOnlyCriteriaRepository(pool)
        mandatory = await repo.load_mandatory_globals()
        if not mandatory:
            pytest.skip("no mandatory global criteria seeded — re-run after PR 5 migration")

        response = _make_response(
            turn=_direct_turn(),
            result=None,  # no artifact
            message="I can help you with district data. What metric are you interested in?",
        )

        written_ids = await evaluate_and_write(
            response,
            pool=pool,
            triggered_by="user_turn",
            use_ai_classifier=False,
        )

        assert len(written_ids) >= 1, (
            f"Expected ≥ 1 verdict (mandatory global) even with no result artifact; "
            f"got {len(written_ids)}."
        )

        # Verify mandatory global criterion appears in results
        from compass_backend.db import VerdictsRepository

        verdict_repo = VerdictsRepository(pool)
        written_criterion_ids = set()
        for vid in written_ids:
            fetched = await verdict_repo.read_one(vid)
            assert fetched is not None
            written_criterion_ids.add(fetched.criterion_id)

        # At least one written criterion should be a mandatory global
        mandatory_ids = {c.id for c in mandatory}
        overlap = written_criterion_ids & mandatory_ids
        assert overlap, (
            f"Expected at least one mandatory global criterion in written verdicts. "
            f"Mandatory IDs: {mandatory_ids}; written criterion IDs: {written_criterion_ids}"
        )

    finally:
        if written_ids:
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM compass.verdicts WHERE id = ANY($1::uuid[])",
                    [uuid.UUID(vid) for vid in written_ids],
                )
        await pool.close()
