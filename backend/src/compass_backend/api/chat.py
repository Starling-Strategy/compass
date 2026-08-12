"""Chat routes for the fresh Compass API.

This module is intentionally thin: it owns transport concerns only —
route registration, auth dispatch, JSON-vs-SSE response shaping, and the
post-turn L2 verdict fire-and-forget call. The orchestration logic
(planner run, execution, validation, rendering, persistence) lives in
`compass_backend.orchestration.chat.build_chat_response`.

Split from the previously-monolithic 930-LOC chat.py in 2026-05 to fix
the god-route layering violation flagged in the structural-drift audit.
"""

import logging

import asyncpg
from fastapi import APIRouter, HTTPException, Request

from compass_backend.agents import agent_model_settings
from compass_backend.api.auth import (
    ApiKeyAuthRepository,
    ApiKeyAuthenticator,
    authenticate_request,
)
from compass_backend.db._pool import ChatPoolHolder
from compass_backend.api.chat_stream import chat_streaming_response
from compass_backend.api.deps import client_ip
from compass_backend.api.rate_limit import (
    FixedWindowRateLimiter,
    is_rate_limit_exempt,
    rate_limit_identity,
)
from compass_backend.contracts.auth import AuthenticatedUser
from compass_backend.catalog import CatalogRecallService, CompassCatalogToolset
from compass_backend.catalog.build import build_catalog_resolver
from compass_backend.catalog.adjudication import create_catalog_adjudicator_agent
from compass_backend.catalog.reconciliation import CatalogReconciler
from compass_backend.planning.catalog_pipeline import CatalogPlanPipeline
from compass_backend.config import Settings, settings
from compass_backend.contracts import ChatRequest, ChatResponse
from compass_backend.db import ReadOnlyCatalogRepository, ReadOnlyPolicyAnswerRepository
from compass_backend.db.nctq_context import NctqContextRepository
from compass_backend.execution import DeterministicQueryExecutor, QueryExecutor
from compass_backend.orchestration import build_chat_response
from compass_backend.planning import CachedPlannerAgent, PlannerAgent
from compass_backend.policy_guidance.library import get_library
from compass_backend.quality import verdict_pipeline
from compass_backend.session import SessionStore, session_store_from_settings

_logger = logging.getLogger(__name__)

# Header that pa-eval (and any eval harness) sends to suppress the live
# fire-and-forget verdict pipeline on /chat/simple.  pa-eval imports these
# constants so the strings can never drift between caller and handler.
EVAL_MODE_HEADER = "x-compass-eval-mode"
EVAL_MODE_SWEEP = "sweep"


def _accepts_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/event-stream" not in accept


def _live_chat_pool_or_none(
    chat_pool: asyncpg.Pool | ChatPoolHolder,
) -> asyncpg.Pool | None:
    """Return the app's live chat pool when startup has populated it.

    Route-level unit tests often construct an app without running the async
    startup hook, so an empty holder should fall back to the verdict pipeline's
    own small pool rather than raising during fire-and-forget scheduling.
    """

    if isinstance(chat_pool, ChatPoolHolder):
        return chat_pool.pool
    return chat_pool


