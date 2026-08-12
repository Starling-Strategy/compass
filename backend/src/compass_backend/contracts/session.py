"""Session and turn snapshot contracts for the fresh Compass API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai.messages import ModelMessage

from compass_backend.artifacts import ResultSet
from compass_backend.catalog import CatalogResolutionReport

from .planning import (
    ClarificationOption,
    PendingQueryContext,
    PlannerTurn,
    QueryPlan,
)
from .validation import ValidationAuthority

CONTEXT_RESULT_REF_ITEM_LIMIT = 200
RESULT_MEMORY_REF_LIMIT = 5
SUMMARY_TEXT_MAX_LENGTH = 1200
MEMORY_TEXT_MAX_LENGTH = 500


def utc_now() -> datetime:
    """Return an aware UTC timestamp for session contracts."""

    return datetime.now(UTC)


def new_id() -> str:
    """Return a stable string ID for sessions and snapshots."""

    return str(uuid4())


class QueryContext(BaseModel):
    """Internal referent memory from the latest successful data turn."""

    model_config = ConfigDict(extra="forbid")

    query_plan: QueryPlan
    authority: ValidationAuthority | None = None
    result_type: str | None = None
    order_statement: str | None = None
    row_count: int = Field(default=0, ge=0)
    displayed_row_count: int | None = Field(default=None, ge=0)
    display_limit: int | None = Field(default=None, ge=1)
    row_display: str | None = None
    data_limit_count: int | None = Field(default=None, ge=1)
    data_limit_kind: str | None = None
    data_limit_source: str | None = None
    display_limit_source: str | None = None
    result_districts: list["QueryContextDistrictRef"] = Field(default_factory=list)
    # The selected POPULATION the prior count was taken over — covered districts
    # WITH A CURRENT VALUE for the metric (the denominator's members), distinct
    # from result_districts which carries the matching subset. Populated only by
    # count results (from ThresholdCountRow.denominator_district_ids); empty for
    # rank/lookup priors. Lets a "who are the <denominator>?" follow-up present
    # the population without re-querying (#393).
    result_population_districts: list["QueryContextDistrictRef"] = Field(
        default_factory=list
    )
    # The prior count's denominator as a reliable integer (the population SIZE),
    # distinct from result_population_districts (the member LIST). #1658: a
    # serialization round-trip intermittently drops the member list (~12%) while
    # the scalar survives, so a "who are the <denominator>?" follow-up grounds on
    # this integer to fire reliably; the members are re-derived at follow-up time.
    count_denominator: int | None = Field(default=None, ge=0)
    result_metrics: list["QueryContextMetricRef"] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class QueryContextDistrictRef(BaseModel):
    """Small district reference from the prior validated result rows."""

    model_config = ConfigDict(extra="forbid")

    district_id: int
    district_name: str = Field(min_length=1)
    state: str | None = None


class QueryContextMetricRef(BaseModel):
    """Small metric reference from the prior validated result rows."""

    model_config = ConfigDict(extra="forbid")

    metric_id: int
    metric_name: str = Field(min_length=1)


class PolicyGuidanceExemplarRef(BaseModel):
    """Rendered policy-guidance exemplar reference for follow-up grounding."""

    model_config = ConfigDict(extra="forbid")

    exemplar_id: str = Field(min_length=1)
    topic_id: str = Field(min_length=1)
    district: str = Field(min_length=1)
    district_id: int | None = None
    subtopic: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    citation_status: str = Field(min_length=1)
    citation_title: str | None = None


class PolicyGuidanceContext(BaseModel):
    """Internal referent memory from the latest rendered guidance response."""

    model_config = ConfigDict(extra="forbid")

    topic_ids: list[str] = Field(min_length=1, max_length=4)
    layers: list[str] = Field(min_length=1, max_length=3)
    focus_terms: list[str] = Field(default_factory=list, max_length=6)
    primary_topic_id: str | None = None
    intent_summary: str = Field(min_length=1, max_length=240)
    exemplars: list[PolicyGuidanceExemplarRef] = Field(
        default_factory=list,
        max_length=20,
    )
    turn_index: int = Field(ge=1)
    snapshot_id: str = Field(min_length=1)
    user_message: str = Field(min_length=1, max_length=500)
    created_at: datetime = Field(default_factory=utc_now)


class ResultMemoryRef(BaseModel):
    """Compact pointer to a prior validated result artifact."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1)
    turn_index: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=MEMORY_TEXT_MAX_LENGTH)
    result_type: str | None = None
    row_count: int = Field(default=0, ge=0)
    displayed_row_count: int | None = Field(default=None, ge=0)
    display_limit: int | None = Field(default=None, ge=1)
    metrics: list[QueryContextMetricRef] = Field(
        default_factory=list,
        max_length=CONTEXT_RESULT_REF_ITEM_LIMIT,
    )
    districts: list[QueryContextDistrictRef] = Field(
        default_factory=list,
        max_length=CONTEXT_RESULT_REF_ITEM_LIMIT,
    )
    has_chart: bool = False
    has_csv_export: bool = False
    digest: str = Field(min_length=1, max_length=MEMORY_TEXT_MAX_LENGTH)


