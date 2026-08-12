"""Per-turn orchestration spine for chat-turn processing.

Houses the mutable `_TurnContext` dataclass threaded through every pipeline
stage, plus the pure message/text helpers that derive a response string from a
planner turn or clarification. Extracted verbatim from `orchestration/chat.py`
(#1130 decomposition) so the stage modules can import `_TurnContext` without
cycling back through `chat`. Behaviour-preserving move — no logic changes.
"""

from dataclasses import dataclass, field

from compass_backend.artifacts import ResultSet
from compass_backend.catalog import (
    CatalogResolutionReport,
    RecallReport,
)
from compass_backend.config import Settings
from compass_backend.contracts import (
    ChatRequest,
    ClarificationRequest,
    ConversationMemory,
    PendingQueryContext,
    QueryContext,
)
from compass_backend.contracts.auth import AuthenticatedUser
from compass_backend.contracts.planning import PlannerTurn
from compass_backend.contracts.rendering import ResponseManifest
from compass_backend.contracts.session import SessionState
from compass_backend.contracts.validation import ValidationReport
from compass_backend.reference import normalize_whitespace_casefold
from compass_backend.answer_layer.nctq_context import NctqContextRepoProtocol
from compass_backend.execution import ExecutionOutcome
from compass_backend.planning import PlannerRun
from compass_backend.progress import NullProgressSink, ProgressSink
from compass_backend.rendering.rescue_clarification import (
    RescueClarificationRef,
    rescue_clarification_question_for,
)

@dataclass(slots=True, kw_only=True)
class _TurnContext:
    """Mutable per-turn state accumulated through the pipeline stages.

    Each stage helper reads the fields it needs and populates the fields it
    produces. Defaults to `None`/empty mirror the inline initialization that
    `build_chat_response` used before this refactor; nothing is auto-derived
    here so that data flow remains explicit at each stage call site.
    """

    # Inputs (set once at the top of build_chat_response).
    request: ChatRequest
    app_settings: Settings
    auth_user: AuthenticatedUser | None
    trace_id: str | None
    progress: ProgressSink = field(default_factory=NullProgressSink)
    # Optional repos injected at construction time.
    nctq_context_repo: NctqContextRepoProtocol | None = None

    # Populated by _load_session_for_turn.
    session_state: SessionState | None = None
    turn_index: int = 0

    # Populated by _run_planner_for_turn (turn mutates across later stages too).
    planner_run: PlannerRun | None = None
    turn: PlannerTurn | None = None
    recall_report: RecallReport | None = None

    # Populated by _promote_pending_context_for_turn / later clarify branches.
    next_pending_context: PendingQueryContext | None = None

    # Populated by execute/render/validation stages.
    result: ResultSet | None = None
    validation: ValidationReport | None = None
    manifest: ResponseManifest | None = None
    query_context: QueryContext | None = None
    resolution_report: CatalogResolutionReport | None = None

    # Captured from the executor's raw outcome before any turn-swap, so
    # the turn-failure taxonomy (Move 0 of #959 plan) can distinguish
    # execution_clarification from planner-route clarifies and detect
    # zero-row success vs. refusal. Telemetry-only; readers should not
    # branch on this for behavior.
    executed_outcome: ExecutionOutcome | None = None

    # Assistant message body (populated by _execute_and_render_turn).
    message: str = ""

    # Populated by _persist_turn_snapshot.
    saved_message_ids: list[str] = field(default_factory=list)
    saved_assistant_message_ids: list[str] = field(default_factory=list)
    saved_session: object | None = None
    snapshot_id: str = ""

    @property
    def session_memory(self) -> ConversationMemory:
        """Conversation memory from the loaded session state."""

        assert self.session_state is not None, "session_state must be loaded first"
        return self.session_state.memory


def _message_for_turn(turn: PlannerTurn) -> str:
    """Return a conservative response message for the current MVP layer."""

    if turn.route == "direct" and turn.direct_response is not None:
        return turn.direct_response.message
    if turn.route == "clarify" and turn.clarification is not None:
        return _message_for_clarification(turn.clarification)
    return rescue_clarification_question_for(
        RescueClarificationRef(
            code="fallback_no_anchor",
            district_count=0,
            metric_name=None,
        )
    )


def _message_for_clarification(clarification: ClarificationRequest) -> str:
    """Render clarification candidates when governed resolution has options.

    The stylist-composed (or f-string-fallback) question already ends with a
    question mark and invites a response. The legacy trailing
    "Reply with one of these options and I'll continue." boilerplate is
    redundant noise on top of the candidate bullets and the question itself,
    so we drop it.
    """

    if not clarification.candidates:
        return clarification.question
    options = "\n".join(f"- {candidate}" for candidate in clarification.candidates)
    return f"{clarification.question}\n\n{options}"

def _normalize_user_text(message: str | None) -> str:
    return normalize_whitespace_casefold(message or "")


