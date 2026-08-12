"""Tests for the compass quality service loaders.

Unit tests mock the backend pure functions to avoid DB connections.
Live-DB smoke tests are gated by --run-live-db.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from compass_backend.quality.scorecard_models import (
    CANONICAL_DIM_SLUGS,
    BuildContext,
    ConversationTurn,
    ConversationWithVerdicts,
    DimensionDetail,
    DimensionScore,
    ScorecardSnapshot,
    Verdict,
)
from nctqai.services.compass_quality import _fixture
from nctqai.services.compass_quality import loaders
from nctqai.services.compass_quality.loaders import (
    load_conversation_with_verdicts,
    load_dimension,
    load_scorecard,
)


# ── Helpers to build fixture-like objects ─────────────────────────────────

def _make_snapshot() -> ScorecardSnapshot:
    """Build a ScorecardSnapshot with 9 placeholder dimensions."""
    dims = [
        DimensionScore(
            dim_slug=slug,
            name=slug.replace("-", " ").title(),
            definition="Test definition.",
            score_pct=0,
            n_trials=0,
            regressed=False,
        )
        for slug in CANONICAL_DIM_SLUGS
    ]
    return ScorecardSnapshot(
        build=BuildContext(
            build_id="b-2026-05-14",
            sweep_id="live",
            criterion_set_version="compass_criteria_v1",
            cases=0,
            trials=0,
        ),
        dimensions=dims,
    )


def _make_detail(dim_slug: str) -> DimensionDetail:
    return DimensionDetail(
        build=BuildContext(
            build_id="b-2026-05-14",
            sweep_id="live",
            criterion_set_version="compass_criteria_v1",
            cases=0,
            trials=0,
        ),
        dimension=DimensionScore(
            dim_slug=dim_slug,
            name=dim_slug.replace("-", " ").title(),
            definition="Test definition.",
            score_pct=0,
            n_trials=0,
            regressed=False,
        ),
        cases=[],
    )


def _make_conversation(session_id: str) -> ConversationWithVerdicts:
    return ConversationWithVerdicts(
        session_id=session_id,
        trace_id=None,
        scenario_id=None,
        scenario_name=None,
        started_at="2026-05-14T10:00:00",
        turns=[
            ConversationTurn(
                turn_index=0,
                user_text="What are the top 5 districts?",
                assistant_text="The top 5 districts are...",
                verdicts=[
                    Verdict(
                        criterion_id="C-sort",
                        judge_source="judge_prompt",
                        outcome="fail",
                        reason="Wrong order",
                    )
                ],
            )
        ],
    )


# ── Unit tests (mocked backend pure functions) ────────────────────────────

def test_load_scorecard_returns_seven_dimensions(monkeypatch):
    """load_scorecard returns a snapshot with 7 dims in canonical order."""
    snapshot = _make_snapshot()

    # Patch _load to call the coro_factory with a fake pool synchronously,
    # bypassing the real asyncio.run() + asyncpg pool creation.
    def fake_load(coro_factory):
        return asyncio.run(coro_factory(MagicMock()))

    async def fake_compute(*args, **kwargs):
        return snapshot

    monkeypatch.setattr("nctqai.services.compass_quality.loaders._load", fake_load)
    monkeypatch.setattr(
        "nctqai.services.compass_quality.loaders.compute_scorecard_snapshot",
        fake_compute,
    )

    snap = load_scorecard()
    assert len(snap.dimensions) == 7
    assert [d.dim_slug for d in snap.dimensions] == list(CANONICAL_DIM_SLUGS)


def test_load_scorecard_canonical_order(monkeypatch):
    """Dimensions come back in CANONICAL_DIM_SLUGS order."""
    snapshot = _make_snapshot()

    def fake_load(coro_factory):
        return asyncio.run(coro_factory(MagicMock()))

    async def fake_compute(*args, **kwargs):
        return snapshot

    monkeypatch.setattr("nctqai.services.compass_quality.loaders._load", fake_load)
    monkeypatch.setattr(
        "nctqai.services.compass_quality.loaders.compute_scorecard_snapshot",
        fake_compute,
    )

    snap = load_scorecard()
    for i, slug in enumerate(CANONICAL_DIM_SLUGS):
        assert snap.dimensions[i].dim_slug == slug


def test_load_scorecard_uses_dashboard_pg_env_without_shared_pg(monkeypatch):
    """The dashboard scorecard loader honors NCTQAI_PG_* without bare PG_*."""
    snapshot = _make_snapshot()
    captured_kwargs = {}

    for key in ("PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NCTQAI_PG_HOST", "dashboard-db")
    monkeypatch.setenv("NCTQAI_PG_PORT", "6543")
    monkeypatch.setenv("NCTQAI_PG_DATABASE", "dashboard_db")
    monkeypatch.setenv("NCTQAI_PG_USER", "dashboard_user")
    monkeypatch.setenv("NCTQAI_PG_PASSWORD", "dashboard_pw")

    class FakePool:
        async def close(self):
            return None

    class FakePoolAwaitable:
        def __await__(self):
            async def _resolve():
                return FakePool()

            return _resolve().__await__()

    def fake_create_pool(**kwargs):
        captured_kwargs.update(kwargs)
        return FakePoolAwaitable()

    async def fake_compute(*args, **kwargs):
        return snapshot

    monkeypatch.setattr(loaders.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(loaders, "compute_scorecard_snapshot", fake_compute)

    snap = load_scorecard()

    assert snap == snapshot
    assert captured_kwargs["host"] == "dashboard-db"
    assert captured_kwargs["port"] == 6543
    assert captured_kwargs["database"] == "dashboard_db"
    assert captured_kwargs["user"] == "dashboard_user"
    assert captured_kwargs["password"] == "dashboard_pw"
    assert captured_kwargs["loop"] is not None


def test_load_dimension_returns_detail(monkeypatch):
    """load_dimension returns a DimensionDetail for the requested slug."""
    detail = _make_detail("sort-accuracy")

    def fake_load(coro_factory):
        return asyncio.run(coro_factory(MagicMock()))

    async def fake_compute(*args, **kwargs):
        return detail

    monkeypatch.setattr("nctqai.services.compass_quality.loaders._load", fake_load)
    monkeypatch.setattr(
        "nctqai.services.compass_quality.loaders.compute_dimension_detail",
        fake_compute,
    )

    d = load_dimension(dim_slug="sort-accuracy")
    assert d.dimension.dim_slug == "sort-accuracy"


def test_load_scorecard_threads_sweep_run_id(monkeypatch):
    """load_scorecard(sweep_run_id=...) passes sweep_run_id to compute_scorecard_snapshot."""
    snapshot = _make_snapshot()
    captured_kwargs = {}

    def fake_load(coro_factory):
        return asyncio.run(coro_factory(MagicMock()))

    async def fake_compute(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return snapshot

    monkeypatch.setattr("nctqai.services.compass_quality.loaders._load", fake_load)
    monkeypatch.setattr(
        "nctqai.services.compass_quality.loaders.compute_scorecard_snapshot",
        fake_compute,
    )

    load_scorecard(sweep_run_id="some-uuid")
    assert captured_kwargs.get("sweep_run_id") == "some-uuid"


def test_load_dimension_threads_sweep_run_id(monkeypatch):
    """load_dimension(sweep_run_id=...) passes sweep_run_id to compute_dimension_detail."""
    detail = _make_detail("sort-accuracy")
    captured_kwargs = {}

    def fake_load(coro_factory):
        return asyncio.run(coro_factory(MagicMock()))

    async def fake_compute(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return detail

    monkeypatch.setattr("nctqai.services.compass_quality.loaders._load", fake_load)
    monkeypatch.setattr(
        "nctqai.services.compass_quality.loaders.compute_dimension_detail",
        fake_compute,
    )

    load_dimension(dim_slug="sort-accuracy", sweep_run_id="some-uuid")
    assert captured_kwargs.get("sweep_run_id") == "some-uuid"


def test_load_conversation_returns_turns(monkeypatch):
    """load_conversation_with_verdicts returns a ConversationWithVerdicts."""
    conv = _make_conversation("s-test-123")

    def fake_load(coro_factory):
        return asyncio.run(coro_factory(MagicMock()))

    async def fake_compute(*args, **kwargs):
        return conv

    monkeypatch.setattr("nctqai.services.compass_quality.loaders._load", fake_load)
    monkeypatch.setattr(
        "nctqai.services.compass_quality.loaders.compute_conversation_with_verdicts",
        fake_compute,
    )

    result = load_conversation_with_verdicts("s-test-123")
    assert result.session_id == "s-test-123"
    assert len(result.turns) == 1
    assert any(v.outcome == "fail" for v in result.turns[0].verdicts)


def test_load_conversation_propagates_key_error(monkeypatch):
    """KeyError from backend propagates through the loader."""
    def fake_load(coro_factory):
        return asyncio.run(coro_factory(MagicMock()))

    async def fake_compute(*args, **kwargs):
        raise KeyError("s-not-found")

    monkeypatch.setattr("nctqai.services.compass_quality.loaders._load", fake_load)
    monkeypatch.setattr(
        "nctqai.services.compass_quality.loaders.compute_conversation_with_verdicts",
        fake_compute,
    )

    with pytest.raises(KeyError):
        load_conversation_with_verdicts("s-not-found")


# ── build_detail: inline verdicts + bridge resilience (DASH-R5 / DASH-R7) ──


def _patch_detail_reads(monkeypatch, *, messages, conv_with_verdicts=None, raises=None):
    """Patch the three reads build_detail makes (transcript, feedback, verdicts)."""
    from fasthtml.common import to_xml  # noqa: F401

    from nctqai.components.compass import conversation_detail as cd
    from nctqai.models.compass import ChatMessage, ConversationDetail

    convo = ConversationDetail(
        session_id="s-1",
        messages=[ChatMessage.model_validate(m) for m in messages],
    )
    monkeypatch.setattr(cd, "get_conversation", lambda sid: convo)
    monkeypatch.setattr(cd, "get_session_feedback", lambda sid: [])

    def _verdict_loader(sid):
        if raises is not None:
            raise raises
        return conv_with_verdicts

    monkeypatch.setattr(cd, "load_conversation_with_verdicts", _verdict_loader)
    return cd


def test_build_detail_renders_inline_verdicts(monkeypatch):
    """build_detail attaches the real per-turn verdicts under each paired turn."""
    from fasthtml.common import to_xml

    cd = _patch_detail_reads(
        monkeypatch,
        messages=[
            {"id": 1, "role": "user", "content": "Top 5 districts?"},
            {"id": 2, "role": "assistant", "content": "Here they are."},
        ],
        conv_with_verdicts=ConversationWithVerdicts(
            session_id="s-1",
            trace_id=None,
            scenario_id=None,
            scenario_name=None,
            started_at="2026-06-21T10:00:00",
            turns=[
                ConversationTurn(
                    turn_index=1,
                    user_text="Top 5 districts?",
                    assistant_text="Here they are.",
                    verdicts=[
                        Verdict(
                            criterion_id="sort_descending",
                            judge_source="deterministic",
                            outcome="pass",
                            reason="ok",
                        )
                    ],
                )
            ],
        ),
    )
    html = to_xml(cd.build_detail("s-1", None))
    assert "quality-verdict-list" in html
    assert "sort_descending" in html
    assert "compass-turn-verdicts" in html


def test_build_detail_survives_verdict_bridge_failure(monkeypatch):
    """If the verdict bridge raises (pool down), the transcript still renders —
    read-only resilience (DASH-R7), no 500, just no verdict blocks."""
    from fasthtml.common import to_xml

    cd = _patch_detail_reads(
        monkeypatch,
        messages=[
            {"id": 1, "role": "user", "content": "A question here"},
            {"id": 2, "role": "assistant", "content": "An answer."},
        ],
        raises=RuntimeError("pool down"),
    )
    html = to_xml(cd.build_detail("s-1", None))
    assert "An answer." in html
    assert "compass-turn-verdicts" not in html  # no verdict block on failure


def test_build_detail_survives_unknown_session_keyerror(monkeypatch):
    """A KeyError (session not in the verdict view) degrades to no verdicts."""
    from fasthtml.common import to_xml

    cd = _patch_detail_reads(
        monkeypatch,
        messages=[
            {"id": 1, "role": "user", "content": "A question here"},
            {"id": 2, "role": "assistant", "content": "An answer."},
        ],
        raises=KeyError("s-1"),
    )
    html = to_xml(cd.build_detail("s-1", None))
    assert "An answer." in html
    assert "compass-turn-verdicts" not in html


# ── Fixture still works as a test utility ────────────────────────────────

def test_fixture_default_scorecard_has_seven_dimensions():
    """The fixture object still has 7 dimensions in canonical order."""
    snap = _fixture.DEFAULT_SCORECARD
    assert len(snap.dimensions) == 7
    slugs = [d.dim_slug for d in snap.dimensions]
    assert slugs == list(CANONICAL_DIM_SLUGS)


def test_fixture_sort_accuracy_has_six_cases():
    """The fixture detail for sort-accuracy has 6 cases."""
    detail = _fixture.dimension_detail("sort-accuracy")
    assert detail.dimension.dim_slug == "sort-accuracy"
    assert len(detail.cases) == 6


def test_fixture_conversation_has_turns_and_verdicts():
    """The fixture conversation has at least 1 turn with a failed verdict."""
    conv = _fixture.conversation("s-7a2b9f")
    assert conv.session_id == "s-7a2b9f"
    assert len(conv.turns) == 1
    assert any(v.outcome == "fail" for v in conv.turns[0].verdicts)


# ── Live-DB smoke tests ───────────────────────────────────────────────────
#
# These tests bypass the singleton-pool loaders to avoid asyncio event-loop
# reuse issues in pytest. They call the backend async functions directly,
# the same way test_scorecard.py does.

@pytest.mark.asyncio
@pytest.mark.live_db
async def test_load_scorecard_live_db_returns_seven_dims(request):
    """Live: scorecard snapshot returns 7 dimensions with correct slugs."""
    import asyncpg
    from compass_backend.config import Settings
    from compass_backend.quality.scorecard import compute_scorecard_snapshot

    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB smoke tests")
    s = Settings()
    if not s.pg_password.get_secret_value():
        pytest.skip("PG_PASSWORD is required for live DB smoke tests")
    pool = await asyncpg.create_pool(
        host=s.pg_host, port=s.pg_port, database=s.pg_database,
        user=s.pg_user, password=s.pg_password.get_secret_value(),
        min_size=1, max_size=2,
    )
    try:
        snap = await compute_scorecard_snapshot(pool)
        assert len(snap.dimensions) == 7
        assert [d.dim_slug for d in snap.dimensions] == list(CANONICAL_DIM_SLUGS)
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.live_db
async def test_load_scorecard_live_db_sort_accuracy_has_real_data(request):
    """Live: sort-accuracy has n_trials > 0 (verdicts seeded by PR 6/7)."""
    import asyncpg
    from compass_backend.config import Settings
    from compass_backend.quality.scorecard import compute_scorecard_snapshot

    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB smoke tests")
    s = Settings()
    if not s.pg_password.get_secret_value():
        pytest.skip("PG_PASSWORD is required for live DB smoke tests")
    pool = await asyncpg.create_pool(
        host=s.pg_host, port=s.pg_port, database=s.pg_database,
        user=s.pg_user, password=s.pg_password.get_secret_value(),
        min_size=1, max_size=2,
    )
    try:
        snap = await compute_scorecard_snapshot(pool)
        sort_dim = next(d for d in snap.dimensions if d.dim_slug == "sort-accuracy")
        assert sort_dim.n_trials > 0, (
            "Expected sort-accuracy to have real trial data. "
            "Run pa-eval sort-accuracy --k 3 --limit 5 to seed verdicts."
        )
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.live_db
async def test_load_scorecard_live_db_non_sort_are_placeholders(request):
    """Live: non-sort dimensions have n_trials=0 (no verdicts yet)."""
    import asyncpg
    from compass_backend.config import Settings
    from compass_backend.quality.scorecard import compute_scorecard_snapshot

    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB smoke tests")
    s = Settings()
    if not s.pg_password.get_secret_value():
        pytest.skip("PG_PASSWORD is required for live DB smoke tests")
    pool = await asyncpg.create_pool(
        host=s.pg_host, port=s.pg_port, database=s.pg_database,
        user=s.pg_user, password=s.pg_password.get_secret_value(),
        min_size=1, max_size=2,
    )
    try:
        snap = await compute_scorecard_snapshot(pool)
        for dim in snap.dimensions:
            if dim.dim_slug != "sort-accuracy":
                assert dim.n_trials == 0, (
                    f"Expected {dim.dim_slug!r} to be placeholder, "
                    f"got n_trials={dim.n_trials}"
                )
    finally:
        await pool.close()
