"""Tests for fresh API-key auth compatibility."""

from __future__ import annotations

from typing import Any

import logfire.testing
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from compass_backend.api.app import create_app
from compass_backend.api.auth import ApiKeyAuthRepository, AuthenticatedUser, generate_api_key, hash_api_key
from compass_backend.config import Settings, settings as runtime_settings
from compass_backend.contracts import DirectResponse, PlannerTurn
from compass_backend.session import InMemorySessionStore
from compass_backend.tests._chat_pool_fixtures import FakeChatPool, FakeConn


def _auth_conn(row: dict[str, Any] | None) -> FakeConn:
    """Build a FakeConn pre-populated with one ``fetchrow`` row."""

    return FakeConn(fetchrow_response=row)


class StaticAuthRepository:
    """Fake auth repository for route-level auth tests."""

    def __init__(self, user: AuthenticatedUser | None) -> None:
        self.user = user
        self.tokens: list[str] = []

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        self.tokens.append(token)
        return self.user


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
        api_key_auth_enabled=enabled,
        session_store_backend="memory",
    )


def _agent_for_turn(turn: PlannerTurn) -> Agent:
    # call_tools=[] keeps the canned-output TestModel from auto-invoking the
    # always-attached Compass catalog toolset (#1248); the offline test app
    # has no live DB pool for a tool call to reach.
    return Agent(
        TestModel(
            custom_output_args=turn.model_dump(mode="json"),
            call_tools=[],
        ),
        output_type=PlannerTurn,
    )


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.98,
        direct_response=DirectResponse(
            message="Authenticated hello.",
            reason="Greeting does not require data execution.",
        ),
    )


def test_hash_api_key_matches_legacy_sha256_full_token() -> None:
    token = "pa_dev_0123456789abcdef0123456789abcdef"

    assert hash_api_key(token) == (
        "5d7bf08d58f0139df8363e92b54fe13a2a8717312f8ceb7708b51ca8a75a2f21"
    )


def test_generate_api_key_preserves_legacy_shape() -> None:
    full_key, key_id, key_hash = generate_api_key("dev")

    assert full_key.startswith("pa_dev_")
    assert key_id.startswith("pa_dev_")
    assert len(full_key.removeprefix("pa_dev_")) == 32
    assert len(key_id.removeprefix("pa_dev_")) == 8
    assert key_hash == hash_api_key(full_key)


@pytest.mark.asyncio
async def test_api_key_repository_accepts_valid_token_and_updates_usage() -> None:
    token = "pa_dev_0123456789abcdef0123456789abcdef"
    connection = _auth_conn(
        {
            "key_id": "pa_dev_01234567",
            "owner_email": "reviewer@example.org",
            "name": "staging admin",
            "is_admin": True,
        }
    )
    repository = ApiKeyAuthRepository(_settings(), pool=FakeChatPool(connection))

    user = await repository.authenticate(token)

    assert user == AuthenticatedUser(
        email="reviewer@example.org",
        name="staging admin",
        auth_method="api_key",
        api_key_id="pa_dev_01234567",
        is_admin=True,
    )
    sql_text = "\n".join(sql for sql, _args in connection.queries)
    assert '"compass"."api_keys"' in sql_text
    assert '"compass"."users"' in sql_text
    assert "policy_advisor" not in sql_text
    assert connection.queries[0][1] == (hash_api_key(token),)
    assert connection.queries[1][1] == ("pa_dev_01234567",)


@pytest.mark.asyncio
async def test_api_key_repository_rejects_unknown_token() -> None:
    connection = _auth_conn(None)
    repository = ApiKeyAuthRepository(_settings(), pool=FakeChatPool(connection))

    user = await repository.authenticate("pa_dev_0123456789abcdef0123456789abcdef")

    assert user is None
    assert len(connection.queries) == 1


def test_chat_requires_valid_api_key_when_api_key_auth_is_enabled() -> None:
    repository = StaticAuthRepository(None)
    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=InMemorySessionStore(),
            app_settings=_settings(enabled=True),
            api_key_auth_repository=repository,
        )
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "hello"},
            headers={"Authorization": "Bearer invalid"},
        )

    assert response.status_code == 401
    assert repository.tokens == ["invalid"]


def test_repeated_unauthenticated_chat_returns_401_not_429() -> None:
    repository = StaticAuthRepository(None)
    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=InMemorySessionStore(),
            app_settings=_settings(enabled=True),
            api_key_auth_repository=repository,
        )
    ) as client:
        responses = [
            client.post(
                "/api/v1/chat/simple",
                json={"message": "hello"},
                headers={"Authorization": "Bearer invalid"},
            )
            for _ in range(55)
        ]

    assert {response.status_code for response in responses} == {401}
    assert repository.tokens == ["invalid"] * 55


def test_chat_accepts_valid_api_key_when_api_key_auth_is_enabled() -> None:
    repository = StaticAuthRepository(
        AuthenticatedUser(
            email="reviewer@example.org",
            auth_method="api_key",
            api_key_id="pa_dev_01234567",
            is_admin=True,
        )
    )
    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=InMemorySessionStore(),
            app_settings=_settings(enabled=True),
            api_key_auth_repository=repository,
        )
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "hello"},
            headers={
                "Authorization": "Bearer pa_dev_0123456789abcdef0123456789abcdef"
            },
        )

    assert response.status_code == 200
    assert response.json()["message"] == "Authenticated hello."
    assert repository.tokens == ["pa_dev_0123456789abcdef0123456789abcdef"]


