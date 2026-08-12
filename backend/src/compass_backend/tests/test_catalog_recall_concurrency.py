"""Regression test for concurrent within-batch recall fan-out (issue #1080).

`_recall_batch` issues up to six independent per-entity-type catalog reads.
They used to run as six sequential `await`s (six DB round trips back to back);
they are now dispatched together with `asyncio.gather`. This probe proves they
are in flight concurrently rather than serialized. Card ordering / dedup /
ranking behavior is covered by `test_catalog_recall.py`, which runs the full
`recall()` path against a fake repository.
"""

import asyncio

from compass_backend.catalog import CatalogRecallService

_ALL_ENTITY_TYPES = (
    "metric",
    "district",
    "profile_field",
    "topic",
    "glossary_term",
    "source_document",
)


class _ConcurrencyProbeRepository:
    """Records the peak number of simultaneously in-flight search calls."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    async def _probe(self) -> list:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Two yields so every gathered coroutine has a chance to reach this
            # point (incrementing the counter) before any of them completes.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            self.in_flight -= 1
        return []

    async def search_metrics(self, query, *, limit):
        return await self._probe()

    async def search_district_candidates(self, query, *, states, limit):
        return await self._probe()

    async def search_nces_fields(self, query, *, limit):
        return await self._probe()

    async def search_topics(self, query, *, limit):
        return await self._probe()

    async def search_glossary_terms(self, query, *, limit):
        return await self._probe()

    async def search_source_documents(self, query, *, limit):
        return await self._probe()


async def test_recall_batch_dispatches_entity_fetches_concurrently() -> None:
    repo = _ConcurrencyProbeRepository()
    service = CatalogRecallService(repo)  # type: ignore[arg-type]

    batch = await service._recall_batch(
        "starting salary",
        requested=_ALL_ENTITY_TYPES,
        limit=10,
        source="direct",
        origin_query="starting salary",
        include_alias_cards=False,
    )

    # Sequential awaits would peak at 1 in-flight; concurrent dispatch peaks at
    # all six requested entity-type reads.
    assert repo.max_in_flight == len(_ALL_ENTITY_TYPES)
    assert batch.candidates == []
