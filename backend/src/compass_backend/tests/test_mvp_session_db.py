"""Tests for the fresh DB-backed session store."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import logfire.testing
import pytest
from pydantic import SecretStr

from compass_backend.config import Settings
from compass_backend.config import settings as runtime_settings
from compass_backend.contracts import (
    ConversationMemory,
    ConversationSummary,
    DirectResponse,
    MetricSpec,
    PlannerTurn,
    PlannerRunEvidence,
    PendingQueryContext,
    QueryContext,
    QueryPlan,
    ResultMemoryRef,
    SelectionSpec,
    TurnSnapshot,
)
from compass_backend.session import (
    PostgresSessionStore,
    SessionAccessDenied,
    TurnErrorEnvelope,
)
from compass_backend.tests._chat_pool_fixtures import FakeChatPool


class FakeTransaction:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> None:
        self.connection.transaction_entries += 1

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.connection.transaction_exits += 1


class FakeConnection:
    """Small asyncpg-like connection for session-store SQL tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.connect_kwargs: dict[str, object] = {}
        self.closed = False
        self.transaction_entries = 0
        self.transaction_exits = 0

    async def execute(self, sql: str, *args: object) -> str:
        self.queries.append((sql, args))
        if "INSERT INTO" in sql and "chat_sessions" in sql:
            session_id = str(args[0])
            self.sessions.setdefault(
                session_id,
                {
                    "session_id": session_id,
                    "owner_email": args[1] if len(args) > 1 else None,
                    "created_at": datetime(2026, 5, 6, tzinfo=UTC),
                },
            )
            return "INSERT 0 1"
        return "OK"

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        self.queries.append((sql, args))
        if "FROM" in sql and "chat_sessions" in sql:
            return self.sessions.get(str(args[0]))
        if "INSERT INTO" in sql and "chat_messages" in sql:
            message_data = json.loads(str(args[3])) if args[3] is not None else None
            message = {
                "id": len(self.messages) + 1,
                "session_id": str(args[0]),
                "role": str(args[1]),
                "content": str(args[2]),
                "message_data": message_data,
                "trace_id": args[4] if len(args) > 4 else None,
                "timestamp": datetime(2026, 5, 6, 0, len(self.messages), tzinfo=UTC),
            }
            self.messages.append(message)
            return {"id": message["id"]}
        return None

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        self.queries.append((sql, args))
        session_id = str(args[0])
        rows = []
        for message in self.messages:
            snapshot = (message.get("message_data") or {}).get(
                "fresh_compass_turn_snapshot"
            )
            if message["session_id"] == session_id and snapshot is not None:
                rows.append({"snapshot": snapshot})
        return sorted(rows, key=lambda row: row["snapshot"]["turn_index"])

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def close(self) -> None:
        self.closed = True


def _settings() -> Settings:
    return Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
        session_store_backend="postgres",
    )


def _snapshot(session_id: str, turn_index: int = 1) -> TurnSnapshot:
    return TurnSnapshot(
        snapshot_id=f"snapshot-{turn_index}",
        session_id=session_id,
        turn_index=turn_index,
        user_message=f"user message {turn_index}",
        assistant_message=f"assistant message {turn_index}",
        planner_turn=PlannerTurn(
            route="direct",
            confidence=0.95,
            direct_response=DirectResponse(
                message=f"assistant message {turn_index}",
                reason="Synthetic session persistence test turn.",
            ),
        ),
        created_at=datetime(2026, 5, 6, 0, turn_index, tzinfo=UTC),
    )


def _query_context() -> QueryContext:
    return QueryContext(
        query_plan=QueryPlan(
            question="Rank California salaries.",
            selection=SelectionSpec(scope="state", states=["CA"]),
            metrics=[MetricSpec(name="starting salary")],
        ),
        result_type="metric_ranking",
        order_statement="Ranked in CA by starting salary, highest to lowest.",
        row_count=10,
    )


def _conversation_memory(turn_index: int = 1) -> ConversationMemory:
    return ConversationMemory(
        summary=ConversationSummary(
            summary="User is comparing salary policies across California districts.",
            active_user_goal="Compare salary policies.",
            open_questions=["Which salary lane should Compass use?"],
            accepted_choices=["California districts"],
            rejected_choices=["Benefits metrics"],
            user_preferences=["Prefers chartable results"],
        ),
        latest_turn_index=turn_index,
        source_snapshot_ids=[f"snapshot-{turn_index}"],
    )


def _memory(
    *,
    query_context: QueryContext | None = None,
    pending_query_context: PendingQueryContext | None = None,
    result_refs: list[ResultMemoryRef] | None = None,
    turn_index: int = 1,
) -> ConversationMemory:
    return ConversationMemory(
        latest_query_context=query_context,
        pending_query_context=pending_query_context,
        result_refs=result_refs or [],
        latest_turn_index=turn_index,
        source_snapshot_ids=[f"snapshot-{turn_index}"],
    )