def create_chat_router(
    *,
    planner_agent: PlannerAgent | None = None,
    session_store: SessionStore | None = None,
    query_executor: QueryExecutor | None = None,
    api_key_auth_repository: ApiKeyAuthenticator | None = None,
    app_settings: Settings = settings,
    chat_pool: asyncpg.Pool | ChatPoolHolder | None = None,
) -> APIRouter:
    """Create the MVP chat router.

    ``chat_pool`` is the shared asyncpg pool wired by
    ``create_app_from_settings``. Every repository this router builds
    borrows connections from it. When called without a ``chat_pool``
    (test path), an empty ``ChatPoolHolder()`` is used — repos still
    construct cleanly, but ``resolve_pool`` raises if a route handler
    actually tries to acquire a connection before the holder is
    populated, surfacing the misconfiguration loudly.
    """

    if chat_pool is None:
        chat_pool = ChatPoolHolder()
    router = APIRouter(prefix="/api/v1", tags=["Chat"])
    store = session_store or session_store_from_settings(
        app_settings, pool=chat_pool
    )
    planner = planner_agent or CachedPlannerAgent(
        agent_model_settings.planner_model,
        library_provider=get_library,
    )
    authenticator = api_key_auth_repository or ApiKeyAuthRepository(
        app_settings, pool=chat_pool
    )
    catalog_repository = ReadOnlyCatalogRepository(app_settings, pool=chat_pool)
    catalog_resolver = build_catalog_resolver(
        catalog_repository,
        settings=app_settings,
    )
    catalog_recall_service = (
        CatalogRecallService(catalog_repository)
        if app_settings.catalog_recall_shadow_enabled
        else None
    )
    # #1248: the Compass catalog toolset is always attached to the planner so
    # it can look things up before it drafts. No flag — one clean path.
    planner_toolsets = [
        CompassCatalogToolset(CatalogRecallService(catalog_repository))
    ]
    executor = query_executor or DeterministicQueryExecutor(
        ReadOnlyPolicyAnswerRepository(app_settings, pool=chat_pool),
        catalog_resolver=catalog_resolver,
        current_academic_year=app_settings.current_academic_year,
        default_limit=app_settings.selection_default_largest_limit,
    )
    nctq_context_repo = NctqContextRepository(app_settings, pool=chat_pool)
    catalog_reconciler = CatalogReconciler(catalog_resolver)
    # Catalog-resolution-unification pipeline (#1248): the always-on verifier.
    # The planner output validator runs the unified reconcile → adjudicate →
    # finalize chain, replacing the draft's query_plan with the slot-corrected
    # plan and turning FinalizerBlockers into ModelRetry hints carrying real
    # catalog candidates. The adjudicator is lazily constructed on first
    # ambiguity (matches the planner agent's per-request lifecycle and keeps
    # app startup free of the gateway-key requirement in tests).
    catalog_pipeline = CatalogPlanPipeline(
        catalog_reconciler,
        adjudicator_factory=create_catalog_adjudicator_agent,
    )

    # Per-router (per-app) limiter: production builds one app, so all requests
    # share one limiter and limiting is real; route-level tests build a fresh
    # app per test, so counters never leak across tests. See issue #1079.
    chat_rate_limiter = (
        FixedWindowRateLimiter(
            max_requests=app_settings.chat_rate_limit_per_minute
        )
        if app_settings.chat_rate_limit_enabled
        else None
    )

    def _enforce_chat_rate_limit(
        request: Request, auth_user: AuthenticatedUser | None
    ) -> None:
        """Raise 429 when the caller's identity has exceeded its window."""

        if chat_rate_limiter is None or is_rate_limit_exempt(auth_user):
            return
        decision = chat_rate_limiter.check(
            rate_limit_identity(
                auth_user=auth_user,
                client_ip_value=client_ip(request),
            )
        )
        if not decision.allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down and retry shortly.",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

    @router.post("/chat")
    async def chat(
        request: Request,
        chat_request: ChatRequest,
    ):
        auth_user = await authenticate_request(
            request,
            source=app_settings,
            authenticator=authenticator,
        )
        _enforce_chat_rate_limit(request, auth_user)
        accepts_json = _accepts_json(request)
        # Pre-flight length check so over-length requests still return
        # 422 on both JSON and SSE paths (an SSE branch can't reshape
        # the HTTP status once the EventSourceResponse has opened).
        if (
            len(chat_request.message)
            > app_settings.chat_message_max_chars
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Message exceeds "
                    f"{app_settings.chat_message_max_chars} characters"
                ),
            )
        if not accepts_json:
            return chat_streaming_response(
                chat_request=chat_request,
                planner_agent=planner,
                store=store,
                executor=executor,
                app_settings=app_settings,
                auth_user=auth_user,
                catalog_recall_service=catalog_recall_service,
                planner_toolsets=planner_toolsets,
                chat_pool=_live_chat_pool_or_none(chat_pool),
                fire_and_forget=verdict_pipeline.fire_and_forget,
                nctq_context_repo=nctq_context_repo,
                catalog_pipeline=catalog_pipeline,
            )

        # JSON branch: keep original behaviour — await the orchestrator,
        # raise a structured 500 with trace_id on failure, then fire the
        # verdict pipeline.
        try:
            response = await build_chat_response(
                chat_request,
                planner_agent=planner,
                store=store,
                executor=executor,
                app_settings=app_settings,
                auth_user=auth_user,
                catalog_recall_service=catalog_recall_service,
                planner_toolsets=planner_toolsets,
                nctq_context_repo=nctq_context_repo,
                catalog_pipeline=catalog_pipeline,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise _build_500_for_exc(exc) from exc

        verdict_pipeline.fire_and_forget(
            response,
            pool=_live_chat_pool_or_none(chat_pool),
            source=app_settings,
            user_message=chat_request.message,
        )
        return response

    @router.post("/chat/simple", response_model=ChatResponse)
    async def chat_simple(
        request: Request,
        chat_request: ChatRequest,
    ) -> ChatResponse:
        auth_user = await authenticate_request(
            request,
            source=app_settings,
            authenticator=authenticator,
        )
        _enforce_chat_rate_limit(request, auth_user)
        try:
            response = await build_chat_response(
                chat_request,
                planner_agent=planner,
                store=store,
                executor=executor,
                app_settings=app_settings,
                auth_user=auth_user,
                catalog_recall_service=catalog_recall_service,
                planner_toolsets=planner_toolsets,
                nctq_context_repo=nctq_context_repo,
                catalog_pipeline=catalog_pipeline,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise _build_500_for_exc(exc) from exc

        # L2 fire-and-forget: classify criteria + run evaluators + write verdicts.
        # Errors logged + swallowed; chat never blocks (per CLAUDE.md invariant).
        # When the caller sends X-Compass-Eval-Mode: sweep the live pipeline is
        # suppressed — pa-eval runs its own evaluate_and_write pass after the
        # call returns, so double-evaluation and duplicate DB writes are avoided.
        eval_mode = request.headers.get(EVAL_MODE_HEADER, "").lower()
        if eval_mode != EVAL_MODE_SWEEP:
            verdict_pipeline.fire_and_forget(
                response,
                pool=_live_chat_pool_or_none(chat_pool),
                source=app_settings,
                user_message=chat_request.message,
            )
        return response

    return router


def _build_500_for_exc(exc: Exception) -> HTTPException:
    """Shape an HTTPException(500) carrying the trace_id the orchestrator
    attached to the bubbled exception.

    The orchestrator attaches `trace_id` to `exc` before re-raising so
    transport doesn't need to re-enter a span context here. If for some
    reason the attribute is missing (e.g. the exception was raised before
    the orchestrator ever entered the turn span), trace_id falls through
    as `None` — still surfaces as a structured field, just empty.
    """
    trace_id = getattr(exc, "trace_id", None)
    # Record the full exception + traceback BEFORE sanitizing it into the opaque
    # client 500. Without this the 500 path drops the frame and a crash is only
    # diagnosable from its error string — the multi-cycle #1772 hunt. exc_info=exc
    # attaches the traceback even if this is ever called outside an `except`;
    # trace_id binds the log to the turn span in Logfire.
    _logger.error(
        "compass.chat.turn_failed",
        exc_info=exc,
        extra={"trace_id": trace_id, "error_type": type(exc).__name__},
    )
    detail = {
        "trace_id": trace_id,
        "error_type": type(exc).__name__,
        "error": "internal_error",
    }
    return HTTPException(status_code=500, detail=detail)
