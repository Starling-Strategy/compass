"""Tests for the debug-view review loop: the case_reports repo + routes.

Two layers, mirroring the existing backend test split:

* Repo-level SQL-mapping tests use ``FakeChatPool``/``FakeConn`` from
  ``_chat_pool_fixtures`` (same as ``test_mvp_db_adapters``) to assert the
  repository emits the right SQL/params and returns the right shape without a
  real database.
* Route-level tests inject a fake repository into ``create_app`` and drive it
  with ``TestClient`` (same as ``test_frontend_contract_routes``). The report
  routes are intentionally UNAUTHENTICATED, so these tests pass no auth header.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from compass_backend.api.app import create_app
from compass_backend.config import Settings
from compass_backend.db.case_reports import CaseReportRepository
from compass_backend.tests._chat_pool_fixtures import FakeChatPool, FakeConn


def _settings() -> Settings:
    return Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
    )


def _pool_for(connection: FakeConn) -> FakeChatPool:
    return FakeChatPool(conn=connection)


# ── Repository layer ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_report_inserts_and_returns_id() -> None:
    new_id = uuid.uuid4()
    connection = FakeConn(fetchrow_response={"id": new_id})
    repo = CaseReportRepository(_settings(), pool=_pool_for(connection))

    session_id = uuid.uuid4()
    returned = await repo.insert_report(
        case_id=42,
        session_id=session_id,
        turn_index=1,
        trace_id="trace-1",
        dimension="selection",
        outcome="partial",
        comments="Missed one district",
        reviewer="macon@starlingstrategy.com",
    )

    assert returned == new_id
    sql, args = connection.queries[0]
    assert "INSERT INTO" in sql.upper()
    assert "case_reports" in sql
    assert "RETURNING id" in sql
    # outcome is positional in the INSERT; the value must round-trip.
    assert "partial" in args
    assert 42 in args


@pytest.mark.asyncio
async def test_get_latest_report_returns_most_recent_row() -> None:
    row = {
        "id": uuid.uuid4(),
        "case_id": 42,
        "session_id": uuid.uuid4(),
        "turn_index": 1,
        "trace_id": "trace-1",
        "dimension": "selection",
        "outcome": "fail",
        "comments": "Wrong sort",
        "status": "open",
        "reviewer": "macon@starlingstrategy.com",
        "linked_issue": None,
        "created_at": datetime(2026, 6, 8, tzinfo=UTC),
        "updated_at": datetime(2026, 6, 8, tzinfo=UTC),
    }
    connection = FakeConn(fetchrow_response=row)
    repo = CaseReportRepository(_settings(), pool=_pool_for(connection))

    latest = await repo.get_latest_report(uuid.uuid4(), 42)

    assert latest is not None
    assert latest["outcome"] == "fail"
    sql, _args = connection.queries[0]
    assert "ORDER BY" in sql.upper()
    assert "created_at" in sql.lower()


@pytest.mark.asyncio
async def test_get_latest_report_returns_none_when_absent() -> None:
    connection = FakeConn(fetchrow_response=None)
    repo = CaseReportRepository(_settings(), pool=_pool_for(connection))

    latest = await repo.get_latest_report(uuid.uuid4(), None)

    assert latest is None


@pytest.mark.asyncio
async def test_session_exists_casts_uuid_input_to_text() -> None:
    # chat_messages.session_id is TEXT, but callers hand a UUID — the query
    # must compare as text so a real session is found.
    connection = FakeConn(fetchrow_response={"?column?": 1})
    repo = CaseReportRepository(_settings(), pool=_pool_for(connection))

    exists = await repo.session_exists(uuid.uuid4())

    assert exists is True
    sql, args = connection.queries[0]
    assert "chat_messages" in sql
    # The bound parameter is passed as a string so it matches the TEXT column.
    assert isinstance(args[0], str)


@pytest.mark.asyncio
async def test_update_status_updates_and_bumps_updated_at() -> None:
    report_id = uuid.uuid4()
    # RETURNING id yields a row when the report exists.
    connection = FakeConn(fetchrow_response={"id": report_id})
    repo = CaseReportRepository(_settings(), pool=_pool_for(connection))

    updated = await repo.update_status(
        report_id, status="triaged", reviewer="macon@starlingstrategy.com"
    )

    assert updated is True
    sql, args = connection.queries[0]
    assert "UPDATE" in sql.upper()
    assert "case_reports" in sql
    assert "updated_at = now()" in sql
    assert "COALESCE" in sql.upper()
    assert "RETURNING id" in sql
    # id is bound first, status second, reviewer third.
    assert args[0] == report_id
    assert "triaged" in args
    assert "macon@starlingstrategy.com" in args


@pytest.mark.asyncio
async def test_update_status_returns_false_when_no_row_matched() -> None:
    connection = FakeConn(fetchrow_response=None)
    repo = CaseReportRepository(_settings(), pool=_pool_for(connection))

    updated = await repo.update_status(uuid.uuid4(), status="resolved")

    assert updated is False


# ── Route layer ──────────────────────────────────────────────────────────────


class FakeCaseReportRepo:
    """In-memory stand-in mirroring CaseReportRepository's public surface."""

    def __init__(
        self,
        *,
        known_sessions: set[str] | None = None,
        known_cases: set[int] | None = None,
        known_reports: set[str] | None = None,
    ) -> None:
        self.known_sessions = known_sessions
        self.known_cases = known_cases if known_cases is not None else set()
        self.known_reports = known_reports if known_reports is not None else set()
        self.inserted: list[dict[str, object]] = []
        self.latest: dict[str, object] | None = None
        self.status_updates: list[dict[str, object]] = []

    async def session_exists(self, session_id) -> bool:  # noqa: ANN001
        if self.known_sessions is None:
            return True
        return str(session_id) in self.known_sessions

    async def case_exists(self, case_id: int) -> bool:
        return case_id in self.known_cases

    async def insert_report(self, **kwargs: object) -> uuid.UUID:
        report_id = uuid.uuid4()
        self.inserted.append({**kwargs, "id": report_id})
        return report_id

    async def get_latest_report(self, session_id, case_id):  # noqa: ANN001, ANN201
        return self.latest

    async def update_status(self, report_id, *, status, reviewer=None) -> bool:  # noqa: ANN001
        self.status_updates.append(
            {"report_id": report_id, "status": status, "reviewer": reviewer}
        )
        return str(report_id) in self.known_reports


