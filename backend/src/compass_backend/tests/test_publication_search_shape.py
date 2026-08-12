"""Offline shape tests for the publication search (#1690).

The full SQL is exercised against staging in the live-DB suite. These offline
tests pin the pure pieces: the tag-key candidate builder (hyphen/space + Title
Case variants) and the empty-query / non-positive-limit short-circuits that
return ``()`` before any DB round-trip — so the search never fires a query for
a blank planner phrase.
"""

from __future__ import annotations

import pytest

from compass_backend.db.nctq_context import NctqContextRepository, _publication_tag_keys


def test_publication_tag_keys_emits_hyphen_and_space_title_variants() -> None:
    keys = _publication_tag_keys("four-day school week")

    # Verbatim phrase plus raw space/hyphen separators, and the Title Case form
    # (always space-joined, since `_kebab_to_title_case` splits on hyphens).
    assert "four-day school week" in keys
    assert "four day school week" in keys
    assert "four-day-school-week" in keys
    assert "Four Day School Week" in keys


def test_publication_tag_keys_empty_query_is_empty() -> None:
    assert _publication_tag_keys("") == []
    assert _publication_tag_keys("   ") == []


def test_publication_tag_keys_dedupes() -> None:
    # A single-word phrase collapses its variants; no duplicate entries.
    keys = _publication_tag_keys("salary")
    assert len(keys) == len(set(keys))


@pytest.mark.asyncio
async def test_search_publications_short_circuits_on_blank_query() -> None:
    # `pool=None` is safe: a blank query must return () *before* `_acquire`
    # touches the pool, so no connection is needed.
    repo = NctqContextRepository.__new__(NctqContextRepository)
    repo._settings = None  # type: ignore[attr-defined]
    repo._pool_or_holder = None  # type: ignore[attr-defined]

    assert await repo.search_publications("") == ()
    assert await repo.search_publications("   ") == ()
    assert await repo.search_publications("topic", limit=0) == ()
