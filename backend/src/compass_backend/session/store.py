"""Session persistence boundary for the fresh Compass API."""

from __future__ import annotations

from asyncio import Lock
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from compass_backend.config import Settings
from compass_backend.contracts import ConversationMemory, SessionState, TurnSnapshot

from .memory import conversation_memory_from_snapshots

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg

    from compass_backend.db._pool import ChatPoolHolder


@dataclass(frozen=True)
class SavedMessageRef:
    """Reference to one persisted chat message."""

    message_id: int
    role: Literal["user", "assistant"]


@dataclass(frozen=True)
class TurnErrorEnvelope:
    """Failure record for a turn that exited the orchestrator unhandled.

    Persisted as a regular `assistant` `chat_messages` row so the L1/L2
    verdict pipeline can still bind the failure to a session + message +
    trace tuple. Without this envelope, an unhandled exception in
    `build_chat_response` leaves zero rows for the failed turn and the
    sweep sees only `staging_http_500` with no diagnostic context.

    The envelope is intentionally narrow — just enough for an operator
    to find the trace in Logfire and the failing input in compass.chat_messages.
    """

    session_id: str
    user_message: str
    assistant_message: str
    error_type: str
    error_message: str
    stage: str
    trace_id: str | None


@dataclass(frozen=True)
class SavedTurnResult:
    """Result of persisting one Compass turn."""

    session: SessionState
    messages: tuple[SavedMessageRef, ...] = ()

    @property
    def message_ids(self) -> list[int]:
        """All inserted message IDs in insertion order."""

        return [message.message_id for message in self.messages]

    @property
    def assistant_message_ids(self) -> list[int]:
        """Inserted assistant message IDs for frontend feedback wiring."""

        return [
            message.message_id
            for message in self.messages
            if message.role == "assistant"
        ]

    def __getattr__(self, name: str) -> object:
        return getattr(self.session, name)


class SessionAccessDenied(Exception):
    """Raised when a caller cannot continue a session."""


class SessionStore(Protocol):
    """Persistence boundary for session state and turn snapshots."""

    async def load(
        self,
        session_id: str | None = None,
        *,
        owner_email: str | None = None,
        is_admin: bool = False,
        create_if_missing: bool = True,
        visitor_id: str | None = None,
    ) -> SessionState:
        """Load an existing session or create one when none exists."""

    async def save_turn(self, snapshot: TurnSnapshot) -> SavedTurnResult:
        """Persist one turn snapshot and return the updated session state."""

    async def save_error_envelope(
        self, envelope: TurnErrorEnvelope
    ) -> SavedTurnResult:
        """Persist a failed-turn envelope (user + error-assistant rows).

        Best-effort: implementations MAY raise, but the caller is
        expected to catch and continue rather than double-fault. Returns
        the same `SavedTurnResult` shape `save_turn` returns so the
        message_id / trace_id flow stays uniform downstream.
        """

    async def snapshots_for_session(self, session_id: str) -> list[TurnSnapshot]:
        """Return snapshots for a session in turn order."""


