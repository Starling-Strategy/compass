"""Chat-turn orchestration — the service layer between transport and domain.

Owns `build_chat_response()`, which composes: auth-scoped session load,
planner run + clarification handling, plan execution, validation, response
rendering, and turn-snapshot persistence.

Lives below `api/` and above the downstream layers (planning, execution,
quality, rendering, session). The route handlers in `api/chat.py` call
this module and stay focused on transport concerns (SSE wrapping, JSON
shim, auth dispatch, verdict fire-and-forget).

Split from the previously-monolithic `api/chat.py` (930 LOC) so the route
file becomes a thin ~200-LOC transport layer.

`build_chat_response` reads as a table of contents over named stage
helpers. State threads through stages via a mutable `_TurnContext`
dataclass; each stage helper owns one `compass_span` boundary and inherits
the outer `compass_turn_span` context implicitly through the async task.
"""

from collections.abc import Sequence
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import HTTPException, status
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.toolsets import AbstractToolset

from compass_backend.agents import agent_model_settings
from compass_backend.progress import (
    NullProgressSink,
    ProgressSink,
    make_planner_event_stream_handler,
)
from compass_backend.answer_layer.briefs import canonical_caveat_fragments
from compass_backend.answer_layer.nctq_context import (
    NctqContextRepoProtocol,
    resolve_nctq_context,
    resolve_nctq_context_for_policy_guidance,
)
from compass_backend.answer_layer.service import (
    allowed_answer_layer_result_types,
    improve_answer,
)
from compass_backend.artifacts import ResultSet
from compass_backend.contracts.auth import AuthenticatedUser
from compass_backend.catalog import (
    CatalogRecallService,
    CatalogResolutionReport,
    RecallReport,
)
from compass_backend.config import Settings
from compass_backend.contracts import (
    ChatRequest,
    ChatResponse,
    ClarificationRequest,
    PendingQueryContext,
    PlannerRunEvidence,
    PolicyGuidanceExemplarRef,
    PolicyGuidancePlan,
    QueryContextDistrictRef,
    TurnSnapshot,
    pending_context_from_plan,
)
from compass_backend.contracts.planning import (
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.contracts.answer_layer import AnswerLayerReport
from compass_backend.contracts.session import SessionState
from compass_backend.execution import (
    ExecutionClarification,
    ExecutionSuccess,
    QueryExecutor,
)
from compass_backend.observability import (
    compass_span,
    compass_turn_span,
    flush_on_error,
    get_current_trace_id,
    logfire_url_for_trace,
    sanitize_logfire_text,
    set_span_attributes,
    trace_id_from_span,
)
from compass_backend.execution.shape_check import (
    unsupported_shape_hint,
)
from compass_backend.planning import (
    PlannerAgent,
    PlannerDeps,
    PlannerRun,
    pending_context_after_turn,
    pending_context_from_execution_clarification,
    promote_pending_context_to_plan,
    run_planner,
)
from compass_backend.planning.catalog_pipeline import CatalogPlanPipeline
from compass_backend.policy_guidance.library import get_library
from compass_backend.quality import validate_result
from compass_backend.rendering import (
    attach_adjacent_metrics_manifest_metadata,
    render_response,
)
from compass_backend.rendering.chart_visibility import (
    ChartVisibilityDecision,
    resolve_chart_visibility,
)
from compass_backend.rendering.rescue_clarification import (
    RescueClarificationCode,
    RescueClarificationRef,
    rescue_clarification_question_for,
)
from compass_backend.rendering.policy_guidance import (
    POLICY_GUIDANCE_VALIDATION_FAILED_BODY,
    render_policy_guidance,
)
from compass_backend.rendering.publication import (
    PUBLICATION_VALIDATION_FAILED_BODY,
    render_publication,
)
from compass_backend.session import (
    SessionAccessDenied,
    SessionStore,
    TurnErrorEnvelope,
)
from compass_backend.session.memory import (
    append_result_memory_ref,
    build_policy_guidance_context,
    build_result_memory_ref,
    conversation_memory_after_turn,
)
from compass_backend.orchestration.result_diagnostics import (
    _attach_recall_manifest_metadata,
    _attach_resolution_manifest_metadata,
    _merge_recall_reports,
    _query_context_for_result,
    _result_rows,
    _result_trace_attributes,
    _turn_trace_attributes,
)
from compass_backend.orchestration.turn_context import (
    _TurnContext,
    _message_for_clarification,
    _message_for_turn,
)
from compass_backend.orchestration.structured_output_failure import (
    _is_planner_structured_output_failure,
    _planner_structured_output_failure_turn,
)
from compass_backend.orchestration.catalog_recall_shadow import (
    _catalog_recall_requests_for_turn,
    _catalog_recall_states_for_turn,
)
from compass_backend.orchestration.execution_stage import (
    _tag_turn_failure_taxonomy,
    _validation_failure_offers_clarification,
)
from compass_backend.execution.filters import filter_kind
from compass_backend.execution.referent_resolution import split_state_suffix
from compass_backend.orchestration.clarification_grounding import (
    apply_district_clarification_grounding,
    apply_metric_clarification_grounding,
)
from compass_backend.orchestration.followups import (
    _deterministic_clarification_choice_planner_run,
    _deterministic_data_detail_planner_run,
    _deterministic_policy_guidance_planner_run,
    _policy_guidance_exemplar_clarification_turn,
)
from compass_backend.orchestration.planning_stage import (
    _is_data_bearing_direct_turn,
    _merge_session_context_for_turn,
    _normalize_planner_turn,
    _plan_likely_dispatchable,
    _rescue_clarification_can_be_enriched,
    _reroute_direct_to_clarify,
)

import logging
import re

_logger = logging.getLogger(__name__)


@asynccontextmanager
async def _stage(progress: ProgressSink, name: str, message: str):
    """Emit ``stage_start``/``stage_end`` events around a pipeline stage.

    Advisory only — failures inside the stage propagate; the surrounding
    `stage_end` carries the elapsed wall time so the client can render
    finished stages even when the next one is in progress. If the stage
    raises, no `stage_end` is emitted (the orchestrator's terminal
    error path takes over the SSE stream).
    """

    progress.emit("stage_start", {"stage": name, "message": message})
    started_at = perf_counter()
    yield
    duration_ms = round((perf_counter() - started_at) * 1000)
    progress.emit(
        "stage_end",
        {"stage": name, "duration_ms": duration_ms},
    )


# Fixed body returned to the user when the orchestrator fails before producing
# a real assistant message. Operators reading compass.chat_messages can grep
# for this string to find error-envelope rows.
CHAT_ERROR_ENVELOPE_BODY = (
    "This turn failed before completing. Please try again or share the "
    "trace id with the team."
)

# W1-08 (#848): both pre-W1-08 constants (the prose message + missing-fields
# tuple) were deleted. The fallback clarify turn now composes its prose via
# rescue_clarification_question_for(RescueClarificationRef(code="fallback_no_anchor", ...))
# and is detected downstream via ClarificationRequest.is_rescue_fallback,
# not by comparing the prose to a verbatim string constant.
# W1-09 (#850): _MAX_CONTEXT_RESULT_REFS is imported from session.memory
# (single source of truth = 200) — local definition deleted.
_CATALOG_RECALL_DEFAULT_LIMIT = 10
_CATALOG_RECALL_MAX_REQUESTS = 12
_POLICY_GUIDANCE_DATA_FOLLOWUP_RE = re.compile(
    r"\b("
    r"table|tables|rank|ranking|compare|count|counts|how many|"
    r"minimum|min|max|maximum|bonus|bonuses|amount|amounts|"
    r"metric|metrics|value|values|data|csv|export"
    r")\b",
    re.IGNORECASE,
)
_POLICY_GUIDANCE_TOP_ONE_RE = re.compile(
    r"\b(top|first|1st)\s+(one|example|exemplar|district|policy)\b",
    re.IGNORECASE,
)
_POLICY_GUIDANCE_DEICTIC_ONE_RE = re.compile(
    r"\b(that|this)\s+(one|example|exemplar|district|policy)\b",
    re.IGNORECASE,
)
_POLICY_GUIDANCE_DETAIL_RE = re.compile(
    r"\b("
    r"actual policy details|policy details|details?|sources?|contracts?|"
    r"contract language|source citation"
    r")\b",
    re.IGNORECASE,
)
_POLICY_GUIDANCE_DO_ALL_RE = re.compile(
    r"\b("
    r"do all|all of them|all \d+ separately|each one separately|"
    r"each of them|all separately|do them all|do each"
    r")\b",
    re.IGNORECASE,
)
# #1755 (B14): deictic back-reference guard for the policy-guidance
# exemplar->selection bridge. Fires only when a follow-up points back at the
# prior exemplar districts ("these 3 districts", "those", "them"), so a fresh,
# unrelated question never falsely binds them. This is a typed-referent
# resolver in the no-prose-dispatch taxonomy (it gates whether to populate a
# typed PendingQueryContext.selection from typed memory; it does not route,
# rewrite, or substitute a typed plan field) — catalogued with that role in
# tests/fixtures/prose_dispatch_baseline.json, the documented grandfather path
# (not a sub-threshold single-word evasion).
_POLICY_GUIDANCE_DEICTIC_REFERENCE_RE = re.compile(
    r"\b(these|those|them)\b",
    re.IGNORECASE,
)


def _message_refers_back_deictically(message: str) -> bool:
    """True when the follow-up points back at the prior districts deictically.

    Narrows the exemplar->selection bridge so it fires only on a genuine
    back-reference ("these 3 districts", "those", "them"), never on a fresh,
    unrelated question."""

    return _POLICY_GUIDANCE_DEICTIC_REFERENCE_RE.search(message) is not None


async def build_chat_response(
    request: ChatRequest,
    *,
    planner_agent: PlannerAgent,
    store: SessionStore,
    executor: QueryExecutor,
    app_settings: Settings,
    auth_user: AuthenticatedUser | None = None,
    catalog_recall_service: CatalogRecallService | None = None,
    planner_toolsets: (
        Sequence[AbstractToolset[PlannerDeps]] | None
    ) = None,
    progress: ProgressSink | None = None,
    nctq_context_repo: NctqContextRepoProtocol | None = None,
    catalog_pipeline: CatalogPlanPipeline | None = None,
) -> ChatResponse:
    """Run the authoritative fresh chat pipeline once.

    Reads as a table of contents over the named stage helpers below. Each
    stage owns its own `compass_span` and reads/writes state on the shared
    `_TurnContext`. The outer `compass_turn_span` propagates to stage helpers
    implicitly through the async task.
    """

    ctx: _TurnContext | None = None
    with compass_turn_span(
        env=app_settings.environment,
        pipeline_version="fresh",
        requested_session_id=request.session_id,
        user_message=sanitize_logfire_text(request.message),
        auth_user_present=auth_user is not None,
        auth_user_is_admin=auth_user.is_admin if auth_user else False,
    ) as turn_span:
        try:
            ctx = _TurnContext(
                request=request,
                app_settings=app_settings,
                auth_user=auth_user,
                trace_id=get_current_trace_id() or trace_id_from_span(turn_span),
                progress=progress if progress is not None else NullProgressSink(),
                nctq_context_repo=nctq_context_repo,
            )
            set_span_attributes(turn_span, trace_id=ctx.trace_id)

            _enforce_message_length(ctx, turn_span)

            await _load_session_for_turn(ctx, store, turn_span)
            async with _stage(ctx.progress, "planner", "Planning your turn…"):
                if not (
                    _maybe_apply_clarification_choice_for_turn(ctx, turn_span)
                    or _maybe_resolve_policy_guidance_followup_for_turn(ctx, turn_span)
                    or _maybe_resolve_data_detail_followup_for_turn(ctx, turn_span)
                ):
                    await _run_planner_for_turn(
                        ctx,
                        planner_agent,
                        planner_toolsets=planner_toolsets,
                        catalog_pipeline=catalog_pipeline,
                    )
            _merge_session_context_for_turn(ctx)
            _promote_pending_context_for_turn(ctx, turn_span)
            _normalize_planner_turn(ctx)
            _apply_post_merge_shape_guard(ctx, turn_span)
            async with _stage(
                ctx.progress,
                "catalog_resolve",
                "Resolving districts and metrics…",
            ):
                await _review_cross_box_plan_for_turn(ctx, executor, turn_span)
                await _ground_clarification_options_for_turn(ctx, executor, turn_span)
            await _run_catalog_recall_shadow_for_turn(
                ctx,
                catalog_recall_service=catalog_recall_service,
                turn_span=turn_span,
            )

            set_span_attributes(turn_span, **_turn_trace_attributes(ctx.turn))

            async with _stage(
                ctx.progress,
                "execute",
                "Working on your answer…",
            ):
                await _execute_and_render_turn(ctx, executor, turn_span)

            _tag_turn_failure_taxonomy(ctx, turn_span)

            async with _stage(ctx.progress, "persist", "Saving turn…"):
                await _persist_turn_snapshot(ctx, store, turn_span)

            return ChatResponse(
                message=ctx.message,
                turn=ctx.turn,
                session=ctx.saved_session,
                snapshot_id=ctx.snapshot_id,
                result=ctx.result,
                validation=ctx.validation,
                manifest=ctx.manifest,
                trace_id=ctx.trace_id,
                logfire_url=logfire_url_for_trace(ctx.trace_id),
                message_ids=ctx.saved_assistant_message_ids,
            )
        except Exception as exc:
            set_span_attributes(
                turn_span,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            envelope_trace_id = (
                ctx.trace_id if ctx is not None else None
            ) or get_current_trace_id() or trace_id_from_span(turn_span)
            # Attach the resolved trace_id to the exception so the
            # transport layer can include it in the 500 response body
            # (or the SSE error event) without having to enter a new
            # span context — the orchestrator's compass_turn_span has
            # already exited by the time the route's except runs.
            try:
                exc.trace_id = envelope_trace_id  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                # Some built-in exceptions disallow attribute assignment; if so,
                # skip the trace_id attach — it's a best-effort debugging aid.
                pass
            await _persist_error_envelope(
                ctx=ctx,
                request=request,
                exc=exc,
                store=store,
                turn_span=turn_span,
            )
            await flush_on_error()
            raise


async def _persist_error_envelope(
    *,
    ctx: _TurnContext | None,
    request: ChatRequest,
    exc: Exception,
    store: SessionStore,
    turn_span,
) -> None:
    """Best-effort write of a TurnErrorEnvelope. Never re-raises.

    Resolves session_id and trace_id from whichever sources are still
    available depending on how early the exception fired:

    - `ctx.session_state.session_id` once `_load_session_for_turn` ran;
    - else `request.session_id` if the caller passed one;
    - else a freshly minted `SessionState().session_id` so the envelope
      still binds to *something* the verdict ledger can see.

    Persistence failures are logged and swallowed — the orchestrator's
    original exception bubbles up unchanged. The envelope is a
    diagnostic best-effort, not a contract guarantee. We don't want
    one broken DB connection to mask the real failure that triggered
    this path.
    """

    # Skip HTTPException — those are intentional control-flow signals
    # (e.g. _enforce_message_length raising 413) where the orchestrator
    # already shaped the failure for the client. Persisting an envelope
    # for them would double-record and confuse the verdict ledger.
    if isinstance(exc, HTTPException):
        return

    session_id: str
    if ctx is not None and ctx.session_state is not None:
        session_id = ctx.session_state.session_id
    elif request.session_id:
        session_id = request.session_id
    else:
        session_id = SessionState().session_id

    trace_id: str | None
    if ctx is not None:
        trace_id = ctx.trace_id
    else:
        trace_id = get_current_trace_id() or trace_id_from_span(turn_span)

    envelope = TurnErrorEnvelope(
        session_id=session_id,
        user_message=request.message,
        assistant_message=CHAT_ERROR_ENVELOPE_BODY,
        error_type=type(exc).__name__,
        error_message=str(exc),
        stage="build_chat_response",
        trace_id=trace_id,
    )

    try:
        await store.save_error_envelope(envelope)
    except Exception as persist_exc:
        _logger.warning(
            "compass.error_envelope.persist_failed session=%s trace=%s "
            "original_error=%s persist_error=%s",
            session_id,
            trace_id,
            type(exc).__name__,
            type(persist_exc).__name__,
        )


def _enforce_message_length(ctx: _TurnContext, turn_span) -> None:
    """Reject oversized requests before any downstream work begins."""

    if len(ctx.request.message) <= ctx.app_settings.chat_message_max_chars:
        return
    error_message = (
        "Message exceeds "
        f"{ctx.app_settings.chat_message_max_chars} characters"
    )
    set_span_attributes(
        turn_span,
        error_type="message_too_long",
        error=error_message,
    )
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=error_message,
    )


async def _load_session_for_turn(
    ctx: _TurnContext,
    store: SessionStore,
    turn_span,
) -> None:
    """Load (or create) the auth-scoped session row for this turn."""

    try:
        with compass_span(
            "compass.session.load",
            trace_id=ctx.trace_id,
            requested_session_id=ctx.request.session_id,
        ):
            session_state = await store.load(
                ctx.request.session_id,
                owner_email=(
                    ctx.auth_user.owner_email if ctx.auth_user else None
                ),
                is_admin=ctx.auth_user.is_admin if ctx.auth_user else False,
                create_if_missing=(
                    ctx.request.session_id is None
                    or not ctx.app_settings.api_key_auth_enabled
                ),
                # WS-5 / G5: persist the pseudonymous visitor id on the session
                # row so the dashboard can count repeat users. Set every turn; the
                # store backfills/reconciles (ON CONFLICT DO UPDATE), so a later
                # parent-issued id supersedes an earlier iframe-local fallback.
                visitor_id=ctx.request.visitor_id,
            )
    except SessionAccessDenied as exc:
        set_span_attributes(turn_span, error_type="session_not_found")
        raise HTTPException(status_code=404, detail="Session not found") from exc

    ctx.session_state = session_state
    ctx.turn_index = session_state.turn_count + 1
    set_span_attributes(
        turn_span,
        session_id=session_state.session_id,
        turn_index=ctx.turn_index,
    )


def _maybe_resolve_policy_guidance_followup_for_turn(
    ctx: _TurnContext,
    turn_span,
) -> bool:
    """Resolve strict exemplar-detail follow-ups from typed guidance memory."""

    assert ctx.session_state is not None
    memory = ctx.session_memory
    prior_route = memory.recent_routes[-1] if memory.recent_routes else None
    guidance_context = memory.latest_policy_guidance_context
    if prior_route != "policy_guidance" or guidance_context is None:
        return False
    if not guidance_context.exemplars:
        return False

    message = ctx.request.message.strip()
    if _POLICY_GUIDANCE_DATA_FOLLOWUP_RE.search(message):
        set_span_attributes(
            turn_span,
            policy_guidance_followup_resolver="skipped_data_request",
        )
        return False

    selected_ref: PolicyGuidanceExemplarRef | None = None
    matched_phrase: str | None = None
    if match := _POLICY_GUIDANCE_TOP_ONE_RE.search(message):
        selected_ref = guidance_context.exemplars[0]
        matched_phrase = match.group(0)
    elif match := _POLICY_GUIDANCE_DEICTIC_ONE_RE.search(message):
        selected_ref = guidance_context.exemplars[0]
        matched_phrase = match.group(0)
    elif match := _POLICY_GUIDANCE_DETAIL_RE.search(message):
        matched_phrase = match.group(0)
        if len(guidance_context.exemplars) == 1:
            selected_ref = guidance_context.exemplars[0]
        else:
            ctx.turn = _policy_guidance_exemplar_clarification_turn(
                guidance_context.exemplars
            )
            ctx.planner_run = _deterministic_policy_guidance_planner_run(
                ctx.turn,
                trace_id=ctx.trace_id,
                matched_phrase=matched_phrase,
                mode="clarify",
            )
            set_span_attributes(
                turn_span,
                policy_guidance_followup_resolver="clarify",
                policy_guidance_exemplar_count=len(guidance_context.exemplars),
            )
            return True

    if selected_ref is None:
        # CONSIST-R4 (#1419): recognize "do all N separately" / "all of them"
        # when the prior guidance response had 2+ exemplars. Build a
        # PendingQueryContext carrying each exemplar's subtopic as a MetricSpec
        # candidate with requires_composite_ranking=True, then emit a clarify
        # turn asking which districts to compare. The planner's pending-context
        # promotion machinery handles the next user reply — the infrastructure
        # was already in place (pending_context_from_plan, promote_pending_context_to_plan),
        # this resolver just feeds it from the policy_guidance path.
        if (
            len(guidance_context.exemplars) >= 2
            and (match := _POLICY_GUIDANCE_DO_ALL_RE.search(message))
        ):
            matched_phrase = match.group(0)
            subtopic_metrics = [
                MetricSpec(name=ref.subtopic)
                for ref in guidance_context.exemplars
            ]
            safe_turn_index = ctx.turn_index if ctx.turn_index >= 1 else None
            pending = PendingQueryContext(
                operation="rank",
                question=message,
                metrics=subtopic_metrics,
                requires_composite_ranking=True,
                missing_fields=["district", "scope"],
                last_clarification_question=(
                    "Which districts would you like to compare across each policy area?"
                ),
                source_turn_index=safe_turn_index,
            )
            clarification = ClarificationRequest(
                question=(
                    "Which districts would you like to compare across each policy area?"
                ),
                missing_fields=["district", "scope"],
                pending_context=pending,
            )
            ctx.turn = PlannerTurn(
                route="clarify",
                confidence=1.0,
                clarification=clarification,
            )
            ctx.next_pending_context = pending
            ctx.planner_run = _deterministic_policy_guidance_planner_run(
                ctx.turn,
                trace_id=ctx.trace_id,
                matched_phrase=matched_phrase,
                mode="do_all_composite",
            )
            set_span_attributes(
                turn_span,
                policy_guidance_followup_resolver="do_all_composite",
                policy_guidance_exemplar_count=len(guidance_context.exemplars),
            )
            return True
        return False

    ctx.turn = PlannerTurn(
        route="policy_guidance",
        confidence=1.0,
        policy_guidance=PolicyGuidancePlan(
            topic_ids=list(guidance_context.topic_ids),
            layers=["exemplars"],
            intent_summary=(
                "User asked for details about a selected prior "
                "policy-guidance exemplar."
            ),
            primary_topic_id=guidance_context.primary_topic_id,
            focus_terms=list(guidance_context.focus_terms),
            selected_exemplar_ids=[selected_ref.exemplar_id],
            response_mode="exemplar_detail",
        ),
    )
    ctx.planner_run = _deterministic_policy_guidance_planner_run(
        ctx.turn,
        trace_id=ctx.trace_id,
        matched_phrase=matched_phrase,
        mode="exemplar_detail",
    )
    set_span_attributes(
        turn_span,
        policy_guidance_followup_resolver="exemplar_detail",
        policy_guidance_selected_exemplar_id=selected_ref.exemplar_id,
        policy_guidance_selected_district=selected_ref.district,
    )
    return True


def _maybe_resolve_data_detail_followup_for_turn(
    ctx: _TurnContext,
    turn_span,
) -> bool:
    """Resolve strict "details for the top one" follow-ups from result memory."""

    assert ctx.session_state is not None
    memory = ctx.session_memory
    context = memory.latest_query_context
    if context is None or not context.result_districts:
        return False
    if not (context.result_metrics or context.query_plan.metrics):
        return False

    message = ctx.request.message.strip()
    detail_match = _POLICY_GUIDANCE_DETAIL_RE.search(message)
    if detail_match is None:
        return False

    selected_district: QueryContextDistrictRef | None = None
    matched_phrase = detail_match.group(0)
    if match := _POLICY_GUIDANCE_TOP_ONE_RE.search(message):
        selected_district = context.result_districts[0]
        matched_phrase = f"{matched_phrase}; {match.group(0)}"
    elif match := _POLICY_GUIDANCE_DEICTIC_ONE_RE.search(message):
        selected_district = context.result_districts[0]
        matched_phrase = f"{matched_phrase}; {match.group(0)}"
    elif len(context.result_districts) == 1:
        selected_district = context.result_districts[0]

    if selected_district is None:
        return False

    metric_name = (
        context.result_metrics[0].metric_name
        if context.result_metrics
        else context.query_plan.metrics[0].name
    )
    ctx.turn = PlannerTurn(
        route="execute",
        confidence=1.0,
        query_plan=QueryPlan(
            operation="lookup",
            question=message,
            selection=SelectionSpec(
                scope="named_districts",
                districts=[selected_district.district_name],
            ),
            metrics=[MetricSpec(name=metric_name)],
            output=OutputSpec(format="table"),
            temporal=context.query_plan.temporal,
        ),
    )
    ctx.planner_run = _deterministic_data_detail_planner_run(
        ctx.turn,
        trace_id=ctx.trace_id,
        matched_phrase=matched_phrase,
    )
    set_span_attributes(
        turn_span,
        data_detail_followup_resolver="top_result_lookup",
        data_detail_selected_district=selected_district.district_name,
        data_detail_selected_metric=metric_name,
    )
    return True


def _maybe_apply_clarification_choice_for_turn(
    ctx: _TurnContext,
    turn_span,
) -> bool:
    """Resume a clarify deterministically when the user clicked a structured option.

    The frontend posts the clicked option's machine handle as
    ``request.selected_option`` (#1348). Validate it against the options
    actually offered last turn (anti-stale / anti-forgery), fill the pending
    slot for the single dimension the clarify was about, and promote to an
    executable plan — skipping the planner (no prose re-parse). A stale or
    forged handle, or a dimension with no deterministic resolver (compound
    clarifies, scope-only, …), returns ``False`` so the planner handles the
    turn and re-clarifies safely.

    Dispatch is keyed on the persisted ``pending_query_context.missing_fields``
    so each dimension fills its own slot (district → ``selection``; metric →
    ``metrics``). Grounding is re-asserted downstream: execution re-resolves the
    chosen district name / metric name, so a clicked option can never bypass the
    catalog boundary (backend guardrail 7). Only typed, persisted data is read
    here — no raw user prose — so this stays within the no-prose-dispatch
    contract.
    """

    assert ctx.session_state is not None
    request = ctx.request
    if not request.selected_option:
        return False
    memory = ctx.session_memory
    offered = memory.pending_clarification_options
    pending = memory.pending_query_context
    if not offered or pending is None:
        return False
    chosen = next(
        (option for option in offered if option.value == request.selected_option),
        None,
    )
    if chosen is None:
        # Stale or forged handle — let the planner re-clarify rather than guess.
        return False
    # Fill the pending slot for the single open dimension. Promotion drops the
    # satisfied missing-field marker on its own
    # (planner._remaining_missing_fields), so no branch clears missing_fields by
    # hand. Anything without a deterministic single-dimension resolver falls
    # through to the planner.
    missing_fields = list(pending.missing_fields)
    choice_attributes: dict[str, object] = {"clarification_choice_value": chosen.value}
    if missing_fields == ["metric"]:
        # B1 guard: a threshold-count / metric-filter query mirrors the ambiguous
        # metric phrase into BOTH ``metrics`` and a ``kind="metric_value"``
        # FilterSpec.field (contracts/planning.py). REPLACE only rewrites
        # ``metrics``, leaving the filter keyed to the stale phrase — execution
        # re-resolves it, re-clarifies with the same chips, and the click loops
        # forever. Those shapes are rare; hand them to the planner, which
        # re-authors a consistent metric+filter pair from the clicked answer
        # (the pre-#1348 behavior, since these clarifies had no chips before).
        if any(filter_kind(spec) == "metric_value" for spec in pending.filters):
            return False
        # Collapse ONLY the ambiguous metric's candidate set to the chosen one,
        # and keep any already-resolved sibling metrics (m1, #1348). The offered
        # options ARE that ambiguous candidate set (their values are the
        # candidate metric names), so a pending metric whose name is NOT among
        # them is a resolved sibling to preserve — e.g. "salary" survives a
        # planning-time level click. The single-metric case is unchanged: every
        # pending metric is in ``offered``, so siblings is empty and the result
        # is exactly ``[chosen]``. Execution re-grounds the chosen canonical name
        # via resolve_metric_bundle's exact-name short-circuit.
        offered_values = {option.value for option in offered}
        sibling_metrics = [
            metric for metric in pending.metrics if metric.name not in offered_values
        ]
        resumed = pending.model_copy(
            update={
                "metrics": sibling_metrics + [MetricSpec(name=chosen.value)],
            }
        )
        choice_attributes["clarification_choice_metric"] = chosen.value
    elif missing_fields == ["district"]:
        # Reconstruct the grounded district name + state from the option label,
        # the deterministic inverse of
        # execution.selection._district_candidate_label ("<name>, <ST>").
        # split_state_suffix is the typed referent helper from #1362; execution
        # re-resolves the result so grounding is preserved.
        name, states = split_state_suffix(chosen.label)
        if not name:
            return False
        resumed = pending.model_copy(
            update={
                "selection": SelectionSpec(
                    scope="named_districts",
                    districts=[name],
                    states=sorted(states),
                )
            }
        )
        choice_attributes["clarification_choice_district"] = name
    else:
        # No deterministic resolver for this dimension (compound clarifies,
        # scope-only, comparison_group, …) — let the planner re-resolve safely.
        return False
    promoted = promote_pending_context_to_plan(resumed, question=request.message)
    if promoted is None:
        return False
    ctx.turn = PlannerTurn(route="execute", confidence=1.0, query_plan=promoted)
    ctx.planner_run = _deterministic_clarification_choice_planner_run(
        ctx.turn,
        trace_id=ctx.trace_id,
        selected_value=chosen.value,
    )
    set_span_attributes(
        turn_span,
        clarification_choice_resolved=True,
        **choice_attributes,
    )
    return True


async def _run_planner_for_turn(
    ctx: _TurnContext,
    planner_agent: PlannerAgent,
    *,
    planner_toolsets: Sequence[AbstractToolset[PlannerDeps]] | None = None,
    catalog_pipeline: CatalogPlanPipeline | None = None,
) -> None:
    """Invoke the planner and absorb structured-output retries.

    ``planner_toolsets`` is the always-attached Compass catalog toolset, so
    the planner can look things up before it drafts (#1248). ``catalog_pipeline``
    is the unified reconcile → adjudicate → finalize verifier the planner's
    output validator runs (see :func:`validate_planner_turn_quality_async`),
    which emits ``ModelRetry``-with-real-candidates on ambiguity.
    """

    assert ctx.session_state is not None
    session_memory = ctx.session_memory
    with compass_span(
        "compass.planner",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        model=agent_model_settings.planner_model,
        has_pending_context=(
            session_memory.pending_query_context is not None
        ),
        has_query_context=session_memory.latest_query_context is not None,
    ) as planner_span:
        try:
            planner_run = await run_planner(
                ctx.request.message,
                model=agent_model_settings.planner_model,
                agent=planner_agent,
                memory=session_memory,
                message_history=session_memory.message_history,
                current_academic_year=ctx.app_settings.current_academic_year,
                trace_id=ctx.trace_id,
                event_stream_handler=make_planner_event_stream_handler(
                    ctx.progress
                ),
                planner_toolsets=planner_toolsets,
                catalog_pipeline=catalog_pipeline,
            )
            _set_planner_evidence_trace_attributes(
                planner_span,
                planner_run.evidence,
            )
        except UnexpectedModelBehavior as exc:
            if not _is_planner_structured_output_failure(exc):
                raise
            set_span_attributes(
                planner_span,
                controlled_failure=True,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            planner_run = PlannerRun(
                turn=_planner_structured_output_failure_turn(ctx.request.message),
            )
        guidance_names = (
            [
                guidance.name
                for guidance in planner_run.evidence.planner_guidance
            ]
            if planner_run.evidence is not None
            else []
        )
        set_span_attributes(
            planner_span,
            planner_guidance_names=guidance_names,
            planner_guidance_count=len(guidance_names),
        )

    ctx.planner_run = planner_run
    # W1-07 (#846): if the planner returned a "direct" turn whose message
    # contains data-shaped tokens, convert it to a typed clarify turn
    # before any later stage can render it. The direct route is for
    # greetings / capability questions; a model that puts a dollar amount
    # or ranking in a direct response is bypassing governed execution.
    if _is_data_bearing_direct_turn(planner_run.turn):
        ctx.planner_run = PlannerRun(
            turn=_reroute_direct_to_clarify(planner_run.turn),
            evidence=planner_run.evidence,
        )
    ctx.turn = ctx.planner_run.turn


def _promote_pending_context_for_turn(
    ctx: _TurnContext,
    turn_span,
) -> None:
    """Promote a pending-context plan when the new turn answers a prior clarify."""

    assert ctx.session_state is not None
    assert ctx.turn is not None
    assert ctx.planner_run is not None
    with compass_span(
        "compass.session.pending_context",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        route=ctx.turn.route,
    ):
        ctx.next_pending_context = pending_context_after_turn(
            ctx.turn,
            ctx.session_memory.pending_query_context,
            turn_index=ctx.turn_index,
        )
        promoted_plan = promote_pending_context_to_plan(
            ctx.next_pending_context,
            question=ctx.request.message,
        )
        if ctx.turn.route == "clarify" and promoted_plan is not None:
            ctx.turn = PlannerTurn(
                route="execute",
                confidence=ctx.turn.confidence,
                query_plan=promoted_plan,
            )
            set_span_attributes(
                turn_span,
                pending_context_promoted=True,
            )
        set_span_attributes(
            turn_span,
            has_pending_context=(
                ctx.session_memory.pending_query_context is not None
            ),
            next_pending_context=ctx.next_pending_context is not None,
            planner_evidence_stored=ctx.planner_run.evidence is not None,
        )


def _policy_guidance_exemplars_to_bridge(
    ctx: _TurnContext,
) -> list[PolicyGuidanceExemplarRef]:
    """Return the prior policy-guidance exemplars to bridge into selection.

    #1755 (B14): when the immediately prior turn was answered via the
    ``policy_guidance`` route, its NCTQ-curated exemplar districts are stored
    in ``latest_policy_guidance_context`` with a real typed ``district_id``
    each. A metric/profile follow-up that points back at them deictically
    ("enrollment of these 3 districts") otherwise drops them entirely and
    rescues. Returns the exemplars only when (a) the prior route was
    policy_guidance, (b) the guidance context carries exemplars, and (c) the
    follow-up message refers back deictically. Reads typed exemplar fields
    only; the deictic check matches single tokens, not multi-word prose."""

    memory = ctx.session_memory
    prior_route = memory.recent_routes[-1] if memory.recent_routes else None
    if prior_route != "policy_guidance":
        return []
    guidance_context = memory.latest_policy_guidance_context
    if guidance_context is None or not guidance_context.exemplars:
        return []
    if not _message_refers_back_deictically(ctx.request.message):
        return []
    return list(guidance_context.exemplars)


def _enrich_rescue_with_prior_context(ctx: _TurnContext, turn_span) -> None:
    """Replace the rescue clarification with a typed one.

    Reads only typed ``QueryContext`` / ``PolicyGuidanceContext`` fields
    (``result_districts``, ``result_metrics``, exemplar ``district``) — no
    inspection of ``deps.message`` / ``plan.question`` prose for routing; the
    only message read is a single-token deictic back-reference guard.

    Three paths:
      - Prior context present: anchored message naming the prior district
        count + metric, with PendingQueryContext carrying the prior selection
        so the next user reply can promote to an executable plan.
      - No QueryContext but a prior policy_guidance turn established exemplar
        districts and the follow-up refers back deictically (#1755): bridge
        those exemplar districts into the PendingQueryContext selection so the
        next reply promotes to a profile/metric lookup on exactly them,
        instead of dropping them and rescuing.
      - No prior context: rephrase-suggestion message that steers the user
        toward concrete query patterns Compass supports. This closes M3c-1
        (multi-metric "BA and MA in one table" with no prior execute turn
        to anchor on) — the user sees a typed clarification with worked
        example patterns instead of the verbatim refusal.
    """
    assert ctx.session_state is not None
    assert ctx.turn is not None
    assert ctx.turn.clarification is not None
    prior = ctx.session_memory.latest_query_context
    has_anchor = prior is not None and bool(prior.result_districts)
    # #1755 (B14): when there is no executed QueryContext to anchor on, the
    # prior turn may still have established districts via the policy_guidance
    # route (NCTQ-curated exemplars). Bridge those exemplar districts forward
    # only when the follow-up refers back deictically ("these 3 districts",
    # "those", "them") — otherwise a fresh question would falsely bind them.
    # Reads typed exemplar fields only; the deictic guard matches single
    # tokens, never multi-word prose routing.
    policy_exemplars = _policy_guidance_exemplars_to_bridge(ctx) if not has_anchor else []
    has_policy_anchor = bool(policy_exemplars)
    with compass_span(
        "compass.planner.shape_guard",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        guard_path=(
            "rescue_enrichment_anchored"
            if has_anchor
            else "rescue_enrichment_policy_guidance_bridge"
            if has_policy_anchor
            else "rescue_enrichment_fallback"
        ),
    ):
        if has_anchor:
            assert prior is not None  # narrowed by has_anchor
            district_count = len(prior.result_districts)
            metric_name = (
                prior.result_metrics[0].metric_name if prior.result_metrics else None
            )
            rescue_code: RescueClarificationCode = (
                "anchored_with_metric" if metric_name else "anchored_no_metric"
            )
        elif has_policy_anchor:
            # The prior policy_guidance exemplars ARE the established
            # districts. We have no carried metric, so this is the
            # anchored_no_metric clarification — the names live in the typed
            # pending context below, ready for next-turn promotion.
            district_count = len(policy_exemplars)
            metric_name = None
            rescue_code = "anchored_no_metric"
        else:
            district_count = 0
            metric_name = None
            # No prior context to anchor on. Falls back to a supported-
            # patterns template instead of the verbatim refusal. Closes
            # M3c-1 (BA + MA in one table) without a prior execute turn.
            rescue_code = "fallback_no_anchor"
        question = rescue_clarification_question_for(
            RescueClarificationRef(
                code=rescue_code,
                district_count=district_count,
                metric_name=metric_name,
            )
        )
        # PendingQueryContext carries the prior selection (when present)
        # so the NEXT turn's promote_pending_context_to_plan can resume
        # from a typed anchor. With no prior context, we still emit a
        # minimal pending context so the typed-clarification contract
        # stays uniform across the two branches.
        # source_turn_index has ge=1 — only pass when we actually have a turn.
        safe_turn_index = ctx.turn_index if ctx.turn_index >= 1 else None
        try:
            if has_anchor:
                assert prior is not None
                pending = PendingQueryContext(
                    operation=prior.query_plan.operation,
                    question=ctx.request.message,
                    selection=SelectionSpec(
                        scope="named_districts",
                        districts=[d.district_name for d in prior.result_districts],
                    ),
                    metrics=(
                        [MetricSpec(name=m.metric_name) for m in prior.result_metrics]
                        if prior.result_metrics
                        else []
                    ),
                    last_clarification_question=question,
                    source_turn_index=safe_turn_index,
                )
            elif has_policy_anchor:
                # Carry the typed exemplar district names forward as a
                # named_districts selection so the next turn promotes to a
                # profile/metric lookup on exactly those districts. The
                # follow-up's metric ("enrollment") is left for the planner
                # to read on the promotion turn — this turn establishes the
                # selection, not the metric.
                pending = PendingQueryContext(
                    question=ctx.request.message,
                    selection=SelectionSpec(
                        scope="named_districts",
                        districts=[ref.district for ref in policy_exemplars],
                    ),
                    last_clarification_question=question,
                    source_turn_index=safe_turn_index,
                )
            else:
                pending = PendingQueryContext(
                    question=ctx.request.message,
                    last_clarification_question=question,
                    source_turn_index=safe_turn_index,
                )
        except Exception as exc:  # noqa: BLE001
            set_span_attributes(
                turn_span,
                shape_guard_fired=False,
                shape_guard_skipped="rescue_pending_construction_failed",
                shape_guard_skip_reason=type(exc).__name__,
            )
            _logger.warning(
                "rescue-path enrichment skipped: %s while constructing "
                "pending context (has_anchor=%s)",
                type(exc).__name__,
                has_anchor,
            )
            return
        ctx.next_pending_context = pending
        ctx.turn = PlannerTurn(
            route="clarify",
            confidence=ctx.turn.confidence,
            clarification=ClarificationRequest(
                question=question,
                # W1-08 (#848): inline missing-fields literal; the
                # pre-W1-08 named constant was deleted along with the
                # prose-equality match it served.
                missing_fields=["comparison_group", "metric"],
                candidates=[],
                pending_context=pending,
                # is_rescue_fallback=False here: this is the typed
                # ENRICHED turn, not the raw planner-output-failure
                # fallback. Re-triggering the rescue path would loop.
                # #1613: but it IS still a rescue-origin over-clarify, so
                # carry the durable rescue_origin marker forward (the
                # enrichment gate reads is_rescue_fallback only, so this
                # cannot re-trigger the loop) — the answerability eval
                # criterion needs a signal that survives this rebuild.
                rescue_origin=True,
            ),
        )
        set_span_attributes(
            turn_span,
            shape_guard_fired=True,
            shape_guard_path=(
                "rescue_enrichment_anchored"
                if has_anchor
                else "rescue_enrichment_policy_guidance_bridge"
                if has_policy_anchor
                else "rescue_enrichment_fallback"
            ),
            shape_guard_prior_district_count=district_count,
            shape_guard_prior_metric_name=metric_name or "(none)",
        )


def _user_facing_shape_clarification(operation: str) -> str:
    """Return a graceful, user-facing clarification for an uncompilable shape.

    Secondary, defense-in-depth guard (#1734). ``unsupported_shape_hint``
    returns an internal retry hint engineered for the planner LLM's re-roll
    (e.g. "Operation `profile_lookup` cannot be compiled: ... remove `sort` and
    `sort_steps` ..."). That diagnostic must never reach an end user. The raw
    hint is still kept for the typed ``pending_context`` (next-turn promotion)
    and the ``shape_guard_hint`` span attribute (observability); only the
    user-visible ``ClarificationRequest.question`` is humanized here.

    This is purely a safety net: the owning-boundary merge fix (#1734, the
    ``rank -> non-rank`` drop-guards in ``planning/planner.py``) prevents the
    impossible shape from being built in the first place, so on the regression
    path this guard does not fire at all. It catches any *other*, latent
    inheritance defect that would otherwise leak a raw diagnostic.
    """

    return (
        "I couldn't put that together as asked. Tell me which districts you "
        "want and which Compass metric or profile field to show, and I'll "
        "pull it."
    )


def _apply_post_merge_shape_guard(ctx: _TurnContext, turn_span) -> None:
    """Convert unsupported-shape execute plans into typed clarifications.

    Sits between merging/normalisation and execution. When the planner emits a
    ``route="execute"`` plan whose ``(operation, scope, metrics, sort, filters,
    limit)`` combination ``execution.shape_check.unsupported_shape_hint`` flags
    as unsupported, we re-emit the turn as ``route="clarify"`` with a typed
    ``PendingQueryContext`` carrying the plan's fields. This replaces the
    generic post-execution refusal ("I could not structure that request
    safely" / "deterministic execution does not support this query shape
    yet") with an actionable, operation-specific clarification message and
    lets the next user turn resume against typed prior context.

    Reads ``QueryPlan`` typed fields only. No prose inspection of
    ``plan.question`` or ``deps.message`` — guarded by
    ``test_no_prose_dispatch.py``.
    """

    assert ctx.session_state is not None
    assert ctx.turn is not None
    # NEW: rescue-path enrichment for the planner's safe-structure clarification.
    # When the planner exhausts retries (UnexpectedModelBehavior →
    # _planner_structured_output_failure_turn) it emits route="clarify" with the
    # generic safe-structure rescue prose. If we have a usable prior
    # QueryContext from the last successful turn, we replace the generic refusal
    # with a typed clarification anchored to the prior selection — and carry the
    # prior selection into PendingQueryContext so the NEXT user turn can promote
    # to an executable plan even from a short reply.
    if _rescue_clarification_can_be_enriched(ctx):
        _enrich_rescue_with_prior_context(ctx, turn_span)
        return
    if ctx.turn.route != "execute" or ctx.turn.query_plan is None:
        return
    plan = ctx.turn.query_plan
    if _plan_likely_dispatchable(plan):
        # The executor has internal delegation paths (e.g., multi-metric rank →
        # lookup, multi-metric rank+limit → _execute_limited_ranked_lookup,
        # profile-ordered ranking) that ``unsupported_shape_hint`` doesn't
        # model. Skip the guard when delegation is plausible and let the
        # executor handle it; if it still refuses, the existing executor
        # refusal path surfaces the message.
        return
    hint = unsupported_shape_hint(plan)
    if hint is None:
        return

    with compass_span(
        "compass.planner.shape_guard",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        operation=plan.operation,
    ):
        # The plan may carry typed fields whose values are valid on QueryPlan
        # but rejected by PendingQueryContext (different Pydantic constraints
        # on some fields). If construction fails, log + skip the guard rather
        # than turning the turn into a 500. The executor's own refusal path
        # then surfaces the message — no regression vs the pre-guard
        # behaviour for affected plan shapes.
        try:
            # Single from-plan builder (#1419): the full W1-02 (#836)
            # round-trip now goes through the shared
            # ``pending_context_from_plan`` — this completes the partial
            # #1321 fix, which carried only ``sort_steps`` and dropped
            # ``inherit_selection_from`` / ``requires_all_metrics`` /
            # ``requires_composite_ranking`` / ``count_kind``. After finalize
            # the user's sort/rank intent lives only on ``plan.sort_steps``
            # (``plan.sort`` is None); the builder round-trips it so next-turn
            # promotion preserves direction.
            pending = pending_context_from_plan(
                plan,
                last_clarification_question=hint,
                source_turn_index=ctx.turn_index,
            )
            # ClarificationRequest.missing_fields has min_length=1. The
            # shape guard doesn't know which specific field of the plan is at
            # fault — the hint is operation-level — so we reuse the same
            # broad signal the existing executor refusal path uses.
            new_turn = PlannerTurn(
                route="clarify",
                confidence=ctx.turn.confidence,
                clarification=ClarificationRequest(
                    # #1734 secondary guard: the raw ``hint`` is an internal
                    # planner-retry diagnostic — never surface it to the user.
                    # It is still carried into ``pending`` above
                    # (``last_clarification_question=hint``) for next-turn
                    # promotion and recorded as ``shape_guard_hint`` below.
                    question=_user_facing_shape_clarification(plan.operation),
                    # W1-08 (#848): inline literal.
                    missing_fields=["comparison_group", "metric"],
                    candidates=[],
                    pending_context=pending,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            set_span_attributes(
                turn_span,
                shape_guard_fired=False,
                shape_guard_skipped="construction_failed",
                shape_guard_skip_reason=type(exc).__name__,
                shape_guard_hint=hint,
            )
            _logger.warning(
                "post-merge shape guard skipped: %s while constructing typed "
                "clarification for operation=%s hint=%r",
                type(exc).__name__,
                plan.operation,
                hint,
            )
            return
        ctx.next_pending_context = pending
        ctx.turn = new_turn
        set_span_attributes(
            turn_span,
            shape_guard_fired=True,
            shape_guard_operation=plan.operation,
            shape_guard_hint=hint,
        )


async def _run_catalog_recall_shadow_for_turn(
    ctx: _TurnContext,
    *,
    catalog_recall_service: CatalogRecallService | None,
    turn_span,
) -> None:
    """Collect advisory catalog candidates without influencing execution."""

    if catalog_recall_service is None or ctx.turn is None:
        return
    assert ctx.session_state is not None
    requests = _catalog_recall_requests_for_turn(ctx.request.message, ctx.turn)
    if not requests:
        return

    batches = []
    failures = 0
    with compass_span(
        "compass.catalog.recall_shadow",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        request_count=len(requests),
    ):
        for phrase, entity_types in requests[:_CATALOG_RECALL_MAX_REQUESTS]:
            try:
                report = await catalog_recall_service.recall(
                    phrase,
                    entity_types=entity_types,
                    limit=_CATALOG_RECALL_DEFAULT_LIMIT,
                    states=_catalog_recall_states_for_turn(ctx.turn),
                    expand_prompt=False,
                )
            except Exception as exc:  # noqa: BLE001
                failures += 1
                _logger.warning(
                    "catalog recall shadow failed for phrase=%r: %s",
                    phrase,
                    type(exc).__name__,
                )
                continue
            batches.extend(report.batches)

    if batches:
        ctx.recall_report = RecallReport(query=ctx.request.message, batches=batches)
        _set_recall_trace_attributes(turn_span, ctx.recall_report)
    set_span_attributes(
        turn_span,
        catalog_recall_shadow_enabled=True,
        catalog_recall_request_count=len(requests),
        catalog_recall_failure_count=failures,
    )


async def _ground_clarification_options_for_turn(
    ctx: _TurnContext,
    executor: QueryExecutor,
    turn_span,
) -> None:
    """Override a planner-emitted clarify's options with grounded ones.

    #1348 (Option B): the planner can recognize ambiguity at planning time and
    emit ``route="clarify"`` directly — for a district (a same-name ambiguity) or
    a metric (an ambiguous metric phrase). Re-resolve the phrase through the
    catalog so the offered options are born from real rows (district_id +
    state-qualified label, or the canonical metric name). Runs post-planner /
    pre-execution, so the execution-origin metric clarify (whose options are
    built at execution.selection's clarify site) never reaches here. No-op for
    non-clarify turns or when the executor exposes no catalog boundary.
    """

    if ctx.turn is None or ctx.turn.route != "clarify":
        return
    catalog = getattr(executor, "catalog", None)
    if catalog is None:
        return
    try:
        grounded_turn = await apply_district_clarification_grounding(ctx.turn, catalog)
        grounded_turn = await apply_metric_clarification_grounding(grounded_turn, catalog)
    except Exception as exc:  # noqa: BLE001
        # T3 (#1348): grounding is a best-effort enrichment. Metric grounding's
        # resolve_metric_bundle can invoke the catalog adjudicator LLM, which can
        # raise — never let that 500 an otherwise-answerable clarify. Keep the
        # planner's prose clarify and record the skip for observability.
        _logger.warning(
            "clarification grounding skipped: %s", type(exc).__name__
        )
        set_span_attributes(turn_span, clarification_grounding_skipped=True)
        return
    if grounded_turn is ctx.turn:
        return
    ctx.turn = grounded_turn
    set_span_attributes(
        turn_span,
        clarification_options_grounded=True,
        clarification_option_count=len(grounded_turn.clarification.candidate_options),
    )


async def _review_cross_box_plan_for_turn(
    ctx: _TurnContext,
    executor: QueryExecutor,
    turn_span,
) -> None:
    """Let capable executors repair typed wrong-box planner output."""

    assert ctx.session_state is not None
    assert ctx.turn is not None
    reviewer = getattr(executor, "review_cross_box_planner_turn", None)
    if reviewer is None:
        return
    before = ctx.turn
    with compass_span(
        "compass.planner.cross_box_review",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        route=ctx.turn.route,
    ) as span:
        ctx.turn = await reviewer(ctx.turn)
        changed = ctx.turn != before
        set_span_attributes(
            span,
            cross_box_review_applied=changed,
            route_before=before.route,
            route_after=ctx.turn.route,
        )
        set_span_attributes(
            turn_span,
            cross_box_review_applied=changed,
        )


async def _execute_and_render_turn(
    ctx: _TurnContext,
    executor: QueryExecutor,
    turn_span,
) -> None:
    """Dispatch to execute / policy-guidance / fallback message based on route."""

    assert ctx.turn is not None
    if ctx.turn.route == "execute" and ctx.turn.query_plan is not None:
        await _execute_query_plan_for_turn(ctx, executor, turn_span)
    elif (
        ctx.turn.route == "policy_guidance"
        and ctx.turn.policy_guidance is not None
    ):
        await _render_policy_guidance_for_turn(ctx, turn_span)
    elif (
        ctx.turn.route == "publication"
        and ctx.turn.publication is not None
    ):
        await _render_publication_for_turn(ctx, turn_span)
    else:
        ctx.message = _message_for_turn(ctx.turn)


async def _execute_query_plan_for_turn(
    ctx: _TurnContext,
    executor: QueryExecutor,
    turn_span,
) -> None:
    """Execute the typed plan, then validate + render, with clarify fallback."""

    assert ctx.session_state is not None
    assert ctx.turn is not None
    assert ctx.turn.query_plan is not None
    with compass_span(
        "compass.execute",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        operation=ctx.turn.query_plan.operation,
    ):
        # #1248: execution re-resolves every plan deterministically via
        # CatalogResolver. The recognition-derived authority hints were only a
        # reuse optimization sourced from the now-deleted pass-B advisor; with
        # no advisor there is no hint to seed, so we let the executor default
        # to normal catalog resolution.
        outcome = await executor.execute(ctx.turn.query_plan)
    ctx.executed_outcome = outcome
    ctx.message = outcome.message
    ctx.resolution_report = outcome.resolution_report
    ctx.recall_report = _merge_recall_reports(
        ctx.recall_report,
        outcome.recall_report,
    )
    _set_resolution_trace_attributes(turn_span, ctx.resolution_report)
    _set_recall_trace_attributes(turn_span, ctx.recall_report)
    if isinstance(outcome, ExecutionClarification):
        attempted_plan = ctx.turn.query_plan
        ctx.turn = PlannerTurn(
            route="clarify",
            confidence=ctx.turn.confidence,
            clarification=outcome.clarification,
        )
        ctx.message = _message_for_clarification(outcome.clarification)
        set_span_attributes(
            turn_span,
            **_turn_trace_attributes(ctx.turn),
            execution_clarification=True,
            attempted_operation=attempted_plan.operation,
            attempted_metrics=[
                metric.name for metric in attempted_plan.metrics
            ],
            result_type="none",
            row_count=0,
        )
        ctx.next_pending_context = (
            pending_context_from_execution_clarification(
                attempted_plan,
                outcome.clarification,
                turn_index=ctx.turn_index,
                ambiguous_metric_phrase=outcome.ambiguous_metric_phrase,
            )
        )
    elif isinstance(outcome, ExecutionSuccess):
        await _validate_and_render_success(ctx, outcome, turn_span)
    else:
        set_span_attributes(turn_span, result_type="none", row_count=0)


_MULTI_METRIC_CLARIFICATION_CANDIDATES: tuple[str, ...] = (
    "Show one combined table with each metric as a separate column",
    "Show separate tables — one ranking per metric",
    "Show the intersection: districts that appear in the top-N for every metric",
)


def _pivot_validation_failure_to_clarification(
    ctx: _TurnContext,
    turn_span,
) -> bool:
    """Replace a multi-metric validation-failed turn with a typed clarify.

    Mirrors the ExecutionClarification branch below: rewrites `ctx.turn`
    to route="clarify", sets `ctx.message`, captures
    `ctx.next_pending_context` so the next user reply can promote a typed
    plan, and emits the same span attributes the post-execution clarify
    path emits. Returns True when the pivot fired (caller stops the normal
    render path); False when the validation failure does not qualify and
    the caller continues.
    """
    assert ctx.session_state is not None
    assert ctx.turn is not None
    assert ctx.turn.query_plan is not None
    assert ctx.validation is not None
    plan = ctx.turn.query_plan
    if not _validation_failure_offers_clarification(plan, ctx.validation):
        return False
    finding_codes = [finding.code for finding in ctx.validation.findings]
    question = (
        "I have rows for each metric you asked about, but they don't "
        "render cleanly as one table. How would you like to see them?"
    )
    try:
        # Single from-plan builder (#1419): carries the full W1-02 (#836)
        # field set so the next-turn promotion keeps the typed meaning.
        pending = pending_context_from_plan(
            plan,
            last_clarification_question=question,
            source_turn_index=ctx.turn_index,
        )
        new_clarification = ClarificationRequest(
            question=question,
            missing_fields=["render_shape"],
            candidates=list(_MULTI_METRIC_CLARIFICATION_CANDIDATES),
            pending_context=pending,
        )
    except Exception as exc:  # noqa: BLE001
        set_span_attributes(
            turn_span,
            validation_clarify_pivot_skipped="construction_failed",
            validation_clarify_pivot_skip_reason=type(exc).__name__,
            validation_findings=finding_codes,
        )
        _logger.warning(
            "validation→clarify pivot skipped: %s while constructing "
            "typed clarification for operation=%s findings=%s",
            type(exc).__name__,
            plan.operation,
            finding_codes,
        )
        return False
    ctx.turn = PlannerTurn(
        route="clarify",
        confidence=ctx.turn.confidence,
        clarification=new_clarification,
    )
    ctx.next_pending_context = pending
    ctx.message = _message_for_clarification(new_clarification)
    set_span_attributes(
        turn_span,
        validation_clarify_pivot_fired=True,
        validation_findings=finding_codes,
        attempted_operation=plan.operation,
        attempted_metric_count=len(plan.metrics),
        result_type="none",
        row_count=0,
        **_turn_trace_attributes(ctx.turn),
    )
    return True


async def _validate_and_render_success(
    ctx: _TurnContext,
    outcome: ExecutionSuccess,
    turn_span,
) -> None:
    """Validate the deterministic result, render it, and re-validate the body."""

    assert ctx.session_state is not None
    assert ctx.turn is not None
    assert ctx.turn.query_plan is not None
    # #1240: gate the chart on user intent before anything downstream reads the
    # result. The per-variant validators build a chart whenever the data is
    # chartable; this is the single authority that decides whether it is shown.
    # Reassign ctx.result so validation, rendering, SSE, the persisted snapshot,
    # and the CSV/table all see one consistent result (no parity bug).
    chart_decision = resolve_chart_visibility(ctx.turn.query_plan, outcome.result)
    ctx.result = chart_decision.result
    result = ctx.result
    with compass_span(
        "compass.validate",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        result_type=result.result_type,
        row_count=len(_result_rows(result)),
    ):
        ctx.validation = validate_result(
            ctx.turn.query_plan,
            result,
            authority=outcome.authority,
        )
    # M3 #1013: when pre-render validation rejects a multi-metric plan,
    # prefer a typed clarification offering concrete render choices over
    # the generic "validation failed before rendering" body. Mirrors the
    # ExecutionClarification handoff one block above.
    if _pivot_validation_failure_to_clarification(ctx, turn_span):
        return
    with compass_span(
        "compass.render",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        validation_valid=ctx.validation.valid,
    ):
        ctx.manifest = render_response(ctx.turn.query_plan, result, ctx.validation)
        ctx.manifest = _attach_resolution_manifest_metadata(
            ctx.manifest,
            ctx.resolution_report,
        )
        ctx.manifest = _attach_recall_manifest_metadata(
            ctx.manifest,
            ctx.recall_report,
        )
        ctx.manifest = attach_adjacent_metrics_manifest_metadata(
            ctx.manifest,
            outcome.adjacent_candidates,
        )
    if ctx.manifest.status == "rendered":
        with compass_span(
            "compass.validate.rendered_body",
            trace_id=ctx.trace_id,
            session_id=ctx.session_state.session_id,
            manifest_status=ctx.manifest.status,
        ):
            ctx.validation = validate_result(
                ctx.turn.query_plan,
                result,
                authority=outcome.authority,
                rendered_body=ctx.manifest.body,
                manifest_metadata=ctx.manifest.metadata,
            )
        if not ctx.validation.valid:
            with compass_span(
                "compass.render.validation_failure",
                trace_id=ctx.trace_id,
                session_id=ctx.session_state.session_id,
            ):
                ctx.manifest = render_response(
                    ctx.turn.query_plan,
                    result,
                    ctx.validation,
                )
                ctx.manifest = _attach_resolution_manifest_metadata(
                    ctx.manifest,
                    ctx.resolution_report,
                )
                ctx.manifest = _attach_recall_manifest_metadata(
                    ctx.manifest,
                    ctx.recall_report,
                )
                ctx.manifest = attach_adjacent_metrics_manifest_metadata(
                    ctx.manifest,
                    list(outcome.adjacent_candidates),
                )
    _attach_chart_visibility_metadata(ctx, chart_decision)
    await _maybe_apply_answer_layer(
        ctx,
        turn_span,
        route="execute",
        require_valid_result=True,
    )
    ctx.message = ctx.manifest.body
    set_span_attributes(
        turn_span,
        result_type=result.result_type,
        row_count=len(_result_rows(result)),
        validation_valid=ctx.validation.valid,
        manifest_status=ctx.manifest.status,
        **_result_trace_attributes(result),
    )
    if ctx.validation.valid:
        ctx.query_context = _query_context_for_result(
            query_plan=ctx.turn.query_plan,
            authority=outcome.authority,
            result=result,
        )
        ctx.next_pending_context = None


async def _render_policy_guidance_for_turn(
    ctx: _TurnContext,
    turn_span,
) -> None:
    """Render a policy-guidance turn from the typed guidance library."""

    assert ctx.session_state is not None
    assert ctx.turn is not None
    assert ctx.turn.policy_guidance is not None
    with compass_span(
        "compass.policy_guidance",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        topic_ids=list(ctx.turn.policy_guidance.topic_ids),
        layers=list(ctx.turn.policy_guidance.layers),
    ):
        library = get_library()
        bundle = library.assemble(
            topic_ids=list(ctx.turn.policy_guidance.topic_ids),
            layers=list(ctx.turn.policy_guidance.layers),
        )
        ctx.manifest = render_policy_guidance(
            ctx.turn.policy_guidance,
            bundle,
            library=library,
        )
    if ctx.manifest.status == "rendered":
        await _maybe_apply_answer_layer(
            ctx,
            turn_span,
            route="policy_guidance",
            require_valid_result=False,
        )
    ctx.message = (
        ctx.manifest.body
        if ctx.manifest.status == "rendered"
        else POLICY_GUIDANCE_VALIDATION_FAILED_BODY
    )
    set_span_attributes(
        turn_span,
        result_type="policy_guidance",
        row_count=(
            len(bundle.stances)
            + len(bundle.rationales)
            + len(bundle.exemplars)
        ),
        manifest_status=ctx.manifest.status,
    )


async def _render_publication_for_turn(
    ctx: _TurnContext,
    turn_span,
) -> None:
    """Render a publication turn by citing NCTQ's published writing.

    Mirrors ``_render_policy_guidance_for_turn``: resolve the read-only NCTQ
    context repo, search ``compass.nctq_publications`` for the planner's typed
    topic phrase, and render the matched rows into a manifest. The renderer
    copies title/url/summary verbatim and grounds every citation in a fetched
    row, so the answer never invents a publication. No answer-layer stylist
    pass — the body is already the final, verbatim citation prose.
    """

    assert ctx.session_state is not None
    assert ctx.turn is not None
    assert ctx.turn.publication is not None
    query = ctx.turn.publication.publication_query
    with compass_span(
        "compass.publication",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        publication_query=query,
    ):
        if ctx.nctq_context_repo is not None:
            hits = await ctx.nctq_context_repo.search_publications(query)
        else:
            hits = ()
        ctx.manifest = render_publication(query, hits)
    ctx.message = (
        ctx.manifest.body
        if ctx.manifest.status == "rendered"
        else PUBLICATION_VALIDATION_FAILED_BODY
    )
    set_span_attributes(
        turn_span,
        result_type="publication",
        row_count=len(hits),
        manifest_status=ctx.manifest.status,
    )


# #1240/#1555: deterministic fallback narrating "you asked for a chart but the
# data is too thin to draw one" when the natural-voice answer layer is OFF, so
# the fact is never silently lost. When the answer layer is on, the structured
# `chart_unavailable` brief signal lets the stylist say this in its own voice.
CHART_UNAVAILABLE_FALLBACK_LINE = (
    "I couldn't draw a chart here — there isn't enough comparable data to plot."
)
# #1555: the single "want a chart?" follow-up suggestion offered when the data
# is chartable but the user didn't ask. Emitted to the frontend as a `followups`
# SSE event; the chip resubmits this prompt.
CHART_SUGGESTION_FOLLOWUP = "Show me this as a bar chart"


def _attach_chart_visibility_metadata(
    ctx: _TurnContext,
    decision: ChartVisibilityDecision,
) -> None:
    """Stamp the chart-visibility signals onto the rendered manifest.

    Carries the structured outcome of ``resolve_chart_visibility`` to the
    surfaces that need it: the answer layer (via ``build_answer_brief`` reading
    ``manifest.metadata``) and the ``followups`` SSE event (via
    ``response.manifest.metadata``). When the answer layer is OFF and the user
    asked for an undrawable chart, also append the deterministic fallback line
    so the fact is never silently dropped.
    """

    if ctx.manifest is None or ctx.manifest.status != "rendered":
        return
    if not (decision.chart_unavailable or decision.suggestion_eligible):
        return
    metadata = dict(ctx.manifest.metadata)
    body = ctx.manifest.body
    if decision.chart_unavailable:
        metadata["chart_unavailable"] = True
        # Append the deterministic line UNCONDITIONALLY so the "couldn't draw a
        # chart" fact is never silently lost (#1240, spec §"never silently
        # lost"). This used to be gated on "the stylist won't narrate it" — but
        # in the default `gated` mode the stylist draft is frequently rejected
        # or times out and the turn falls back to THIS deterministic body, which
        # then lacked the note (the R7 client report: a requested chart silently
        # became a bare table). The note now always lives in the deterministic
        # body; when the answer layer serves a draft it REWRITES this body (see
        # `improve_answer`), so it restates the fact in its own voice rather than
        # duplicating the canned line — no double mention. (We deliberately do
        # NOT add it to the caveat-seal markers: the stylist's natural rephrase
        # would not match the exact fragment and would trip a false rejection,
        # the #1720 seal-coupling trap.)
        body = f"{body}\n\n{CHART_UNAVAILABLE_FALLBACK_LINE}"
    if decision.suggestion_eligible:
        metadata["suggested_followups"] = [CHART_SUGGESTION_FOLLOWUP]
    ctx.manifest = ctx.manifest.model_copy(
        update={"body": body, "metadata": metadata}
    )


async def _maybe_apply_answer_layer(
    ctx: _TurnContext,
    turn_span,
    *,
    route: str,
    require_valid_result: bool,
) -> None:
    """Run the optional answer layer after deterministic rendering is valid."""

    if ctx.manifest is None:
        return
    mode = ctx.app_settings.answer_layer_mode
    if mode == "off":
        _set_answer_layer_trace_attributes(
            turn_span,
            AnswerLayerReport(
                mode=mode,
                attempted=False,
                accepted=False,
                status="disabled",
                reason="mode_off",
            ),
        )
        return
    if ctx.manifest.status != "rendered":
        return
    if require_valid_result and (
        ctx.validation is None or not ctx.validation.valid
    ):
        return
    if ctx.manifest.result_type not in allowed_answer_layer_result_types(
        ctx.app_settings.answer_layer_result_types
    ):
        _set_answer_layer_trace_attributes(
            turn_span,
            AnswerLayerReport(
                mode=mode,
                attempted=False,
                accepted=False,
                status="disabled",
                reason="result_type_not_allowed",
            ),
        )
        return

    # Stamp caveat count so has_prose_room can use it as a prose-room signal.
    caveat_count = len(canonical_caveat_fragments(ctx.manifest.body))
    if caveat_count and "answer_layer_caveat_count" not in ctx.manifest.metadata:
        metadata = dict(ctx.manifest.metadata)
        metadata["answer_layer_caveat_count"] = caveat_count
        ctx.manifest = ctx.manifest.model_copy(update={"metadata": metadata})

    allowed_nctq_context: tuple[str, ...] = ()
    if ctx.nctq_context_repo is not None and ctx.turn is not None:
        displayed = ctx.manifest.metadata.get("displayed_row_count")
        try:
            displayed_int = int(displayed) if displayed is not None else None
        except (TypeError, ValueError):
            displayed_int = None
        try:
            if ctx.turn.query_plan is not None:
                allowed_nctq_context = await resolve_nctq_context(
                    ctx.turn.query_plan,
                    manifest=ctx.manifest,
                    displayed_row_count=displayed_int,
                    repo=ctx.nctq_context_repo,
                )
            elif ctx.turn.policy_guidance is not None:
                allowed_nctq_context = await resolve_nctq_context_for_policy_guidance(
                    tuple(ctx.turn.policy_guidance.topic_ids),
                    manifest=ctx.manifest,
                    displayed_row_count=displayed_int,
                    repo=ctx.nctq_context_repo,
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "answer layer nctq context resolution failed: %s",
                type(exc).__name__,
            )
            set_span_attributes(
                turn_span,
                answer_layer_nctq_resolver_error=type(exc).__name__,
            )
            allowed_nctq_context = ()

    body, report = await improve_answer(
        ctx.manifest,
        mode=mode,
        allowed_result_types=ctx.app_settings.answer_layer_result_types,
        route=route,
        result_set=ctx.result,
        allowed_nctq_context=allowed_nctq_context,
    )
    metadata = dict(ctx.manifest.metadata)
    metadata["answer_layer"] = report.to_manifest_metadata()
    ctx.manifest = ctx.manifest.model_copy(
        update={"body": body, "metadata": metadata}
    )
    set_span_attributes(
        turn_span,
        answer_layer_nctq_snippet_count=len(allowed_nctq_context),
    )
    _set_answer_layer_trace_attributes(turn_span, report)


def _set_answer_layer_trace_attributes(
    turn_span,
    report: AnswerLayerReport,
) -> None:
    set_span_attributes(
        turn_span,
        answer_layer_mode=report.mode,
        answer_layer_status=report.status,
        answer_layer_accepted=report.accepted,
        answer_layer_finding_codes=[finding.code for finding in report.findings],
        answer_layer_finding_messages=[
            finding.message for finding in report.findings
        ],
        answer_layer_draft_body=(
            sanitize_logfire_text(report.draft_body)
            if report.draft_body is not None
            else None
        ),
    )


async def _persist_turn_snapshot(
    ctx: _TurnContext,
    store: SessionStore,
    turn_span,
) -> None:
    """Compose the turn snapshot, conversation memory, and save it."""

    assert ctx.session_state is not None
    assert ctx.turn is not None
    assert ctx.planner_run is not None
    session_memory = ctx.session_memory
    snapshot_result = _snapshot_result_under_size_cap(
        ctx.result,
        validation_valid=ctx.validation is not None and ctx.validation.valid,
        max_bytes=ctx.app_settings.conversation_memory_result_max_bytes,
    )
    base_snapshot = TurnSnapshot(
        session_id=ctx.session_state.session_id,
        turn_index=ctx.session_state.turn_count + 1,
        user_message=ctx.request.message,
        assistant_message=ctx.message,
        planner_turn=ctx.turn,
        result=snapshot_result,
        planner_evidence=ctx.planner_run.evidence,
        resolution_report=ctx.resolution_report,
        trace_id=ctx.trace_id,
    )
    result_ref = (
        build_result_memory_ref(
            base_snapshot,
            ctx.result,
            query_context=ctx.query_context,
        )
        if (
            ctx.validation is not None
            and ctx.validation.valid
            and ctx.result is not None
        )
        else None
    )
    result_memory_refs = append_result_memory_ref(
        session_memory.result_refs,
        result_ref,
    )
    policy_guidance_context = (
        build_policy_guidance_context(base_snapshot, ctx.manifest)
        if ctx.manifest is not None
        else None
    )
    conversation_memory = conversation_memory_after_turn(
        session_memory,
        base_snapshot,
        latest_query_context=ctx.query_context,
        latest_policy_guidance_context=policy_guidance_context,
        pending_query_context=ctx.next_pending_context,
        result_refs=result_memory_refs,
    )
    snapshot = base_snapshot.model_copy(
        update={
            "memory": conversation_memory,
        }
    )
    set_span_attributes(
        turn_span,
        snapshot_id=snapshot.snapshot_id,
        assistant_message=sanitize_logfire_text(ctx.message),
        conversation_memory_turn=conversation_memory.latest_turn_index,
        conversation_memory_result_ref_count=len(result_memory_refs),
        snapshot_result_stored=snapshot_result is not None,
        snapshot_result_bytes=(
            _serialized_result_size(ctx.result)
            if (
                ctx.result is not None
                and ctx.validation is not None
                and ctx.validation.valid
            )
            else None
        ),
    )
    with compass_span(
        "compass.session.save",
        trace_id=ctx.trace_id,
        session_id=ctx.session_state.session_id,
        snapshot_id=snapshot.snapshot_id,
    ):
        saved_turn = await store.save_turn(snapshot)
    set_span_attributes(
        turn_span,
        message_ids=saved_turn.message_ids,
        assistant_message_ids=saved_turn.assistant_message_ids,
    )
    ctx.saved_message_ids = list(saved_turn.message_ids)
    ctx.saved_assistant_message_ids = list(saved_turn.assistant_message_ids)
    ctx.saved_session = saved_turn.session
    ctx.snapshot_id = snapshot.snapshot_id


def _snapshot_result_under_size_cap(
    result: ResultSet | None,
    *,
    validation_valid: bool,
    max_bytes: int,
) -> ResultSet | None:
    """Return a result for snapshot storage only when valid and under cap."""

    if not validation_valid or result is None or max_bytes <= 0:
        return None
    if _serialized_result_size(result) > max_bytes:
        return None
    return result


def _serialized_result_size(result: ResultSet) -> int:
    """Return serialized result size in bytes for snapshot storage guards."""

    return len(result.model_dump_json().encode("utf-8"))


def _set_resolution_trace_attributes(
    span,
    resolution_report: CatalogResolutionReport | None,
) -> None:
    if resolution_report is None:
        return
    set_span_attributes(
        span,
        catalog_resolution_status=resolution_report.summary_status(),
        catalog_resolution_methods=resolution_report.summary_methods(),
        catalog_resolution_entities=[
            f"{entity.entity_type}:{entity.status}"
            for entity in resolution_report.entities
        ],
        catalog_resolution_blocker_count=len(resolution_report.blockers),
    )


def _set_recall_trace_attributes(
    span,
    recall_report: RecallReport | None,
) -> None:
    if recall_report is None:
        return
    candidates = recall_report.candidates
    methods = sorted(
        {
            method
            for candidate in candidates
            for method in candidate.source_methods
        }
    )
    set_span_attributes(
        span,
        catalog_recall_batch_count=len(recall_report.batches),
        catalog_recall_candidate_count=len(candidates),
        catalog_recall_methods=methods,
        catalog_recall_entities=sorted(
            {candidate.entity_type for candidate in candidates}
        ),
    )


def _set_planner_evidence_trace_attributes(
    span,
    evidence: PlannerRunEvidence | None,
) -> None:
    """Attach private planner-thinking evidence to the planner span."""

    if evidence is None:
        return
    set_span_attributes(
        span,
        planner_thinking_policy=evidence.thinking_policy,
        planner_thinking_enabled=evidence.thinking_enabled,
        planner_thinking_effort=evidence.thinking_effort,
        planner_thinking_policy_reason=evidence.thinking_policy_reason,
        planner_thinking_provider_fallback=evidence.thinking_provider_error is not None,
        planner_thinking_provider_error=evidence.thinking_provider_error,
        planner_duration_ms=evidence.planner_duration_ms,
        planner_usage_requests=evidence.usage_requests,
        planner_usage_input_tokens=evidence.usage_input_tokens,
        planner_usage_output_tokens=evidence.usage_output_tokens,
        planner_usage_total_tokens=evidence.usage_total_tokens,
        planner_usage_cache_read_tokens=evidence.usage_cache_read_tokens,
        planner_usage_cache_write_tokens=evidence.usage_cache_write_tokens,
    )


