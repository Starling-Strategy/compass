"""Tests for nctqai.services.auth OTP storage and verification.

Covers audit finding #3 (docs/audit/2026-05-25-audit-report.md):
codes are stored plaintext, bounded by TTL + claim-on-use + rate limits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from typing import Any

import pytest

from nctqai.services import auth as auth_svc


# ---------------------------------------------------------------------------
# Fake DB
# ---------------------------------------------------------------------------

class FakeDB:
    """In-memory stand-in for run_sql / run_sql_write.

    Captures the OTP lifecycle: a single auth_codes row per user, plus
    the bookkeeping needed for rate-limit counts and user lookup. We only
    model the SQL shapes that auth.py actually issues — anything else
    raises so we notice silent contract drift.
    """

    def __init__(self, user: dict | None = None) -> None:
        self.user = user
        self.auth_codes: list[dict[str, Any]] = []
        self.rate_events: list[dict[str, Any]] = []
        self.users_last_login: dict[int, datetime] = {}
        self._next_code_id = 1

    # -- run_sql (reads) ----------------------------------------------------
    def run_sql(self, sql: str, params: tuple = ()) -> list[dict]:
        s = " ".join(sql.split())

        if s.startswith("SELECT id, email, name, role, is_active, last_login FROM nctqai.users WHERE email"):
            if self.user and self.user["email"] == params[0]:
                return [dict(self.user)]
            return []

        if s.startswith("SELECT id, email, name, role, is_active, last_login FROM nctqai.users WHERE id"):
            if self.user and self.user["id"] == params[0]:
                return [dict(self.user)]
            return []

        if s.startswith("SELECT COUNT(*) AS cnt FROM nctqai.rate_limits"):
            ip, action, cutoff = params
            cnt = sum(
                1 for r in self.rate_events
                if r["ip"] == ip and r["action"] == action and r["created_at"] > cutoff
            )
            return [{"cnt": cnt}]

        if s.startswith("SELECT COUNT(*) AS cnt FROM nctqai.auth_codes WHERE user_id"):
            user_id, cutoff = params
            cnt = sum(
                1 for c in self.auth_codes
                if c["user_id"] == user_id and c["created_at"] > cutoff
            )
            return [{"cnt": cnt}]

        if s.startswith("SELECT id, code, expires_at, ip_address, attempts FROM nctqai.auth_codes WHERE user_id"):
            user_id = params[0]
            unused = [
                c for c in self.auth_codes
                if c["user_id"] == user_id and not c["used"]
            ]
            unused.sort(key=lambda c: c["created_at"], reverse=True)
            if not unused:
                return []
            c = unused[0]
            return [{
                "id": c["id"],
                "code": c["code"],
                "expires_at": c["expires_at"],
                "ip_address": c["ip_address"],
                "attempts": c["attempts"],
            }]

        raise AssertionError(f"Unexpected SELECT: {s}")

    # -- run_sql_write (writes) --------------------------------------------
    def run_sql_write(self, sql: str, params: tuple = ()) -> int:
        s = " ".join(sql.split())

        if s.startswith("INSERT INTO nctqai.rate_limits"):
            self.rate_events.append({
                "ip": params[0],
                "action": params[1],
                "created_at": datetime.now(UTC),
            })
            return 1

        if s.startswith("UPDATE nctqai.auth_codes SET used = true WHERE user_id"):
            user_id = params[0]
            n = 0
            for c in self.auth_codes:
                if c["user_id"] == user_id and not c["used"]:
                    c["used"] = True
                    n += 1
            return n

        if s.startswith("INSERT INTO nctqai.auth_codes"):
            user_id, code, expires_at, ip_address, created_at = params
            self.auth_codes.append({
                "id": self._next_code_id,
                "user_id": user_id,
                "code": code,
                "expires_at": expires_at,
                "ip_address": ip_address,
                "created_at": created_at,
                "used": False,
                "attempts": 0,
            })
            self._next_code_id += 1
            return 1

        if s.startswith("UPDATE nctqai.auth_codes SET used = true WHERE id ="):
            code_id = params[0]
            for c in self.auth_codes:
                if c["id"] == code_id:
                    c["used"] = True
                    return 1
            return 0

        if s.startswith("UPDATE nctqai.auth_codes SET used = true WHERE id = %s AND used = false"):
            # Already handled by the broader prefix above.
            raise AssertionError("CAS branch should be caught earlier")

        if s.startswith("UPDATE nctqai.auth_codes SET attempts = attempts + 1"):
            code_id = params[0]
            for c in self.auth_codes:
                if c["id"] == code_id:
                    c["attempts"] += 1
                    return 1
            return 0

        if s.startswith("UPDATE nctqai.users SET last_login"):
            now, user_id = params
            self.users_last_login[user_id] = now
            return 1

        raise AssertionError(f"Unexpected WRITE: {s}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def user_row() -> dict:
    return {
        "id": 42,
        "email": "alice@example.com",
        "name": "Alice",
        "role": "viewer",
        "is_active": True,
        "last_login": None,
    }


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch, user_row: dict) -> FakeDB:
    db = FakeDB(user=user_row)
    monkeypatch.setattr(auth_svc, "run_sql", db.run_sql)
    monkeypatch.setattr(auth_svc, "run_sql_write", db.run_sql_write)
    # Silence the console OTP print in dev mode.
    monkeypatch.setattr(auth_svc, "_send_code_email", lambda *_a, **_kw: None)
    return db


# ---------------------------------------------------------------------------
# Tests — audit finding #3
# ---------------------------------------------------------------------------

def test_issued_code_is_stored_plaintext(fake_db: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 6-digit code persisted to auth_codes.code is the literal code,
    not a SHA-256 (or any other) hash."""
    # Pin the RNG so we know exactly what code was generated.
    monkeypatch.setattr(auth_svc.secrets, "randbelow", lambda _n: 123456)

    result = auth_svc.send_otp("alice@example.com", "1.2.3.4")

    assert result == {"ok": True}
    assert len(fake_db.auth_codes) == 1
    stored = fake_db.auth_codes[0]["code"]
    assert stored == "123456", f"expected plaintext '123456', got {stored!r}"
    assert len(stored) == 6 and stored.isdigit()


