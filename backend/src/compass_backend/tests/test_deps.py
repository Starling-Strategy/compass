"""Tests for ``compass_backend.api.deps`` transport-layer request identity.

Covers audit finding #4 (2026-05-25-audit-report.md):

- ``X-Forwarded-For`` is only honored when the connection comes from a
  configured trusted proxy. Empty allowlist (the default) forces
  ``request.client.host`` regardless of header value.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from compass_backend.api import deps
from compass_backend.api.app import create_app
from compass_backend.execution import ExecutionRefusal
from compass_backend.session import InMemorySessionStore
from compass_backend.tests.test_mvp_chat import (
    FakeQueryExecutor,
    _agent_for_turn,
    _direct_turn,
)


# ---------------------------------------------------------------------------
# client_ip — trusted-proxy allowlist
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, *, client_host: str | None, headers: dict[str, str] | None = None) -> None:
        self.client = _FakeClient(client_host) if client_host is not None else None
        self.headers = headers or {}


def test_client_ip_uses_request_client_host_when_no_proxies_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty proxy allowlist (default) means ``request.client.host`` always wins."""

    monkeypatch.setattr(deps._runtime_settings, "trusted_proxies", [])
    deps._parse_trusted_proxies.cache_clear()

    request = _FakeRequest(
        client_host="203.0.113.7",
        headers={"X-Forwarded-For": "1.2.3.4"},
    )

    assert deps.client_ip(request) == "203.0.113.7"  # type: ignore[arg-type]


def test_client_ip_ignores_xff_from_untrusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X-Forwarded-For from a peer outside the trusted CIDR must be ignored."""

    monkeypatch.setattr(deps._runtime_settings, "trusted_proxies", ["10.0.0.0/8"])
    deps._parse_trusted_proxies.cache_clear()

    request = _FakeRequest(
        client_host="203.0.113.7",  # not inside 10.0.0.0/8
        headers={"X-Forwarded-For": "1.2.3.4"},
    )

    assert deps.client_ip(request) == "203.0.113.7"  # type: ignore[arg-type]


def test_client_ip_honors_xff_from_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X-Forwarded-For from a peer inside the trusted CIDR is honored."""

    monkeypatch.setattr(deps._runtime_settings, "trusted_proxies", ["10.0.0.0/8"])
    deps._parse_trusted_proxies.cache_clear()

    request = _FakeRequest(
        client_host="10.0.1.5",
        headers={"X-Forwarded-For": "198.51.100.42, 10.0.1.5"},
    )

    assert deps.client_ip(request) == "198.51.100.42"  # type: ignore[arg-type]


def test_client_ip_honors_x_real_ip_from_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only X-Real-IP is set, the trusted proxy path uses it."""

    monkeypatch.setattr(deps._runtime_settings, "trusted_proxies", ["10.0.0.0/8"])
    deps._parse_trusted_proxies.cache_clear()

    request = _FakeRequest(
        client_host="10.0.1.5",
        headers={"X-Real-IP": "198.51.100.99"},
    )

    assert deps.client_ip(request) == "198.51.100.99"  # type: ignore[arg-type]


def test_client_ip_returns_unknown_when_no_client_and_no_trusted_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No TCP peer + empty allowlist → ``unknown`` (cannot derive identity)."""

    monkeypatch.setattr(deps._runtime_settings, "trusted_proxies", [])
    deps._parse_trusted_proxies.cache_clear()

    request = _FakeRequest(client_host=None, headers={"X-Forwarded-For": "1.2.3.4"})

    assert deps.client_ip(request) == "unknown"  # type: ignore[arg-type]


def test_parse_trusted_proxies_skips_unparseable_entries() -> None:
    """Bad CIDR entries must not blow up; they're skipped silently."""

    deps._parse_trusted_proxies.cache_clear()
    networks = deps._parse_trusted_proxies(("10.0.0.0/8", "not-an-ip", "", "::1"))

    assert len(networks) == 2  # 10.0.0.0/8 and ::1


def _make_chat_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a TestClient with a real TestModel planner and working route."""

    monkeypatch.setattr(
        "compass_backend.api.chat.verdict_pipeline.fire_and_forget",
        MagicMock(),
    )
    agent = _agent_for_turn(_direct_turn("ok"))
    store = InMemorySessionStore()
    executor = FakeQueryExecutor(ExecutionRefusal(message="unused"))
    app = create_app(
        planner_agent=agent,  # type: ignore[arg-type]
        session_store=store,  # type: ignore[arg-type]
        query_executor=executor,  # type: ignore[arg-type]
    )
    return TestClient(app)


def test_chat_simple_has_no_transport_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compass should not throttle chat usage at the transport layer."""

    client = _make_chat_client(monkeypatch)

    with client:
        statuses = [
            client.post("/api/v1/chat/simple", json={"message": "hi"}).status_code
            for _ in range(55)
        ]

    assert set(statuses) == {200}
