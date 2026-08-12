"""Blocking psycopg2 DB calls must run OFF the async event loop (audit #27).

AuthMiddleware.dispatch and the async Metric-Calculator write handlers used to
call synchronous ``run_sql`` (psycopg2) directly while awaiting on the loop
thread, parking every other request for the duration of 2-3 blocking queries.
The fix pushes those calls through ``run_in_thread`` (``asyncio.to_thread``).

These tests prove the *property the finding names* — "not on the loop thread" —
by having the patched blocking function record ``threading.get_ident()`` and
asserting it differs from the loop thread's ident. Pre-fix (inline call) the
idents match and the test is RED; post-fix (thread hop) they differ.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nctqai import middleware
from nctqai.middleware import AuthMiddleware
from nctqai.models.auth import User
from nctqai.routes.mc import api, batches


def _admin() -> User:
    return User(id=1, email="admin@example.com", name="Admin", role="admin")


class _FakeRequest:
    """Minimal stand-in for a Starlette Request used by the handlers."""

    def __init__(self, *, user=None, cookies=None, form=None, headers=None):
        self.state = SimpleNamespace(user=user)
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.url = SimpleNamespace(path="/compass/overview")
        self._form = form if form is not None else {}

    async def form(self):
        return self._form


async def _call_next(_request):
    return SimpleNamespace(status_code=200, headers={})


def _routes_from(register_fn) -> dict:
    """Run a route module's ``register(rt)`` with a fake ``rt`` that captures
    the decorated closures by name, so each handler can be called directly
    without a TestClient, middleware stack, or a live server."""
    captured: dict = {}

    def fake_rt(path, methods=None):
        def decorator(fn):
            captured[fn.__name__] = fn
            return fn

        return decorator

    register_fn(fake_rt)
    return captured


# ── AuthMiddleware (the critical path — every authenticated request) ──────


@pytest.mark.asyncio
async def test_auth_middleware_offloads_session_lookup(monkeypatch) -> None:
    """get_session_user (2-3 blocking psycopg2 queries) must run in a worker
    thread, not on the event loop."""
    monkeypatch.setattr(middleware._config, "auth_disabled", False)

    main_ident = threading.get_ident()
    captured: dict = {}

    def fake_get_session_user(session_id):
        captured["ident"] = threading.get_ident()
        return SimpleNamespace(email="user@example.com")  # truthy -> proceed

    with patch.object(middleware, "get_session_user", fake_get_session_user):
        request = _FakeRequest(cookies={"nctqai_session": "session-token"})
        response = await AuthMiddleware(None).dispatch(request, _call_next)

    assert captured["ident"] != main_ident, "session lookup ran on the loop thread"
    assert response.status_code == 200
    assert request.state.user.email == "user@example.com"


# ── MC batch start (full-table SELECT) ────────────────────────────────────


@pytest.mark.asyncio
async def test_post_start_batch_offloads_district_lookup() -> None:
    """post_start_batch's get_available_districts() full-table SELECT must run
    off the loop."""
    main_ident = threading.get_ident()
    captured: dict = {}

    def fake_get_available_districts():
        captured["ident"] = threading.get_ident()
        return [{"district_id": 123, "district_name": "Test", "state": "CA"}]

    class _FakeManager:
        def is_district_running(self, did, ay):
            return False

        def start_batch(self, did, name, ay):
            pass

        def get_all_jobs(self):
            return []

    handler = _routes_from(batches.register)["post_start_batch"]
    request = _FakeRequest(user=_admin(), form={"district_id": "123", "ay": "2024"})

    with (
        patch.object(batches, "get_available_districts", fake_get_available_districts),
        patch.object(batches, "get_batch_manager", lambda: _FakeManager()),
    ):
        await handler(request)

    assert captured["ident"] != main_ident, "district SELECT ran on the loop thread"


# ── MC approve (hold-check + write) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_post_approve_offloads_blocking_calls(monkeypatch) -> None:
    """post_approve's is_answer_held check and approve_answer write must both
    run off the loop."""
    # dry_run defaults True and short-circuits before approve_answer; disable it
    # so both blocking calls execute.
    monkeypatch.setattr(api._config, "dry_run", False)

    main_ident = threading.get_ident()
    held_ident: dict = {}
    write_ident: dict = {}

    def fake_is_answer_held(district_id, ay_id, q_id):
        held_ident["ident"] = threading.get_ident()
        return False

    def fake_approve_answer(**kwargs):
        write_ident["ident"] = threading.get_ident()
        return True

    handler = _routes_from(api.register)["post_approve"]
    request = _FakeRequest(user=_admin(), headers={"hx-request": "true"}, form={})

    with (
        patch.object(api, "is_answer_held", fake_is_answer_held),
        patch.object(api, "approve_answer", fake_approve_answer),
    ):
        await handler(request, district_id=1, ay_id=1, q_id=1)

    assert held_ident["ident"] != main_ident, "is_answer_held ran on the loop thread"
    assert write_ident["ident"] != main_ident, "approve_answer ran on the loop thread"
