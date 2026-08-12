"""Tests for NctqContextRepository against the staging Postgres."""

from __future__ import annotations

import pytest

from compass_backend.answer_layer.nctq_context import NctqSnippet
from compass_backend.config import settings
from compass_backend.db._pool import create_chat_pool, close_chat_pool
from compass_backend.db.nctq_context import NctqContextRepository


pytestmark = [pytest.mark.asyncio, pytest.mark.live_db]


def _require_live_db(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB smoke tests")
    if not settings.pg_password.get_secret_value():
        pytest.skip("PG_PASSWORD is required for live DB smoke tests")


@pytest.fixture
async def chat_pool(request: pytest.FixtureRequest):
    _require_live_db(request)
    pool = await create_chat_pool(settings)
    try:
        yield pool
    finally:
        await close_chat_pool(pool)


async def test_fetch_snippets_returns_curated_first_for_known_topic(chat_pool) -> None:
    repo = NctqContextRepository(settings, pool=chat_pool)

    snippets = await repo.fetch_snippets(("general-salary",), limit=2)

    assert 1 <= len(snippets) <= 2
    assert all(isinstance(snippet, NctqSnippet) for snippet in snippets)
    # Rationales currently have NULL source_url across staging, so the
    # URL filter excludes them; exemplars cover the curated-first behavior
    # in this test.
    assert snippets[0].source_kind in {"rationale", "exemplar"}


async def test_fetch_snippets_returns_empty_for_unknown_topic(chat_pool) -> None:
    repo = NctqContextRepository(settings, pool=chat_pool)

    snippets = await repo.fetch_snippets(("definitely-not-a-real-topic",))

    assert snippets == ()


async def test_fetch_snippets_caps_at_limit(chat_pool) -> None:
    repo = NctqContextRepository(settings, pool=chat_pool)

    snippets = await repo.fetch_snippets(("general-salary", "differentiated-pay"), limit=2)

    assert len(snippets) <= 2