def test_verify_with_correct_plaintext_succeeds_and_marks_used(
    fake_db: FakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correct plaintext OTP returns the User and atomically claims the row."""
    monkeypatch.setattr(auth_svc.secrets, "randbelow", lambda _n: 42)
    auth_svc.send_otp("alice@example.com", "1.2.3.4")

    user = auth_svc.verify_otp("alice@example.com", "000042", "1.2.3.4")

    assert user is not None
    assert user.id == 42
    assert fake_db.auth_codes[0]["used"] is True
    assert fake_db.users_last_login[42] is not None


def test_verify_with_wrong_code_fails_and_does_not_claim(
    fake_db: FakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong code increments attempts but does NOT mark used=true (unless
    that wrong guess was the final allowed attempt)."""
    monkeypatch.setattr(auth_svc.secrets, "randbelow", lambda _n: 111111)
    auth_svc.send_otp("alice@example.com", "1.2.3.4")

    result = auth_svc.verify_otp("alice@example.com", "999999", "1.2.3.4")

    assert result is None
    row = fake_db.auth_codes[0]
    assert row["used"] is False, "wrong code should not claim the OTP"
    assert row["attempts"] == 1


def test_verify_after_ttl_expiry_fails(
    fake_db: FakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the row's expires_at is in the past, even the correct code fails."""
    monkeypatch.setattr(auth_svc.secrets, "randbelow", lambda _n: 654321)
    auth_svc.send_otp("alice@example.com", "1.2.3.4")

    # Backdate the row so it's expired.
    fake_db.auth_codes[0]["expires_at"] = datetime.now(UTC) - timedelta(minutes=1)

    result = auth_svc.verify_otp("alice@example.com", "654321", "1.2.3.4")

    assert result is None
    # Expired code is marked used so it can't be retried.
    assert fake_db.auth_codes[0]["used"] is True
