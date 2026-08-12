"""Tests for the dashboard-side 'flag for review' action (DASH-R6, U5).

Three surfaces, none needing a live DB or a running backend:
  - ``create_report`` HTTP client — payload shape + 200/404/422/429/network
    handling, with ``httpx`` patched so no real request is made. Mirrors the
    existing ``update_report_status`` tests in spirit.
  - ``flag_control`` component — the rendered detail-pane control (outcome
    select defaulting to 'fail', comment box, the #compass-flag-status aria-live
    slot, the POST target).
  - The ``/compass/conversations/{session_id}/flag`` route handler — proxies to
    ``create_report`` (not the DB) and returns the ok/error banner.

The true round-trip (flag → backend insert → row appears in the Flagged Issues
queue) needs the staging DB + a reachable backend, so it is verified by browser
replay, not here.
"""
from __future__ import annotations

import asyncio

import fasthtml.common as fh

from nctqai.components.compass.conversation_detail import flag_control
from nctqai.services import compass_reports_client as client
from nctqai.services.compass_reports_client import CreateReportResult, create_report

_SESSION = "22222222-2222-2222-2222-222222222222"


# ── create_report HTTP client ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class _FakeClient:
    """Stand-in for httpx.Client capturing the POSTed payload and returning a
    canned response (or raising a network error)."""

    captured: dict = {}

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None):
        _FakeClient.captured = {"url": url, "json": json}
        if self._raise is not None:
            raise self._raise
        return self._response


def _patch_httpx(monkeypatch, *, response=None, raise_exc=None):
    _FakeClient.captured = {}

    def fake_client(timeout=None):
        return _FakeClient(response=response, raise_exc=raise_exc)

    monkeypatch.setattr(client.httpx, "Client", fake_client)


def test_create_report_success_payload(monkeypatch):
    """A 200 returns ok; the payload carries session_id + outcome + reviewer +
    comments and posts to the backend /report endpoint."""
    _patch_httpx(monkeypatch, response=_FakeResponse(200, {"id": "x", "status": "open"}))
    result = create_report(
        session_id=_SESSION,
        outcome="fail",
        reviewer="macon@starlingstrategy.com",
        comments="check turn 2",
    )
    assert result.ok is True
    assert result.status_code == 200
    payload = _FakeClient.captured["json"]
    assert payload["session_id"] == _SESSION
    assert payload["outcome"] == "fail"
    assert payload["reviewer"] == "macon@starlingstrategy.com"
    assert payload["comments"] == "check turn 2"
    assert _FakeClient.captured["url"].endswith("/api/v1/debug/report")


def test_create_report_defaults_outcome_to_fail(monkeypatch):
    """The default outcome is 'fail' so the new flag lands in the Flagged Issues
    open queue (outcome IN ('fail','partial') AND status NOT IN closed)."""
    _patch_httpx(monkeypatch, response=_FakeResponse(200, {"id": "x", "status": "open"}))
    create_report(session_id=_SESSION)
    assert _FakeClient.captured["json"]["outcome"] == "fail"


def test_create_report_omits_optional_fields_when_absent(monkeypatch):
    """A whole-conversation flag sends no case_id/turn_index (the backend skips
    case_exists when case_id is None) and no empty reviewer/comments keys."""
    _patch_httpx(monkeypatch, response=_FakeResponse(200))
    create_report(session_id=_SESSION, outcome="fail")
    payload = _FakeClient.captured["json"]
    assert "case_id" not in payload
    assert "turn_index" not in payload
    assert "reviewer" not in payload
    assert "comments" not in payload


def test_create_report_404_session_not_found(monkeypatch):
    _patch_httpx(
        monkeypatch, response=_FakeResponse(404, {"detail": "Session not found"})
    )
    result = create_report(session_id=_SESSION)
    assert result.ok is False
    assert result.status_code == 404
    assert "Session not found" in result.detail


def test_create_report_422_bad_outcome(monkeypatch):
    _patch_httpx(monkeypatch, response=_FakeResponse(422, {"detail": "bad outcome"}))
    result = create_report(session_id=_SESSION, outcome="nonsense")
    assert result.ok is False
    assert result.status_code == 422
    assert result.detail  # some message surfaces


def test_create_report_429_rate_limited_has_readable_message(monkeypatch):
    """A bare 429 with no detail body still surfaces a human-readable reason
    (the endpoint is IP-rate-limited)."""
    _patch_httpx(monkeypatch, response=_FakeResponse(429, {}))
    result = create_report(session_id=_SESSION)
    assert result.ok is False
    assert result.status_code == 429
    assert "Too many" in result.detail


def test_create_report_network_error_degrades(monkeypatch):
    """A network error folds into a non-ok result (status_code 0), never a
    raised exception — the detail surface shows a banner, not a 500."""
    _patch_httpx(monkeypatch, raise_exc=client.httpx.ConnectError("boom"))
    result = create_report(session_id=_SESSION)
    assert isinstance(result, CreateReportResult)
    assert result.ok is False
    assert result.status_code == 0
    assert "Could not reach" in result.detail


# ── flag_control component ─────────────────────────────────────────────────


def test_flag_control_renders_form_and_aria_live_slot():
    html = fh.to_xml(flag_control(_SESSION))
    # POSTs to the flag route for this session, swapping only the status slot.
    assert f'hx-post="/compass/conversations/{_SESSION}/flag"' in html
    assert 'hx-target="#compass-flag-status"' in html
    # The aria-live result slot is present and starts not-busy.
    assert 'id="compass-flag-status"' in html
    assert 'aria-live="polite"' in html
    # Outcome select defaults to 'fail' (queue-visible) and offers only the two
    # failing verdicts — a review flag is inherently fail/partial, and 'pass'
    # would be excluded from the default Flagged Issues queue anyway.
    assert 'value="fail"' in html
    assert 'value="partial"' in html
    assert 'value="pass"' not in html
    # A reviewer can identify the issue now or leave the stable Not sure default.
    assert 'name="dimension"' in html
    assert 'value="not-sure"' in html
    assert ">Not sure</option>" in html
    # No teal class leaks in — the Quality cluster stays sober/blue.
    assert "compass-teal" not in html


