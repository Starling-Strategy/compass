"""Criterion classifier — 4-safeguard selection of active criteria for a turn.

Implements ADR Decision: Classifier safeguards.

select_criteria_for_turn(ctx, pool, use_ai_classifier=True) returns the union of:

  Safeguard 1 — Mandatory globals: all CriterionRecords with is_mandatory_global=True
                Always fired regardless of any other signal.
  Safeguard 2 — Scenario-required: criteria declared by the scenario (via scenario_ids
                FK join). Only active when ctx.scenario_id is provided.
  Safeguard 3 — Deterministic prefilters: pure-Python rules in prefilters.py that
                map observable turn properties (intent, artifact_snapshot) to codes.
  Safeguard 4 — AI classifier: small LLM (Haiku 4.5) given a Literal[*active_codes]
                output type so JSON-schema enum enforcement makes Type I errors
                (fabricated criterion codes) representationally impossible.

All four sources are unioned; the result is de-duplicated by criterion_code.
Safeguard 4 is additive only: it can ADD codes that safeguards 1-3 didn't fire,
but it cannot REMOVE codes that safeguards 1-3 already selected.

Exception handling: if the AI classifier call raises any exception, it is silently
swallowed and the union of safeguards 1-3 is returned unchanged. This preserves
correctness at the cost of recall under AI failure — the right tradeoff for eval.

DB interaction: this module accepts an asyncpg.Pool (injected by callers); it
never imports from compass_backend.db directly to keep the dependency direction
testable without a live DB. Callers provide the pool; unit tests inject a mock.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits

from compass_backend.agents.criterion_classifier import (
    build_criterion_classifier_agent,
    criterion_classifier_usage_limits,
)
from compass_backend.agents.model_settings import AgentProfile
from compass_backend.config import Settings, settings
from compass_backend.db.criteria import ReadOnlyCriteriaRepository
from compass_backend.quality._evaluator_context import (
    ArtifactSnapshot,
    CompassEvaluatorContext,
)
from compass_backend.quality.criteria import CriterionRecord
from compass_backend.quality.prefilters import apply_prefilters

logger = logging.getLogger(__name__)

# ─── AI classifier output type ───────────────────────────────────────────────


class _ClassifierOutput(BaseModel):
    """Structured output from the AI classifier.

    criterion_codes is constrained to Literal[*active_codes] at agent-build time,
    so the JSON-schema enum enforcement makes fabricated codes representationally
    impossible (Type I error guard).
    """

    criterion_codes: list[str]  # overridden at build time with Literal[*codes]
    rationale: str = Field(
        default="",
        max_length=300,
        description="Brief rationale for the selected codes. Not persisted.",
    )


# ─── AI classifier agent factory ─────────────────────────────────────────────

# Module-level cache: key = frozenset of active codes → Agent.
# One agent per unique code set. Prevents Agent construction overhead per turn.
_CLASSIFIER_AGENT_CACHE: dict[frozenset[str], Agent] = {}


def _build_classifier_agent(active_codes: tuple[str, ...]) -> Agent:
    """Build (or retrieve from cache) a criterion-classifier Agent.

    The output_type is dynamically constructed so that criterion_codes is
    constrained to Literal[*active_codes]. This makes the JSON-schema enum
    property contain only the live criterion_codes from compass.criteria,
    so the model cannot hallucinate codes that don't exist in the DB.

    Args:
        active_codes: Tuple of all active criterion_code values from compass.criteria.

    Returns:
        A pydantic_ai.Agent configured for criterion classification.
    """
    cache_key = frozenset(active_codes)
    cached = _CLASSIFIER_AGENT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    # Build a _ClassifierOutput subclass with Literal[*codes] on criterion_codes.
    # We use create_model dynamically so the Literal is set at runtime based on
    # the current active criterion codes (not a compile-time constant).
    from typing import Literal  # noqa: PLC0415 — local to keep outer namespace clean

    import pydantic  # noqa: PLC0415

    if not active_codes:
        # Degenerate: no active criteria at all. Build an empty-output model.
        CodedOutput = pydantic.create_model(
            "_EmptyClassifierOutput",
            __base__=BaseModel,
            criterion_codes=(list[str], Field(default_factory=list)),
            rationale=(str, Field(default="")),
        )
    else:
        # Literal[*active_codes] — the key Type I error guard
        code_literal = Literal[active_codes]  # type: ignore[valid-type]
        CodedOutput = pydantic.create_model(
            "_ClassifierOutput",
            __base__=BaseModel,
            criterion_codes=(
                list[Annotated[code_literal, Field()]],  # type: ignore[valid-type]
                Field(default_factory=list),
            ),
            rationale=(str, Field(default="")),
        )

    agent: Agent = build_criterion_classifier_agent(CodedOutput)

    _CLASSIFIER_AGENT_CACHE[cache_key] = agent
    return agent


def _classifier_usage_limits() -> UsageLimits:
    """Thin back-compat wrapper around `agents.criterion_classifier.criterion_classifier_usage_limits`."""

    return criterion_classifier_usage_limits()


# ─── Main selection function ──────────────────────────────────────────────────


async def select_criteria_for_turn(
    ctx: CompassEvaluatorContext,
    pool: Any,  # asyncpg.Pool — typed as Any to avoid hard dep in tests
    use_ai_classifier: bool = True,
    source: Settings = settings,
) -> list[CriterionRecord]:
    """Select criteria for one turn using all 4 safeguards.

    Args:
        ctx:               Turn context (session_id, turn_index, intent, artifact_snapshot, ...).
        pool:              asyncpg.Pool pointing at compass schema (staging or prod).
        use_ai_classifier: Set False to disable Safeguard 4 (useful for tests or
                           when LLM budget is exhausted).
        source:            Runtime settings whose schema owns the criteria table.

    Returns:
        De-duplicated list of CriterionRecords that should evaluate this turn.
        Ordered: mandatory globals first (by priority DESC), then others.
    """
    repo = ReadOnlyCriteriaRepository(pool, source=source)

    scenario_fit_applicable = (
        ctx.triggered_by == "sweep_run" and ctx.expected_output is not None
    )

    # ── Safeguard 1: Mandatory globals ───────────────────────────────────────
    mandatory = [
        c
        for c in await repo.load_mandatory_globals()
        if c.check_type != "scenario_fit" or scenario_fit_applicable
    ]
    selected: dict[str, CriterionRecord] = {c.criterion_code: c for c in mandatory}

    # ── Safeguard 2: Scenario-required criteria ───────────────────────────────
    if ctx.scenario_id is not None:
        scenario_criteria = await repo.load_for_scenario(ctx.scenario_id)
        for c in scenario_criteria:
            if c.check_type == "scenario_fit" and not scenario_fit_applicable:
                continue
            if c.criterion_code not in selected:
                selected[c.criterion_code] = c

    # ── Safeguard 3: Deterministic prefilters ────────────────────────────────
    prefilter_codes = apply_prefilters(ctx)
    if prefilter_codes:
        # Load only the prefilter-fired codes that aren't already selected.
        # We load all active and pick the ones in prefilter_codes for efficiency
        # (avoids N individual queries).
        active_all = [
            c
            for c in await repo.load_active()
            if c.check_type != "scenario_fit" or scenario_fit_applicable
        ]
        active_by_code = {c.criterion_code: c for c in active_all}
        for code in prefilter_codes:
            if code not in selected and code in active_by_code:
                selected[code] = active_by_code[code]
    else:
        # No prefilter codes fired; we still need active_all for Safeguard 4.
        active_all = [
            c
            for c in await repo.load_active()
            if c.check_type != "scenario_fit" or scenario_fit_applicable
        ]
        active_by_code = {c.criterion_code: c for c in active_all}

    # ── Safeguard 4: AI classifier (additive only) ───────────────────────────
    if use_ai_classifier and active_all:
        active_codes = tuple(c.criterion_code for c in active_all)
        try:
            agent = _build_classifier_agent(active_codes)
            prompt = _build_classifier_prompt(ctx, active_all)
            result = await agent.run(
                prompt,
                usage_limits=_classifier_usage_limits(),
                metadata={
                    "agent_profile": AgentProfile.CRITERION_CLASSIFIER.value,
                    "session_id": ctx.session_id,
                    "turn_index": ctx.turn_index,
                    "scenario_id": ctx.scenario_id,
                },
            )
            ai_codes: list[str] = list(result.output.criterion_codes)
            for code in ai_codes:
                # Additive only: never remove a code selected by safeguards 1-3
                if code not in selected and code in active_by_code:
                    selected[code] = active_by_code[code]
        except Exception as exc:
            # Safeguard 4 failures must never break the selection pipeline.
            # Safeguards 1-3 still produce a valid (conservative) set.
            logger.warning(
                "criterion_classifier AI call failed; using safeguards 1-3 only",
                exc_info=exc,
                extra={
                    "session_id": ctx.session_id,
                    "turn_index": ctx.turn_index,
                },
            )

    # Return mandatory globals first (by original priority order), then others
    mandatory_codes = {c.criterion_code for c in mandatory}
    result_list = [c for c in mandatory if c.criterion_code in selected]
    result_list += [
        c for code, c in selected.items() if code not in mandatory_codes
    ]
    return result_list


def _build_classifier_prompt(
    ctx: CompassEvaluatorContext,
    pool_of_criteria: list[CriterionRecord],
) -> str:
    """Build the prompt sent to the AI classifier.

    Describes the turn and lists available criterion codes with their text so
    the classifier can make an informed selection.
    """
    criteria_lines = "\n".join(
        f"  - {c.criterion_code}: {c.text[:120]}"
        for c in pool_of_criteria
    )
    artifact_summary = _summarise_artifact(ctx.artifact_snapshot)
    return (
        f"TURN CONTEXT\n"
        f"  intent: {ctx.intent or '(not set)'}\n"
        f"  turn_index: {ctx.turn_index}\n"
        f"  artifact: {artifact_summary}\n"
        f"  answer_text (first 300 chars): {ctx.answer_text[:300]!r}\n\n"
        f"AVAILABLE CRITERION CODES (select the applicable ones):\n"
        f"{criteria_lines}\n\n"
        f"Return only the codes that genuinely apply to this turn."
    )


def _summarise_artifact(snapshot: ArtifactSnapshot) -> str:
    """Compact summary of the artifact_snapshot for the classifier prompt."""
    parts: list[str] = []
    if snapshot.table:
        parts.append(f"table ({len(snapshot.table)} rows)")
    if snapshot.citations:
        parts.append(f"citations ({len(snapshot.citations)})")
    return ", ".join(parts) or "(empty)"


__all__ = [
    "select_criteria_for_turn",
    "_build_classifier_agent",
    "_build_classifier_prompt",
    "_classifier_usage_limits",
]
