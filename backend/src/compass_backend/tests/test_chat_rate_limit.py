"""Tests for chat-route rate limiting (issue #1079).

`/chat` and `/chat/simple` drive paid LLM gateway calls, so an unthrottled
flood from one identity is a financial-DoS on the gateway bill. These tests
cover the limiter primitive deterministically (via an injected clock) and the
route wiring (429 + Retry-After, admin exemption) end to end.
"""

from fastapi.testclient import TestClient

from compass_backend.api.rate_limit import (
    FixedWindowRateLimiter,
    is_rate_limit_exempt,
    rate_limit_identity,
)
from compass_backend.config import Settings
from compass_backend.contracts.auth import AuthenticatedUser
from compass_backend.session import InMemorySessionStore
from compass_backend.tests.test_mvp_chat import (
    _agent_for_turn,
    _direct_turn,
    create_app,
)


class _FakeClock:
    """Manually-advanced monotonic clock for deterministic window tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_allows_up_to_max_then_denies() -> None:
    clock = _FakeClock()
    limiter = FixedWindowRateLimiter(
        max_requests=2, window_seconds=60.0, time_fn=clock
    )

    assert limiter.check("ip:a").allowed is True
    assert limiter.check("ip:a").allowed is True
    denied = limiter.check("ip:a")
    assert denied.allowed is False
    # Retry-After rounds up the seconds left in the window.
    assert denied.retry_after_seconds == 60


def test_limiter_is_per_identity() -> None:
    clock = _FakeClock()
    limiter = FixedWindowRateLimiter(
        max_requests=1, window_seconds=60.0, time_fn=clock
    )

    assert limiter.check("ip:a").allowed is True
    # A different identity has its own window.
    assert limiter.check("ip:b").allowed is True
    assert limiter.check("ip:a").allowed is False


def test_limiter_window_resets_after_elapsed_time() -> None:
    clock = _FakeClock()
    limiter = FixedWindowRateLimiter(
        max_requests=1, window_seconds=60.0, time_fn=clock
    )

    assert limiter.check("ip:a").allowed is True
    assert limiter.check("ip:a").allowed is False
    clock.now += 60.0  # window elapsed
    assert limiter.check("ip:a").allowed is True


def test_rate_limit_identity_precedence() -> None:
    email_user = AuthenticatedUser(email="a@b.co", api_key_id="k1")
    key_user = AuthenticatedUser(email=None, api_key_id="k1")

    assert (
        rate_limit_identity(auth_user=email_user, client_ip_value="9.9.9.9")
        == "user:a@b.co"
    )
    assert (
        rate_limit_identity(auth_user=key_user, client_ip_value="9.9.9.9")
        == "api_key:k1"
    )
    assert (
        rate_limit_identity(auth_user=None, client_ip_value="9.9.9.9")
        == "ip:9.9.9.9"
    )


def test_admin_is_exempt() -> None:
    assert is_rate_limit_exempt(AuthenticatedUser(is_admin=True)) is True
    assert is_rate_limit_exempt(AuthenticatedUser(is_admin=False)) is False
    assert is_rate_limit_exempt(None) is False


def test_chat_simple_returns_429_after_limit() -> None:
    agent = _agent_for_turn(_direct_turn())
    settings = Settings(
        session_store_backend="memory", chat_rate_limit_per_minute=2
    )

    with TestClient(
        create_app(
            planner_agent=agent,
            session_store=InMemorySessionStore(),
            app_settings=settings,
        )
    ) as client:
        first = client.post("/api/v1/chat/simple", json={"message": "hi"})
        second = client.post("/api/v1/chat/simple", json={"message": "hi"})
        third = client.post("/api/v1/chat/simple", json={"message": "hi"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert int(third.headers["Retry-After"]) >= 1


def test_chat_rate_limit_disabled_allows_burst() -> None:
    agent = _agent_for_turn(_direct_turn())
    settings = Settings(
        session_store_backend="memory",
        chat_rate_limit_enabled=False,
        chat_rate_limit_per_minute=1,
    )

    with TestClient(
        create_app(
            planner_agent=agent,
            session_store=InMemorySessionStore(),
            app_settings=settings,
        )
    ) as client:
        statuses = [
            client.post("/api/v1/chat/simple", json={"message": "hi"}).status_code
            for _ in range(3)
        ]

    assert statuses == [200, 200, 200]
