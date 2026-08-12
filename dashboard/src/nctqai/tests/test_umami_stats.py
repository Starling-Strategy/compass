"""Unit tests for the windowed Umami unique-visitors fetch (#1807).

The Overview "Unique visitors" card used to be stuck on a hardcoded 7-day
window. ``get_visitors(start, end)`` now maps the route's resolved
``since``/``until`` onto Umami's ``startAt``/``endAt`` ms-epoch params, so the
tile honors the 7d / 30d / all-time pill. These tests mock httpx so no real
request is made, and assert the params for each window.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from nctqai.services import umami_stats


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {"visitors": {"value": 42}}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that records the ``params`` of the GET."""

    captured: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url, headers=None, params=None) -> _FakeResponse:
        _FakeAsyncClient.captured = dict(params or {})
        return _FakeResponse()


@pytest.fixture
def _umami(monkeypatch):
    """Configure umami_stats with creds, a stubbed login, and the fake client."""
    monkeypatch.setattr(
        umami_stats,
        "_config",
        SimpleNamespace(
            umami_enabled=True,
            umami_base_url="http://umami.test",
            umami_website_id="site-1",
        ),
    )

    async def _fake_login(_client) -> str:
        return "tok"

    monkeypatch.setattr(umami_stats, "_login", _fake_login)
    monkeypatch.setattr(umami_stats.httpx, "AsyncClient", _FakeAsyncClient)
    umami_stats._stats_cache.clear()
    _FakeAsyncClient.captured = {}
    yield


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_get_visitors_7d_window(_umami) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(days=7)
    summary = asyncio.run(umami_stats.get_visitors(start, end))
    assert summary.total == 42
    params = _FakeAsyncClient.captured
    # ms-epoch within a second of the requested bounds (clock drift tolerance).
    assert abs(params["startAt"] - _ms(start)) < 1000
    assert abs(params["endAt"] - _ms(end)) < 1000


def test_get_visitors_30d_window(_umami) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(days=30)
    asyncio.run(umami_stats.get_visitors(start, end))
    params = _FakeAsyncClient.captured
    assert abs(params["startAt"] - _ms(start)) < 1000
    assert abs(params["endAt"] - _ms(end)) < 1000


def test_get_visitors_alltime_floors_start(_umami) -> None:
    """since=None → the all-time window floors startAt to the fixed far-back
    lower bound; endAt is ~now."""
    before = datetime.now(UTC)
    asyncio.run(umami_stats.get_visitors(None, None))
    after = datetime.now(UTC)
    params = _FakeAsyncClient.captured
    assert params["startAt"] == _ms(umami_stats._UMAMI_ALLTIME_FLOOR)
    assert _ms(before) - 1000 <= params["endAt"] <= _ms(after) + 1000


def test_get_visitors_caches_per_day_bucket(_umami) -> None:
    """The cache keys on a UTC-day bucket, not exact ms. Two calls for the SAME
    logical window whose bounds differ only by seconds (as the rolling presets
    do every request, recomputing now()) must share one fetch — otherwise the
    5-minute TTL would never hit and every page load would re-hit Umami.
    """
    # Fixed mid-day datetimes so end+5s stays inside the same UTC day (no
    # midnight-boundary flakiness).
    start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    end1 = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    end2 = datetime(2026, 5, 8, 12, 0, 5, tzinfo=UTC)

    asyncio.run(umami_stats.get_visitors(start, end1))
    first = dict(_FakeAsyncClient.captured)
    assert first, "first call should fetch"

    _FakeAsyncClient.captured = {}  # stays empty on a cache hit
    asyncio.run(umami_stats.get_visitors(start, end2))
    assert _FakeAsyncClient.captured == {}, "same-day window should hit the cache"

    # A genuinely different window (different day bucket) misses.
    asyncio.run(umami_stats.get_visitors(datetime(2026, 4, 1, 12, tzinfo=UTC), end1))
    assert _FakeAsyncClient.captured != {}, "a new window should miss the cache"
    assert _FakeAsyncClient.captured["startAt"] != first["startAt"]


def test_get_visitors_7d_wrapper_delegates(_umami) -> None:
    """The backwards-compatible wrapper still works and produces a ~7-day span."""
    summary = asyncio.run(umami_stats.get_visitors_7d())
    assert summary.total == 42
    params = _FakeAsyncClient.captured
    span_days = (params["endAt"] - params["startAt"]) / 1000 / 86400
    assert 6.9 < span_days < 7.1


def test_get_visitors_raises_when_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        umami_stats, "_config", SimpleNamespace(umami_enabled=False)
    )
    umami_stats._stats_cache.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(umami_stats.get_visitors(None, None))
