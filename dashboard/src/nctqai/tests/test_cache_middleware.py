"""Tests for HTTP cache middleware and server-side stats caching."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from nctqai.middleware import CacheMiddleware, _cache_max_age
from nctqai.services import compass_stats


class _FakeRequest:
    def __init__(self, method: str = "GET", path: str = "/compass/overview"):
        self.method = method
        self.url = type("URL", (), {"path": path})()
        self.headers = {}


class _FakeResponse:
    def __init__(self, headers: dict | None = None, status_code: int = 200):
        self.headers = headers or {}
        self.status_code = status_code


async def _fake_call_next(request):
    return _FakeResponse()


def _call_next_status(status_code: int):
    """A call_next that returns a response with the given status code."""

    async def _inner(request):
        return _FakeResponse(status_code=status_code)

    return _inner


# ── HTTP Cache Headers ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compass_overview_gets_cache_control() -> None:
    """Overview page gets private, max-age=60, stale-while-revalidate=300."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("GET", "/compass/overview"), _fake_call_next
    )
    assert "Cache-Control" in response.headers
    cc = response.headers["Cache-Control"]
    assert "private" in cc
    assert "max-age=60" in cc
    assert "stale-while-revalidate=300" in cc


@pytest.mark.asyncio
async def test_compass_conversations_full_page_gets_30s() -> None:
    """Full conversations page gets max-age=30 (more interactive)."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("GET", "/compass/conversations"), _fake_call_next
    )
    cc = response.headers.get("Cache-Control", "")
    assert "max-age=30" in cc


@pytest.mark.asyncio
async def test_conversations_list_htmx_gets_15s() -> None:
    """HTMX partial gets shorter max-age (interactive filter)."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("GET", "/compass/conversations/list?range=7d"), _fake_call_next
    )
    cc = response.headers.get("Cache-Control", "")
    assert "max-age=15" in cc


@pytest.mark.asyncio
async def test_conversations_detail_htmx_gets_10s() -> None:
    """Detail pane gets shortest max-age (click-to-view must feel instant)."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("GET", "/compass/conversations/detail?session_id=abc"), _fake_call_next
    )
    cc = response.headers.get("Cache-Control", "")
    assert "max-age=10" in cc


@pytest.mark.asyncio
async def test_post_not_cached() -> None:
    """POST requests never get cache headers."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("POST", "/compass/overview"), _fake_call_next
    )
    assert "Cache-Control" not in response.headers


@pytest.mark.asyncio
async def test_health_check_not_cached() -> None:
    """Health checks are never cached."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("GET", "/health"), _fake_call_next
    )
    assert "Cache-Control" not in response.headers


@pytest.mark.asyncio
async def test_auth_routes_not_cached() -> None:
    """Auth routes are never cached (security)."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("GET", "/auth/login"), _fake_call_next
    )
    assert "Cache-Control" not in response.headers


# ── Non-200 responses are never cached ───────────────────────────────
# CacheMiddleware runs outermost, so it sees AuthMiddleware's 302 -> /login
# redirect for an unauthenticated request. Caching that 302 would make the
# browser replay the login bounce after sign-in (a login loop). Errors must
# not be cached either.


@pytest.mark.asyncio
async def test_redirect_302_not_cached() -> None:
    """An auth redirect (302) on a /compass path must NOT be cached."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("GET", "/compass/overview"), _call_next_status(302)
    )
    assert "Cache-Control" not in response.headers


@pytest.mark.asyncio
async def test_not_found_404_not_cached() -> None:
    """A transient 404 on a /compass path must NOT be cached."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("GET", "/compass/conversations/detail"), _call_next_status(404)
    )
    assert "Cache-Control" not in response.headers


def test_full_middleware_stack_does_not_cache_auth_redirect() -> None:
    """Integration: real Auth + Cache stack — the 302 -> /login carries no
    Cache-Control, while an authenticated 200 page does.

    Exercises the real Starlette ASGI / BaseHTTPMiddleware path (the unit
    tests above stub call_next), so the outermost-middleware-sees-the-redirect
    behavior is covered end to end.
    """
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse, RedirectResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    authed = {"value": False}

    class _StubAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if not authed["value"]:
                return RedirectResponse("/login", status_code=302)
            return await call_next(request)

    async def overview(request):
        return PlainTextResponse("ok", status_code=200)

    app = Starlette(routes=[Route("/compass/overview", overview)])
    # Mirror main.py registration order: Auth added first (innermost),
    # Cache added last (outermost).
    app.add_middleware(_StubAuth)
    app.add_middleware(CacheMiddleware)
    client = TestClient(app, follow_redirects=False)

    # Unauthenticated: 302 -> /login must NOT be cacheable.
    r = client.get("/compass/overview")
    assert r.status_code == 302
    assert "max-age" not in r.headers.get("Cache-Control", "")

    # Authenticated: the 200 page gets the cache header (the intended feature).
    authed["value"] = True
    r = client.get("/compass/overview")
    assert r.status_code == 200
    assert "max-age=60" in r.headers.get("Cache-Control", "")