def _result_memory_ref(snapshot_id: str = "snapshot-1", turn_index: int = 1) -> ResultMemoryRef:
    return ResultMemoryRef(
        snapshot_id=snapshot_id,
        turn_index=turn_index,
        question="Rank California salaries.",
        result_type="metric_ranking",
        row_count=10,
        displayed_row_count=10,
        display_limit=None,
        metrics=[{"metric_id": 89, "metric_name": "Starting salary"}],
        districts=[{"district_id": 1, "district_name": "Alpha USD", "state": "CA"}],
        has_chart=True,
        has_csv_export=True,
        digest="10 salary rows for California districts.",
    )


@pytest.mark.asyncio
async def test_postgres_session_store_creates_new_db_session() -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    state = await store.load()

    assert state.session_id in connection.sessions
    assert state.turn_count == 0
    assert state.latest_snapshot_id is None


@pytest.mark.asyncio
async def test_postgres_session_store_records_owner_email_for_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    state = await store.load(owner_email="owner@example.org")

    assert connection.sessions[state.session_id]["owner_email"] == "owner@example.org"


@pytest.mark.asyncio
async def test_postgres_session_store_continues_existing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    connection.sessions["session-1"] = {
        "session_id": "session-1",
        "owner_email": "owner@example.org",
        "created_at": datetime(2026, 5, 6, tzinfo=UTC),
    }
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    state = await store.load("session-1", owner_email="owner@example.org")

    assert state.session_id == "session-1"
    assert state.created_at == datetime(2026, 5, 6, tzinfo=UTC)
    assert state.turn_count == 0
    assert len(connection.sessions) == 1


@pytest.mark.asyncio
async def test_postgres_session_store_rejects_non_owner_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    connection.sessions["session-1"] = {
        "session_id": "session-1",
        "owner_email": "owner@example.org",
        "created_at": datetime(2026, 5, 6, tzinfo=UTC),
    }
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    with pytest.raises(SessionAccessDenied):
        await store.load(
            "session-1",
            owner_email="other@example.org",
            create_if_missing=False,
        )


@pytest.mark.asyncio
async def test_postgres_session_store_allows_admin_to_continue_any_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    connection.sessions["session-1"] = {
        "session_id": "session-1",
        "owner_email": "owner@example.org",
        "created_at": datetime(2026, 5, 6, tzinfo=UTC),
    }
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    state = await store.load(
        "session-1",
        owner_email="admin@example.org",
        is_admin=True,
        create_if_missing=False,
    )

    assert state.session_id == "session-1"


@pytest.mark.asyncio
async def test_postgres_session_store_saves_one_turn_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))
    snapshot = _snapshot("session-1")

    saved = await store.save_turn(snapshot)

    assert saved.session_id == "session-1"
    assert saved.turn_count == 1
    assert saved.latest_snapshot_id == "snapshot-1"
    assert saved.message_ids == [1, 2]
    assert saved.assistant_message_ids == [2]
    assert connection.transaction_entries == 1
    assert connection.transaction_exits == 1
    assert [message["role"] for message in connection.messages] == [
        "user",
        "assistant",
    ]
    assert connection.messages[0]["message_data"] is None
    assert connection.messages[1]["message_data"]["fresh_compass_turn_snapshot"][
        "snapshot_id"
    ] == "snapshot-1"


@pytest.mark.asyncio
async def test_postgres_session_store_persists_trace_id_on_messages_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))
    snapshot = _snapshot("session-1").model_copy(update={"trace_id": "trace-123"})

    saved = await store.save_turn(snapshot)

    assert saved.message_ids == [1, 2]
    assert [message["trace_id"] for message in connection.messages] == [
        "trace-123",
        "trace-123",
    ]
    assert connection.messages[1]["message_data"]["fresh_compass_turn_snapshot"][
        "trace_id"
    ] == "trace-123"


@pytest.mark.asyncio
async def test_postgres_session_store_reloads_turn_state_from_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    await store.save_turn(_snapshot("session-1", turn_index=1))
    await store.save_turn(_snapshot("session-1", turn_index=2))
    state = await store.load("session-1")

    assert state.turn_count == 2
    assert state.latest_snapshot_id == "snapshot-2"
    assert state.updated_at == datetime(2026, 5, 6, 0, 2, tzinfo=UTC)


