"""Tests for the auth-error log level on 401/403 responses.

Covers audit finding #49 / CL3: `token_shape` inference (jwt vs opaque vs
none) leaks Bearer-token structure to production logs. Demoted to debug so
the diagnostic stays available locally without surfacing on every failed
auth attempt in prod.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from compass_backend.api import middleware as middleware_module


def _install_handler(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, middleware_module.http_exception_handler)


def _capture_log_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    captured: dict[str, list[Any]] = {"debug": [], "warn": [], "error": []}

    def _record(level: str):
        def _impl(event: str, **kwargs: Any) -> None:
            captured[level].append((event, kwargs))
        return _impl

    monkeypatch.setattr(middleware_module, "log_debug", _record("debug"))
    monkeypatch.setattr(middleware_module, "log_warn", _record("warn"))
    return captured


def _make_app() -> FastAPI:
    app = FastAPI()
    _install_handler(app)

    @app.get("/needs-auth")
    def _route() -> dict[str, str]:
        raise HTTPException(status_code=401, detail="auth required")

    return app


def test_token_shape_log_is_debug_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 must route through log_debug, not log_error or log_warn."""
    captured = _capture_log_calls(monkeypatch)
    app = _make_app()

    with TestClient(app) as client:
        response = client.get(
            "/needs-auth",
            headers={"Authorization": "Bearer abc.def.ghi"},
        )

    assert response.status_code == 401
    assert len(captured["debug"]) == 1, "auth error should be logged at debug level"
    assert captured["warn"] == [], "auth error must not appear at warn level"
    event, kwargs = captured["debug"][0]
    assert event == "Auth error"
    # token_shape diagnostic survives at debug; production logs should not see it.
    assert kwargs["token_shape"] == "jwt"
    assert kwargs["status_code"] == 401


def test_token_shape_not_present_in_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON response must not contain token_shape or any token diagnostic."""
    _capture_log_calls(monkeypatch)
    app = _make_app()

    with TestClient(app) as client:
        response = client.get(
            "/needs-auth",
            headers={"Authorization": "Bearer opaque-token-xyz"},
        )

    assert response.status_code == 401
    body = response.json()
    assert "token_shape" not in body
    # Body shape stays the historical {"detail": "..."}.
    assert set(body.keys()) == {"detail"}
