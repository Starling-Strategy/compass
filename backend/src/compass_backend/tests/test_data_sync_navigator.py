from __future__ import annotations

import pytest
import httpx

from compass_data_sync import sync_navigator


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, payload: dict | None = None):
        self.payload = payload or {"data": []}
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, url: str, params: dict | None = None) -> _Response:
        self.calls.append((url, params))
        return _Response(self.payload)


class _FlakyClient:
    def __init__(self):
        self.calls = 0

    async def get(self, url: str, params: dict | None = None) -> _Response:
        self.calls += 1
        if self.calls == 1:
            raise httpx.ReadTimeout("source endpoint timed out")
        return _Response({"ok": True})


class _RateLimitedClient:
    def __init__(self):
        self.calls = 0

    async def get(self, url: str, params: dict | None = None) -> _Response:
        self.calls += 1
        if self.calls == 1:
            return _Response({"error": "slow down"}, status_code=429)
        return _Response({"data": [{"district_id": 7}]})


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self):
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql: str, *args):
        return "sync-start"

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "DELETE 0" if sql.strip().startswith("DELETE") else "INSERT 0 1"


@pytest.mark.asyncio
async def test_fetch_results_page_uses_default_page_size_without_year_filter():
    client = _Client()

    await sync_navigator.fetch_results_page(client, page=2, district_id=7)

    assert client.calls == [
        (
            "https://api.nctq.org/tcd/results/topics",
            {"page": 2, "pageSize": sync_navigator.DEFAULT_TCD_PAGE_SIZE, "district_id": 7},
        )
    ]


@pytest.mark.asyncio
async def test_fetch_results_page_allows_page_size_override():
    client = _Client()

    await sync_navigator.fetch_results_page(client, page=1, district_id=7, page_size=1000)

    assert client.calls[0][1] == {"page": 1, "pageSize": 1000, "district_id": 7}


@pytest.mark.asyncio
async def test_fetch_sources_page_uses_configured_page_size():
    client = _Client()

    await sync_navigator.fetch_sources_page(client, page=3, page_size=250)

    assert client.calls == [
        ("https://api.nctq.org/tcd/sources", {"page": 3, "pageSize": 250})
    ]


@pytest.mark.asyncio
async def test_fetch_with_retry_retries_transient_transport_errors(monkeypatch):
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(sync_navigator.asyncio, "sleep", fake_sleep)
    client = _FlakyClient()

    response = await sync_navigator.fetch_with_retry(
        client,
        "https://api.nctq.org/tcd/sources",
        params={"page": 1, "pageSize": 500},
    )

    assert response.json() == {"ok": True}
    assert client.calls == 2
    assert sleeps == [2]


@pytest.mark.asyncio
async def test_fetch_districts_uses_retry_for_rate_limits(monkeypatch):
    sleeps: list[int] = []

    async def fake_sleep(delay: int) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(sync_navigator.asyncio, "sleep", fake_sleep)
    client = _RateLimitedClient()

    rows = await sync_navigator.fetch_districts(client)

    assert rows == [{"district_id": 7}]
    assert client.calls == 2
    assert sleeps == [2]


@pytest.mark.asyncio
async def test_sync_sources_raises_before_stale_cleanup_when_page_fails(monkeypatch):
    conn = _Conn()

    async def fake_fetch_sources_page(client, page, page_size):
        if page == 1:
            return {
                "totalPages": 2,
                "totalRows": 2,
                "data": [
                    {
                        "src_id": 1,
                        "district_id": 10,
                        "src_name_formatted": "Page 1 source",
                    }
                ],
            }
        raise RuntimeError("page 2 failed")

    monkeypatch.setattr(sync_navigator, "fetch_sources_page", fake_fetch_sources_page)

    with pytest.raises(RuntimeError, match="source page"):
        await sync_navigator.sync_sources(
            _Pool(conn),
            _Client(),
            dry_run=False,
            concurrency=1,
        )

    assert not any(
        "DELETE FROM compass.navigator_sources" in sql for sql, _ in conn.execute_calls
    )


@pytest.mark.asyncio
async def test_sync_answers_raises_before_stale_cleanup_when_district_fails(monkeypatch):
    conn = _Conn()

    async def fake_fetch_districts(client):
        return [
            {"district_id": 1, "district_name": "Alpha"},
            {"district_id": 2, "district_name": "Beta"},
        ]

    async def fake_sync_answers_for_district(
        conn, client, district_id, district_name, metrics_cache, page_size
    ):
        if district_id == 2:
            raise RuntimeError("district failed")
        return 1, 1

    monkeypatch.setattr(sync_navigator, "fetch_districts", fake_fetch_districts)
    monkeypatch.setattr(
        sync_navigator,
        "sync_answers_for_district",
        fake_sync_answers_for_district,
    )

    with pytest.raises(RuntimeError, match="district"):
        await sync_navigator.sync_answers(
            _Pool(conn),
            _Client(),
            dry_run=False,
            concurrency=2,
        )

    assert not any(
        "DELETE FROM compass.navigator_answers" in sql
        or "DELETE FROM compass.navigator_citations" in sql
        for sql, _ in conn.execute_calls
    )
