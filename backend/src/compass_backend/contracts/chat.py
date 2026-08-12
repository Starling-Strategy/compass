"""Chat request and response contracts for the fresh Compass API."""

from pydantic import BaseModel, ConfigDict, Field

from compass_backend.artifacts import ResultSet

from .planning import PlannerTurn
from .rendering import ResponseManifest
from .session import SessionState
from .validation import ValidationReport


class ChatRequest(BaseModel):
    """Chat request for the fresh Compass API."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    # #1348: when the user clicks a structured clarification option, the
    # frontend posts the option's machine handle here (for districts, the
    # district_id as a string). The orchestration validates it against the
    # options that were actually offered and resumes the pending query
    # deterministically — no prose re-parse. Optional → existing callers and
    # the normal free-text path (selected_option=None) are unaffected.
    selected_option: str | None = Field(default=None, min_length=1)
    # WS-5 / G5: a pseudonymous, PII-free visitor id minted by the embedded chat
    # frontend (parent-issued first-party UUID from nctq.org, or an iframe-local
    # fallback) so the dashboard can count repeat users — chat is otherwise
    # anonymous. Persisted on the session row (see PostgresSessionStore). Optional
    # → existing callers and pa-eval (which never sends it) are unaffected.
    visitor_id: str | None = Field(default=None, min_length=1, max_length=128)


class ChatResponse(BaseModel):
    """Chat response for the fresh Compass API MVPs.

    `message` keeps the HTTP endpoint usable while `turn` exposes the typed
    planner contract that future execution, validation, and rendering layers
    will consume. `session` and `snapshot_id` expose the persistence boundary
    that makes every turn debuggable. `result` carries deterministic execution
    artifacts when the execute route supports the requested query shape.

    `extra="forbid"` so any field added to the backend response shape that
    pa-eval hasn't been taught about fails the round-trip parse loudly —
    silent field drift between backend and runner is the symptom this
    contract is meant to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    message: str
    turn: PlannerTurn
    session: SessionState
    snapshot_id: str
    result: ResultSet | None = None
    validation: ValidationReport | None = None
    manifest: ResponseManifest | None = None
    trace_id: str | None = None
    logfire_url: str | None = None
    message_ids: list[int] = Field(default_factory=list)
