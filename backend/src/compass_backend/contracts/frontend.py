"""Strict contracts for the active PHP frontend compatibility surface."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationMessage(_StrictModel):
    """Persisted chat message shape used by SSE recovery and admin debug views."""

    id: int | None = None
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime | None = None
    message_json: str | None = None
    trace_id: str | None = Field(default=None, max_length=256)
    data: dict[str, Any] | None = None


class ConversationResponse(_StrictModel):
    """Conversation replay payload for the frontend resync path."""

    session_id: str = Field(min_length=1, max_length=128)
    created_at: datetime | None = None
    title: str | None = Field(default=None, max_length=500)
    messages: list[ConversationMessage] = Field(default_factory=list)


class FeedbackRequest(_StrictModel):
    """Submit or update feedback for one assistant message."""

    session_id: str = Field(min_length=1, max_length=128)
    message_id: int = Field(gt=0)
    rating: Literal[-1, 1]
    feedback_tags: list[str] = Field(default_factory=list, max_length=20)
    feedback_text: str | None = Field(default=None, max_length=2000)
    trace_id: str | None = Field(default=None, max_length=256)


class FeedbackDeleteRequest(_StrictModel):
    """Delete feedback for one assistant message."""

    session_id: str = Field(min_length=1, max_length=128)
    message_id: int = Field(gt=0)


class FeedbackResponse(_StrictModel):
    """Persisted feedback row."""

    id: int
    session_id: str
    message_id: int
    rating: Literal[-1, 1]
    feedback_tags: list[str] = Field(default_factory=list)
    feedback_text: str | None = None
    trace_id: str | None = None
    # user_email is intentionally NOT exposed here (#1121): it's submitter PII
    # kept internal for upsert dedup only. Returning it leaked emails to
    # unauthenticated callers when auth is off (the staging default), so the
    # READ projections (list_feedback SELECT, upsert_feedback RETURNING) must
    # not select it either — this contract is strict (extra="forbid").
    created_at: datetime | None = None
    updated_at: datetime | None = None


class FeedbackListResponse(_StrictModel):
    """All feedback rows for a conversation."""

    session_id: str
    feedback: list[FeedbackResponse] = Field(default_factory=list)


class FeedbackDeleteResponse(_StrictModel):
    """Feedback delete result."""

    deleted: bool


class ConversationExportTable(_StrictModel):
    """One table artifact included in a conversation export bundle."""

    label: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    columns: list[str] | None = None
    rows: list[dict[str, Any] | list[Any]] = Field(default_factory=list)
    # Pruned public CSV projection from _public_csv_export (#1611). When both
    # are present the ZIP writer builds this table's CSV from them; otherwise it
    # falls back to columns/rows (older payloads, charts).
    public_columns: list[str] | None = None
    public_rows: list[dict[str, Any]] | None = None
    message_id: int | None = None
    export_id: str | None = Field(default=None, max_length=80)


class ConversationExportChart(_StrictModel):
    """One chart artifact included in a conversation export bundle."""

    label: str | None = Field(default=None, max_length=200)
    data: dict[str, Any] = Field(default_factory=dict)


class ConversationExportTurn(_StrictModel):
    """Paired user/assistant turn as built by the active frontend."""

    user_text: str = Field(default="", max_length=20000)
    assistant_text: str = Field(default="", max_length=200000)
    tables: list[ConversationExportTable] = Field(default_factory=list)
    charts: list[ConversationExportChart] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)


class ConversationExportMessage(_StrictModel):
    """Message-based export input accepted for tests and future clients."""

    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=200000)
    tables: list[ConversationExportTable] = Field(default_factory=list)
    charts: list[ConversationExportChart] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)


class ConversationExportRequest(_StrictModel):
    """Conversation ZIP export request.

    The active PHP frontend posts `turns`; tests and future clients may post
    `messages`. Both are explicitly modeled so no unrecognized fields are
    accepted.
    """

    session_id: str = Field(min_length=1, max_length=128)
    exported_at: datetime | None = None
    conversation_title: str | None = Field(default=None, max_length=500)
    turns: list[ConversationExportTurn] = Field(default_factory=list)
    messages: list[ConversationExportMessage] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_export_content(self) -> "ConversationExportRequest":
        if not self.turns and not self.messages:
            raise ValueError("Export requires turns or messages")
        return self


class UsageTotals(_StrictModel):
    """Aggregated token and cost fields consumed by debug.js."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0
    cost_usd: float = 0.0


class DebugBackfillResponse(_StrictModel):
    """Admin-only debug payload for historical cost and trace backfill."""

    session_id: str
    conversation: ConversationResponse | None = None
    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    usage: list[dict[str, Any]] = Field(default_factory=list)
    usage_totals: UsageTotals = Field(default_factory=UsageTotals)
    linked_scenarios: list[dict[str, Any]] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