@pytest.mark.asyncio
async def test_postgres_session_store_reloads_latest_query_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))
    first_snapshot = _snapshot("session-1", turn_index=1)
    second_snapshot = _snapshot("session-1", turn_index=2).model_copy(
        update={"memory": _memory(query_context=_query_context(), turn_index=2)}
    )

    await store.save_turn(first_snapshot)
    await store.save_turn(second_snapshot)
    state = await store.load("session-1")

    assert state.query_context is not None
    assert state.query_context.query_plan.selection.states == ["CA"]
    assert state.query_context.result_type == "metric_ranking"
    assert state.query_context.row_count == 10


@pytest.mark.asyncio
async def test_postgres_session_store_reloads_latest_conversation_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    first_snapshot = _snapshot("session-1", turn_index=1).model_copy(
        update={"memory": _conversation_memory(1)}
    )
    second_snapshot = _snapshot("session-1", turn_index=2).model_copy(
        update={"memory": _conversation_memory(2)}
    )
    await store.save_turn(first_snapshot)
    await store.save_turn(second_snapshot)

    state = await store.load("session-1")

    assert state.conversation_summary is not None
    assert state.memory.latest_turn_index == 2
    assert state.conversation_summary.summary.startswith("User is comparing")
    assert state.memory.source_snapshot_ids == ["snapshot-1", "snapshot-2"]


@pytest.mark.asyncio
async def test_postgres_session_store_reloads_latest_five_result_memory_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    for turn_index in range(1, 8):
        snapshot_id = f"snapshot-{turn_index}"
        await store.save_turn(
            _snapshot("session-1", turn_index=turn_index).model_copy(
                update={
                    "snapshot_id": snapshot_id,
                    "memory": _memory(
                        result_refs=[
                            _result_memory_ref(snapshot_id, turn_index=turn_index)
                        ],
                        turn_index=turn_index,
                    ),
                }
            )
        )

    state = await store.load("session-1")

    assert [ref.turn_index for ref in state.result_memory_refs] == [3, 4, 5, 6, 7]
    assert state.result_memory_refs[-1].snapshot_id == "snapshot-7"


@pytest.mark.asyncio
async def test_postgres_session_store_reloads_latest_pending_query_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))
    first_snapshot = _snapshot("session-1", turn_index=1).model_copy(
        update={
            "memory": _memory(
                pending_query_context=PendingQueryContext(
                    operation="lookup",
                    selection=SelectionSpec(scope="state", states=["CA"]),
                    missing_fields=["metric"],
                ),
                turn_index=1,
            )
        }
    )
    second_snapshot = _snapshot("session-1", turn_index=2).model_copy(
        update={
            "memory": _memory(
                pending_query_context=PendingQueryContext(
                    operation="lookup",
                    selection=SelectionSpec(scope="state", states=["CA"]),
                    metrics=[MetricSpec(name="starting salary")],
                ),
                turn_index=2,
            )
        }
    )

    await store.save_turn(first_snapshot)
    await store.save_turn(second_snapshot)
    state = await store.load("session-1")

    assert state.pending_query_context is not None
    assert state.pending_query_context.selection == SelectionSpec(
        scope="state",
        states=["CA"],
    )
    assert state.pending_query_context.metrics == [MetricSpec(name="starting salary")]


@pytest.mark.asyncio
async def test_postgres_session_store_clears_pending_query_context_after_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))
    pending_snapshot = _snapshot("session-1", turn_index=1).model_copy(
        update={
            "memory": _memory(
                pending_query_context=PendingQueryContext(
                    operation="lookup",
                    selection=SelectionSpec(scope="state", states=["CA"]),
                    missing_fields=["metric"],
                ),
                turn_index=1,
            )
        }
    )
    executed_snapshot = _snapshot("session-1", turn_index=2).model_copy(
        update={
            "memory": _memory(query_context=_query_context(), turn_index=2),
        }
    )

    await store.save_turn(pending_snapshot)
    await store.save_turn(executed_snapshot)
    state = await store.load("session-1")

    assert state.pending_query_context is None
    assert state.query_context is not None


@pytest.mark.asyncio
async def test_postgres_session_store_persists_planner_run_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))
    snapshot = _snapshot("session-1", turn_index=1).model_copy(
        update={
            "planner_evidence": PlannerRunEvidence(
                new_messages_json="[]",
                message_count=0,
                model="test-model",
            )
        }
    )

    await store.save_turn(snapshot)
    snapshots = await store.snapshots_for_session("session-1")

    assert snapshots[0].planner_evidence is not None
    assert snapshots[0].planner_evidence.new_messages_json == "[]"
    assert connection.messages[1]["message_data"]["fresh_compass_turn_snapshot"][
        "planner_evidence"
    ]["model"] == "test-model"


