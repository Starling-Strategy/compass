"""Tests for the OTP verify cookie security flag.

Covers audit finding #23: the Secure cookie attribute must be driven by
`Config.environment`, not by the request URL hostname.
"""

from __future__ import annotations

import pytest
from fasthtml.common import fast_app
from starlette.testclient import TestClient

from nctqai import config as config_module
from nctqai.routes import auth as auth_routes
from nctqai.services import auth as auth_svc


@pytest.fixture
def fake_user_row() -> dict:
    return {
        "id": 7,
        "email": "alice@example.com",
        "name": "Alice",
        "role": "viewer",
        "is_active": True,
        "last_login": None,
    }


def _make_client(monkeypatch: pytest.MonkeyPatch, fake_user_row: dict) -> TestClient:
    """Build a minimal FastHTML app with auth routes wired in."""
    # Skip the OTP machinery — return the user directly.
    from nctqai.models.auth import User

    def _fake_verify_otp(email: str, code: str, ip: str) -> User:
        return User(
            id=fake_user_row["id"],
            email=fake_user_row["email"],
            name=fake_user_row["name"],
            role=fake_user_row["role"],
            is_active=True,
        )

    monkeypatch.setattr(auth_routes, "verify_otp", _fake_verify_otp)
    monkeypatch.setattr(auth_routes, "create_session", lambda user_id, ip: "sess-xyz")
    monkeypatch.setattr(auth_svc, "verify_otp", _fake_verify_otp)

    app, rt = fast_app(pico=False)
    auth_routes.register_auth_routes(rt)
    return TestClient(app)


def _make_config(environment: str) -> config_module.Config:
    return config_module.Config(environment=environment)


def test_cookie_secure_true_in_production(
    monkeypatch: pytest.MonkeyPatch, fake_user_row: dict
) -> None:
    monkeypatch.setattr(
        auth_routes, "Config", lambda: _make_config("production")
    )
    client = _make_client(monkeypatch, fake_user_row)

    response = client.post(
        "/auth/verify",
        data={"email": "alice@example.com", "code": "123456"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    set_cookie = response.headers.get("set-cookie", "")
    assert "nctqai_session=" in set_cookie
    assert "Secure" in set_cookie, f"Expected Secure flag in production: {set_cookie}"
    assert "HttpOnly" in set_cookie
    assert "strict" in set_cookie.lower()


def test_cookie_secure_true_in_staging(
    monkeypatch: pytest.MonkeyPatch, fake_user_row: dict
) -> None:
    monkeypatch.setattr(
        auth_routes, "Config", lambda: _make_config("staging")
    )
    client = _make_client(monkeypatch, fake_user_row)

    response = client.post(
        "/auth/verify",
        data={"email": "alice@example.com", "code": "123456"},
        follow_redirects=False,
    )
    set_cookie = response.headers.get("set-cookie", "")
    assert "Secure" in set_cookie, (
        f"Non-development envs must set Secure: {set_cookie}"
    )


def test_cookie_secure_false_in_development(
    monkeypatch: pytest.MonkeyPatch, fake_user_row: dict
) -> None:
    monkeypatch.setattr(
        auth_routes, "Config", lambda: _make_config("development")
    )
    client = _make_client(monkeypatch, fake_user_row)

    response = client.post(
        "/auth/verify",
        data={"email": "alice@example.com", "code": "123456"},
        follow_redirects=False,
    )
    set_cookie = response.headers.get("set-cookie", "")
    assert "Secure" not in set_cookie, (
        f"Development must NOT set Secure: {set_cookie}"
    )
    # Other safety attributes still present even in dev.
    assert "HttpOnly" in set_cookie
