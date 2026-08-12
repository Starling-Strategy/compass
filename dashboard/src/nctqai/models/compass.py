"""Response models for the Compass API.

These mirror the JSON shapes returned by the Compass API endpoints,
allowing the dashboard to deserialize responses with type safety.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ── Conversations ──────────────────────────────────────────────────────────


class ConversationSummary(BaseModel):
    """A conversation as returned by GET /api/v1/conversations."""

    session_id: str
    created_at: datetime | None = None
    message_count: int = 0
    first_user_message: str | None = Field(default=None, alias="first_message")
    apparent_role: str | None = None
    last_verdict_approved: bool | None = None
    quality_avg: float | None = None
    flags_count: int | None = None
    intent: str | None = None
    district_id: int | None = None
    district_name: str | None = None
    state: str | None = None
    district_count: int = 0
    thumbs_up_count: int = 0
    thumbs_down_count: int = 0
    scenario_id: int | None = None
    scenario_title: str | None = None

    model_config = {"populate_by_name": True}


class ChatMessage(BaseModel):
    """A single message in a conversation."""

    session_id: str | None = None
    id: int | None = None
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime | None = None
    trace_id: str | None = None


class MessageFeedback(BaseModel):
    """A user's thumbs-up/thumbs-down rating for a message."""

    id: int | None = None
    session_id: str
    message_id: int
    rating: int  # -1 or 1
    feedback_tags: list[str] = Field(default_factory=list)
    feedback_text: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ConversationDetail(BaseModel):
    """Full conversation with messages."""

    session_id: str
    created_at: datetime | None = None
    messages: list[ChatMessage] = Field(default_factory=list)


# ── Scenarios ─────────────────────────────────────────────────────────────


class Scenario(BaseModel):
    """A runnable B-spine case with parent scenario context."""

    id: int
    case_id: int | None = None
    case_code: str | None = None
    scenario_id: int | None = None
    scenario_code: str | None = None
    scenario_title: str = ""
    initial_user_message: str = ""
    input_steps: list[dict[str, Any]] = Field(default_factory=list)
    what_we_are_testing: str = ""
    expected_behaviour: str = ""
    feature: str | None = None
    type: str = "golden"
    sort_order: int = 0
    active: bool = True
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    example_conversations: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime | None = None