def _client(repo: FakeCaseReportRepo) -> TestClient:
    app = create_app(
        app_settings=_settings(),
        case_report_repository=repo,
    )
    return TestClient(app)


def test_post_report_happy_path_returns_id_and_open_status() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id}, known_cases={42})
    client = _client(repo)

    response = client.post(
        "/api/v1/debug/report",
        json={
            "session_id": session_id,
            "case_id": 42,
            "turn_index": 1,
            "trace_id": "trace-1",
            "dimension": "selection",
            "outcome": "partial",
            "comments": "Missed one district",
            "reviewer": "macon@starlingstrategy.com",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "open"
    assert uuid.UUID(body["id"])
    assert len(repo.inserted) == 1
    assert repo.inserted[0]["outcome"] == "partial"
    assert repo.inserted[0]["case_id"] == 42


def test_post_report_requires_no_auth_header() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id})
    client = _client(repo)

    # No Authorization header at all — the route is intentionally open.
    response = client.post(
        "/api/v1/debug/report",
        json={"session_id": session_id, "outcome": "pass"},
    )

    assert response.status_code == 200


def test_post_report_rejects_unknown_session() -> None:
    repo = FakeCaseReportRepo(known_sessions=set())
    client = _client(repo)

    response = client.post(
        "/api/v1/debug/report",
        json={"session_id": str(uuid.uuid4()), "outcome": "pass"},
    )

    assert response.status_code == 404
    assert repo.inserted == []


def test_post_report_rejects_unknown_case() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id}, known_cases=set())
    client = _client(repo)

    response = client.post(
        "/api/v1/debug/report",
        json={"session_id": session_id, "case_id": 999, "outcome": "pass"},
    )

    assert response.status_code == 404
    assert repo.inserted == []


def test_post_report_rejects_invalid_outcome() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id})
    client = _client(repo)

    response = client.post(
        "/api/v1/debug/report",
        json={"session_id": session_id, "outcome": "maybe"},
    )

    # Pydantic Literal rejects the value before it reaches the repo.
    assert response.status_code == 422
    assert repo.inserted == []


def test_get_report_returns_latest() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id})
    repo.latest = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "case_id": 42,
        "outcome": "fail",
        "status": "open",
    }
    client = _client(repo)

    response = client.get(
        "/api/v1/debug/report",
        params={"session_id": session_id, "case_id": 42},
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "fail"


def test_get_report_returns_null_when_absent() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id})
    repo.latest = None
    client = _client(repo)

    response = client.get(
        "/api/v1/debug/report",
        params={"session_id": session_id},
    )

    assert response.status_code == 200
    assert response.json() is None