class ConversationSummary(BaseModel):
    """Non-authoritative prose and preference summary for planner interpretation."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(default=None, max_length=SUMMARY_TEXT_MAX_LENGTH)
    active_user_goal: str | None = Field(default=None, max_length=MEMORY_TEXT_MAX_LENGTH)
    open_questions: list[str] = Field(default_factory=list, max_length=10)
    accepted_choices: list[str] = Field(default_factory=list, max_length=12)
    rejected_choices: list[str] = Field(default_factory=list, max_length=12)
    user_preferences: list[str] = Field(default_factory=list, max_length=12)
    last_clarification_options: list[str] = Field(default_factory=list, max_length=12)


class ConversationMemory(BaseModel):
    """Single internal cross-turn memory envelope for a Compass session.

    W0.5 (#832): the planner-visible recent transcript was retired in favor of
    pydantic-ai canonical ``message_history`` loaded on-demand from each
    snapshot's ``planner_evidence.new_messages_json``. The only remaining
    transcript-derived metadata that lives in memory is ``recent_routes`` — a
    lightweight list of prior turn routes used by the planner-instruction
    snippet selector to gate one snippet (``required_prior_route``).
    """

    model_config = ConfigDict(extra="forbid")

    summary: ConversationSummary | None = None
    latest_query_context: QueryContext | None = None
    latest_policy_guidance_context: PolicyGuidanceContext | None = None
    pending_query_context: PendingQueryContext | None = None
    # #1348: the structured clarification options most recently offered to the
    # user, persisted so the next turn can validate a clicked option's machine
    # handle against what was actually offered (anti-stale / anti-forgery) and
    # resume deterministically. Default-empty; mirrors the prose
    # ConversationSummary.last_clarification_options.
    pending_clarification_options: list[ClarificationOption] = Field(
        default_factory=list, max_length=12
    )
    result_refs: list[ResultMemoryRef] = Field(
        default_factory=list, max_length=RESULT_MEMORY_REF_LIMIT
    )
    recent_routes: list[str] = Field(default_factory=list, max_length=6)
    latest_turn_index: int = Field(default=0, ge=0)
    source_snapshot_ids: list[str] = Field(default_factory=list, max_length=10)
    # W0.5 (#832): transient cache of canonical pydantic-ai message_history,
    # rebuilt each session load from prior snapshots'
    # planner_evidence.new_messages_json. Excluded from serialization — the
    # source of truth lives in compass.chat_messages.message_data; this is the
    # decoded form passed to the next planner.run(message_history=...).
    message_history: list[ModelMessage] = Field(default_factory=list, exclude=True)


class PlannerGuidanceEvidence(BaseModel):
    """Selected planner instruction snippet evidence; never execution authority."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)
    matched_phrase: str | None = None


class PlannerRunEvidence(BaseModel):
    """Serialized Pydantic AI planner messages for debugging and replay."""

    model_config = ConfigDict(extra="forbid")

    new_messages_json: str | None = None
    message_count: int = Field(default=0, ge=0)
    model: str | None = None
    trace_id: str | None = None
    planner_guidance: list[PlannerGuidanceEvidence] = Field(default_factory=list)
    thinking_enabled: bool = False
    thinking_effort: str | None = None
    thinking_policy: str = "off"
    thinking_policy_reason: str | None = None
    thinking_provider_error: str | None = None
    planning_brief: dict[str, object] | None = None
    planning_recognition: dict[str, object] | None = None
    planner_duration_ms: float | None = Field(default=None, ge=0)
    usage_requests: int | None = Field(default=None, ge=0)
    usage_input_tokens: int | None = Field(default=None, ge=0)
    usage_output_tokens: int | None = Field(default=None, ge=0)
    usage_total_tokens: int | None = Field(default=None, ge=0)
    usage_cache_read_tokens: int | None = Field(default=None, ge=0)
    usage_cache_write_tokens: int | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_preplanner_keys(cls, data: Any) -> Any:
        """Strip retired preplanner_* fields from older persisted snapshots.

        Move 4 of the #959 plan removed the preplanner stage. Snapshots
        persisted before this change carry ``preplanner_enabled``,
        ``preplanner_model``, etc. in `chat_messages.message_data`. Drop
        those keys at load time so ``extra="forbid"`` stays strict for
        new fields without breaking historical reads.
        """
        if isinstance(data, dict):
            stripped = {k: v for k, v in data.items() if not k.startswith("preplanner_")}
            if len(stripped) != len(data):
                return stripped
        return data


class SessionState(BaseModel):
    """Small cross-turn state for a Compass session.

    This is intentionally narrower than the legacy CaseContext. It tracks the
    session's identity and progression so future layers can attach richer state
    without coupling transport directly to database rows or agent internals.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(default_factory=new_id, min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    turn_count: int = Field(default=0, ge=0)
    latest_snapshot_id: str | None = None
    memory: ConversationMemory = Field(default_factory=ConversationMemory, exclude=True)

    @property
    def query_context(self) -> QueryContext | None:
        """Compatibility accessor for the latest validated query context."""

        return self.memory.latest_query_context

    @property
    def pending_query_context(self) -> PendingQueryContext | None:
        """Compatibility accessor for the current pending clarification context."""

        return self.memory.pending_query_context

    @property
    def conversation_summary(self) -> ConversationSummary | None:
        """Return the non-authoritative prose summary subpart."""

        return self.memory.summary

    @property
    def result_memory_refs(self) -> list[ResultMemoryRef]:
        """Compatibility accessor for compact prior-result references."""

        return self.memory.result_refs

    @property
    def recent_routes(self) -> list[str]:
        """Return prior turn routes (planner-instruction snippet gating)."""

        return self.memory.recent_routes


class TurnSnapshot(BaseModel):
    """Debuggable record of one user turn through the current pipeline."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(default_factory=new_id, min_length=1)
    session_id: str = Field(min_length=1)
    turn_index: int = Field(ge=1)
    user_message: str = Field(min_length=1)
    assistant_message: str = Field(min_length=1)
    planner_turn: PlannerTurn
    memory: ConversationMemory | None = None
    result: ResultSet | None = None
    planner_evidence: PlannerRunEvidence | None = None
    resolution_report: CatalogResolutionReport | None = None
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def fold_legacy_memory_fields(cls, data: object) -> object:
        """Convert pre-unification snapshot fields into the memory envelope."""

        if not isinstance(data, dict):
            return data

        existing_memory = data.get("memory")
        legacy_summary = data.pop("conversation_memory", None)
        legacy_query = data.pop("query_context", None)
        legacy_pending = data.pop("pending_query_context", None)
        legacy_refs = data.pop("result_memory_refs", None)
        # W0.5 (#832): recent_transcript was retired; drop silently from any
        # legacy snapshot payload so the canonical message_history path is the
        # only memory of prior turn prose.
        data.pop("recent_transcript", None)
        if not any(
            value is not None
            for value in (
                legacy_summary,
                legacy_query,
                legacy_pending,
                legacy_refs,
            )
        ):
            return data

        snapshot_id = data.get("snapshot_id")
        source_ids = [snapshot_id] if isinstance(snapshot_id, str) else []
        if isinstance(existing_memory, ConversationMemory):
            memory = existing_memory.model_dump(mode="python", exclude_none=True)
        elif isinstance(existing_memory, dict):
            memory = dict(existing_memory)
        else:
            memory = {}
        # W0.5: strip any persisted recent_transcript from legacy memory dicts.
        memory.pop("recent_transcript", None)
        if legacy_summary is not None:
            if isinstance(legacy_summary, dict):
                latest_turn_index = legacy_summary.pop("latest_turn_index", None)
                summary_source_ids = legacy_summary.pop("source_snapshot_ids", None)
                if latest_turn_index is not None:
                    memory["latest_turn_index"] = latest_turn_index
                if summary_source_ids is not None:
                    memory["source_snapshot_ids"] = summary_source_ids
            memory["summary"] = legacy_summary
        if legacy_query is not None:
            memory["latest_query_context"] = legacy_query
        if legacy_pending is not None:
            memory["pending_query_context"] = legacy_pending
        if legacy_refs is not None:
            memory["result_refs"] = legacy_refs
        memory.setdefault("latest_turn_index", data.get("turn_index", 0))
        memory.setdefault("source_snapshot_ids", source_ids)
        data["memory"] = memory
        return data

    @property
    def query_context(self) -> QueryContext | None:
        """Compatibility accessor for the latest validated query context."""

        return self.memory.latest_query_context if self.memory is not None else None

    @property
    def pending_query_context(self) -> PendingQueryContext | None:
        """Compatibility accessor for the current pending clarification context."""

        return self.memory.pending_query_context if self.memory is not None else None

    @property
    def result_memory_refs(self) -> list[ResultMemoryRef]:
        """Compatibility accessor for compact prior-result references."""

        return self.memory.result_refs if self.memory is not None else []

    @property
    def result_refs(self) -> list[ResultMemoryRef]:
        """Return compact prior-result references from the memory envelope."""

        return self.result_memory_refs