def test_cache_max_age_lookup() -> None:
    """_cache_max_age returns correct max-age for known paths."""
    assert _cache_max_age("/compass/overview") == 60
    assert _cache_max_age("/compass/conversations") == 30
    assert _cache_max_age("/compass/conversations/list") == 15
    assert _cache_max_age("/compass/conversations/detail") == 10
    assert _cache_max_age("/compass/quality/scorecard") == 60
    assert _cache_max_age("/unknown/path") is None


@pytest.mark.asyncio
async def test_non_compass_page_not_cached() -> None:
    """A 200 page outside the /compass prefixes (e.g. Metric Calculator) gets
    no Cache-Control — guards the `_cache_max_age is None` dispatch branch."""
    middleware = CacheMiddleware(None)
    response = await middleware.dispatch(
        _FakeRequest("GET", "/mc/review"), _fake_call_next
    )
    assert "Cache-Control" not in response.headers


# ── Server-Side Stats Caching ─────────────────────────────────────────


def test_engagement_stats_cache_hit(monkeypatch) -> None:
    """Second call within TTL hits the in-process cache."""
    call_count = 0

    def fake_run_sql(_sql, _bind=()):
        nonlocal call_count
        call_count += 1
        return [
            {
                "sessions": 100,
                "user_messages": 250,
                "multi_2plus": 30,
                "multi_3plus": 10,
            }
        ]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)

    # Clear the cache to start fresh.
    compass_stats._ENGAGEMENT_CACHE.clear()

    since = datetime.now(UTC)
    result1 = compass_stats.get_engagement_stats(since)
    assert call_count == 1
    assert result1.sessions == 100

    # Second call should hit cache, not run_sql again.
    result2 = compass_stats.get_engagement_stats(since)
    assert call_count == 1, "cache should prevent second DB hit"
    assert result2.sessions == 100


def test_feedback_stats_cache_hit(monkeypatch) -> None:
    """Second call within TTL hits the in-process cache."""
    call_count = 0

    def fake_run_sql(_sql, _bind=()):
        nonlocal call_count
        call_count += 1
        return [
            {
                "sessions": 100,
                "thumbs_up": 60,
                "thumbs_down": 10,
            }
        ]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)

    # Clear the cache to start fresh.
    compass_stats._FEEDBACK_CACHE.clear()

    since = datetime.now(UTC)
    result1 = compass_stats.get_feedback_stats(since)
    assert call_count == 1
    assert result1.sessions == 100

    # Second call should hit cache.
    result2 = compass_stats.get_feedback_stats(since)
    assert call_count == 1, "cache should prevent second DB hit"
    assert result2.sessions == 100


def test_engagement_stats_cache_expiry(monkeypatch) -> None:
    """Cache entry expires after TTL and triggers fresh DB hit."""
    call_count = 0

    def fake_run_sql(_sql, _bind=()):
        nonlocal call_count
        call_count += 1
        return [
            {
                "sessions": call_count * 100,
                "user_messages": 250,
                "multi_2plus": 30,
                "multi_3plus": 10,
            }
        ]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)

    # Clear cache and mock monotonic to control time.
    compass_stats._ENGAGEMENT_CACHE.clear()
    fake_time = 1000.0

    with patch.object(compass_stats, "monotonic", side_effect=lambda: fake_time):
        since = datetime.now(UTC)
        result1 = compass_stats.get_engagement_stats(since)
        assert call_count == 1
        assert result1.sessions == 100

        # Advance time past TTL (60s).
        fake_time += 61.0
        result2 = compass_stats.get_engagement_stats(since)
        assert call_count == 2, "expired cache should trigger DB hit"
        assert result2.sessions == 200


def test_cache_key_hour_grain() -> None:
    """Cache key uses hour grain, so requests within the same hour share a slot."""
    since1 = datetime(2026, 6, 26, 14, 30, 0, tzinfo=UTC)
    since2 = datetime(2026, 6, 26, 14, 45, 0, tzinfo=UTC)

    key1 = compass_stats._cache_key(since1, None)
    key2 = compass_stats._cache_key(since2, None)

    assert key1 == key2, "same-hour requests should share a cache key"


def test_cache_key_distinct_windows() -> None:
    """Different windows must produce different keys, so one date-range's stats
    can never be served for another."""
    base = datetime(2026, 6, 26, 14, 0, 0, tzinfo=UTC)
    # Different `since` hour.
    assert compass_stats._cache_key(base, None) != compass_stats._cache_key(
        datetime(2026, 6, 26, 16, 0, 0, tzinfo=UTC), None
    )
    # Same `since`, different `until`.
    assert compass_stats._cache_key(base, base) != compass_stats._cache_key(
        base, datetime(2026, 6, 26, 18, 0, 0, tzinfo=UTC)
    )