class InMemorySessionStore:
    """In-memory `SessionStore` used until a DB-backed store is admitted."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[str, SessionState] = {}
        self._snapshots: dict[str, list[TurnSnapshot]] = {}
        self._owners: dict[str, str | None] = {}
        self._visitors: dict[str, str | None] = {}
        self._next_message_id = 1

    async def load(
        self,
        session_id: str | None = None,
        *,
        owner_email: str | None = None,
        is_admin: bool = False,
        create_if_missing: bool = True,
        visitor_id: str | None = None,
    ) -> SessionState:
        """Load an existing session or create a new state.

        ``visitor_id`` mirrors ``PostgresSessionStore.load`` (which records the
        analytics visitor on the session row). The in-memory store keeps it for
        signature parity with the ``SessionStore`` protocol; it drives no
        analytics pipeline of its own.
        """

        async with self._lock:
            if session_id is not None and session_id in self._sessions:
                _authorize_owner(
                    self._owners.get(session_id),
                    owner_email=owner_email,
                    is_admin=is_admin,
                )
                return self._sessions[session_id].model_copy(deep=True)

            if session_id is not None and not create_if_missing:
                raise SessionAccessDenied()

            state = SessionState(session_id=session_id) if session_id else SessionState()
            self._sessions[state.session_id] = state
            self._snapshots.setdefault(state.session_id, [])
            self._owners[state.session_id] = owner_email
            self._visitors[state.session_id] = visitor_id
            return state.model_copy(deep=True)

    async def save_turn(self, snapshot: TurnSnapshot) -> SavedTurnResult:
        """Save a snapshot and advance the session's lightweight state."""

        async with self._lock:
            current = self._sessions.get(snapshot.session_id)
            if current is None:
                current = SessionState(session_id=snapshot.session_id)

            updated = current.model_copy(
                update={
                    "updated_at": snapshot.created_at,
                    "turn_count": max(current.turn_count, snapshot.turn_index),
                    "latest_snapshot_id": snapshot.snapshot_id,
                    "memory": conversation_memory_from_snapshots(
                        [*self._snapshots.get(snapshot.session_id, []), snapshot]
                    )
                    or ConversationMemory(),
                }
            )
            self._sessions[snapshot.session_id] = updated
            self._snapshots.setdefault(snapshot.session_id, []).append(snapshot)
            self._owners.setdefault(snapshot.session_id, None)
            user_ref = SavedMessageRef(self._next_message_id, "user")
            assistant_ref = SavedMessageRef(self._next_message_id + 1, "assistant")
            self._next_message_id += 2
            return SavedTurnResult(
                session=updated.model_copy(deep=True),
                messages=(user_ref, assistant_ref),
            )

    async def save_error_envelope(
        self, envelope: TurnErrorEnvelope
    ) -> SavedTurnResult:
        """Record a failed turn without writing a TurnSnapshot."""

        async with self._lock:
            current = self._sessions.get(envelope.session_id)
            if current is None:
                current = SessionState(session_id=envelope.session_id)
                self._sessions[envelope.session_id] = current
            self._owners.setdefault(envelope.session_id, None)
            user_ref = SavedMessageRef(self._next_message_id, "user")
            assistant_ref = SavedMessageRef(
                self._next_message_id + 1, "assistant"
            )
            self._next_message_id += 2
            return SavedTurnResult(
                session=current.model_copy(deep=True),
                messages=(user_ref, assistant_ref),
            )

    async def snapshots_for_session(self, session_id: str) -> list[TurnSnapshot]:
        """Return stored snapshots for a session."""

        async with self._lock:
            return [
                snapshot.model_copy(deep=True)
                for snapshot in self._snapshots.get(session_id, [])
            ]


def session_store_from_settings(
    source: Settings,
    *,
    pool: "asyncpg.Pool | ChatPoolHolder | None" = None,
) -> SessionStore:
    """Build the configured fresh session store.

    ``pool`` is the shared chat pool. When ``None`` (test path), an empty
    ``ChatPoolHolder()`` is used so the store constructs cleanly; the
    holder must be populated before any DB-touching method is called.
    """

    if source.session_store_backend == "memory":
        return InMemorySessionStore()

    from .postgres import PostgresSessionStore
    from compass_backend.db._pool import ChatPoolHolder

    return PostgresSessionStore(source, pool=pool if pool is not None else ChatPoolHolder())


def _authorize_owner(
    existing_owner: str | None,
    *,
    owner_email: str | None,
    is_admin: bool,
) -> None:
    """Raise when the current caller is not authorized for the session.

    Audit #18: anonymous sessions (``existing_owner is None``) are
    reachable only by anonymous callers; a non-admin authed caller cannot
    claim them, and an anonymous caller cannot read owned sessions.
    """

    if is_admin:
        return
    if existing_owner is None:
        if owner_email is None:
            return
        raise SessionAccessDenied()
    if existing_owner != owner_email:
        raise SessionAccessDenied()