def test_request_identifier_uses_authenticated_owner_email() -> None:
    from compass_backend.api.deps import request_identifier

    class Request:
        headers = {}
        client = None

        class State:
            auth_user = AuthenticatedUser(
                email="owner@example.org",
                auth_method="api_key",
                api_key_id="pa_dev_01234567",
            )

        state = State()

    assert request_identifier(Request()) == "user:owner@example.org"


def test_request_identifier_uses_api_key_id_when_owner_email_is_absent() -> None:
    from compass_backend.api.deps import request_identifier

    class Request:
        headers = {}
        client = None

        class State:
            auth_user = AuthenticatedUser(
                email=None,
                auth_method="api_key",
                api_key_id="pa_dev_01234567",
            )

        state = State()

    assert request_identifier(Request()) == "api_key:pa_dev_01234567"


def test_chat_rejects_non_owner_session_continuation() -> None:
    store = InMemorySessionStore()
    repository = StaticAuthRepository(
        AuthenticatedUser(
            email="owner@example.org",
            auth_method="api_key",
            api_key_id="pa_dev_owner",
        )
    )

    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=store,
            app_settings=_settings(enabled=True),
            api_key_auth_repository=repository,
        )
    ) as client:
        first_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "hello"},
            headers={"Authorization": "Bearer pa_dev_owner"},
        )
        session_id = first_response.json()["session"]["session_id"]

        repository.user = AuthenticatedUser(
            email="other@example.org",
            auth_method="api_key",
            api_key_id="pa_dev_other",
        )
        second_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "continue", "session_id": session_id},
            headers={"Authorization": "Bearer pa_dev_other"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 404
    assert second_response.json()["detail"] == "Session not found"


def test_chat_allows_admin_session_continuation() -> None:
    store = InMemorySessionStore()
    repository = StaticAuthRepository(
        AuthenticatedUser(
            email="owner@example.org",
            auth_method="api_key",
            api_key_id="pa_dev_owner",
        )
    )

    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=store,
            app_settings=_settings(enabled=True),
            api_key_auth_repository=repository,
        )
    ) as client:
        first_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "hello"},
            headers={"Authorization": "Bearer pa_dev_owner"},
        )
        session_id = first_response.json()["session"]["session_id"]

        repository.user = AuthenticatedUser(
            email="admin@example.org",
            auth_method="api_key",
            api_key_id="pa_dev_admin",
            is_admin=True,
        )
        second_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "continue", "session_id": session_id},
            headers={"Authorization": "Bearer pa_dev_admin"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["session"]["session_id"] == session_id


# ---------------------------------------------------------------------------
# compass.auth.api_key span coverage (Tier 2.2 audit)
# ---------------------------------------------------------------------------


def _enable_logfire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force settings.logfire_enabled True so compass_span emits real spans."""
    monkeypatch.setattr(
        runtime_settings,
        "logfire_token",
        SecretStr("pylf_v1_us_real_token_for_auth_test"),
    )


@pytest.mark.asyncio
async def test_api_key_auth_span_records_success_attributes(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compass.auth.api_key carries outcome=success on a valid token."""
    _enable_logfire(monkeypatch)
    token = "pa_dev_0123456789abcdef0123456789abcdef"
    connection = _auth_conn(
        {
            "key_id": "pa_dev_01234567",
            "owner_email": "reviewer@example.org",
            "name": "staging admin",
            "is_admin": True,
        }
    )
    repository = ApiKeyAuthRepository(_settings(), pool=FakeChatPool(connection))

    user = await repository.authenticate(token)
    assert user is not None

    auth_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.auth.api_key"
    ]
    assert len(auth_spans) == 1
    attrs = auth_spans[0]["attributes"]
    assert attrs["outcome"] == "success"
    assert attrs["is_admin"] is True
    # Identity attributes are present (scrubbing-config-dependent in test fixture
    # vs production — see compass_logfire_scrubbing_callback). Assert presence.
    assert "key_id" in attrs
    assert "owner_email" in attrs


@pytest.mark.asyncio
async def test_api_key_auth_span_records_not_found_for_unknown_token(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compass.auth.api_key carries outcome=not_found when the DB lookup misses."""
    _enable_logfire(monkeypatch)
    connection = _auth_conn(None)
    repository = ApiKeyAuthRepository(_settings(), pool=FakeChatPool(connection))

    user = await repository.authenticate("pa_dev_0123456789abcdef0123456789abcdef")
    assert user is None

    auth_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.auth.api_key"
    ]
    assert len(auth_spans) == 1
    attrs = auth_spans[0]["attributes"]
    assert attrs["outcome"] == "not_found"
    # Identity attributes are deliberately NOT set when no row was found.
    assert "key_id" not in attrs
    assert "owner_email" not in attrs


@pytest.mark.asyncio
async def test_api_key_auth_span_records_not_pa_prefix_without_db(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-pa_ token rejects fast without touching the database."""
    _enable_logfire(monkeypatch)
    # The empty pool would raise on _acquire(); the test passes iff the
    # prefix check rejects first and never touches the pool.
    repository = ApiKeyAuthRepository(_settings(), pool=FakeChatPool())

    user = await repository.authenticate("not-a-pa-token")
    assert user is None

    auth_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.auth.api_key"
    ]
    assert len(auth_spans) == 1
    attrs = auth_spans[0]["attributes"]
    assert attrs["outcome"] == "not_pa_prefix"
