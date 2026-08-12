"""Pydantic AI planner for Compass MVP 2."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Literal, Protocol

import logfire
from pydantic import Field, ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.agent.abstract import EventStreamHandler
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UserError
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import UsageLimits

from compass_backend.instructions.loader import load_model_instructions
from compass_backend.observability import compass_span
from compass_backend.agents.hooks import build_diagnostic_hooks
from compass_backend.execution.shape_check import unsupported_shape_hint
from compass_backend.agents.model_settings import (
    AgentProfile,
    PlannerThinkingEffort,
    PlannerThinkingPolicy,
    agent_model_settings,
    model_settings_for_profile,
)
from compass_backend.contracts import (
    ConversationMemory,
    LimitSpec,
    MetricSpec,
    PendingQueryContext,
    PlannerRunEvidence,
    PlannerTurn,
    PolicyGuidancePlan,
    QueryContext,
    QueryPlan,
    ResultMemoryRef,
    SelectionSpec,
    SortSpec,
    SortStepSpec,
    pending_context_from_plan,
)
from compass_backend.contracts.finalization import (
    AmbiguousDistrictBlocker,
    FinalizerBlocker,
    NonRankableProfileFieldBlocker,
    UnresolvedPhraseBlocker,
    UnsupportedConceptBlocker,
)
from compass_backend.planning.catalog_pipeline import CatalogPlanPipeline
from compass_backend.planning.instruction_snippets import (
    PlannerInstructionSelection,
    select_planner_instruction_snippets,
)
from compass_backend.policy_guidance.models import PolicyGuidanceLibrary
from compass_backend.reference import expand_state_or_region_values, normalize_state
from compass_backend.planning.thinking import (
    PlannerThinkingDecision,
    decide_planner_thinking,
    is_unsupported_thinking_error,
)
from compass_backend.planning.delta_detection import (
    DeltaIntent,
    detect_delta_intent,
)

PLANNER_INSTRUCTIONS = load_model_instructions("planner.md")

# #1248: the catalog toolset is always attached to the planner now, so every
# run can make a bounded number of catalog tool calls before drafting. This
# caps that loop. A CONSTANT (not a Settings field) — the bound is part of the
# planner contract, not an operator-tunable knob. 8 leaves headroom for the
# handful of lookups a plan needs (districts + metrics + a peer/profile field)
# without allowing a runaway tool loop.
PLANNER_USAGE_LIMITS = UsageLimits(tool_calls_limit=8)

_GENERIC_UNSUPPORTED_CLARIFICATION_SENTINELS: tuple[str, ...] = (
    "supported query shape",
    "one metric at a time",
    "try running each one separately",
    "deterministic execution does not support",
)

DELTA_INTENT_CONSTRAINT = (
    "DELTA-INTENT CONSTRAINT (deterministic detector fired):\n"
    "\n"
    "The user is referring to results from a prior turn. You MUST emit:\n"
    "  - inherit_from_session: true\n"
    '  - inherit_selection_from: "prior_result_rows"\n'
    '  - selection.scope: "unspecified"\n'
    "\n"
    "You SHOULD emit:\n"
    "  - metrics/profile_fields only when the user names a new metric or "
    "profile field\n"
    "  - sort: the new sort field/direction the user is requesting\n"
    "  - filters: new filter constraints if specified\n"
    "  - limit: the new limit if specified\n"
    "  - output: the requested table/chart/summary format if specified\n"
    "\n"
    "Leave metrics/profile_fields empty only when the user is asking to "
    "re-sort, reverse, limit, or reformat the prior result using the same "
    "prior metric/field. If the user names a new metric or profile field "
    "such as MA starting salary, enrollment, FRPL, or workdays, include that "
    "new metric/profile field in the plan so execution changes the measure "
    "while inheriting only the prior result rows.\n"
    "\n"
    "Override by emitting route=clarify ONLY when the user asked to ADD a\n"
    "genuinely new metric/profile field that is not in the catalog. Do NOT\n"
    "clarify to re-ask which metric the user means when they are asking to\n"
    "list or identify the PRIOR result's set (e.g. \"which districts are in\n"
    "the denominator?\", \"who has data on this?\", \"name them\") — that metric\n"
    "is INHERITED from the prior turn, not a new one to resolve. Inherit it\n"
    "and present the set.\n"
)


def planner_delta_intent_instructions(deps: "PlannerDeps") -> str:
    """Render the delta constraint when the detector fired; empty otherwise."""
    if deps.delta_intent is None:
        return ""
    return DELTA_INTENT_CONSTRAINT


def normalize_delta_intent_turn(
    planner_turn: PlannerTurn,
    detected_delta: DeltaIntent | None,
) -> tuple[PlannerTurn, bool]:
    """Force deterministic prior-row inheritance for fired delta intents.

    The LLM still owns the requested operation, new metrics/profile fields,
    filters, sort, limit, output, and other typed execution meaning. The
    deterministic layer owns the prior-result-row selection contract.
    """

    if (
        detected_delta is None
        or planner_turn.route != "execute"
        or planner_turn.query_plan is None
    ):
        return planner_turn, False

    plan = planner_turn.query_plan
    # #393: a "who are the <denominator>?" follow-up presents the count's selected
    # POPULATION (covered districts with a value — the denominator members), not
    # its matching subset. The detector decides this (typed present_target),
    # so the conduit is a distinct typed inherit mode, not a prose re-read below
    # the planner. "matches" (default) keeps the existing prior_result_rows path.
    presents_population = detected_delta.present_target == "population"
    inherit_mode = (
        "prior_result_population" if presents_population else "prior_result_rows"
    )
    plan_update: dict[str, object] = {
        "inherit_from_session": True,
        "inherit_selection_from": inherit_mode,
        "selection": SelectionSpec(scope="unspecified"),
    }
    # The HARD #393 parity constraint — listing the population must NOT re-apply
    # the count's value threshold (it would narrow the 90 back to the 19) — is
    # enforced in the always-run merge/inherit layer (merge_query_plan_with_context
    # and _normalize_plan_selection_with_context), keyed on
    # inherit_selection_from='prior_result_population', so it covers BOTH this
    # detector path AND an LLM-emitted population plan the detector never saw. No
    # filter clearing here (single authority below).
    #
    # A bare "name/list the inherited set" ask (e.g. "What are the 18 districts?"
    # or, for the population, "Who are the 90?") must PRESENT the set, not re-run
    # the prior count that discards the names. Force the list operation
    # structurally instead of trusting the LLM to flip it — a typed decision off
    # the detected DeltaIntent, no prose re-read (C2, #1414; population #393). A
    # list needs a value to show per district: the metric inherits downstream
    # (merge_query_plan_with_context), so we force the lookup only when this turn
    # names a metric OR the prior turn carried one to inherit — a metric-less
    # prior (e.g. a covered-universe count) would otherwise refuse instead of
    # list. Enumerating the set means ALL of it: force the all-rows limit (a
    # numeric limit is rejected on a named_districts lookup) and show every row
    # rather than a preview.
    if detected_delta.is_name_list_intent() and (
        plan.metrics or detected_delta.prior_has_metric
    ):
        plan_update["operation"] = "lookup"
        plan_update["limit"] = LimitSpec(kind="all")
        plan_update["output"] = plan.output.model_copy(update={"row_display": "all"})
    normalized_plan = plan.model_copy(update=plan_update)
    normalized_turn = planner_turn.model_copy(update={"query_plan": normalized_plan})
    return normalized_turn, normalized_plan != plan


def _emit_delta_detector_span(
    detected_delta: DeltaIntent | None,
    planner_turn: PlannerTurn,
    *,
    normalized: bool = False,
) -> None:
    """Telemetry: record detector fire + planner constraint compliance.

    Only emits when the detector fired — we don't need a span on every
    non-delta turn. Compliance is whether the planner emitted
    ``query_plan.inherit_from_session=True`` (or chose route=clarify, which
    the constraint allows as the only override).
    """
    if detected_delta is None:
        return
    plan = planner_turn.query_plan
    inherited = plan is not None and plan.inherit_from_session is True
    preserved_new_field = bool(
        plan is not None and (plan.metrics or plan.profile_fields)
    )
    honored = inherited or planner_turn.route == "clarify"
    with compass_span(
        "compass.planning.delta_detector",
        fired=True,
        normalized=normalized,
        honored=honored,
        route=planner_turn.route,
        inherited=inherited,
        preserved_new_field=preserved_new_field,
        matched_anaphora=",".join(detected_delta.matched_anaphora),
        matched_verbs=",".join(detected_delta.matched_verbs),
    ):
        pass


@dataclass(frozen=True)
class PlannerDeps:
    """Typed context passed to the planner and its output validators."""

    message: str
    memory: ConversationMemory = field(default_factory=ConversationMemory)
    current_academic_year: str | None = None
    trace_id: str | None = None
    scenario_id: str | None = None
    planner_guidance: tuple[PlannerInstructionSelection, ...] = ()
    delta_intent: DeltaIntent | None = None
    # W0.5 (#832): canonical pydantic-ai message_history threaded into
    # ``agent.run(..., message_history=...)`` so the planner sees its prior
    # turn output as typed ModelMessages instead of a prose-rebuilt transcript.
    message_history: tuple[ModelMessage, ...] = ()
    # Step 5 of the catalog-resolution-unification plan. When set, the
    # planner output validator runs the unified
    # reconcile → adjudicate → finalize chain in place of the legacy
    # shape check. ``None`` keeps the legacy path (production today).
    # Step 6 will retire the legacy path entirely after pa-eval nightly
    # proves parity.
    catalog_pipeline: CatalogPlanPipeline | None = None

    def with_guidance(
        self,
        planner_guidance: tuple[PlannerInstructionSelection, ...],
    ) -> "PlannerDeps":
        """Return planner deps with selected instruction-only guidance attached."""

        return replace(self, planner_guidance=planner_guidance)

    @property
    def query_context(self) -> QueryContext | None:
        """Return the latest validated query context from memory."""

        return self.memory.latest_query_context

    @property
    def pending_context(self) -> PendingQueryContext | None:
        """Return pending clarification context from memory."""

        return self.memory.pending_query_context

    @property
    def result_memory_refs(self) -> tuple[ResultMemoryRef, ...]:
        """Return prior result refs from memory."""

        return tuple(self.memory.result_refs)

    @property
    def recent_routes(self) -> tuple[str, ...]:
        """Return prior turn routes from memory."""

        return tuple(self.memory.recent_routes)


class PlannerAgent(Protocol):
    """Small protocol for real or test Pydantic AI planner agents."""

    async def run(self, user_prompt: str, **kwargs) -> AgentRunResult[PlannerTurn]:
        """Run the planner for one user prompt."""


class CachedPlannerAgent:
    """Lazy reusable planner agent for app/router-owned runtime use."""

    def __init__(
        self,
        model: str,
        *,
        library: PolicyGuidanceLibrary | None = None,
        library_provider: Callable[[], PolicyGuidanceLibrary] | None = None,
    ) -> None:
        self.model = model
        self.library = library
        self.library_provider = library_provider
        self._agent: PlannerAgent | None = None

    async def run(self, user_prompt: str, **kwargs) -> AgentRunResult[PlannerTurn]:
        """Build the real planner once, then reuse it for later turns."""

        if self._agent is None:
            library = self.library
            if library is None and self.library_provider is not None:
                library = self.library_provider()
            self._agent = create_planner_agent(self.model, library=library)
        return await self._agent.run(user_prompt, **kwargs)


@dataclass(frozen=True)
class PlannerRun:
    """Typed planner output plus persisted evidence from the Pydantic AI run."""

    turn: PlannerTurn
    evidence: PlannerRunEvidence | None = None


def make_bound_planner_turn(
    library: PolicyGuidanceLibrary,
) -> type[PlannerTurn]:
    """Return a PlannerTurn subclass whose policy_guidance topic_ids are
    Literal-bound to the loaded library's topic enum.

    The JSON schema produced for the LLM advertises a string-enum constraint
    on topic_ids and primary_topic_id, so the model literally cannot emit a
    topic the library doesn't define. This is the strongest defense against
    routing-time hallucination — the saved-feedback rule
    ``feedback_constrain_llm_outputs_with_literal`` codified.
    """

    topic_ids = library.all_topic_ids
    if not topic_ids:
        raise ValueError(
            "PolicyGuidanceLibrary has no topics — cannot bind a planner "
            "Literal[*topic_ids] enum"
        )

    # Literal[(t1, t2, t3)] reads back as Literal[t1, t2, t3] — confirmed in
    # CPython 3.12. Pydantic 2 emits the corresponding JSON-schema enum.
    topic_literal = Literal[topic_ids]  # type: ignore[valid-type]

    class BoundPolicyGuidancePlan(PolicyGuidancePlan):
        topic_ids: list[topic_literal] = Field(  # type: ignore[valid-type]
            min_length=1, max_length=4,
        )
        primary_topic_id: topic_literal | None = Field(  # type: ignore[valid-type]
            default=None,
        )

    BoundPolicyGuidancePlan.__name__ = "PolicyGuidancePlan"
    BoundPolicyGuidancePlan.__qualname__ = "PolicyGuidancePlan"

    class BoundPlannerTurn(PlannerTurn):
        policy_guidance: BoundPolicyGuidancePlan | None = None

    BoundPlannerTurn.__name__ = "PlannerTurn"
    BoundPlannerTurn.__qualname__ = "PlannerTurn"
    return BoundPlannerTurn


def create_planner_agent(
    model: str,
    *,
    library: PolicyGuidanceLibrary | None = None,
) -> Agent[PlannerDeps, PlannerTurn]:
    """Create the Compass planner using vanilla Pydantic AI structured output.

    When ``library`` is provided, the planner's PolicyGuidancePlan is bound
    to ``Literal[*library.all_topic_ids]`` so the LLM cannot emit a topic
    outside the loaded library. Without ``library``, falls back to the
    unbound contract (``str`` topic_ids) — useful for early-bootstrap tests
    where wiring the library would be circular.
    """

    output_type: type[PlannerTurn] = (
        PlannerTurn if library is None else make_bound_planner_turn(library)
    )

    agent = Agent(
        model,
        deps_type=PlannerDeps,
        output_type=output_type,
        instructions=PLANNER_INSTRUCTIONS,
        model_settings=_planner_base_model_settings(),
        # retries={"output": 0} means any ModelRetry raised by the output
        # validator immediately surfaces as UnexpectedModelBehavior — no silent
        # re-roll. orchestration/chat.py catches that exception and converts it
        # into the polite safe-structure rescue clarification
        # (is_rescue_fallback=True) turn. Rationale: at temperature=0.0 the LLM
        # can still emit different valid QueryPlans on retry; both pass
        # validators; only the final emission reaches the user, creating
        # non-deterministic output (C12 variance). Clarification is strictly
        # better UX than silent variance. (Output-only budget; tool retries keep
        # the Pydantic AI default. Pre-1.99 this was output_retries=0.)
        retries={"output": 0},
        capabilities=[build_diagnostic_hooks("planner")],
    )

    @agent.instructions
    def add_runtime_context_instructions(ctx: RunContext[PlannerDeps]) -> str:
        return planner_runtime_context_instructions(ctx.deps)

    @agent.instructions
    def add_planner_guidance_instructions(ctx: RunContext[PlannerDeps]) -> str:
        return planner_guidance_instructions(ctx.deps)

    @agent.instructions
    def add_planner_guidance_authority_warning(ctx: RunContext[PlannerDeps]) -> str:
        return planner_guidance_authority_warning(ctx.deps)

    @agent.instructions
    def add_planner_delta_intent_constraint(ctx: RunContext[PlannerDeps]) -> str:
        return planner_delta_intent_instructions(ctx.deps)

    @agent.output_validator
    async def validate_planner_output(
        ctx: RunContext[PlannerDeps],
        turn: PlannerTurn,
    ) -> PlannerTurn:
        return await validate_planner_turn_quality_async(turn, ctx.deps)

    return agent


async def run_planner_turn(
    message: str,
    *,
    model: str,
    agent: PlannerAgent,
    context: QueryContext | None = None,
) -> PlannerTurn:
    """Run the planner and return a typed turn decision."""

    run = await run_planner(
        message,
        model=model,
        agent=agent,
        context=context,
    )
    return run.turn


async def run_planner(
    message: str,
    *,
    model: str,
    agent: PlannerAgent,
    memory: ConversationMemory | None = None,
    context: QueryContext | None = None,
    pending_context: PendingQueryContext | None = None,
    result_memory_refs: list[ResultMemoryRef] | None = None,
    recent_routes: list[str] | None = None,
    message_history: Sequence[ModelMessage] | None = None,
    current_academic_year: str | None = None,
    trace_id: str | None = None,
    scenario_id: str | None = None,
    planner_guidance_max_snippets: int = 3,
    planner_thinking_policy: PlannerThinkingPolicy | None = None,
    planner_thinking_max_effort: PlannerThinkingEffort | None = None,
    planner_toolsets: Sequence[AbstractToolset[PlannerDeps]] | None = None,
    event_stream_handler: EventStreamHandler[PlannerDeps] | None = None,
    catalog_pipeline: CatalogPlanPipeline | None = None,
) -> PlannerRun:
    """Run the planner and return the typed decision plus replay evidence."""

    planner_memory = memory or ConversationMemory(
        latest_query_context=context,
        pending_query_context=pending_context,
        result_refs=list(result_memory_refs or []),
        recent_routes=list(recent_routes or []),
    )
    detected_delta = detect_delta_intent(message, planner_memory)
    deps = PlannerDeps(
        message=message,
        memory=planner_memory,
        current_academic_year=current_academic_year,
        trace_id=trace_id,
        scenario_id=scenario_id,
        delta_intent=detected_delta,
        message_history=tuple(message_history or ()),
        catalog_pipeline=catalog_pipeline,
    )
    # Snippet guidance is a standard part of Compass planning — no flag. Selection
    # is deterministic and instruction-only; a turn that matches no snippet injects
    # nothing. (Previously gated by the planner_guidance_enabled feature flag,
    # removed 2026-06: one clean path, rollback = git revert.)
    deps = deps.with_guidance(
        select_planner_instruction_snippets(
            deps,
            max_snippets=planner_guidance_max_snippets,
        )
    )
    thinking_decision = decide_planner_thinking(
        deps,
        policy=planner_thinking_policy or agent_model_settings.planner_thinking_policy,
        max_effort=(
            planner_thinking_max_effort
            or agent_model_settings.planner_thinking_max_effort
        ),
    )
    model_settings = _planner_base_model_settings()
    model_settings.update(thinking_decision.model_settings_overlay())
    run_kwargs: dict[str, object] = {
        "deps": deps,
        "model_settings": model_settings,
        # #1248: bound tool calls on the always-attached catalog tool.
        "usage_limits": PLANNER_USAGE_LIMITS,
    }
    # W0.5 (#832): thread the canonical pydantic-ai message_history so the
    # planner sees its own prior turn outputs as typed ModelMessages.
    if deps.message_history:
        run_kwargs["message_history"] = list(deps.message_history)
    if planner_toolsets:
        run_kwargs["toolsets"] = list(planner_toolsets)
    if event_stream_handler is not None:
        run_kwargs["event_stream_handler"] = event_stream_handler
    started_at = perf_counter()
    thinking_provider_error: str | None = None
    try:
        result = await agent.run(
            _planner_prompt(deps),
            **run_kwargs,
        )
    except (ModelAPIError, ModelHTTPError, UserError) as exc:
        if not (
            thinking_decision.enabled and is_unsupported_thinking_error(exc)
        ):
            raise
        thinking_provider_error = str(exc)
        retry_kwargs: dict[str, object] = {
            "deps": deps,
            "model_settings": _without_thinking_setting(model_settings),
            # #1248: keep the same tool surface + bound on the thinking-fallback
            # re-run as the primary attempt.
            "usage_limits": PLANNER_USAGE_LIMITS,
        }
        if deps.message_history:
            retry_kwargs["message_history"] = list(deps.message_history)
        if planner_toolsets:
            retry_kwargs["toolsets"] = list(planner_toolsets)
        if event_stream_handler is not None:
            retry_kwargs["event_stream_handler"] = event_stream_handler
        result = await agent.run(
            _planner_prompt(deps),
            **retry_kwargs,
        )
    planner_duration_ms = (perf_counter() - started_at) * 1000
    planner_turn, delta_normalized = normalize_delta_intent_turn(
        result.output,
        detected_delta,
    )
    _emit_delta_detector_span(
        detected_delta,
        planner_turn,
        normalized=delta_normalized,
    )
    return PlannerRun(
        turn=planner_turn,
        evidence=_planner_run_evidence(
            result,
            model=model,
            trace_id=trace_id,
            planner_guidance=deps.planner_guidance,
            thinking_decision=thinking_decision,
            thinking_provider_error=thinking_provider_error,
            planner_duration_ms=planner_duration_ms,
        ),
    )


def _planner_prompt(deps: PlannerDeps) -> str:
    """Return the raw user message as the planner run prompt."""

    return deps.message


def _planner_base_model_settings() -> dict[str, object]:
    """Return planner settings without the legacy global thinking default."""

    settings = dict(model_settings_for_profile(AgentProfile.PLANNER))
    settings.pop("thinking", None)
    return settings


def _without_thinking_setting(settings: dict[str, object]) -> dict[str, object]:
    """Return a per-run settings copy with thinking disabled."""

    result = _planner_base_model_settings()
    result.pop("thinking", None)
    return result


def planner_context_instructions(deps: PlannerDeps) -> str:
    """Render all dynamic planner instructions for compatibility callers."""

    return "\n\n".join(
        part
        for part in (
            planner_runtime_context_instructions(deps),
            planner_guidance_instructions(deps),
            planner_guidance_authority_warning(deps),
        )
        if part
    )


def planner_runtime_context_instructions(deps: PlannerDeps) -> str:
    """Render typed planner deps as compact model-visible instructions."""

    memory = deps.memory
    # W0.5 (#832): recent_transcript was retired in favor of pydantic-ai
    # message_history threaded directly into agent.run(). The planner sees
    # prior turns natively; the runtime-context instructions no longer have
    # to render a prose transcript block.
    if (
        memory.latest_query_context is None
        and memory.pending_query_context is None
        and memory.summary is None
        and not memory.result_refs
        and deps.current_academic_year is None
    ):
        return ""

    payload: dict[str, object] = {}
    if deps.current_academic_year is not None:
        payload["current_academic_year"] = deps.current_academic_year
    memory_payload: dict[str, object] = {}
    if memory.summary is not None:
        memory_payload["summary"] = memory.summary.model_dump(
            mode="json",
            exclude_none=True,
        )
    if memory.pending_query_context is not None:
        memory_payload["pending_query"] = memory.pending_query_context.model_dump(
            mode="json",
            exclude_none=True,
        )
    if memory.result_refs:
        memory_payload["prior_results"] = [
            ref.model_dump(mode="json", exclude_none=True)
            for ref in memory.result_refs
        ]
    if memory.latest_query_context is not None:
        context = memory.latest_query_context
        memory_payload["latest_validated_query"] = {
            "query_plan": context.query_plan.model_dump(
                mode="json",
                exclude_none=True,
            ),
            "result": {
                "result_type": context.result_type,
                "order_statement": context.order_statement,
                "row_count": context.row_count,
                "displayed_row_count": context.displayed_row_count,
                "display_limit": context.display_limit,
                "row_display": context.row_display,
                "data_limit_count": context.data_limit_count,
                "data_limit_kind": context.data_limit_kind,
                "data_limit_source": context.data_limit_source,
                "display_limit_source": context.display_limit_source,
                "result_districts": [
                    district.model_dump(mode="json", exclude_none=True)
                    for district in context.result_districts
                ],
                "result_metrics": [
                    metric.model_dump(mode="json", exclude_none=True)
                    for metric in context.result_metrics
                ],
            },
        }
    if memory.latest_turn_index:
        memory_payload["latest_turn_index"] = memory.latest_turn_index
    if memory.source_snapshot_ids:
        memory_payload["source_snapshot_ids"] = list(memory.source_snapshot_ids)
    payload["conversation_memory"] = memory_payload
    context_json = json.dumps(payload, sort_keys=True)
    return (
        "Non-authoritative Compass conversation memory:\n"
        f"{context_json}\n\n"
        "Use recent transcript only to understand user language. Use pending "
        "query context only to preserve slots already supplied before a "
        "query has executed. Use latest_validated_query only when "
        "the current user message is a follow-up to a validated result. For "
        "validated-result follow-ups, set QueryPlan.inherit_from_session=true "
        "and include only the requested changes. Result refs identify prior "
        "validated outputs, but do not by themselves authorize execution unless "
        "resolved to validated query context. summary text is interpretive "
        "context only, and execution still needs a typed QueryPlan that resolves "
        "through Compass validators."
    )


def planner_guidance_instructions(deps: PlannerDeps) -> str:
    """Render selected planner guidance snippets as dynamic instructions."""

    if not deps.planner_guidance:
        return ""
    payload = [guidance.model_payload() for guidance in deps.planner_guidance]
    return (
        "Compass planner guidance snippets:\n"
        f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}"
    )


def planner_guidance_authority_warning(deps: PlannerDeps) -> str:
    """Render guidance authority boundaries only when guidance is selected."""

    if not deps.planner_guidance:
        return ""
    return (
        "Planner guidance is planning guidance only. It is not execution "
        "authority, catalog authority, citation authority, or NCTQ policy "
        "content authority. Catalog resolution, deterministic execution, "
        "validators, policy-guidance library content, and renderers remain "
        "the only authorities for user-facing data, metrics, citations, "
        "stances, rationales, and exemplar policies."
    )


def validate_planner_turn_quality(
    turn: PlannerTurn,
    frame: PlannerDeps,
    *,
    skip_legacy_shape_check: bool = False,
) -> PlannerTurn:
    """Reject typed planner outputs that drop required conversation context.

    ``skip_legacy_shape_check`` is set by the catalog-pipeline async
    wrapper (step 5) — when the unified
    reconcile→adjudicate→finalize chain ran upstream, the legacy
    ``unsupported_shape_hint`` path would either double-fire on a plan
    the pipeline already validated, or reject a slot-corrected plan the
    pipeline rewrote. Step 6 removes the parameter when the legacy
    shape check itself is retired.
    """

    turn, _ = normalize_delta_intent_turn(turn, frame.delta_intent)

    # Shape check first: cheap, dep-free, fires before any session/context
    # validation. Skip inherited plans — their selection is filled in by
    # merge_turn_with_session_context downstream, so the shape isn't fully
    # assembled at validator time. See gap 1 in
    # docs/plans/2026-05-17-post-b-spine-quality-gaps.md.
    if (
        not skip_legacy_shape_check
        and turn.route == "execute"
        and turn.query_plan is not None
        and not turn.query_plan.inherit_from_session
    ):
        hint = unsupported_shape_hint(turn.query_plan)
        if hint is not None and not _shape_hint_should_defer_to_cross_box_review(
            turn.query_plan,
            hint,
        ):
            logfire.info(
                "compass.planner.shape_retry",
                hint=hint,
                operation=turn.query_plan.operation,
                selection_scope=turn.query_plan.selection.scope,
            )
            raise ModelRetry(hint)
        if hint is not None:
            logfire.info(
                "compass.planner.shape_deferred_to_cross_box_review",
                hint=hint,
                operation=turn.query_plan.operation,
                selection_scope=turn.query_plan.selection.scope,
            )

    required_operation = _required_operation_from_guidance(frame)
    if required_operation == "peer_comparison" and not _routes_operation(
        turn,
        required_operation,
    ):
        raise ModelRetry(
            "Selected planner guidance requires operation='peer_comparison'. Use "
            "route='execute' with operation='peer_comparison', keep only the "
            "anchor district in selection.districts, and put the user's policy "
            "topic in metrics. Use operation='similarity' only when no policy "
            "metric or topic is requested."
        )

    if _guidance_requires_intent(frame, "profile_ordered_metric_display") and (
        _plan_dropped_policy_metric_for_profile_ranking(turn)
    ):
        raise ModelRetry(
            "Selected planner guidance is the profile-ordered metric display "
            "shape: rank or select districts BY the profile field but DISPLAY "
            "the policy metric. Keep the user's policy metric as the primary "
            "MetricSpec and put the profile field in a selection-phase sort "
            "step (SortStepSpec phase='selection', key_type='profile_field'). "
            "Do not emit a plain profile-field ranking that drops the policy "
            "metric."
        )

    if (
        turn.route == "execute"
        and turn.query_plan is not None
        and turn.query_plan.inherit_from_session
        and frame.query_context is None
    ):
        raise ModelRetry(
            "QueryPlan.inherit_from_session=true requires previous validated "
            "query context. If no validated result exists yet, preserve known "
            "slots in clarification.pending_context or ask a clarification."
        )

    if (
        turn.route == "execute"
        and turn.query_plan is not None
        and turn.query_plan.inherit_from_session
        and turn.query_plan.inherit_selection_from == "prior_result_rows"
        and frame.query_context is not None
        and not frame.query_context.result_districts
    ):
        raise ModelRetry(
            "QueryPlan.inherit_selection_from='prior_result_rows' requires "
            "previous validated result rows. If no row result exists yet, ask "
            "which districts or scope the user wants."
        )

    # #393 / #1673: the population conduit needs a prior count to inherit. The
    # member list (result_population_districts) flakes empty ~12% of the time in
    # the serialization round-trip (#1658), but the count_denominator integer
    # survives it (#1660). When that integer is present we let the turn through:
    # the merge stage re-derives the population from the inherited metric
    # (_selection_from_context_population_rows -> all_covered_districts), the same
    # int-grounded recovery the delta detector already relies on. Reject ONLY when
    # there is no prior count at all (no members AND no denominator) — otherwise
    # this output-validator turns a recoverable follow-up into a rescue
    # clarification, because the planner runs retries={"output":0}.
    if (
        turn.route == "execute"
        and turn.query_plan is not None
        and turn.query_plan.inherit_from_session
        and turn.query_plan.inherit_selection_from == "prior_result_population"
        and frame.query_context is not None
        and not frame.query_context.result_population_districts
        and frame.query_context.count_denominator is None
    ):
        raise ModelRetry(
            "QueryPlan.inherit_selection_from='prior_result_population' requires "
            "a previous count (its denominator members or count_denominator). If "
            "no such count exists yet, ask which districts or scope the user wants."
        )

    _reject_cross_state_anchor_drop(turn, frame)

    if turn.route != "clarify" or turn.clarification is None:
        return turn

    incoming = turn.clarification.pending_context
    if incoming is None:
        raise ModelRetry(
            "ClarificationRequest must include pending_context with all typed "
            "slots already known from the current message, recent transcript, "
            "and any pending Compass clarification context."
        )
    if _looks_like_generic_unsupported_clarification(turn.clarification.question):
        raise ModelRetry(
            "Planner clarification used generic unsupported-shape refusal prose. "
            "Ask a targeted clarification with concrete choices and preserve "
            "all known typed slots in clarification.pending_context."
        )

    if frame.pending_context is not None:
        _raise_if_pending_slot_was_dropped(frame.pending_context, incoming)
    return turn


async def validate_planner_turn_quality_async(
    turn: PlannerTurn,
    frame: PlannerDeps,
) -> PlannerTurn:
    """Async wrapper that runs the catalog pipeline when configured.

    Step 5 of the catalog-resolution-unification plan
    (``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``).

    When :attr:`PlannerDeps.catalog_pipeline` is ``None`` (production
    today, flag off), this is a pass-through to the legacy sync
    validator — same behavior as before this PR.

    When the pipeline is set and the turn is an execute draft with a
    concrete ``query_plan``:

    1. The plan is normalized via :func:`normalize_delta_intent_turn`
       so the pipeline sees the canonical shape (idempotent — the sync
       validator re-normalizes downstream).
    2. The pipeline runs reconcile → adjudicate → finalize.
    3. On a ready :class:`FinalizedPlan`, the turn's ``query_plan`` is
       replaced with the slot-corrected plan and the sync validator
       runs the remaining checks (peer-comparison gating, inheritance,
       clarify hygiene) with ``skip_legacy_shape_check=True``.
    4. On blockers, a ``ModelRetry`` is raised with hint text built
       from typed catalog evidence (see
       :func:`_model_retry_hint_from_finalizer_blockers`).
    """

    if (
        frame.catalog_pipeline is None
        or turn.route != "execute"
        or turn.query_plan is None
        or turn.query_plan.inherit_from_session
    ):
        return validate_planner_turn_quality(turn, frame)

    turn, _ = normalize_delta_intent_turn(turn, frame.delta_intent)
    finalized = await frame.catalog_pipeline.reconcile_and_finalize(
        turn.query_plan
    )

    logfire.info(
        "compass.planner.catalog_pipeline_finalized",
        ready=finalized.is_ready,
        blocker_count=len(finalized.blockers),
        slot_move_count=len(finalized.slot_moves),
        blocker_kinds=sorted({blocker.kind for blocker in finalized.blockers}),
        slot_move_kinds=sorted({move.kind for move in finalized.slot_moves}),
    )

    if not finalized.is_ready:
        raise ModelRetry(
            _model_retry_hint_from_finalizer_blockers(finalized.blockers)
        )

    updated_turn = turn.model_copy(update={"query_plan": finalized.plan})
    return validate_planner_turn_quality(
        updated_turn, frame, skip_legacy_shape_check=True
    )


def _model_retry_hint_from_finalizer_blockers(
    blockers: tuple[FinalizerBlocker, ...],
) -> str:
    """Translate typed :class:`FinalizerBlocker` evidence into a model-facing hint.

    Priority order: unsupported (catalog says we don't do this) →
    non-rankable profile (we have a candidate list to suggest) →
    ambiguous district (clarify with real names) → unresolved (catalog
    gap; rephrase). The model sees one combined message; each blocker
    contributes one line of actionable instruction.
    """

    lines: list[str] = []
    for blocker in _prioritize_blockers(blockers):
        if isinstance(blocker, UnsupportedConceptBlocker):
            lines.append(
                f"Phrase {blocker.phrase!r} is governed-unsupported per the "
                f"catalog ({blocker.message}). Switch route to 'clarify' "
                "with a targeted question, or 'refuse' if no covered "
                "alternative exists. Do not place unsupported phrases in "
                "metrics, profile_fields, sort, or sort_steps."
            )
        elif isinstance(blocker, NonRankableProfileFieldBlocker):
            alternatives = ", ".join(
                candidate.label for candidate in blocker.rankable_candidates
            ) or "(none configured)"
            lines.append(
                f"Phrase {blocker.phrase!r} resolves to NCES profile field "
                f"{blocker.resolved_field_key!r}, which is not in the "
                "governed rank-fields enum. Rankable alternatives: "
                f"{alternatives}. Pick one of these for the sort/rank "
                "context, or switch route to 'clarify' and ask the user."
            )
        elif isinstance(blocker, AmbiguousDistrictBlocker):
            choices = ", ".join(
                candidate.label for candidate in blocker.candidates
            )
            lines.append(
                f"Phrase {blocker.phrase!r} matches multiple covered "
                f"districts: {choices}. Switch route to 'clarify' and "
                "ask the user which district they mean."
            )
        elif isinstance(blocker, UnresolvedPhraseBlocker):
            location = (
                f" (from {blocker.source_field})" if blocker.source_field else ""
            )
            lines.append(
                f"Phrase {blocker.phrase!r}{location} has no candidates in "
                "the catalog. Try a different phrasing the catalog might "
                "recognize, or switch route to 'clarify' with a targeted "
                "question."
            )
    return "\n".join(lines)


def _prioritize_blockers(
    blockers: tuple[FinalizerBlocker, ...],
) -> list[FinalizerBlocker]:
    """Order blockers so the most actionable hint comes first."""

    order = {
        "unsupported_concept": 0,
        "non_rankable_profile_field": 1,
        "ambiguous_district": 2,
        "unresolved_phrase": 3,
    }
    return sorted(blockers, key=lambda blocker: order.get(blocker.kind, 99))


def _looks_like_generic_unsupported_clarification(question: str) -> bool:
    normalized = question.casefold()
    return any(
        sentinel in normalized
        for sentinel in _GENERIC_UNSUPPORTED_CLARIFICATION_SENTINELS
    )


def _required_operation_from_guidance(frame: PlannerDeps) -> str | None:
    for guidance in frame.planner_guidance:
        required_operation = guidance.metadata.get("required_operation")
        if required_operation:
            return required_operation
    return None


def _guidance_requires_intent(frame: PlannerDeps, intent: str) -> bool:
    """Return whether any selected guidance carries the given typed intent.

    Reads only the typed ``metadata`` dict on selected guidance evidence — no
    raw user prose — so the no-prose-dispatch guard stays green.
    """

    return any(
        guidance.metadata.get("intent") == intent
        for guidance in frame.planner_guidance
    )


def _plan_dropped_policy_metric_for_profile_ranking(turn: PlannerTurn) -> bool:
    """Return whether an execute turn became a plain profile-field ranking.

    Reads only typed ``QueryPlan`` fields: the plan carries a profile field
    (in ``profile_fields`` or a selection-phase ``profile_field`` sort step)
    but has no primary policy ``MetricSpec`` to display — i.e. the policy
    metric the guidance requires was dropped and the displayed value is the
    profile field itself.
    """

    if turn.route != "execute" or turn.query_plan is None:
        return False
    plan = turn.query_plan
    has_profile_field = bool(plan.profile_fields) or any(
        step.key_type == "profile_field" for step in plan.sort_steps
    )
    if not has_profile_field:
        return False
    display_metrics = [
        metric for metric in plan.metrics if metric.role not in {"filter", "grouping"}
    ]
    has_policy_primary = bool(display_metrics) and display_metrics[0].role == "primary"
    return not has_policy_primary


def _routes_operation(turn: PlannerTurn, operation: str) -> bool:
    if (
        turn.route == "execute"
        and turn.query_plan is not None
        and turn.query_plan.operation == operation
    ):
        return True
    return (
        turn.route == "clarify"
        and turn.clarification is not None
        and turn.clarification.pending_context is not None
        and turn.clarification.pending_context.operation == operation
    )


def _shape_hint_should_defer_to_cross_box_review(
    plan: QueryPlan,
    hint: str,
) -> bool:
    """Let typed profile-field rank plans reach catalog-backed review.

    The shape checker runs before catalog resolution, so it cannot know whether
    an extra rank "metric" is actually an approved NCES/profile field in the
    wrong box. Defer only the narrow rank metric-count failure when the plan
    already carries typed profile-field evidence; generic multi-metric ranks
    still retry at the planner boundary.

    Superseded by the always-on reconcile -> adjudicate -> finalize pipeline
    (#1248): the finalizer rebalances metrics <-> profile_fields with catalog
    evidence, so cross-box review is no longer the deferral path. Slated for
    removal once the legacy in-planner shape check is retired.
    """

    if plan.operation != "rank":
        return False
    if "ranking requires exactly one primary metric" not in hint:
        return False
    if plan.selection.scope not in {
        "all_covered_districts",
        "state",
        "named_districts",
        "largest_districts",
    }:
        return False
    if plan.filters:
        return False
    return bool(
        plan.profile_fields
        or any(step.key_type == "profile_field" for step in plan.sort_steps)
    )


def _raise_if_pending_slot_was_dropped(
    previous: PendingQueryContext,
    incoming: PendingQueryContext,
) -> None:
    """Raise when a clarification response forgets prior typed slots."""

    dropped: list[str] = []
    if previous.operation is not None and incoming.operation is None:
        dropped.append("operation")
    if previous.selection is not None and incoming.selection is None:
        dropped.append("selection")
    if previous.metrics and not incoming.metrics:
        dropped.append("metrics")
    elif (
        previous.metrics
        and incoming.metrics
        and (
            "metric" not in previous.missing_fields
            or len(previous.metrics) > 1
        )
        and not _metric_specs_preserve_all(previous.metrics, incoming.metrics)
    ):
        dropped.append("metrics")
    if previous.profile_fields and not incoming.profile_fields:
        dropped.append("profile_fields")
    if previous.filters and not incoming.filters:
        dropped.append("filters")
    if previous.sort is not None and incoming.sort is None:
        dropped.append("sort")
    if previous.limit is not None and incoming.limit is None:
        dropped.append("limit")
    if previous.output is not None and incoming.output is None:
        dropped.append("output")
    if previous.temporal is not None and incoming.temporal is None:
        dropped.append("temporal")
    if dropped:
        raise ModelRetry(
            "ClarificationRequest.pending_context dropped prior typed slots: "
            f"{', '.join(dropped)}. Preserve them and merge only the user's "
            "new information."
        )


def _merge_pending_metric_slots(
    existing: PendingQueryContext,
    incoming: PendingQueryContext,
) -> list[MetricSpec]:
    """Merge pending metrics without losing already-committed metric groups."""

    if not incoming.metrics:
        return list(existing.metrics)
    if not existing.metrics:
        return list(incoming.metrics)
    if "metric" in existing.missing_fields and len(existing.metrics) <= 1:
        # The metric slot was still unresolved, so the new user answer may
        # legitimately fill or replace a single placeholder metric.
        return list(incoming.metrics)
    return _unique_metric_specs([*existing.metrics, *incoming.metrics])


def _metric_specs_preserve_all(
    previous: list[MetricSpec],
    incoming: list[MetricSpec],
) -> bool:
    incoming_keys = {_metric_spec_key(metric) for metric in incoming}
    return all(_metric_spec_key(metric) in incoming_keys for metric in previous)


def _unique_metric_specs(metrics: list[MetricSpec]) -> list[MetricSpec]:
    seen: set[tuple[str, str, str | None]] = set()
    unique: list[MetricSpec] = []
    for metric in metrics:
        key = _metric_spec_key(metric)
        if key in seen:
            continue
        seen.add(key)
        unique.append(metric)
    return unique


def _metric_spec_key(metric: MetricSpec) -> tuple[str, str, str | None]:
    return (metric.name.strip().casefold(), metric.role, metric.degree_lane)


def _planner_run_evidence(
    result: AgentRunResult[PlannerTurn],
    *,
    model: str,
    trace_id: str | None,
    planner_guidance: tuple[PlannerInstructionSelection, ...] = (),
    thinking_decision: PlannerThinkingDecision | None = None,
    thinking_provider_error: str | None = None,
    planner_duration_ms: float | None = None,
) -> PlannerRunEvidence | None:
    """Serialize Pydantic AI planner messages for replay/debug evidence."""

    try:
        raw_json = result.new_messages_json().decode("utf-8")
    except UnicodeDecodeError as exc:
        logfire.warning("planner_run_evidence_decode_failed: %s", type(exc).__name__)
        return None
    try:
        message_count = len(ModelMessagesTypeAdapter.validate_json(raw_json))
    except ValidationError as exc:
        logfire.warning("planner_message_count_invalid: %s", type(exc).__name__)
        message_count = 0
    usage = _planner_usage_fields(result)
    return PlannerRunEvidence(
        new_messages_json=raw_json,
        message_count=message_count,
        model=model,
        trace_id=trace_id,
        planner_guidance=[
            guidance.to_evidence() for guidance in planner_guidance
        ],
        thinking_enabled=(
            thinking_decision.enabled if thinking_decision is not None else False
        ),
        thinking_effort=thinking_decision.effort if thinking_decision else None,
        thinking_policy=thinking_decision.policy if thinking_decision else "off",
        thinking_policy_reason=thinking_decision.reason if thinking_decision else None,
        thinking_provider_error=thinking_provider_error,
        planner_duration_ms=planner_duration_ms,
        **usage,
    )


def _planner_usage_fields(result: AgentRunResult[PlannerTurn]) -> dict[str, int | None]:
    """Return a stable subset of Pydantic AI usage counters."""

    try:
        usage = result.usage  # Pydantic AI >=1.99: usage is a property, not a method.
    except AttributeError as exc:
        logfire.warning("planner_usage_unavailable: %s", type(exc).__name__)
        return {}
    return {
        "usage_requests": getattr(usage, "requests", None),
        "usage_input_tokens": getattr(usage, "input_tokens", None),
        "usage_output_tokens": getattr(usage, "output_tokens", None),
        "usage_total_tokens": getattr(usage, "total_tokens", None),
        "usage_cache_read_tokens": getattr(usage, "cache_read_tokens", None),
        "usage_cache_write_tokens": getattr(usage, "cache_write_tokens", None),
    }


def merge_turn_with_session_context(
    turn: PlannerTurn,
    context: QueryContext | None,
) -> PlannerTurn:
    """Merge a follow-up planner delta with typed session referent memory."""

    if (
        context is None
        or turn.route != "execute"
        or turn.query_plan is None
    ):
        return turn

    if turn.query_plan.inherit_from_session:
        merged_plan = merge_query_plan_with_context(turn.query_plan, context)
    else:
        merged_plan = _normalize_plan_sort_with_context(turn.query_plan, context)
        merged_plan = _normalize_plan_selection_with_context(merged_plan, context)
    return turn.model_copy(update={"query_plan": merged_plan})


def pending_context_after_turn(
    turn: PlannerTurn,
    existing: PendingQueryContext | None,
    *,
    turn_index: int,
) -> PendingQueryContext | None:
    """Return pending clarification memory after a planner turn."""

    if turn.route == "direct":
        return existing
    if turn.route == "execute":
        return None
    if turn.clarification is None:
        return existing

    incoming = turn.clarification.pending_context
    if existing is None and incoming is None:
        return PendingQueryContext(
            missing_fields=turn.clarification.missing_fields,
            last_clarification_question=turn.clarification.question,
            source_turn_index=turn_index,
        )
    merged = _merge_pending_context(
        existing,
        incoming,
        turn_index=turn_index,
        fallback_missing_fields=turn.clarification.missing_fields,
        clarification_question=turn.clarification.question,
    )
    return merged


def normalize_ba_ma_salary_rank_turn(
    turn: PlannerTurn,
) -> PlannerTurn:
    """Default BA+MA salary rankings to the composite two-table shape.

    Operates purely on typed plan fields: promotes any execute-rank plan that
    already carries both BA and MA degree-lane metrics to requires_composite_ranking.
    The planner recipe in teacher-compensation-salary.md emits the BA+MA metrics
    directly, so no user-prose inspection is needed here.
    """

    if turn.route != "execute" or turn.query_plan is None:
        return turn
    plan = turn.query_plan
    if plan.operation != "rank" or plan.requires_composite_ranking:
        return turn
    if not _plan_has_ba_and_ma_salary_metrics(plan):
        return turn

    metrics = [
        metric.model_copy(update={"role": "primary"})
        if _metric_degree_lane(metric) in {"ba", "ma"}
        else metric
        for metric in plan.metrics
    ]
    return turn.model_copy(
        update={
            "query_plan": plan.model_copy(
                update={
                    "metrics": metrics,
                    "requires_composite_ranking": True,
                }
            )
        }
    )


def _plan_has_ba_and_ma_salary_metrics(plan: QueryPlan) -> bool:
    lanes = {
        lane
        for metric in plan.metrics
        if metric.role not in {"filter", "grouping"}
        for lane in [_metric_degree_lane(metric)]
        if lane is not None
    }
    return {"ba", "ma"}.issubset(lanes)


def _metric_degree_lane(metric: MetricSpec) -> str | None:
    if metric.degree_lane is not None:
        return metric.degree_lane
    name = metric.name.casefold()
    if re.search(r"\bba\b", name) or "bachelor" in name:
        return "ba"
    if re.search(r"\bma\b", name) or "master" in name:
        return "ma"
    return None


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    return any(marker in text for marker in markers)


def _categorical_count_grouping_axis(
    metrics: list[MetricSpec],
) -> list[MetricSpec] | None:
    """Return ``metrics`` carrying a ``role='grouping'`` breakdown axis for a
    ``count_kind='categorical_value_count'`` plan, or ``None`` when no single
    axis is recoverable.

    ``categorical_value_count`` produces a per-value breakdown, and the QueryPlan
    validator (``count_kind_matches_count_operation_shape``,
    contracts/planning.py) requires exactly one ``role='grouping'`` metric to
    name that axis. A pending context captured from a clarify often carries the
    axis metric with the default ``role='primary'``; when there is exactly ONE
    non-grouping metric it *is* the breakdown axis, so re-role it. This is
    TYPED-field derive-recover — it reads only the typed ``role`` field, never
    the user's prose (backend guardrail 4).

    Narrow by design: leave already-grouping shapes untouched, and return
    ``None`` for the genuinely ambiguous shapes (zero metrics, or 2+ non-grouping
    metrics with no single axis) so the caller degrades to the clarify instead
    of fabricating a breakdown axis the planner did not choose.
    """

    if any(metric.role == "grouping" for metric in metrics):
        return metrics
    non_grouping = [metric for metric in metrics if metric.role != "grouping"]
    if len(non_grouping) != 1:
        return None
    axis = non_grouping[0]
    return [
        metric.model_copy(update={"role": "grouping"}) if metric is axis else metric
        for metric in metrics
    ]


def promote_pending_context_to_plan(
    context: PendingQueryContext | None,
    *,
    question: str,
) -> QueryPlan | None:
    """Promote complete pending clarification slots into an executable plan."""

    if context is None or not _pending_context_is_complete(context):
        return None
    # Fix 3 (count-crash quality upgrade): a categorical_value_count context
    # captured with a single default-role='primary' metric has exactly one
    # groupable axis — re-role it to 'grouping' so the QueryPlan validator
    # accepts it and the turn answers instead of degrading to a dead-end
    # clarify. Tightened completeness (`_has_execution_fields`) already rejected
    # the unrecoverable shapes (zero / multi-metric), so here the axis is
    # recoverable; `or context.metrics` is a defensive pass-through.
    metrics = context.metrics
    if context.count_kind == "categorical_value_count":
        metrics = _categorical_count_grouping_axis(context.metrics) or context.metrics
    try:
        return QueryPlan(
            operation=context.operation or "lookup",
            question=context.question or question,
            selection=context.selection,
            metrics=metrics,
            profile_fields=context.profile_fields,
            filters=context.filters,
            sort=context.sort,
            # W1-02 (#836): round-trip the four execution-relevant QueryPlan
            # fields that PendingQueryContext now carries (sort_steps,
            # inherit_selection_from, requires_all_metrics, count_kind). Pre-W1-02,
            # promotion dropped these — a clarify→answer cycle could silently
            # weaken multi-step sort intent, AND-intersection prompts, count
            # shape, and inherited-selection scope.
            sort_steps=context.sort_steps,
            limit=context.limit,
            inherit_selection_from=context.inherit_selection_from,
            output=context.output or {},
            temporal=context.temporal or {},
            requires_all_metrics=context.requires_all_metrics,
            requires_composite_ranking=context.requires_composite_ranking,
            count_kind=context.count_kind,
            similarity=context.similarity,
            peer_overrides=context.peer_overrides,
        )
    except ValidationError as exc:
        # Promotion is a best-effort auto-answer of a clarify: a pending context
        # that passed `_pending_context_is_complete` can still fail QueryPlan
        # validation (e.g. count_kind='categorical_value_count' captured without a
        # role='grouping' metric). This runs in `_promote_pending_context_for_turn`
        # at the top of `build_chat_response` — OUTSIDE the planner's
        # retries=0 rescue envelope — so raising here 500s the whole turn. That is
        # the production count-crash: "How many sick days do most districts offer?"
        # (sessions da74d522 / c4b511a6 / f31c1f1e, 2026-07-01) surfaced an
        # uncaught ValidationError as "This turn failed before completing."
        # Degrade to the clarify the user should have seen; emitting a *promotable*
        # shape stays the planner's job (validated on the scorecard / via the model).
        logfire.warning(
            "pending_context_promotion_invalid_plan {error_type}",
            error_type=type(exc).__name__,
        )
        return None


def pending_context_from_execution_clarification(
    plan: QueryPlan,
    clarification,
    *,
    turn_index: int,
    ambiguous_metric_phrase: str | None = None,
) -> PendingQueryContext:
    """Capture an attempted executable plan when execution asks to clarify."""

    candidate_metrics = _metric_specs_from_clarification_candidates(clarification)
    if candidate_metrics and ambiguous_metric_phrase:
        # m1 (#1348): when execution clarifies ONE ambiguous metric inside a
        # multi-metric plan (e.g. "salary AND planning time"), keep the
        # already-resolved siblings — drop only the clarified metric, whose slot
        # the candidate expansions take. The single-metric case is unchanged:
        # siblings is empty, so metrics == candidate_metrics, exactly as the
        # ``candidate_metrics or plan.metrics`` branch below produced before.
        siblings = [
            metric for metric in plan.metrics if metric.name != ambiguous_metric_phrase
        ]
        metrics = siblings + candidate_metrics
    else:
        metrics = candidate_metrics or plan.metrics
    # W1-02 (#836) via the single from-plan builder (#1419): capture the full
    # QueryPlan typed meaning when execution asks to clarify, so the promoted
    # plan after the user answers carries the same multi-step sort /
    # intersection / count_kind / inherited-selection the planner produced.
    return pending_context_from_plan(
        plan,
        metrics=metrics,
        last_clarification_question=clarification.question,
        source_turn_index=turn_index,
        missing_fields=clarification.missing_fields,
    )


def _metric_specs_from_clarification_candidates(clarification) -> list[MetricSpec]:
    if "metric" not in getattr(clarification, "missing_fields", []):
        return []
    candidates = getattr(clarification, "candidates", [])
    return [MetricSpec(name=candidate) for candidate in candidates if candidate]


def _merge_pending_context(
    existing: PendingQueryContext | None,
    incoming: PendingQueryContext | None,
    *,
    turn_index: int,
    fallback_missing_fields,
    clarification_question: str,
) -> PendingQueryContext:
    """Merge prior pending slots with the latest typed clarification update."""

    existing = existing or PendingQueryContext()
    incoming = incoming or PendingQueryContext()
    # W1-02 (#836): incoming-wins for the four added typed-meaning fields,
    # falling back to existing so the prior turn's plan shape is preserved
    # when the new clarification chunk doesn't restate it. Booleans use
    # incoming OR existing (truthy-wins) since "False" is the default.
    merged = PendingQueryContext(
        operation=incoming.operation or existing.operation,
        question=incoming.question or existing.question,
        selection=incoming.selection or existing.selection,
        metrics=_merge_pending_metric_slots(existing, incoming),
        profile_fields=incoming.profile_fields or existing.profile_fields,
        filters=incoming.filters or existing.filters,
        sort=incoming.sort or existing.sort,
        sort_steps=incoming.sort_steps or existing.sort_steps,
        limit=incoming.limit or existing.limit,
        inherit_selection_from=(
            incoming.inherit_selection_from or existing.inherit_selection_from
        ),
        output=incoming.output or existing.output,
        temporal=incoming.temporal or existing.temporal,
        requires_all_metrics=(
            incoming.requires_all_metrics or existing.requires_all_metrics
        ),
        requires_composite_ranking=(
            incoming.requires_composite_ranking
            or existing.requires_composite_ranking
        ),
        # count_kind is non-Optional with default "threshold_count"; prefer
        # incoming when it diverges from default, else preserve existing.
        count_kind=(
            incoming.count_kind
            if incoming.count_kind != "threshold_count"
            else existing.count_kind
        ),
        similarity=incoming.similarity or existing.similarity,
        peer_overrides=incoming.peer_overrides or existing.peer_overrides,
        missing_fields=incoming.missing_fields or fallback_missing_fields,
        last_clarification_question=clarification_question,
        source_turn_index=existing.source_turn_index or turn_index,
    )
    return merged.model_copy(
        update={
            "missing_fields": _remaining_missing_fields(merged),
        }
    )


def _pending_context_is_complete(context: PendingQueryContext) -> bool:
    """Return true when pending slots can form a concrete QueryPlan."""

    return (
        _has_concrete_selection(context)
        and _has_execution_fields(context)
        and not _remaining_missing_fields(context)
    )


def _remaining_missing_fields(context: PendingQueryContext):
    """Drop missing-field markers satisfied by the accumulated pending context."""

    remaining = []
    for missing_field in context.missing_fields:
        if missing_field in {"district", "scope"} and _has_concrete_selection(context):
            continue
        if missing_field == "metric" and (context.metrics or context.profile_fields):
            continue
        if missing_field == "profile_field" and context.profile_fields:
            continue
        if missing_field == "comparison_group" and _has_comparison_group(context):
            continue
        remaining.append(missing_field)
    return remaining


def _has_concrete_selection(context: PendingQueryContext) -> bool:
    selection = context.selection
    if selection is None or selection.scope == "unspecified":
        return False
    if selection.scope == "named_districts":
        return bool(selection.districts)
    if selection.scope == "state":
        return bool(selection.states)
    return True


def _has_comparison_group(context: PendingQueryContext) -> bool:
    selection = context.selection
    if selection is None:
        return False
    if context.operation == "peer_comparison" and selection.scope == "named_districts":
        return bool(selection.districts)
    if selection.scope == "named_districts":
        return len(selection.districts) > 1
    return selection.scope != "unspecified"


def _has_execution_fields(context: PendingQueryContext) -> bool:
    """W1-03 (#838): operation-aware completeness check.

    The pre-W1-03 generic rule (\"profile_lookup needs profile_fields, else
    needs metrics\") rejected valid shapes the executor supports:
    * ``count`` + ``count_kind == \"covered_universe_count\"`` counts the
      covered universe and forbids metrics (QueryPlan validator at
      contracts/planning.py:535).
    * ``similarity`` needs a similarity spec + anchor; metrics are optional.

    The per-operation rules below match the QueryPlan validators so a
    valid metricless clarify→answer is no longer blocked at promotion.
    """

    operation = context.operation

    if operation == "profile_lookup":
        return bool(context.profile_fields)

    if operation == "count":
        if context.count_kind == "covered_universe_count":
            # Counts the covered universe; selection alone is enough.
            return True
        if context.count_kind == "categorical_value_count":
            # Fix 3: a per-value breakdown needs a metric that IS, or is
            # normalizable to, the grouping axis — not merely that metrics
            # exist. `bool(context.metrics)` passed the multi-metric and
            # single-primary shapes that then failed QueryPlan validation at
            # promotion (the crash). Share the one authority: complete iff
            # `_categorical_count_grouping_axis` can name a single axis (already
            # grouping, or exactly one non-grouping metric to re-role). Zero or
            # 2+ non-grouping metrics degrade here, before QueryPlan build.
            return _categorical_count_grouping_axis(context.metrics) is not None
        # threshold_count (default): metric OR a metric-shaped filter.
        return bool(context.metrics or context.filters)

    if operation == "similarity":
        # Anchor name lives in similarity.anchor_name; metrics optional.
        return context.similarity is not None

    if operation == "peer_comparison":
        # The peer-set selection is the executable surface; defer to the
        # shared comparison-group check used by missing-fields resolution.
        return _has_comparison_group(context)

    if operation == "rank":
        # Ranking can be driven by a profile sort step alone even without
        # an explicit metric (e.g., "rank by enrollment" → profile field).
        has_profile_sort = any(
            step.key_type == "profile_field"
            for step in context.sort_steps
        )
        return bool(context.metrics or context.profile_fields or has_profile_sort)

    # lookup / trend / fallback: metric OR profile_fields.
    return bool(context.metrics or context.profile_fields)


def merge_query_plan_with_context(
    plan: QueryPlan,
    context: QueryContext,
) -> QueryPlan:
    """Return a concrete query plan authorized by the prior query context."""

    from compass_backend.execution.filters import filter_kind

    previous = context.query_plan
    explicit_fields = plan.model_fields_set
    operation = (
        plan.operation
        if "operation" in explicit_fields
        else previous.operation
    )
    selection = _merge_selection_with_context(plan, previous, context)
    metrics = plan.metrics or previous.metrics
    if _should_chart_prior_peer_rows(plan, previous, context, explicit_fields):
        operation = "lookup"
        selection = _selection_from_context_result_rows(context)
        metrics = previous.metrics
    inherited_filters = (
        plan.filters if "filters" in explicit_fields else previous.filters
    )
    # #393 (MAJOR 2): listing the count's POPULATION must NOT re-apply the value
    # threshold that defined the smaller matched subset (parity: the listed
    # population must equal the stated denominator, not narrow back to the
    # matches). Dropping the metric-value filter lives HERE — in the always-run
    # merge layer keyed on inherit_selection_from — so it applies on BOTH the
    # detector path (normalize_delta_intent_turn) AND the LLM-direct path where
    # the planner emits inherit_selection_from='prior_result_population' on a
    # phrasing the deterministic detector does not recognize. State/region scope
    # filters are kept.
    if plan.inherit_selection_from == "prior_result_population":
        inherited_filters = [
            filter_spec
            for filter_spec in inherited_filters
            if filter_kind(filter_spec) in {"state", "region"}
        ]
    # #1754: operation-scoped fields must not bleed onto a follow-up that shifts
    # operation. `model_copy(update=)` skips the @model_validator, so a stale
    # `count_kind` (or `requires_composite_ranking`) born on the wire is silently
    # ignored by execution but re-discovered as a crash when
    # `_query_context_for_result` rebuilds a QueryContext post-execution
    # (count_kind_matches_count_operation_shape). Sanitize against the RESOLVED
    # operation here so the merged plan exits coherent:
    #   - count_kind is only meaningful for operation='count'
    #   - requires_composite_ranking is only meaningful for operation='rank'
    merged_count_kind = (
        plan.count_kind if "count_kind" in explicit_fields else previous.count_kind
    )
    if operation != "count":
        merged_count_kind = "threshold_count"
    merged_requires_composite_ranking = (
        plan.requires_composite_ranking
        if "requires_composite_ranking" in explicit_fields
        else previous.requires_composite_ranking
    )
    if operation != "rank":
        merged_requires_composite_ranking = False
    return plan.model_copy(
        update={
            "inherit_from_session": False,
            "operation": operation,
            "selection": selection,
            "metrics": metrics,
            "profile_fields": plan.profile_fields or previous.profile_fields,
            "filters": inherited_filters,
            "sort": _merge_sort_with_context(
                plan.sort,
                previous,
                explicit_fields,
                operation=operation,
                context=context,
            ),
            "sort_steps": _merge_sort_steps_with_context(
                plan.sort_steps,
                previous,
                explicit_fields,
                operation=operation,
                context=context,
            ),
            "limit": _merge_limit_with_context(
                plan,
                previous,
                explicit_fields,
                operation=operation,
            ),
            "output": (
                plan.output
                if "output" in explicit_fields
                else previous.output
            ),
            "temporal": (
                plan.temporal
                if "temporal" in explicit_fields
                else previous.temporal
            ),
            # W1-04 (#840): four execution-relevant fields that were
            # silently dropped pre-W1-04. Use the same explicit-fields-
            # or-previous semantics as filters/output/temporal so a
            # follow-up that doesn't restate intent inherits prior
            # typed meaning instead of falling back to QueryPlan
            # defaults.
            "requires_all_metrics": (
                plan.requires_all_metrics
                if "requires_all_metrics" in explicit_fields
                else previous.requires_all_metrics
            ),
            "requires_composite_ranking": merged_requires_composite_ranking,
            "count_kind": merged_count_kind,
            "inherit_selection_from": (
                plan.inherit_selection_from
                if "inherit_selection_from" in explicit_fields
                else previous.inherit_selection_from
            ),
            "similarity": (
                plan.similarity
                if "similarity" in explicit_fields
                else previous.similarity
            ),
            "peer_overrides": (
                plan.peer_overrides
                if "peer_overrides" in explicit_fields
                else previous.peer_overrides
            ),
        }
    )


def _merge_selection_with_context(
    plan: QueryPlan,
    previous: QueryPlan,
    context: QueryContext,
) -> SelectionSpec:
    """Return the inherited selection requested by a follow-up turn."""

    # Risk #3 mitigation: a delta plan that emits scope="named_districts" with
    # sentinel strings (e.g. districts=["those districts"]) must be treated the
    # same as the prior_result_rows path.  Apply before the scope guard so the
    # guard's early return does not block materialisation.
    state_filter = _state_filter_for_selection_and_filters(plan.selection, plan.filters)
    # #393: the population flag takes precedence over the sentinel-referent branch
    # below (which materialises the MATCHES). A population follow-up that also
    # carries a sentinel referent ("those districts") must still list the
    # population — mirror the non-session path's precedence in
    # _normalize_plan_selection_with_context.
    if plan.inherit_selection_from == "prior_result_population":
        # present the count's selected population (denominator members), not its
        # matching subset.
        return _selection_from_context_population_rows(context, states=state_filter)
    if (
        plan.selection.scope == "named_districts"
        and _selection_districts_are_referent(plan.selection.districts)
        and context.result_districts
    ):
        return _selection_from_context_result_rows(context, states=state_filter)

    if plan.inherit_selection_from == "prior_result_rows":
        return _selection_from_context_result_rows(context, states=state_filter)
    if plan.selection.scope != "unspecified":
        return plan.selection
    return previous.selection


def _selection_from_context_result_rows(
    context: QueryContext,
    *,
    states: set[str] | None = None,
) -> SelectionSpec:
    districts = _context_districts_for_states(context.result_districts, states=states)
    return _named_districts_selection(districts, states=states)


def _selection_from_context_population_rows(
    context: QueryContext,
    *,
    states: set[str] | None = None,
) -> SelectionSpec:
    """Build the selection for the prior count's POPULATION (#393).

    When the count carried its denominator members
    (``result_population_districts``), enumerate them exactly. When that member
    list was dropped in serialization (#1658, ~12%), RE-DERIVE the population by
    scoping to all covered districts for the inherited metric — the value filter
    is cleared on the population-inherit path, so the follow-up lists the
    covered-with-data districts deterministically instead of an empty named set.
    Re-derivation is immune to the persistence flake; grounding the detector on
    the denominator INTEGER (not this list) ensures the population path still
    fires when the list is gone.
    """
    districts = _context_districts_for_states(
        context.result_population_districts, states=states
    )
    if not districts:
        return SelectionSpec(
            scope="all_covered_districts",
            states=sorted(states) if states else [],
        )
    return _named_districts_selection(districts, states=states)


def _context_districts_for_states(
    districts,
    *,
    states: set[str] | None = None,
):
    """Filter carried prior-result refs to the requested states.

    Shared by the matches and population selection helpers (#393, MINOR 5b) so
    the state-filter loop lives in one place. ``states=None`` keeps every ref.
    """
    if not states:
        return list(districts)
    return [
        district
        for district in districts
        if normalize_state(district.state) in states
    ]


def _named_districts_selection(
    districts,
    *,
    states: set[str] | None = None,
) -> SelectionSpec:
    """Materialise a ``named_districts`` selection from carried prior-result refs.

    #393 (MAJOR 1): emit an ID-disambiguating ``"Name, ST"`` handle — not the
    bare name — whenever the carried ref knows its state. The real resolver
    (``db/catalog.py resolve_districts``) buckets a name with >1 covered match to
    'ambiguous' and drops it; the covered universe has exactly one such duplicate
    ("Portland Public Schools" in BOTH ME and OR), so a bare-name re-resolution
    would silently lose it and break parity (listed != stated denominator). The
    state suffix routes each ref through ``CatalogResolver``'s existing
    ``split_state_suffix`` retry (catalog/resolution.py), which re-resolves the
    bare name under its single state and returns the unique covered district —
    no parallel resolver, no new SelectionSpec field. Refs without a state fall
    back to the bare name (unique names resolve fine).
    """
    return SelectionSpec(
        scope="named_districts",
        districts=[
            f"{district.district_name}, {district.state}"
            if district.state
            else district.district_name
            for district in districts
        ],
        states=sorted(states) if states else [],
    )


def _reject_cross_state_anchor_drop(
    turn: PlannerTurn,
    frame: PlannerDeps,
) -> None:
    """Reject a cross-state-comparison follow-up that drops the prior anchor.

    #1511 / #1069: after a prior turn established an anchor district (e.g. Dallas
    ISD, TX), the follow-up "how does that compare to <state>?" must keep that
    anchor. The documented valid shapes (planner.md "Cross-state comparison")
    are: name the anchor + every comparison district together in a
    ``named_districts`` selection, OR ``route=clarify`` carrying the anchor in
    ``pending_context.selection.districts``. The ``SelectionSpec`` validator
    already rejects ``scope="state"`` with a stray anchor in ``districts``
    (#1069). The remaining anchor-drop shape this guard closes is a
    ``named_districts`` plan that restricts to comparison state(s) which do NOT
    contain the prior anchor while omitting the anchor from ``districts`` — the
    state filter then silently excludes the anchor and Compass falsely reports
    the comparison-state district as the only (or an uncovered) result.

    Purely typed/grounded: it reads the prior anchor's resolved state from
    ``result_districts`` (not user prose) and the current plan's typed
    selection/filters. It fires only when the requested states are disjoint from
    every prior anchor's state AND no prior anchor name appears in
    ``selection.districts`` — a contradiction a correct two-group plan cannot
    hit, so it cannot reject a well-formed comparison.

    Eval-gated (a new rejecting planner-output validator is user-visible): prove
    it on the Consistency scorecard before merge.
    """

    if turn.route != "execute" or turn.query_plan is None:
        return
    plan = turn.query_plan
    if plan.inherit_from_session or plan.selection.scope != "named_districts":
        return
    context = frame.query_context
    if context is None or not context.result_districts:
        return

    requested = _state_filter_for_selection_and_filters(plan.selection, plan.filters)
    if not requested:
        return

    anchor_states = {
        normalize_state(district.state)
        for district in context.result_districts
        if district.state
    }
    anchor_states.discard(None)
    # Only the genuine cross-state case: the follow-up restricts to state(s) that
    # contain NONE of the prior anchors.
    if not anchor_states or anchor_states & requested:
        return

    anchor_names = {
        district.district_name.casefold().strip()
        for district in context.result_districts
    }
    plan_names = {name.casefold().strip() for name in plan.selection.districts}
    if anchor_names & plan_names:
        return

    raise ModelRetry(
        "Cross-state comparison dropped the prior anchor district. The "
        "follow-up restricts to comparison state(s) that do not contain the "
        "anchor from the prior turn, and the anchor is missing from "
        "selection.districts — the state filter would silently exclude it. "
        "Apply the documented shape: emit selection.scope='named_districts' "
        "with districts listing the anchor AND every comparison district by "
        "name (do not set a state filter that excludes the anchor), OR "
        "route='clarify' asking which comparison-state districts to include "
        "with the anchor named explicitly in "
        "clarification.pending_context.selection.districts."
    )


def _state_filter_for_selection_and_filters(
    selection: SelectionSpec,
    filters,
) -> set[str]:
    from compass_backend.execution.filters import filter_kind

    values: list[str] = list(selection.states)
    for filter_spec in filters:
        if filter_kind(filter_spec) not in {"state", "region"}:
            continue
        if filter_spec.operator == "equals" and isinstance(filter_spec.value, str):
            values.append(filter_spec.value)
        elif filter_spec.operator == "in" and isinstance(filter_spec.value, list):
            values.extend(str(value) for value in filter_spec.value)
    return expand_state_or_region_values(values)


def _should_chart_prior_peer_rows(
    plan: QueryPlan,
    previous: QueryPlan,
    context: QueryContext,
    explicit_fields,
) -> bool:
    if previous.operation != "peer_comparison":
        return False
    if not context.result_districts:
        return False
    if plan.inherit_selection_from == "prior_result_rows":
        return True
    if plan.selection.scope != "unspecified":
        return False
    output_is_explicit = "output" in explicit_fields
    return output_is_explicit and plan.output.format in {"chart", "csv_export"}


def _merge_limit_with_context(
    plan: QueryPlan,
    previous: QueryPlan,
    explicit_fields,
    *,
    operation,
):
    """Inherit rank limits only when they still describe the follow-up shape.

    #1734: drop the carried limit on EVERY ``rank -> non-rank`` transition, not
    just ``rank -> lookup``. ``profile_lookup``/``peer_comparison``/``trend``/
    ``similarity`` all forbid a manual ``limit`` (see each operation's
    ``*_shape_is_supported`` predicate). Restricting the drop to ``lookup``
    carried turn-1's top-N limit into a ``profile_lookup`` follow-up
    ("enrollment of those districts"), producing a structurally impossible plan
    that the post-merge shape guard rejected. ``rank -> rank`` re-ranking is the
    one legitimate carry, so it is preserved. A follow-up that genuinely wants a
    limit restates it (``"limit" in explicit_fields``) and is unaffected.
    """

    if "limit" in explicit_fields:
        return plan.limit
    if previous.operation == "rank" and operation != "rank":
        return None
    return previous.limit


def _merge_sort_with_context(
    sort: SortSpec | None,
    previous: QueryPlan,
    explicit_fields,
    *,
    operation,
    context: QueryContext | None = None,
) -> SortSpec | None:
    """Replace planner placeholder sort fields with prior metric authority."""

    if "sort" not in explicit_fields:
        # #1734: drop the carried manual sort on every rank -> non-rank
        # transition (profile_lookup/peer_comparison/trend/similarity all forbid
        # it), keeping rank -> rank re-ranking. See _merge_limit_with_context.
        if previous.operation == "rank" and operation != "rank":
            return None
        return previous.sort
    if sort is None:
        return None
    field = sort.field.casefold().strip()
    if field not in {"<inherit>", "inherit", "that", "those", ""} and not (
        context is not None and _sort_field_matches_context_metric(field, context)
    ):
        return sort
    prior_metric_name = previous.metrics[0].name if previous.metrics else "value"
    return sort.model_copy(update={"field": prior_metric_name})


def _merge_sort_steps_with_context(
    sort_steps: list[SortStepSpec],
    previous: QueryPlan,
    explicit_fields,
    *,
    operation,
    context: QueryContext | None = None,
) -> list[SortStepSpec]:
    """Merge ordered sort steps across follow-up turns."""

    if "sort_steps" not in explicit_fields:
        # #1734: drop the carried sort steps on every rank -> non-rank
        # transition, keeping rank -> rank re-ranking. See
        # _merge_limit_with_context for the full rationale.
        if previous.operation == "rank" and operation != "rank":
            return []
        return previous.sort_steps
    return [
        _merge_sort_step_with_context(step, previous, context)
        for step in sort_steps
    ]


def _merge_sort_step_with_context(
    step: SortStepSpec,
    previous: QueryPlan,
    context: QueryContext | None,
) -> SortStepSpec:
    field = step.field.casefold().strip()
    if field not in {"<inherit>", "inherit", "that", "those", ""} and not (
        context is not None and _sort_field_matches_context_metric(field, context)
    ):
        return step
    prior_metric_name = previous.metrics[0].name if previous.metrics else "value"
    return step.model_copy(update={"field": prior_metric_name})


def _normalize_plan_sort_with_context(
    plan: QueryPlan,
    context: QueryContext,
) -> QueryPlan:
    """Normalize planner sort aliases that refer to a prior resolved metric."""

    updates: dict[str, object] = {}
    if plan.sort is not None:
        field = plan.sort.field.casefold().strip()
        if _sort_field_matches_context_metric(field, context):
            sort_field = (
                plan.metrics[0].name
                if plan.metrics
                else context.query_plan.metrics[0].name
            )
            updates["sort"] = plan.sort.model_copy(update={"field": sort_field})

    normalized_steps = [
        _merge_sort_step_with_context(step, context.query_plan, context)
        for step in plan.sort_steps
    ]
    if normalized_steps != plan.sort_steps:
        updates["sort_steps"] = normalized_steps

    if not updates:
        return plan
    return plan.model_copy(update=updates)


def _sort_field_matches_context_metric(
    normalized_field: str,
    context: QueryContext,
) -> bool:
    """Return whether a sort field names a prior resolved metric."""

    metric_names = {
        metric.name.casefold().strip()
        for metric in context.query_plan.metrics
    }
    metric_names.update(
        metric.metric_name.casefold().strip()
        for metric in context.result_metrics
    )
    return normalized_field in metric_names


# Typed sentinel strings the planner emits to signal a prior-result district referent.
# These are protocol markers from the typed SelectionSpec.districts contract — NOT
# free-text user messages. Detection reads SelectionSpec.districts (a typed list[str]).
_SELECTION_REFERENT_SENTINELS: frozenset[str] = frozenset({
    "<inherit>",
    "inherit",
    "those",
    "those districts",
    "these",
    "these districts",
    "that",
    "that list",
    "the previous",
    "previous districts",
    "prior districts",
    "prior result",
})


def _selection_districts_are_referent(districts: list[str]) -> bool:
    """Return True when ALL district strings in the list are known referent sentinels.

    Empty list returns False (no referent signal).
    Mixed list (sentinel + real name) returns False (conservative — leave untouched).
    """
    if not districts:
        return False
    return all(d.casefold().strip() in _SELECTION_REFERENT_SENTINELS for d in districts)


def _normalize_plan_selection_with_context(
    plan: QueryPlan,
    context: QueryContext,
) -> QueryPlan:
    """Normalise planner selection referents that name the prior result-district list.

    Triggers when the plan carries sentinel strings in SelectionSpec.districts OR
    when the plan signals prior_result_rows intent via inherit_selection_from without
    setting inherit_from_session=True (the non-delta referent path).

    When context.result_districts is empty, silently no-ops and logs so the team
    can measure how often this edge case occurs.  Downstream layers handle UX:
    catalog resolution rejects unresolved sentinel strings; the user sees a refusal
    or clarification from existing machinery.
    """
    selection = plan.selection
    is_sentinel = (
        selection.scope == "named_districts"
        and _selection_districts_are_referent(selection.districts)
    )
    # #393 (MAJOR 2): the population conduit must be honored on this direct
    # (non-inherit_from_session) path too — an LLM plan that sets the population
    # flag but omits inherit_from_session=True skips merge_query_plan_with_context
    # entirely, so without this it would no-op and surface the matches.
    is_population_inherit = (
        not plan.inherit_from_session
        and plan.inherit_selection_from == "prior_result_population"
    )
    is_inherit_flag = (
        not plan.inherit_from_session
        and plan.inherit_selection_from == "prior_result_rows"
    )

    if not (is_sentinel or is_inherit_flag or is_population_inherit):
        return plan

    source_districts = (
        context.result_population_districts
        if is_population_inherit
        else context.result_districts
    )
    if not source_districts:
        logfire.info(
            "compass.planner.selection_referent_skipped",
            reason="result_districts_empty",
            scope=selection.scope,
            inherit_selection_from=plan.inherit_selection_from,
        )
        return plan

    states = _state_filter_for_selection_and_filters(plan.selection, plan.filters)
    if is_population_inherit:
        from compass_backend.execution.filters import filter_kind

        materialised = _selection_from_context_population_rows(
            context, states=states
        )
        # Parity (#393): listing the population must not re-apply the value
        # threshold that defined the matches — drop it here mirroring the merge
        # layer. State/region scope filters are kept.
        filters = [
            filter_spec
            for filter_spec in plan.filters
            if filter_kind(filter_spec) in {"state", "region"}
        ]
        return plan.model_copy(
            update={"selection": materialised, "filters": filters}
        )

    materialised = _selection_from_context_result_rows(context, states=states)
    return plan.model_copy(update={"selection": materialised})