def test_post_status_happy_path_returns_id_and_status() -> None:
    report_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_reports={report_id})
    client = _client(repo)

    response = client.post(
        f"/api/v1/debug/report/{report_id}/status",
        json={"status": "triaged", "reviewer": "macon@starlingstrategy.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == report_id
    assert body["status"] == "triaged"
    assert len(repo.status_updates) == 1
    assert repo.status_updates[0]["status"] == "triaged"
    assert repo.status_updates[0]["reviewer"] == "macon@starlingstrategy.com"


def test_post_status_requires_no_auth_header() -> None:
    report_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_reports={report_id})
    client = _client(repo)

    # No Authorization header — mirrors the P1 routes' open posture.
    response = client.post(
        f"/api/v1/debug/report/{report_id}/status",
        json={"status": "resolved"},
    )

    assert response.status_code == 200


def test_post_status_returns_404_for_unknown_report() -> None:
    repo = FakeCaseReportRepo(known_reports=set())
    client = _client(repo)

    response = client.post(
        f"/api/v1/debug/report/{uuid.uuid4()}/status",
        json={"status": "triaged"},
    )

    assert response.status_code == 404


def test_post_status_rejects_invalid_status() -> None:
    report_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_reports={report_id})
    client = _client(repo)

    response = client.post(
        f"/api/v1/debug/report/{report_id}/status",
        json={"status": "archived"},
    )

    # Pydantic Literal rejects the value before it reaches the repo.
    assert response.status_code == 422
    assert repo.status_updates == []


def test_post_status_accepts_reviewed_no_followup() -> None:
    """#1806: the new terminal status round-trips through the writer endpoint —
    accepted by the Literal (no 422) and passed to the repo, matching the
    widened DB CHECK (migration 168) and nctqai STATUSES."""
    report_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_reports={report_id})
    client = _client(repo)

    response = client.post(
        f"/api/v1/debug/report/{report_id}/status",
        json={"status": "reviewed_no_followup"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "reviewed_no_followup"
    assert len(repo.status_updates) == 1
    assert repo.status_updates[0]["status"] == "reviewed_no_followup"


# ── Input bounds (unauthenticated endpoint hardening, #1349) ──────────────────


def test_post_report_rejects_overlong_comments() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id})
    client = _client(repo)

    response = client.post(
        "/api/v1/debug/report",
        json={
            "session_id": session_id,
            "outcome": "pass",
            "comments": "x" * 4001,
        },
    )

    # max_length=4000 → Pydantic rejects before the repo is touched, so an
    # unbounded comment can never be persisted by an unauthenticated caller.
    assert response.status_code == 422
    assert repo.inserted == []


def test_post_report_rejects_overrange_case_id() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id})
    client = _client(repo)

    response = client.post(
        "/api/v1/debug/report",
        # Above INTEGER max — must 422 here, not overflow to a 500 downstream.
        json={"session_id": session_id, "outcome": "pass", "case_id": 9_999_999_999},
    )

    assert response.status_code == 422
    assert repo.inserted == []


def test_post_report_rejects_negative_turn_index() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id})
    client = _client(repo)

    response = client.post(
        "/api/v1/debug/report",
        json={"session_id": session_id, "outcome": "pass", "turn_index": -1},
    )

    assert response.status_code == 422
    assert repo.inserted == []


# ── Rate limiting (unauthenticated endpoint hardening, #1349) ─────────────────


def _client_with(settings: Settings, repo: FakeCaseReportRepo) -> TestClient:
    return TestClient(
        create_app(app_settings=settings, case_report_repository=repo)
    )


def test_post_report_enforces_rate_limit() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id})
    settings = Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
        debug_report_rate_limit_per_minute=1,
    )
    client = _client_with(settings, repo)
    payload = {"session_id": session_id, "outcome": "pass"}

    first = client.post("/api/v1/debug/report", json=payload)
    second = client.post("/api/v1/debug/report", json=payload)

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    # Only the first request reached the repo; the flood is rejected at the edge.
    assert len(repo.inserted) == 1


def test_rate_limit_disabled_allows_burst() -> None:
    session_id = str(uuid.uuid4())
    repo = FakeCaseReportRepo(known_sessions={session_id})
    settings = Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
        debug_report_rate_limit_enabled=False,
    )
    client = _client_with(settings, repo)
    payload = {"session_id": session_id, "outcome": "pass"}

    for _ in range(5):
        assert (
            client.post("/api/v1/debug/report", json=payload).status_code == 200
        )
    assert len(repo.inserted) == 5
