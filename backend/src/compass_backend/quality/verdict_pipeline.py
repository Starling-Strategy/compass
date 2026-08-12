"""Fire-and-forget verdict pipeline — orchestrates classify → evaluate → write per turn.

The L2 trigger calls fire_and_forget() after build_chat_response() returns.
The pipeline never blocks the chat response: every exception is logged and
swallowed per the CLAUDE.md "L2 never blocks chat" invariant.

Pool management: the pipeline creates a singleton asyncpg.Pool on first call,
using the same Settings the rest of the backend reads. Tests may inject a pool
via evaluate_and_write(pool=...) to skip the singleton.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from typing import Any, NamedTuple, Protocol, runtime_checkable

import asyncpg
from pydantic import BaseModel

# Bound the concurrent judge_prompt LLM calls per turn. Judges are mutually
# independent (same ctx, no cross-verdict reads) so fanning them out via
# asyncio.gather is safe. The cap protects the shared Anthropic gateway key
# from running into per-org RPM ceilings under sweep load (a *concurrency* cap,
# not a request-rate limiter — see RateLimiter below for rate pacing).
#
# Env-tunable so a sweep can dial judge concurrency down independently of the
# live path. COMPASS_MAX_CONCURRENT_JUDGES is the documented name;
# COMPASS_VERDICT_FANOUT stays as a back-compat alias.
MAX_CONCURRENT_JUDGES = int(
    os.environ.get("COMPASS_MAX_CONCURRENT_JUDGES")
    or os.environ.get("COMPASS_VERDICT_FANOUT")
    or "8"
)

# check_types whose evaluators make a gateway (LLM) call. Only these consume a
# rate-limiter slot when one is supplied — deterministic / span_assertion run
# locally and must never pace.
_GATEWAY_CALLING_CHECK_TYPES: frozenset[str] = frozenset({"judge_prompt", "scenario_fit"})


@runtime_checkable
class RateLimiter(Protocol):
    """Structural protocol for the optional sweep-side gateway rate limiter.

    Defined by shape so the backend never imports from ``scripts/pa_eval``: the
    sweep runner's ``GatewayRateLimiter`` satisfies it. ``acquire`` blocks until
    the caller may issue its next gateway request. The live L1 path passes
    ``None`` (see ``evaluate_and_write``), so user traffic is never paced.
    """

    async def acquire(self) -> None: ...

from compass_backend.observability import compass_span, set_span_attributes
from compass_backend.artifacts.citations import CitationRef
from compass_backend.config import Settings, settings as global_settings
from compass_backend.contracts.chat import ChatResponse
from compass_backend.contracts.rendering import ResponseManifest
from compass_backend.db.criteria import ReadOnlyCriteriaRepository
from compass_backend.db.rows import ScenarioExpectation
from compass_backend.db.scenarios_v2 import ReadOnlyScenariosV2Repository
from compass_backend.quality._evaluator_context import (
    ArtifactSnapshot,
    ArtifactTableRow,
    CompassEvaluatorContext,
    SelectionExpected,
)
from compass_backend.quality.classifier import select_criteria_for_turn
from compass_backend.quality.criteria import (
    COMPASS_CRITERIA_SET_VERSION,
    CriterionRecord,
    VerdictRecord,
    criterion_record_to_evaluator,
)
from compass_backend.quality.evidence import build_evidence_envelope
from compass_backend.quality.verdict_writer import write_one

logger = logging.getLogger(__name__)


# Strong references to in-flight fire_and_forget tasks. asyncio.get_running_loop()
# only holds *weak* refs to user tasks (see asyncio.Task docs), so under memory
# pressure or aggressive GC a fire-and-forget task can vanish mid-flight and
# silently truncate L2 verdict writes. Stashing the Task here keeps it strongly
# referenced; the discard done-callback releases it as soon as it completes.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


# ─── check_type filter resolver ───────────────────────────────────────────────


def _resolve_check_type_filter() -> tuple[str, ...] | None:
    """Return tuple of allowed check_type values, or None for 'all'.

    Reads COMPASS_VERDICT_CHECK_TYPES env var. Empty / unset = None (all types).
    Comma-separated subset of 'deterministic,judge_prompt,span_assertion'.
    Unknown values are silently dropped (logged at debug level).

    This is a convenience reader for callers that want the env-var default
    without going through the CLI. The pa-eval CLI uses its own
    ``_resolve_check_types`` helper (scripts/pa_eval/cli.py) which raises
    ``typer.BadParameter`` for unknown values instead of silently dropping them.
    """
    raw = os.environ.get("COMPASS_VERDICT_CHECK_TYPES", "").strip()
    if not raw:
        return None  # all types
    valid = {"deterministic", "judge_prompt", "span_assertion", "scenario_fit"}
    requested = {v.strip() for v in raw.split(",") if v.strip()}
    unknown = requested - valid
    if unknown:
        logger.debug(
            "_resolve_check_type_filter: dropping unknown check_type values %s; valid=%s",
            sorted(unknown),
            sorted(valid),
        )
    filtered = tuple(sorted(requested & valid))
    return filtered if filtered else None


# ─── Sweep-run metadata model ─────────────────────────────────────────────────


class EvalContext(BaseModel):
    """Sweep-run metadata to bake into VerdictRecords at insert time.

    Passed by pa-eval's runner when calling evaluate_and_write directly (the
    sweep path). Absent on the live /chat/simple path (fire_and_forget never
    receives an EvalContext). When present, sweep_run_id, scenario_id,
    case_id, and step_index are written directly onto every VerdictRecord
    produced for this turn — eliminating the post-write UPDATE stamp the
    runner previously issued (runner.py:210-248).

    step_index defaults to 0 for single-step scenarios so callers that don't
    track step granularity can omit it.
    """

    sweep_run_id: str
    scenario_id: int
    case_id: int
    step_index: int | None = 0


# ─── Result type returned by evaluate_and_write ───────────────────────────────


class EvaluateWriteResult(NamedTuple):
    """Typed return value from :func:`evaluate_and_write`.

    Carries the written verdict UUIDs alongside an in-memory tally of
    outcomes so callers (e.g. the pa-eval sweep runner) can compute fail
    rates without a follow-up DB query.

    Attributes:
        verdict_ids:    UUIDs of rows actually written to ``compass.verdicts``.
        outcome_counts: Counter-style dict mapping outcome label → count for
                        every verdict evaluated in this call (including those
                        that were not written due to a ``write_one`` failure).
                        Keys: ``"pass"``, ``"fail"``, ``"skip"``, ``"error"``.
    """

    verdict_ids: list[str]
    outcome_counts: dict[str, int]


# ─── Singleton pool for the verdict pipeline ─────────────────────────────────
#
# The pool is created lazily on first evaluate_and_write() call and reused for
# the lifetime of the process. Tests inject their own pool to avoid creating one.

_PIPELINE_POOL: asyncpg.Pool | None = None
_POOL_LOCK = asyncio.Lock()


async def _get_or_create_pool(source: Settings = global_settings) -> asyncpg.Pool:
    """Return the singleton asyncpg.Pool, creating it on first call."""
    global _PIPELINE_POOL
    async with _POOL_LOCK:
        if _PIPELINE_POOL is not None:
            return _PIPELINE_POOL
        try:
            _PIPELINE_POOL = await asyncpg.create_pool(
                host=source.pg_host,
                port=source.pg_port,
                database=source.pg_database,
                user=source.pg_user,
                password=source.pg_password.get_secret_value(),
                min_size=1,
                max_size=3,
                command_timeout=source.pg_command_timeout,
                # Issue #1122: recycle idle connections before the Tailscale
                # NAT path reclaims them so the verdict-write path never
                # acquires a dead connection.
                max_inactive_connection_lifetime=source.pg_pool_max_inactive_lifetime,
                server_settings={"search_path": source.pg_schema},
            )
            logger.info(
                "verdict_pipeline: pool created (min=1 max=3 max_inactive=%.1fs)",
                source.pg_pool_max_inactive_lifetime,
            )
        except Exception as exc:
            logger.warning(
                "verdict_pipeline: pool creation failed — verdicts will not be written: %s",
                exc,
                exc_info=True,
            )
            raise
    return _PIPELINE_POOL


# ─── Context builder ──────────────────────────────────────────────────────────


def _policy_guidance_citations_from_manifest(
    manifest: ResponseManifest | None,
) -> list[CitationRef]:
    """Convert manifest-side policy_guidance citations into `CitationRef`s.

    The renderer's per-turn citation index (built by
    `rendering/policy_guidance.py::_build_citation_index`) lives on
    `manifest.metadata['citations']` as a list of dicts. Each entry carries
    an int `id` (the user-facing marker number) plus `title`, `url`,
    `stable_id`, etc. This helper lifts the subset of fields that
    `CitationRef` cares about so the same field validators all citation
    checks read — `artifact_snapshot.citations` — covers every render path.

    Malformed entries are silently dropped: the verdict pipeline runs
    fire-and-forget after the chat response, so raising here would leak
    a noisy stack trace for what the existing `validate_policy_guidance_manifest`
    pre-check already gates at render time.
    """
    if manifest is None:
        return []
    raw = manifest.metadata.get("citations")
    if not isinstance(raw, list):
        return []
    converted: list[CitationRef] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        marker = entry.get("id")
        title = entry.get("title")
        if not isinstance(marker, int) or marker < 1:
            continue
        if not isinstance(title, str) or not title:
            continue
        url_raw = entry.get("url")
        url = url_raw if isinstance(url_raw, str) and url_raw else None
        converted.append(CitationRef(marker=marker, title=title, url=url))
    return converted


def _build_context(
    response: ChatResponse,
    *,
    triggered_by: str = "user_turn",
    user_message: str = "",
    eval_context: EvalContext | None = None,
    expected_output: ScenarioExpectation | None = None,
    case_transcript: list[dict[str, str]] | None = None,
) -> CompassEvaluatorContext:
    """Extract a CompassEvaluatorContext from a ChatResponse.

    Reads session_id, turn_index, intent, artifact_snapshot, answer, and
    trace_id from the live response. No DB calls here — purely in-process.

    `user_message` is the raw prompt the user sent. It is passed through
    from the caller because ChatResponse does not echo the request body,
    and validators like `respects_user_limit` need the verbatim text to
    detect user-stated structural constraints (e.g. "top 5") that the
    planner's paraphrased intent drops.
    """
    session_id = response.session.session_id
    # turn_count reflects the count *after* this turn was saved
    turn_index = response.session.turn_count

    # Derive intent from the planner turn: combine operation + sort direction so
    # the "sort" prefilter fires on ranking turns (operation='rank' with a sort step).
    intent: str | None = None
    if response.turn.query_plan is not None:
        qp = response.turn.query_plan
        operation = qp.operation or ""
        temporal_intent = (qp.temporal.intent or "") if qp.temporal else ""
        # Map ranking operations to "sort" so the sort_descending prefilter fires.
        # The prefilter checks for "sort" in intent; rank/sort operations are the
        # same semantic class for eval purposes.
        if operation in ("rank", "sort"):
            operation = "sort"
        # Combine operation + temporal intent for richer signal
        intent_parts = [p for p in [operation, temporal_intent] if p]
        intent = " ".join(intent_parts) or None

    # Build a typed snapshot of the artifacts present in this turn.
    snapshot = ArtifactSnapshot()
    if response.result is not None:
        result = response.result
        if result.rows:
            # Compact table for L1 rules and L2 validators.
            # `state` was added for the L2 selection_set_matches_request validator
            # (Phase E2) so it can verify rows live in the requested state set.
            snapshot.table = [
                ArtifactTableRow(
                    district_name=row.district_name,
                    state=row.state,
                    display_value=row.display_value,
                    sort_display_value=getattr(row, "sort_display_value", None),
                    # Long-form multi-metric tables emit one row per
                    # district-metric cell; carry the cell's metric so the
                    # sort_descending validator can isolate the ranked column
                    # instead of scanning an interleaved row sequence (#1460).
                    metric_name=getattr(row, "metric_name", None),
                )
                for row in result.rows[:50]  # cap at 50 for prompt safety
            ]
            # Name of the metric the rows are ordered by, set only by the
            # multi-metric ranked-lookup path (#1220). Lets the sort_descending
            # validator filter `table` to the ranked column (#1460). None for
            # single-metric and non-ranked results.
            snapshot.ranked_by_metric_name = getattr(
                result, "ranked_by_metric_name", None
            )
        if result.citations:
            snapshot.citations = list(result.citations)

    # Policy-guidance AND publication turns emit `[N]` markers in the rendered
    # body but produce no `ResultSet` — so `result.citations` is empty for them.
    # Both renderers store the per-turn citation index on
    # `manifest.metadata['citations']` (see
    # `rendering/policy_guidance.py::_build_citation_index` and
    # `rendering/publication.py::_build_citations`). Mirror it into
    # `snapshot.citations` so the deterministic `citation_format` validator and
    # the route-scoped `sources_block_is_deduplicated` validator (which reads
    # `artifact_snapshot.citations` when `ctx.route` is policy_guidance or
    # publication) see every marker the rendered body actually references — not
    # just the ones attached to data rows. The helper is route-agnostic; it
    # lifts whatever `metadata['citations']` the active renderer produced.
    manifest_citations = _policy_guidance_citations_from_manifest(response.manifest)
    if manifest_citations:
        existing_markers = {citation.marker for citation in snapshot.citations}
        snapshot.citations.extend(
            citation
            for citation in manifest_citations
            if citation.marker not in existing_markers
        )

    # Expose the planner's requested selection so the L2
    # selection_set_matches_request validator can compare requested vs returned.
    # Omitted when this turn took a route without a structured QueryPlan
    # (clarification, policy_guidance, errors).
    if response.turn.query_plan is not None:
        sel = response.turn.query_plan.selection
        snapshot.selection_expected = SelectionExpected(
            scope=sel.scope,
            states=list(sel.states),
            districts=list(sel.districts),
        )

    triggered_literal = triggered_by if triggered_by in ("user_turn", "sweep_run", "manual_replay") else "user_turn"

    # Memory snapshot for the M3 `_rule_preserves_prior_context` prefilter:
    # it needs to know there was a prior result_ref (a count/lookup/rank
    # outcome the user can plausibly refer back to) AND that this turn is a
    # follow-up. Both come from ConversationMemory on the persisted session.
    memory = getattr(response.session, "memory", None)
    prior_result_ref_count = (
        len(memory.result_refs) if memory is not None else 0
    )
    recent_routes = (
        list(memory.recent_routes) if memory is not None else []
    )

    return CompassEvaluatorContext(
        session_id=session_id,
        turn_index=turn_index,
        # Sweep-run metadata: populated from EvalContext when provided (pa-eval
        # sweep path), None on the live /chat/simple path (fire_and_forget).
        step_index=eval_context.step_index if eval_context is not None else None,
        case_id=eval_context.case_id if eval_context is not None else None,
        scenario_id=eval_context.scenario_id if eval_context is not None else None,
        sweep_run_id=eval_context.sweep_run_id if eval_context is not None else None,
        artifact_snapshot=snapshot,
        answer_text=response.message,
        intent=intent,
        user_message=user_message,
        # plan + result feed the dimension-bridge validators in
        # validators/registry.py (dim_*). They forward both to
        # quality.validate_result() and filter findings by dimension label.
        plan=response.turn.query_plan,
        result=response.result,
        route=response.turn.route,
        clarification_rescue_origin=(
            response.turn.clarification.rescue_origin
            if response.turn.clarification is not None
            else False
        ),
        trace_id=response.trace_id,
        triggered_by=triggered_literal,  # type: ignore[arg-type]
        prior_result_ref_count=prior_result_ref_count,
        recent_routes=recent_routes,
        expected_output=expected_output,
        case_transcript=list(case_transcript or []),
    )


# ─── Main evaluation orchestrator ─────────────────────────────────────────────


async def evaluate_one_criterion(
    *,
    ctx: CompassEvaluatorContext,
    criterion: CriterionRecord,
    rate_limiter: RateLimiter | None = None,
) -> VerdictRecord | None:
    """Evaluate ONE criterion against ONE context.

    The shared core that evaluate_and_write's parallel fan-out and
    scripts/pa_eval/replay_criterion.replay_one_verdict both call. Handles:

    - applicable_routes gating (criterion opts out of routes where it's
      never meaningful → emits a `not applicable: ...` error verdict).
    - Evaluator dispatch via `criterion_record_to_evaluator` so any
      registered check_type (deterministic, judge_prompt, span_assertion,
      scenario_fit) works without callers knowing the concrete class.
    - Optional gateway rate limiting: when ``rate_limiter`` is supplied (the
      pa-eval sweep path) and this criterion makes a gateway call
      (``judge_prompt`` / ``scenario_fit``), ``acquire`` is awaited *after* the
      route-skip pre-check and *before* the evaluator runs, so the combined
      (chat + judge) gateway rate stays under the ceiling (issue #1277). The
      live L1 path passes ``None`` → no pacing of user traffic. Route-skipped
      judges and local-only check_types never acquire (they make no gateway
      call).
    - Exception trapping → returns a structured `outcome="error"` verdict
      so an evaluator bug never bubbles up to the caller.
    - Telemetry span around the evaluator call (criterion_code,
      criterion_id, check_type, outcome attributes).

    Returns None ONLY when even the error VerdictRecord construction
    fails — a sentinel for irrecoverable failures the caller treats as
    a counted error without writing a row. Concurrency control (the
    judge semaphore in evaluate_and_write) and writes are NOT this
    function's concern.
    """
    # ── applicable_routes pre-check ──────────────────────────────────────
    # A criterion can declare `payload.applicable_routes` to opt out of
    # routes where it's never meaningful. Returns a
    # `outcome="error", reason="not applicable: ..."` verdict that the
    # Scorecard's `_is_excluded_from_score` filter drops cleanly.
    applicable_routes = criterion.payload.applicable_routes
    if (
        applicable_routes
        and ctx.route is not None
        and ctx.route not in applicable_routes
    ):
        return VerdictRecord(
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            step_index=ctx.step_index,
            case_id=ctx.case_id,
            scenario_id=ctx.scenario_id,
            criterion_id=criterion.id,
            criterion_version_hash=criterion.version_hash,
            criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
            judge_source=criterion.check_type,  # type: ignore[arg-type]
            scope="turn",
            references_turn_indices=list(ctx.references_turn_indices),
            outcome="error",
            reason=(
                f"not applicable: route is {ctx.route!r}, "
                f"criterion applies only to {sorted(applicable_routes)!r}"
            ),
            evidence=build_evidence_envelope(
                ctx,
                check_specific={
                    "skip_reason": "route_not_applicable",
                    "ctx_route": ctx.route,
                    "applicable_routes": list(applicable_routes),
                },
            ),
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
            sweep_run_id=ctx.sweep_run_id,
        )

    # Pace gateway-calling criteria against the sweep ceiling *after* the
    # route-skip pre-check (a skipped judge makes no gateway call, so it must
    # not consume a rate slot) and *before* the evaluator's LLM call. None on
    # the live L1 path → user traffic is never paced (issue #1277).
    if rate_limiter is not None and criterion.check_type in _GATEWAY_CALLING_CHECK_TYPES:
        await rate_limiter.acquire()

    evaluator = criterion_record_to_evaluator(criterion)

    with compass_span(
        "compass.quality.evaluate_criterion",
        criterion_code=criterion.criterion_code,
        criterion_id=str(criterion.id),
        check_type=criterion.check_type,
    ) as criterion_span:
        try:
            verdict = await evaluator.evaluate(ctx)
            set_span_attributes(criterion_span, outcome=verdict.outcome)
            return verdict
        except Exception as exc:
            logger.warning(
                "verdict_pipeline: evaluator raised for criterion=%s session=%s: %s",
                criterion.criterion_code,
                ctx.session_id,
                exc,
                exc_info=True,
            )
            set_span_attributes(
                criterion_span,
                outcome="error",
                error_type=type(exc).__name__,
            )
            try:
                return VerdictRecord(
                    session_id=ctx.session_id,
                    turn_index=ctx.turn_index,
                    step_index=ctx.step_index,
                    case_id=ctx.case_id,
                    scenario_id=ctx.scenario_id,
                    criterion_id=criterion.id,
                    criterion_version_hash=criterion.version_hash,
                    criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
                    judge_source=criterion.check_type,  # type: ignore[arg-type]
                    scope="turn",
                    outcome="error",
                    reason=f"evaluator raised: {type(exc).__name__}: {exc}",
                    evidence=build_evidence_envelope(
                        ctx,
                        check_specific={
                            "criterion_code": criterion.criterion_code,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        },
                    ),
                    trace_id=ctx.trace_id,
                    triggered_by=ctx.triggered_by,
                    sweep_run_id=ctx.sweep_run_id,
                )
            except Exception:
                # Truly irrecoverable — sentinel signals the caller to
                # treat as a counted error without writing a row.
                return None


async def evaluate_and_write(
    response: ChatResponse,
    *,
    pool: asyncpg.Pool | None = None,
    source: Settings = global_settings,
    triggered_by: str = "user_turn",
    use_ai_classifier: bool = True,
    user_message: str = "",
    eval_context: EvalContext | None = None,
    check_types: tuple[str, ...] | None = None,
    expected_output: ScenarioExpectation | None = None,
    case_transcript: list[dict[str, str]] | None = None,
    rate_limiter: RateLimiter | None = None,
) -> EvaluateWriteResult:
    """Classify criteria, run evaluators, and write verdicts for one turn.

    Args:
        response:         The ChatResponse returned by build_chat_response().
        pool:             asyncpg.Pool to use. When None, the singleton pool is
                          created on demand from source.
        source:           Runtime settings whose database/schema own the verdict
                          tables for this app instance.
        triggered_by:     Provenance label stored on each VerdictRecord.
        use_ai_classifier: Pass False to skip Safeguard 4 (e.g. in tests).
        user_message:     Raw user prompt that produced this response. Needed
                          by validators that look for verbatim user-stated
                          constraints (e.g. respects_user_limit reading
                          "top 5" from the original prompt). Defaults to ""
                          for backward compatibility; callers should pass
                          `request.message` when available.
        check_types:      Optional filter: only evaluate criteria whose
                          check_type is in this tuple. When None (default),
                          all check_types are evaluated (today's behavior).
                          The live fire_and_forget path always passes None;
                          pa-eval sweeps pass the resolved CLI/env value.
        expected_output:  Arc-level expectation from compass.cases.expected_output,
                          passed by pa-eval's runner. None on the live user_turn
                          path (fire_and_forget never sets this). Forwarded to
                          CompassEvaluatorContext so the scenario_fit evaluator
                          (Task 5.3) can compare actual answers against expected
                          behaviour and per-step expectations.
        case_transcript: Full user/assistant transcript for the current pa-eval
                          case trial. Only supplied for case-level scenario_fit
                          evaluation.
        rate_limiter:     Optional :class:`RateLimiter` (pa-eval sweep path).
                          When supplied, every gateway-calling criterion
                          (``judge_prompt`` / ``scenario_fit``) acquires it
                          before its LLM call so the combined (chat + judge)
                          gateway rate stays under the ceiling (issue #1277).
                          ``None`` on the live ``fire_and_forget`` L1 path → user
                          traffic is never paced. The ``MAX_CONCURRENT_JUDGES``
                          semaphore still caps in-flight judges; the limiter
                          bounds their *rate*.

    Returns:
        :class:`EvaluateWriteResult` carrying the written verdict UUIDs and an
        in-memory outcome tally.  ``verdict_ids`` is empty on total failure.
    """
    # Build context from the live response (no DB / pool needed)
    ctx = _build_context(
        response,
        triggered_by=triggered_by,
        user_message=user_message,
        eval_context=eval_context,
        expected_output=expected_output,
        case_transcript=case_transcript,
    )

    with compass_span(
        "compass.quality.verdict_pipeline",
        session_id=ctx.session_id,
        turn_index=ctx.turn_index,
        trace_id=ctx.trace_id,
        triggered_by=ctx.triggered_by,
    ) as pipeline_span:
        # Resolve pool — create singleton if not injected
        if pool is None:
            try:
                pool = await _get_or_create_pool(source)
            except Exception:
                # Pool creation already logged; bail silently
                set_span_attributes(pipeline_span, outcome="no_pool")
                return EvaluateWriteResult(verdict_ids=[], outcome_counts={})

        # Select criteria (all 4 safeguards)
        try:
            selected: list[CriterionRecord] = await select_criteria_for_turn(
                ctx,
                pool=pool,
                source=source,
                use_ai_classifier=use_ai_classifier,
            )
        except Exception as exc:
            logger.warning(
                "verdict_pipeline: criterion selection failed for session=%s turn=%s: %s",
                ctx.session_id,
                ctx.turn_index,
                exc,
                exc_info=True,
            )
            set_span_attributes(
                pipeline_span,
                outcome="selection_error",
                error_type=type(exc).__name__,
            )
            return EvaluateWriteResult(verdict_ids=[], outcome_counts={})

        # scenario_fit is opt-in and case-scoped. It is not mandatory-global, so
        # when an L3 lane explicitly asks for it we add the active scenario_fit
        # criterion after ordinary selection. Live user_turn calls never pass an
        # expected_output and therefore cannot enroll it.
        if check_types and "scenario_fit" in check_types and expected_output is not None:
            try:
                active = await ReadOnlyCriteriaRepository(pool, source=source).load_active()
                selected_by_code = {c.criterion_code: c for c in selected}
                for criterion in active:
                    if (
                        criterion.check_type == "scenario_fit"
                        and criterion.criterion_code not in selected_by_code
                    ):
                        selected.append(criterion)
                        selected_by_code[criterion.criterion_code] = criterion
            except Exception as exc:
                logger.warning(
                    "verdict_pipeline: scenario_fit criterion enrollment failed "
                    "for session=%s turn=%s: %s",
                    ctx.session_id,
                    ctx.turn_index,
                    exc,
                    exc_info=True,
                )

            # Hydrate case metadata so the scenario_fit evaluator can apply its
            # applicability gate (structural-diagnostic FOUNDATION cases record a
            # not_applicable verdict instead of being judged on contracts the
            # answer-text judge cannot see — #939). The applicability *decision*
            # lives in the evaluator; this is just the data hand-off. A failed
            # load leaves case_metadata empty (judge runs as before).
            if ctx.case_id is not None and not ctx.case_metadata:
                try:
                    ctx.case_metadata = await ReadOnlyScenariosV2Repository(
                        pool
                    ).load_case_metadata_by_id(ctx.case_id)
                except Exception as exc:
                    logger.warning(
                        "verdict_pipeline: case metadata load failed for "
                        "case_id=%s session=%s turn=%s: %s",
                        ctx.case_id,
                        ctx.session_id,
                        ctx.turn_index,
                        exc,
                        exc_info=True,
                    )

        if not selected:
            logger.debug(
                "verdict_pipeline: no criteria selected for session=%s turn=%s",
                ctx.session_id,
                ctx.turn_index,
            )
            set_span_attributes(
                pipeline_span,
                outcome="no_criteria_selected",
                selected_count=0,
            )
            return EvaluateWriteResult(verdict_ids=[], outcome_counts={})

        # ── check_types filter ───────────────────────────────────────────────
        # When check_types is set (pa-eval sweep lanes), drop criteria whose
        # check_type is not in the allowed set. The live fire_and_forget path
        # always passes check_types=None so it is unaffected.
        if check_types:
            allowed_types: set[str] = set(check_types)
            selected = [c for c in selected if c.check_type in allowed_types]
            if not selected:
                logger.debug(
                    "verdict_pipeline: all criteria filtered out by check_types=%s "
                    "for session=%s turn=%s",
                    check_types,
                    ctx.session_id,
                    ctx.turn_index,
                )
                set_span_attributes(
                    pipeline_span,
                    outcome="no_criteria_selected",
                    selected_count=0,
                    check_types_filter=",".join(sorted(check_types)),
                )
                return EvaluateWriteResult(verdict_ids=[], outcome_counts={})

        # ── Parallel criteria evaluation ─────────────────────────────────────
        # Judges are mutually independent (same ctx, no cross-verdict reads),
        # so the only thing keeping them serial was the loop itself. Fan them
        # out under a bounded Semaphore to cap concurrent gateway calls.
        #
        # Per-task contract: return a VerdictRecord or None (sentinel for
        # "irrecoverable construction failure" — preserves the original
        # loop's `continue` semantics, where verdict_outcomes["error"] is
        # bumped and no verdict row is written).
        #
        # The applicable_routes shortcut stays inside the task so judge_prompt
        # criteria still avoid the LLM call on inapplicable turns.
        sem = asyncio.Semaphore(MAX_CONCURRENT_JUDGES)

        async def _evaluate_one(crit: CriterionRecord) -> VerdictRecord | None:
            async with sem:
                return await evaluate_one_criterion(
                    ctx=ctx, criterion=crit, rate_limiter=rate_limiter
                )

        # gather preserves submission order, so written_ids stays aligned with `selected`.
        # _evaluate_one catches its own exceptions, so return_exceptions=False is safe.
        results: list[VerdictRecord | None] = await asyncio.gather(
            *(_evaluate_one(c) for c in selected)
        )

        # Sequential write phase. The asyncpg pool is the real bottleneck for
        # writes (max_size cap), and writes are ~10ms each — parallelizing
        # them would add lock contention without a wall-clock win. Keep simple.
        written_ids: list[str] = []
        verdict_outcomes: Counter[str] = Counter()
        for verdict in results:
            if verdict is None:
                verdict_outcomes["error"] += 1
                continue
            verdict_outcomes[verdict.outcome] += 1
            new_id = await write_one(verdict, pool=pool, source=source)
            if new_id:
                written_ids.append(new_id)

        logger.info(
            "verdict_pipeline: wrote %d/%d verdicts for session=%s turn=%s",
            len(written_ids),
            len(selected),
            ctx.session_id,
            ctx.turn_index,
        )
        set_span_attributes(
            pipeline_span,
            outcome="completed",
            selected_count=len(selected),
            written_count=len(written_ids),
            pass_count=verdict_outcomes.get("pass", 0),
            fail_count=verdict_outcomes.get("fail", 0),
            error_count=verdict_outcomes.get("error", 0),
            skip_count=verdict_outcomes.get("skip", 0),
        )
        return EvaluateWriteResult(
            verdict_ids=written_ids,
            outcome_counts=dict(verdict_outcomes),
        )


# ─── Fire-and-forget entry point ─────────────────────────────────────────────


def fire_and_forget(
    response: ChatResponse,
    *,
    pool: asyncpg.Pool | None = None,
    source: Settings = global_settings,
    triggered_by: str = "user_turn",
    user_message: str = "",
) -> None:
    """Schedule evaluate_and_write as a background asyncio task.

    Non-blocking: returns immediately. Any error in the background task is
    logged and swallowed — chat is never blocked on verdict generation.

    Args:
        response:     The ChatResponse returned by build_chat_response().
        pool:         asyncpg.Pool to inject (defaults to singleton).
        source:       Runtime settings whose database/schema own verdict writes.
        triggered_by: Provenance label stored on each VerdictRecord.
        user_message: Raw user prompt that produced this response. Passed
                      through to evaluate_and_write so validators that
                      need verbatim user text (e.g. respects_user_limit
                      reading "top 5") can read it from the context.
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            evaluate_and_write(
                response,
                pool=pool,
                source=source,
                triggered_by=triggered_by,
                user_message=user_message,
            )
        )
        # Hold a strong reference so GC can't collect the task mid-flight.
        # The event loop only stores a weak ref to user tasks.
        _BACKGROUND_TASKS.add(task)

        def _on_done(fut: asyncio.Future) -> None:
            try:
                fut.result()
            except Exception as exc:
                logger.warning(
                    "verdict_pipeline.fire_and_forget: background task failed: %s",
                    exc,
                    exc_info=True,
                )

        # Two independent callbacks: the discard releases the strong ref, the
        # logger surfaces failures. discard() (not remove()) is intentional —
        # it won't KeyError if the task was already pruned.
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        task.add_done_callback(_on_done)
    except Exception as exc:
        # Catches RuntimeError from get_running_loop() when called outside a loop
        # (e.g. test harnesses), as well as any unexpected create_task failures.
        logger.warning(
            "verdict_pipeline.fire_and_forget: could not schedule background task: %s",
            exc,
            exc_info=True,
        )


__all__ = [
    "EvalContext",
    "EvaluateWriteResult",
    "MAX_CONCURRENT_JUDGES",
    "RateLimiter",
    "_resolve_check_type_filter",
    "evaluate_and_write",
    "evaluate_one_criterion",
    "fire_and_forget",
]