def test_flag_control_is_wired_into_build_detail(monkeypatch):
    """The flag control is actually appended to build_detail()'s output, not
    just renderable in isolation — patch the three reads build_detail does so it
    renders without a DB, then assert the flag form/slot are present."""
    from types import SimpleNamespace

    from nctqai.components.compass import conversation_detail as detail

    # Minimal conversation: one user + one assistant message, no feedback, and
    # the verdict loader raises KeyError (session not in the verdict view) so the
    # detail degrades gracefully — the flag control must still be appended.
    msgs = [
        SimpleNamespace(role="user", content="hi", id=1, timestamp=None),
        SimpleNamespace(role="assistant", content="hello", id=2, timestamp=None),
    ]
    convo = SimpleNamespace(messages=msgs, created_at=None)

    monkeypatch.setattr(detail, "get_conversation", lambda sid: convo)
    monkeypatch.setattr(detail, "get_session_feedback", lambda sid: [])

    def _raise(sid):
        raise KeyError(sid)

    monkeypatch.setattr(detail, "load_conversation_with_verdicts", _raise)

    html = fh.to_xml(detail.build_detail(_SESSION, convo=None))
    assert f'hx-post="/compass/conversations/{_SESSION}/flag"' in html
    assert 'id="compass-flag-status"' in html


# ── flag route handler ─────────────────────────────────────────────────────


class _FakeUser:
    email = "macon@starlingstrategy.com"
    name = "Macon"
    role = "power_user"

    def can_access(self, _section: str) -> bool:
        return True

    def is_admin(self) -> bool:
        return False


class _FakeRequest:
    class _state:
        user = _FakeUser()

    state = _state()
    headers: dict = {"HX-Request": "true"}

    def __init__(self, form: dict):
        self._form = form

    async def form(self):
        return self._form


def _capture_routes(register_fn):
    routes: dict = {}

    def fake_rt(path, **_kw):
        def deco(fn):
            routes[path] = fn
            return fn

        return deco

    register_fn(fake_rt)
    return routes


def test_flag_route_proxies_to_create_report_and_renders_ok(monkeypatch):
    """The flag route calls create_report (not the DB) with the reviewer
    identity and returns an ok banner the form swaps into the status slot."""
    from nctqai.routes.compass import conversations as convo_route

    calls = {}

    def fake_create(*, session_id, outcome, reviewer=None, comments=None, **kw):
        calls.update(
            session_id=session_id,
            outcome=outcome,
            reviewer=reviewer,
            comments=comments,
            dimension=kw.get("dimension"),
        )
        return CreateReportResult(ok=True, status_code=200)

    monkeypatch.setattr(convo_route, "create_report", fake_create)
    # require_compass_partial must allow this power_user through.
    monkeypatch.setattr(
        convo_route, "require_compass_partial", lambda req: (req.state.user, None)
    )

    handler = _capture_routes(convo_route.register)[
        "/compass/conversations/{session_id}/flag"
    ]
    req = _FakeRequest(
        {
            "outcome": "partial",
            "comments": "  look here  ",
            "dimension": "citation-accuracy",
        }
    )
    result = asyncio.run(handler(req, _SESSION))
    html = fh.to_xml(result)

    assert calls["session_id"] == _SESSION
    assert calls["outcome"] == "partial"
    assert calls["reviewer"] == "macon@starlingstrategy.com"
    assert calls["comments"] == "look here"  # trimmed
    assert calls["dimension"] == "citation-accuracy"
    assert "Flagged for review" in html
    assert "quality-reports-banner-ok" in html


def test_flag_route_renders_error_banner_on_failure(monkeypatch):
    """A non-ok create_report result becomes an inline error banner (no 500,
    no phantom row), with the backend detail surfaced."""
    from nctqai.routes.compass import conversations as convo_route

    def fake_create(*, session_id, outcome, reviewer=None, comments=None, **kw):
        return CreateReportResult(ok=False, status_code=404, detail="Session not found")

    monkeypatch.setattr(convo_route, "create_report", fake_create)
    monkeypatch.setattr(
        convo_route, "require_compass_partial", lambda req: (req.state.user, None)
    )

    handler = _capture_routes(convo_route.register)[
        "/compass/conversations/{session_id}/flag"
    ]
    req = _FakeRequest({"outcome": "fail", "comments": ""})
    result = asyncio.run(handler(req, _SESSION))
    html = fh.to_xml(result)

    assert "Session not found" in html
    assert "quality-reports-banner-error" in html


def test_flag_route_denies_when_guard_denies(monkeypatch):
    """If the section guard denies, the handler returns the deny partial and
    never calls create_report (no write on a denied request)."""
    from nctqai.routes.compass import conversations as convo_route

    called = {"create": False}

    def fake_create(**kw):
        called["create"] = True
        return CreateReportResult(ok=True, status_code=200)

    monkeypatch.setattr(convo_route, "create_report", fake_create)
    deny = fh.Div("forbidden")
    monkeypatch.setattr(
        convo_route, "require_compass_partial", lambda req: (None, deny)
    )

    handler = _capture_routes(convo_route.register)[
        "/compass/conversations/{session_id}/flag"
    ]
    req = _FakeRequest({"outcome": "fail"})
    result = asyncio.run(handler(req, _SESSION))
    html = fh.to_xml(result).lower()

    assert "forbidden" in html
    assert called["create"] is False
