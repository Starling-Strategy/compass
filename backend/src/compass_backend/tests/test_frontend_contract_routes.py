from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from compass_backend.api.app import create_app
from compass_backend.api.auth import AuthenticatedUser
from compass_backend.api.frontend_contracts import (
    _export_filename,
    _neutralize_formula,
    _table_csv,
)
from compass_backend.config import Settings
from compass_backend.contracts.frontend import (
    ConversationExportMessage,
    ConversationExportRequest,
    ConversationExportTable,
    ConversationMessage,
    ConversationResponse,
    DebugBackfillResponse,
    FeedbackResponse,
)


class StaticAuthRepository:
    def __init__(self, user: AuthenticatedUser) -> None:
        self.user = user

    async def authenticate(self, _token: str) -> AuthenticatedUser | None:
        return self.user


class FakeFrontendRepository:
    def __init__(self) -> None:
        self.access_checks: list[tuple[str, str | None, bool, bool]] = []
        self.feedback: dict[tuple[str, int], FeedbackResponse] = {}
        self.debug_calls: list[str] = []

    async def get_conversation(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> ConversationResponse:
        self.access_checks.append((session_id, owner_email, is_admin, auth_required))
        return ConversationResponse(
            session_id=session_id,
            created_at=datetime(2026, 5, 8, 12, 0, tzinfo=UTC),
            title="Contract smoke test",
            messages=[
                ConversationMessage(
                    id=101,
                    role="user",
                    content="Show me the data",
                    created_at=datetime(2026, 5, 8, 12, 1, tzinfo=UTC),
                ),
                ConversationMessage(
                    id=102,
                    role="assistant",
                    content="Here is a table.",
                    created_at=datetime(2026, 5, 8, 12, 2, tzinfo=UTC),
                    trace_id="trace-102",
                    data={"tables": [{"title": "Rows", "columns": ["Name"], "rows": [["A"]]}]},
                ),
            ],
        )

    async def ensure_session_access(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> None:
        self.access_checks.append((session_id, owner_email, is_admin, auth_required))

    async def upsert_feedback(
        self,
        *,
        session_id: str,
        message_id: int,
        rating: int,
        feedback_tags: list[str],
        feedback_text: str,
        trace_id: str | None,
        user_email: str | None,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> FeedbackResponse:
        self.access_checks.append((session_id, owner_email, is_admin, auth_required))
        feedback = FeedbackResponse(
            id=7,
            session_id=session_id,
            message_id=message_id,
            rating=rating,
            feedback_tags=feedback_tags,
            feedback_text=feedback_text,
            trace_id=trace_id,
            created_at=datetime(2026, 5, 8, 12, 3, tzinfo=UTC),
            updated_at=datetime(2026, 5, 8, 12, 3, tzinfo=UTC),
        )
        self.feedback[(session_id, message_id)] = feedback
        return feedback

    async def list_feedback(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> list[FeedbackResponse]:
        self.access_checks.append((session_id, owner_email, is_admin, auth_required))
        return [item for (stored_session, _), item in self.feedback.items() if stored_session == session_id]

    async def delete_feedback(
        self,
        *,
        session_id: str,
        message_id: int,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> bool:
        self.access_checks.append((session_id, owner_email, is_admin, auth_required))
        return self.feedback.pop((session_id, message_id), None) is not None

    async def get_debug_backfill(
        self,
        session_id: str,
        *,
        owner_email: str | None,
        is_admin: bool,
        auth_required: bool,
    ) -> DebugBackfillResponse:
        self.debug_calls.append(session_id)
        self.access_checks.append((session_id, owner_email, is_admin, auth_required))
        return DebugBackfillResponse(
            session_id=session_id,
            trace_ids=["trace-102"],
            usage=[],
            usage_totals={"cost_usd": 0.0},
            verdicts=[],
            linked_scenarios=[],
        )


class DenyingFrontendRepository(FakeFrontendRepository):
    async def get_conversation(self, *_args: Any, **_kwargs: Any) -> ConversationResponse:
        raise HTTPException(status_code=404, detail="Conversation not found")


def _settings() -> Settings:
    return Settings(
        api_key_auth_enabled=True,
        api_key_hash_secret=SecretStr("test-secret"),
        allow_dev_user=False,
        session_store_backend="memory",
    )


def _client(
    repo: FakeFrontendRepository | None = None,
    *,
    is_admin: bool = False,
    owner_email: str = "owner@example.org",
) -> tuple[TestClient, FakeFrontendRepository]:
    frontend_repo = repo or FakeFrontendRepository()
    app = create_app(
        app_settings=_settings(),
        api_key_auth_repository=StaticAuthRepository(
            AuthenticatedUser(email=owner_email, is_admin=is_admin)
        ),
        frontend_contract_repository=frontend_repo,
    )
    return TestClient(app), frontend_repo


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_conversation_resync_route_returns_strict_message_contract() -> None:
    client, repo = _client()

    response = client.get("/api/v1/conversations/session-1", headers=_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "session-1"
    assert payload["messages"][1]["id"] == 102
    assert payload["messages"][1]["data"]["tables"][0]["title"] == "Rows"
    assert repo.access_checks == [("session-1", "owner@example.org", False, True)]


def test_conversation_route_hides_denied_sessions() -> None:
    client, _repo = _client(DenyingFrontendRepository())

    response = client.get("/api/v1/conversations/other-session", headers=_headers())

    assert response.status_code == 404


def test_feedback_submit_list_and_delete_use_authenticated_owner() -> None:
    client, repo = _client()

    submit = client.post(
        "/api/v1/feedback",
        headers=_headers(),
        json={
            "session_id": "session-1",
            "message_id": 102,
            "rating": 1,
            "feedback_tags": ["useful"],
            "feedback_text": "This answered my question.",
            "trace_id": "trace-102",
        },
    )
    assert submit.status_code == 200
    # #1121: submitter email must NOT come back in the response body.
    assert "user_email" not in submit.json()

    listed = client.get("/api/v1/feedback", headers=_headers(), params={"session_id": "session-1"})
    assert listed.status_code == 200
    assert listed.json()["feedback"][0]["feedback_tags"] == ["useful"]

    deleted = client.request(
        "DELETE",
        "/api/v1/feedback",
        headers=_headers(),
        json={"session_id": "session-1", "message_id": 102},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert ("session-1", 102) not in repo.feedback


def test_feedback_response_contract_excludes_submitter_email() -> None:
    """#1121: submitter PII (user_email) must never be in the feedback response
    contract — so it cannot leak via GET/POST /feedback even when auth is off
    (the staging default), where the per-owner authorization short-circuits."""
    fr = FeedbackResponse(id=1, session_id="s", message_id=2, rating=1)
    assert "user_email" not in FeedbackResponse.model_fields
    assert "user_email" not in fr.model_dump()
    # The contract is strict (extra="forbid"), so it also refuses the field on
    # input — a future SELECT/RETURNING that re-adds user_email fails loudly.
    with pytest.raises(ValidationError):
        FeedbackResponse(id=1, session_id="s", message_id=2, rating=1, user_email="x@y.z")


def test_conversation_export_returns_a_zip_after_session_access_check() -> None:
    client, repo = _client()

    response = client.post(
        "/api/v1/conversations/export",
        headers=_headers(),
        json={
            "session_id": "session-1",
            "conversation_title": "Contract export",
            "messages": [
                {"role": "user", "content": "Show rows"},
                {
                    "role": "assistant",
                    "content": "Done",
                    "tables": [
                        {
                            "title": "Example table",
                            "columns": ["Name", "Value"],
                            "rows": [["A", "1"]],
                        }
                    ],
                },
            ],
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment;" in response.headers["content-disposition"]
    assert repo.access_checks == [("session-1", "owner@example.org", False, True)]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "conversation.json" in names
        assert "conversation.md" in names
        assert "tables/example-table.csv" in names


def test_conversation_export_with_unicode_title_does_not_crash_header() -> None:
    """#1611: a conversation title with a smart quote + em-dash (common in
    LLM-generated titles) must not raise UnicodeEncodeError in the ASGI Latin-1
    header encoder. The route emits an RFC 6266 ASCII fallback plus filename*."""
    client, _repo = _client()

    response = client.post(
        "/api/v1/conversations/export",
        headers=_headers(),
        json={
            "session_id": "session-1",
            "conversation_title": "Teachers’ pay — a look",
            "messages": [
                {"role": "user", "content": "Show rows"},
                {
                    "role": "assistant",
                    "content": "Done",
                    "tables": [{"title": "T", "columns": ["Name"], "rows": [["A"]]}],
                },
            ],
        },
    )

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    # The ASCII fallback drops the non-Latin-1 chars but keeps the readable stem.
    assert 'filename="NCTQ Compass - Teachers pay  a look - ' in disposition


def test_debug_backfill_requires_admin_token() -> None:
    user_client, user_repo = _client(is_admin=False)
    denied = user_client.get("/api/v1/conversations/session-1/debug", headers=_headers())
    assert denied.status_code == 403
    assert user_repo.debug_calls == []

    admin_client, admin_repo = _client(is_admin=True, owner_email="admin@example.org")
    allowed = admin_client.get("/api/v1/conversations/session-1/debug", headers=_headers())
    assert allowed.status_code == 200
    assert allowed.json()["trace_ids"] == ["trace-102"]
    assert admin_repo.access_checks == [("session-1", "admin@example.org", True, True)]


def test_retired_document_and_lazy_message_export_backend_routes_stay_absent() -> None:
    client, _repo = _client()

    assert client.get("/api/v1/documents/doc-1", headers=_headers()).status_code == 404
    assert (
        client.get("/api/v1/messages/102/exports/table.csv", headers=_headers()).status_code
        == 404
    )


def _csv_rows(csv_text: str) -> list[list[str]]:
    import csv as _csv

    return list(_csv.reader(io.StringIO(csv_text)))


def test_table_csv_uses_public_projection_when_present() -> None:
    """#1611: when a table carries the pruned public projection, the ZIP CSV is
    built from public_columns/public_rows, not the raw rendered columns."""
    table = ConversationExportTable(
        columns=["District", "Value", "Data Note", "Ranked By"],
        rows=[{"District": "Alpha", "Value": "1", "Data Note": "", "Ranked By": "Salary"}],
        public_columns=["District", "Value"],
        public_rows=[{"District": "Alpha", "Value": "1"}],
    )

    rows = _csv_rows(_table_csv(table))

    assert rows[0] == ["District", "Value"]
    assert rows[1] == ["Alpha", "1"]


def test_table_csv_falls_back_when_public_projection_absent() -> None:
    """Older payloads (no public projection) keep today's columns/rows behavior."""
    table = ConversationExportTable(
        columns=["District", "Value"],
        rows=[{"District": "Beta", "Value": "2"}],
    )

    rows = _csv_rows(_table_csv(table))

    assert rows[0] == ["District", "Value"]
    assert rows[1] == ["Beta", "2"]


def test_export_filename_uses_human_title_and_dated_shape() -> None:
    body = ConversationExportRequest(
        session_id="session-1",
        exported_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
        conversation_title="Top 10 salaries (bachelor's)",
        messages=[
            ConversationExportMessage(role="user", content="Hi"),
            ConversationExportMessage(role="assistant", content="Hello"),
        ],
    )

    assert _export_filename(body) == (
        "NCTQ Compass - Top 10 salaries (bachelor's) - 2026-06-15.zip"
    )


def test_export_filename_sanitizes_os_illegal_characters() -> None:
    """A title with a slash (and other OS-illegal chars) is stripped, while
    spaces/commas/parens/apostrophes are kept."""
    body = ConversationExportRequest(
        session_id="session-1",
        exported_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
        conversation_title='Pay: high/low, "best" <districts>',
        messages=[
            ConversationExportMessage(role="user", content="Hi"),
            ConversationExportMessage(role="assistant", content="Hello"),
        ],
    )

    name = _export_filename(body)

    assert name == "NCTQ Compass - Pay highlow, best districts - 2026-06-15.zip"
    for illegal in "\\/:*?\"<>|":
        assert illegal not in name.removesuffix(".zip")


def test_export_filename_falls_back_to_session_slug_without_title() -> None:
    body = ConversationExportRequest(
        session_id="ABC-123",
        exported_at=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
        messages=[
            ConversationExportMessage(role="user", content="Hi"),
            ConversationExportMessage(role="assistant", content="Hello"),
        ],
    )

    assert _export_filename(body) == "compass-2026-06-15-abc-123.zip"


def test_neutralize_formula_blocks_payloads_and_keeps_numbers() -> None:
    """#1611/#1496: dangerous leading chars are quoted; bare numbers pass."""
    assert _neutralize_formula("=SUM(A1)") == "'=SUM(A1)"
    assert _neutralize_formula("@cmd") == "'@cmd"
    assert _neutralize_formula("\tTAB") == "'\tTAB"
    assert _neutralize_formula("\rCR") == "'\rCR"
    # Leading - / + that are not bare numbers get neutralized.
    assert _neutralize_formula("-2+3") == "'-2+3"
    assert _neutralize_formula("+1+1") == "'+1+1"
    # Legitimate negative numbers stay readable (not neutralized).
    assert _neutralize_formula("-5") == "-5"
    assert _neutralize_formula("-3.2") == "-3.2"
    # Canonical shared shape with the JS `BARE_NUMBER`: sign-led dot-decimals
    # are bare numbers and pass through on BOTH sinks; a "%"-suffixed value is
    # NOT a bare number and is neutralized. These lock the cross-language seam.
    assert _neutralize_formula("-.5") == "-.5"
    assert _neutralize_formula("+.5") == "+.5"
    assert _neutralize_formula("-5.") == "-5."
    assert _neutralize_formula("-3.2%") == "'-3.2%"
    # Ordinary values are untouched.
    assert _neutralize_formula("Alpha District") == "Alpha District"
    assert _neutralize_formula("$50,000") == "$50,000"


def test_table_csv_neutralizes_dangerous_cells() -> None:
    """A formula payload in a real ZIP table CSV is neutralized; a negative
    number flows through unchanged."""
    table = ConversationExportTable(
        public_columns=["District", "Delta"],
        public_rows=[{"District": "=SUM(A1)", "Delta": "-3.2"}],
    )

    rows = _csv_rows(_table_csv(table))

    assert rows[1] == ["'=SUM(A1)", "-3.2"]