@pytest.mark.asyncio
async def test_postgres_session_store_preserves_last_successful_query_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))
    first_snapshot = _snapshot("session-1", turn_index=1).model_copy(
        update={"memory": _memory(query_context=_query_context(), turn_index=1)}
    )
    clarification_snapshot = _snapshot("session-1", turn_index=2)

    await store.save_turn(first_snapshot)
    await store.save_turn(clarification_snapshot)
    state = await store.load("session-1")

    assert state.turn_count == 2
    assert state.latest_snapshot_id == "snapshot-2"
    assert state.query_context is not None
    assert state.query_context.query_plan.selection.states == ["CA"]
    assert state.query_context.row_count == 10


@pytest.mark.asyncio
async def test_postgres_session_store_returns_snapshots_in_turn_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    await store.save_turn(_snapshot("session-1", turn_index=2))
    await store.save_turn(_snapshot("session-1", turn_index=1))
    snapshots = await store.snapshots_for_session("session-1")

    assert [snapshot.turn_index for snapshot in snapshots] == [1, 2]
    assert [snapshot.snapshot_id for snapshot in snapshots] == [
        "snapshot-1",
        "snapshot-2",
    ]


@pytest.mark.asyncio
async def test_postgres_session_store_ignores_legacy_message_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    connection.messages.append(
        {
            "id": 1,
            "session_id": "session-1",
            "role": "assistant",
            "content": "legacy message",
            "message_data": {"legacy": True},
            "timestamp": datetime(2026, 5, 6, tzinfo=UTC),
        }
    )
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    snapshots = await store.snapshots_for_session("session-1")

    assert snapshots == []


@pytest.mark.asyncio
async def test_postgres_session_store_uses_compass_schema_not_policy_advisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    await store.save_turn(_snapshot("session-1"))

    sql_text = "\n".join(sql for sql, _args in connection.queries)
    assert '"compass"."chat_sessions"' in sql_text
    assert '"compass"."chat_messages"' in sql_text
    assert "policy_advisor" not in sql_text


def test_authorize_owner_rejects_authed_caller_on_anonymous_session() -> None:
    """Audit #18: a non-admin authenticated caller cannot claim an
    anonymous (NULL-owner) session."""
    from compass_backend.session.store import _authorize_owner

    # Anonymous caller on anonymous session: allowed.
    _authorize_owner(None, owner_email=None, is_admin=False)

    # Authed non-admin caller on anonymous session: denied.
    with pytest.raises(SessionAccessDenied):
        _authorize_owner(None, owner_email="user@example.org", is_admin=False)

    # Admin on anonymous session: allowed.
    _authorize_owner(None, owner_email="admin@example.org", is_admin=True)

    # Anonymous caller on owned session: denied (previously silently allowed).
    with pytest.raises(SessionAccessDenied):
        _authorize_owner("owner@example.org", owner_email=None, is_admin=False)


def _enable_logfire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force settings.logfire_enabled True so compass_span emits real spans."""

    monkeypatch.setattr(
        runtime_settings,
        "logfire_token",
        SecretStr("pylf_v1_us_real_token_for_save_turn_span_test"),
    )


@pytest.mark.asyncio
async def test_save_turn_emits_persistence_save_turn_span(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy persistence path emits the canonical save_turn span.

    The ``persistence_succeeded_for_user_turn`` span-assertion (#940) checks
    for ``compass.persistence.save_turn`` in the turn's trace.
    """

    _enable_logfire(monkeypatch)
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    await store.save_turn(_snapshot("session-1"))

    names = {s["name"] for s in capfire.exporter.exported_spans_as_dict()}
    assert "compass.persistence.save_turn" in names


@pytest.mark.asyncio
async def test_save_error_envelope_emits_persistence_save_turn_span(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error-envelope path persists the user turn, so it must emit the
    canonical ``compass.persistence.save_turn`` span (#940).

    On the orchestrator's error path the user turn is still persisted (user +
    assistant rows written), and the L1/L2 verdict pipeline binds its verdicts
    to that turn's trace. The ``persistence_succeeded_for_user_turn``
    span-assertion checks the *presence* of ``compass.persistence.save_turn``;
    without it, every error-path turn false-fails that assertion even though
    persistence succeeded — mechanically depressing the Selection score.
    """

    _enable_logfire(monkeypatch)
    connection = FakeConnection()
    store = PostgresSessionStore(_settings(), pool=FakeChatPool(connection))

    await store.save_error_envelope(
        TurnErrorEnvelope(
            session_id="session-1",
            user_message="rank the highest-paying districts in Texas",
            assistant_message="Something went wrong on our end.",
            error_type="RuntimeError",
            error_message="boom",
            stage="build_chat_response",
            trace_id="trace-error-path",
        )
    )

    names = {s["name"] for s in capfire.exporter.exported_spans_as_dict()}
    # The error-path diagnostic span is still emitted...
    assert "compass.persistence.save_error_envelope" in names
    # ...and the canonical user-turn-persisted span the assertion expects.
    assert "compass.persistence.save_turn" in names
