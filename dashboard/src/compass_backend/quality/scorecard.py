"""Backend pure functions for the Compass Quality Scorecard.

These functions read from compass.* tables (verdicts, scenarios, cases,
criteria, chat_sessions, chat_messages) and return typed Pydantic objects
defined in ``scorecard_models``. They are pure in the sense that all
external I/O is via the injected ``asyncpg.Pool`` parameter.

Called by ``nctqai.services.compass_quality.loaders`` via ``asyncio.run()``.
No REST API hop — the dashboard imports these directly.

K=3 macro-averaging math (per the ADR worked example):
    case_pass_rate  = passes / (total_trials - stub_errors)
    scenario_score  = mean(case_pass_rates over all cases in the scenario)
    category_score  = mean(scenario_scores over all scenarios in the dimension)
    score_pct       = round(category_score * 100)

Critical: ``(outcome='error' AND judge_source='deterministic')`` verdicts
are SKIPPED from BOTH numerator AND denominator (stub-error filter). These
arise from PR 5's RecordDeterministicEvaluator before real deterministic
checks are wired. Counting them as failures would produce ~0% on Sort
Accuracy even with passing judge_prompt verdicts.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from statistics import mean

import asyncpg

from compass_backend.db.builds import (
    BuildRecord,
    load_build,
    load_latest_build,
)
from compass_backend.db.scorecard import (
    CaseRollup,
    SweepRunSummary,
    load_case_rollups_for_dimension,
    load_completed_sweep_runs_for_dimension,
    load_conversation_turns,
    load_session_metadata,
    load_session_scenario_context,
    load_verdicts_for_session_grouped_by_turn,
)
from compass_backend.execution._text_utils import truncate
from compass_backend.quality.exemplar_manifest import (
    DimensionExemplarInventory,
    ExemplarManifest,
    audit_exemplar_coverage,
    load_exemplar_inventory,
    load_exemplar_manifest,
)
from compass_backend.quality.scorecard_models import (
    CANONICAL_DIM_SLUGS,
    BuildContext,
    ConversationTurn,
    ConversationWithVerdicts,
    DimensionDetail,
    DimensionScore,
    ScenarioCase,
    ScorecardSnapshot,
    Trial,
    Verdict,
)
from compass_backend.quality.thresholds import (
    ThresholdConfig,
    load_threshold_config,
    threshold_for_slug,
    threshold_status,
)

logger = logging.getLogger(__name__)

# ── Build registry ────────────────────────────────────────────────────────

# Bootstrap build identifier. Used as the fallback when the registry table
# (compass.builds — migration 075) is empty and no build_id is supplied.
# The row with this id is seeded by migration 075; passing a different
# build_id makes resolve_build_or_default query the registry.
_DEFAULT_BUILD_ID = "b-2026-05-14"
_DEFAULT_CRITERION_SET_VERSION = "compass_criteria_v1"


async def resolve_build_or_default(
    pool: asyncpg.Pool | None,
    build_id: str | None,
) -> BuildRecord:
    """Resolve ``build_id`` against ``compass.builds``.

    None  → returns the most recently created registered build. Falls back
            to a synthetic BuildRecord at _DEFAULT_BUILD_ID if the registry
            is empty or the pool is None (test fixtures that don't touch
            the DB).
    set   → requires the build to exist in the registry; raises ValueError
            otherwise so callers can't silently address an unknown build.
    """

    if pool is None:
        # Test paths that monkeypatch the data loaders pass pool=None and
        # never need a real registry record.
        return _bootstrap_build_record(build_id or _DEFAULT_BUILD_ID)

    if build_id is None:
        latest = await load_latest_build(pool)
        if latest is not None:
            return latest
        return _bootstrap_build_record(_DEFAULT_BUILD_ID)

    record = await load_build(pool, build_id)
    if record is None:
        raise ValueError(
            f"build_id={build_id!r} is not registered in compass.builds. "
            f"Register the build before reading its scorecard, or omit "
            f"build_id to use the latest registered build."
        )
    return record


def _bootstrap_build_record(build_id: str) -> BuildRecord:
    """Synthetic BuildRecord used when the registry is unreachable.

    Only returned for pool=None paths; production reads go through the DB.
    """
    return BuildRecord(
        build_id=build_id,
        criterion_set_version=_DEFAULT_CRITERION_SET_VERSION,
        git_sha=None,
        description=None,
        created_at=datetime(2026, 5, 14, tzinfo=UTC),
        finished_at=None,
    )

# ── Dimension slug ↔ human name + definition ─────────────────────────────

STOCK_DIMENSIONS: dict[str, tuple[str, str]] = {
    "selection-accuracy": (
        "Selection Accuracy",
        "Picked the right districts, metrics, peer set, year.",
    ),
    "data-fidelity": (
        "Data Fidelity",
        "Every number, count, denominator matches the source.",
    ),
    "coverage-state-labeling": (
        "Coverage-State Labeling",
        "Tells the user when a district is or is not in the covered universe.",
    ),
    "filter-accuracy": (
        "Filter Accuracy",
        "Honored every constraint in the question.",
    ),
    "sort-accuracy": (
        "Sort Accuracy",
        "Picked the right ordering and broke ties correctly.",
    ),
    "citation-accuracy": (
        "Citation Accuracy",
        "Every claim ties back to a real source row.",
    ),
    "consistency": (
        "Consistency",
        "Same prompt produces the same answer across reruns.",
    ),
}


def _slug_to_human(dim_slug: str) -> str:
    """Convert kebab-case slug to the human-readable dimension name.

    e.g. "sort-accuracy" → "Sort Accuracy".
    Falls back to title-case conversion for any unknown slug.
    """
    name, _ = STOCK_DIMENSIONS.get(dim_slug, (None, None))
    if name:
        return name
    return dim_slug.replace("-", " ").title()


def _exemplar_fields(
    dim_slug: str,
    manifest: ExemplarManifest,
    exemplar_inventory: Sequence[DimensionExemplarInventory] | None,
) -> dict[str, object]:
    audit = audit_exemplar_coverage(manifest, list(exemplar_inventory or ()))
    rows_by_slug = {row.dim_slug: row for row in audit.dimensions}
    row = rows_by_slug.get(dim_slug)
    return {
        "exemplar_case_count": row.active_case_count if row else 0,
        "exemplar_status": row.status if row else "incomplete",
    }


# ── Score-exclusion filters ───────────────────────────────────────────────


_NOT_APPLICABLE_REASON_PREFIX = "not applicable:"


def _is_stub_error(trial: dict | object) -> bool:
    """Return True if this trial is a deterministic stub error.

    Stub errors arise from PR 5's RecordDeterministicEvaluator, which
    emits outcome='error' with judge_source='deterministic' for every rule
    that hasn't been wired to a real validator yet. These should be excluded
    from BOTH numerator and denominator so they don't deflate the score.
    """
    if isinstance(trial, dict):
        return trial["outcome"] == "error" and trial["judge_source"] == "deterministic"
    return trial.outcome == "error" and trial.judge_source == "deterministic"


def _is_not_applicable(trial: dict | object) -> bool:
    """Return True if this trial recorded "not applicable" on this turn.

    Convention introduced for `respects_user_limit` (see
    `quality/validators/registry.py`): a validator that fires on every turn
    of a given shape but only meaningfully applies to a subset returns
    outcome='error' with reason prefixed `not applicable:` for turns where
    it has no constraint to evaluate. The Scorecard excludes these from
    both numerator and denominator so they neither pass nor fail vacuously.

    Real `missing artifact:` errors (validator wanted X, got nothing) stay
    in the error bucket as signal that something genuinely broke.
    """
    if isinstance(trial, dict):
        outcome = trial.get("outcome")
        reason = trial.get("reason") or ""
    else:
        outcome = getattr(trial, "outcome", None)
        reason = getattr(trial, "reason", None) or ""
    return outcome == "error" and reason.startswith(_NOT_APPLICABLE_REASON_PREFIX)


def _is_infra_skipped(trial: dict | object) -> bool:
    """Return True for evaluator lanes skipped because required infra is absent."""
    if isinstance(trial, dict):
        outcome = trial.get("outcome")
        reason = trial.get("reason") or ""
    else:
        outcome = getattr(trial, "outcome", None)
        reason = getattr(trial, "reason", None) or ""
    reason_lower = reason.casefold()
    return outcome == "error" and (
        "infra unavailable" in reason_lower
        or "logfire_read_token" in reason_lower
        or "pydantic_ai_gateway_api_key" in reason_lower
    )


def _is_excluded_from_score(trial: dict | object) -> bool:
    """Return True if this trial should be skipped from pass-rate math.

    Combines stub errors (unwired deterministic validators) and
    not-applicable trials (validators that fired but had no constraint to
    evaluate). Both are excluded from numerator AND denominator so the
    Scorecard reports the rate of pass over *evaluable* trials only.
    """
    return _is_stub_error(trial) or _is_not_applicable(trial) or _is_infra_skipped(trial)


# ── Display helpers ───────────────────────────────────────────────────────


_CASE_NAME_MAX_LEN = 60


def _derive_case_display_name(
    case_code: str,
    inputs: list | None,
) -> str:
    """Return a human-friendly display name for a case.

    Strategy: take the first user-prompt string from the case's ``inputs``
    JSONB payload, trim whitespace, truncate to ``_CASE_NAME_MAX_LEN`` and
    append an ellipsis when truncated. Falls back to ``case_code`` if no
    usable prompt is present, so display never breaks.

    The expected ``inputs`` shape is a list of step dicts, each shaped like
    ``{"prompt": "...", "step_complete_when": ...}`` (see
    ``compass_backend.quality.criteria.StepInput``).
    """
    if not inputs or not isinstance(inputs, list):
        return case_code
    for step in inputs:
        if not isinstance(step, dict):
            continue
        prompt = step.get("prompt")
        if not isinstance(prompt, str):
            continue
        prompt = prompt.strip()
        if not prompt:
            continue
        return truncate(prompt, _CASE_NAME_MAX_LEN)
    return case_code


# ── Case-level math ───────────────────────────────────────────────────────


def _case_pass_rate(rollup: CaseRollup) -> float | None:
    """Compute pass rate for one (scenario, case, session) triple.

    Returns None if every verdict in this case is excluded (stub error
    or not-applicable), so the case is excluded from the scenario mean
    entirely.

    Excluded trials (skipped from BOTH numerator AND denominator):
    - stub errors: ``outcome='error' AND judge_source='deterministic'``
      from unwired deterministic validators
    - not applicable: ``outcome='error' AND reason starts with 'not
      applicable:'`` from validators that fired but had no constraint
      to evaluate (convention; see ``_is_not_applicable``)
    """
    evaluable = [t for t in rollup.trials if not _is_excluded_from_score(t)]
    if not evaluable:
        return None
    passes = sum(1 for t in evaluable if t.outcome == "pass")
    return passes / len(evaluable)


# ── Scenario-level math ───────────────────────────────────────────────────


def _scenario_score(case_rates: list[float]) -> float:
    """Mean of per-case pass rates; returns 0.0 if no cases."""
    return mean(case_rates) if case_rates else 0.0


def _score_rollups(rollups: list[CaseRollup]) -> tuple[int, int] | None:
    """Return (score_pct, n_trials) for rollups, or None when no evaluable data."""
    if not rollups:
        return None

    by_scenario: dict[int, list[CaseRollup]] = {}
    for r in rollups:
        by_scenario.setdefault(r.scenario_id, []).append(r)

    scenario_scores: list[float] = []
    for scen_rollups in by_scenario.values():
        case_rates = []
        for cr in scen_rollups:
            rate = _case_pass_rate(cr)
            if rate is not None:
                case_rates.append(rate)
        if case_rates:
            scenario_scores.append(_scenario_score(case_rates))

    if not scenario_scores:
        return None

    score_pct = round(mean(scenario_scores) * 100)
    n_trials = sum(
        len([t for t in r.trials if not _is_excluded_from_score(t)])
        for r in rollups
    )
    return score_pct, n_trials


_COMPARABLE_SWEEP_FIELDS = (
    "case_count",
    "trial_count",
    "verdict_count",
    "k",
    "git_sha",
    # Grader-provenance fields (migration 091, issue #1257). A changed grader —
    # bumped criterion_set_version, a different criterion_version_hashes digest
    # (the silent single-criterion edit the static version constant misses), or
    # a different check_types set — makes the two runs not comparable so the
    # Scorecard does not report a delta across a moved grader (Gate G0).
    "criterion_set_version",
    "criterion_version_hashes",
    "check_types",
)


def _sweep_runs_are_comparable(
    current: SweepRunSummary,
    previous: SweepRunSummary,
) -> bool:
    """Return whether two sweep runs are similar enough for trend math.

    Requires every comparable field — run-shape (case/trial/verdict counts, k,
    git_sha) and grader provenance (criterion_set_version,
    criterion_version_hashes, check_types) — to match and be present before
    exposing a delta. A None on either side (e.g. a pre-091 run with no grader
    fingerprint) is treated as not comparable, the safe default.
    """
    for field_name in _COMPARABLE_SWEEP_FIELDS:
        current_value = getattr(current, field_name)
        previous_value = getattr(previous, field_name)
        if current_value is None or current_value != previous_value:
            return False
    return True


# ── Backend pure functions ─────────────────────────────────────────────────


async def compute_dimension_score(
    pool: asyncpg.Pool,
    dim_slug: str,
    *,
    sweep_run_id: str | None = None,
    threshold_config: ThresholdConfig | None = None,
    exemplar_manifest: ExemplarManifest | None = None,
    exemplar_inventory: Sequence[DimensionExemplarInventory] | None = None,
) -> DimensionScore:
    """Compute a DimensionScore for one dimension slug.

    Returns a placeholder DimensionScore (score_pct=0, n_trials=0) when
    the dimension has no scenarios or no verdicts yet.

    Args:
        pool: asyncpg connection pool.
        dim_slug: Kebab-case dimension identifier, e.g. "sort-accuracy".
        sweep_run_id: Optional sweep run UUID to restrict to one sweep.
    """
    name, definition = STOCK_DIMENSIONS.get(
        dim_slug,
        (_slug_to_human(dim_slug), ""),
    )
    human_name = name
    resolved_threshold_config = threshold_config or load_threshold_config()
    threshold_pct = threshold_for_slug(dim_slug, resolved_threshold_config)
    manifest = exemplar_manifest or load_exemplar_manifest()
    exemplar_kwargs = _exemplar_fields(dim_slug, manifest, exemplar_inventory)

    effective_sweep_run_id = sweep_run_id
    latest_sweep_run_id: str | None = None
    latest_finished_at: str | None = None
    previous_sweep_run_id: str | None = None
    if sweep_run_id is None:
        try:
            completed_runs = await load_completed_sweep_runs_for_dimension(
                pool,
                human_name,
                limit=2,
            )
        except Exception:
            if pool is not None:
                raise
            completed_runs = []
        if completed_runs:
            latest = completed_runs[0]
            effective_sweep_run_id = latest.id
            latest_sweep_run_id = latest.id
            latest_finished_at = latest.finished_at
            if len(completed_runs) > 1:
                previous = completed_runs[1]
                if _sweep_runs_are_comparable(latest, previous):
                    previous_sweep_run_id = previous.id

    rollups = await load_case_rollups_for_dimension(
        pool, human_name, sweep_run_id=effective_sweep_run_id
    )

    score_result = _score_rollups(rollups)
    previous_score_pct: int | None = None
    delta_pct: int | None = None
    if previous_sweep_run_id is not None and score_result is not None:
        previous_rollups = await load_case_rollups_for_dimension(
            pool,
            human_name,
            sweep_run_id=previous_sweep_run_id,
        )
        previous_result = _score_rollups(previous_rollups)
        if previous_result is not None:
            previous_score_pct = previous_result[0]
            delta_pct = score_result[0] - previous_score_pct

    if score_result is None:
        return DimensionScore(
            dim_slug=dim_slug,
            name=human_name,
            definition=definition,
            score_pct=0,
            n_trials=0,
            regressed=False,
            threshold_pct=threshold_pct,
            threshold_status=threshold_status(
                score_pct=0,
                n_trials=0,
                threshold_pct=threshold_pct,
            ),
            latest_sweep_run_id=latest_sweep_run_id,
            latest_finished_at=latest_finished_at,
            **exemplar_kwargs,
        )

    score_pct, n_trials = score_result

    return DimensionScore(
        dim_slug=dim_slug,
        name=human_name,
        definition=definition,
        score_pct=score_pct,
        n_trials=n_trials,
        regressed=delta_pct is not None and delta_pct < 0,
        threshold_pct=threshold_pct,
        threshold_status=threshold_status(
            score_pct=score_pct,
            n_trials=n_trials,
            threshold_pct=threshold_pct,
        ),
        latest_sweep_run_id=latest_sweep_run_id,
        latest_finished_at=latest_finished_at,
        previous_score_pct=previous_score_pct,
        delta_pct=delta_pct,
        **exemplar_kwargs,
    )


async def compute_scorecard_snapshot(
    pool: asyncpg.Pool,
    *,
    build_id: str | None = None,
    sweep_run_id: str | None = None,
) -> ScorecardSnapshot:
    """Compute a full ScorecardSnapshot across all 7 canonical dimensions.

    The non-Sort dimensions will return placeholder DimensionScores
    (score_pct=0, n_trials=0) until their scenarios and verdicts are seeded.

    Args:
        pool: asyncpg connection pool.
        build_id: Identifier into ``compass.builds``. ``None`` resolves to
                  the most recently registered build (or the bootstrap
                  row if the registry is empty).
        sweep_run_id: Optional sweep run UUID to restrict verdicts.
    """
    build_record = await resolve_build_or_default(pool, build_id)
    threshold_config = load_threshold_config()
    exemplar_manifest = load_exemplar_manifest()
    try:
        exemplar_inventory = await load_exemplar_inventory(pool)
    except Exception:
        if pool is not None:
            raise
        exemplar_inventory = []

    dimensions = []
    total_trials = 0
    total_cases = 0
    for slug in CANONICAL_DIM_SLUGS:
        dim = await compute_dimension_score(
            pool,
            slug,
            sweep_run_id=sweep_run_id,
            threshold_config=threshold_config,
            exemplar_manifest=exemplar_manifest,
            exemplar_inventory=exemplar_inventory,
        )
        dimensions.append(dim)
        total_trials += dim.n_trials
        if dim.n_trials > 0:
            total_cases += 1  # approximate: at least 1 case per dim with data

    build = BuildContext(
        build_id=build_record.build_id,
        sweep_id=sweep_run_id or "live",
        criterion_set_version=build_record.criterion_set_version,
        cases=total_cases,
        trials=total_trials,
        threshold_version=threshold_config.version,
        threshold_review_date=threshold_config.review_date,
    )

    return ScorecardSnapshot(build=build, dimensions=dimensions)


async def compute_dimension_detail(
    pool: asyncpg.Pool,
    dim_slug: str,
    *,
    sweep_run_id: str | None = None,
    build_id: str | None = None,
) -> DimensionDetail:
    """Compute a full DimensionDetail for one dimension (Scenarios × Cases × Trials).

    Returns a DimensionDetail with an empty cases list when the dimension
    has no verdicts yet.

    Args:
        pool: asyncpg connection pool.
        dim_slug: Kebab-case dimension identifier, e.g. "sort-accuracy".
        sweep_run_id: Optional sweep run UUID to restrict verdicts.
        build_id: Optional build identifier into ``compass.builds``. Defaults
                  to the most recently registered build.
    """
    build_record = await resolve_build_or_default(pool, build_id)
    threshold_config = load_threshold_config()
    exemplar_manifest = load_exemplar_manifest()
    try:
        exemplar_inventory = await load_exemplar_inventory(pool)
    except Exception:
        if pool is not None:
            raise
        exemplar_inventory = []

    dim_score = await compute_dimension_score(
        pool,
        dim_slug,
        sweep_run_id=sweep_run_id,
        threshold_config=threshold_config,
        exemplar_manifest=exemplar_manifest,
        exemplar_inventory=exemplar_inventory,
    )

    name, _ = STOCK_DIMENSIONS.get(dim_slug, (_slug_to_human(dim_slug), ""))
    rollups = await load_case_rollups_for_dimension(
        pool, name, sweep_run_id=sweep_run_id
    )

    # Group by (scenario_id, case_id) → list of session rollups (one per K trial)
    by_case: dict[tuple[int, int], list[CaseRollup]] = {}
    for r in rollups:
        key = (r.scenario_id, r.case_id)
        by_case.setdefault(key, []).append(r)

    scenario_cases: list[ScenarioCase] = []
    for (scenario_id, case_id), session_rollups in sorted(by_case.items()):
        # Compute pass rate across all sessions for this case
        all_evaluable_trials: list[object] = []
        trials: list[Trial] = []
        for sr in session_rollups:
            evaluable = [t for t in sr.trials if not _is_excluded_from_score(t)]
            all_evaluable_trials.extend(evaluable)
            # One "trial" per session: pass if all evaluable verdicts pass
            # Priority order: fail (real signal) > error (judge couldn't decide) > pass.
            # Checking fail BEFORE error means a (fail, fail, error) case surfaces as
            # "fail" in the drill-down, not "error" — the failures are the real signal
            # the reviewer cares about.
            if not evaluable:
                outcome = "error"
                reason = "all verdicts were stub errors or not applicable"
            elif any(t.outcome == "fail" for t in evaluable):
                outcome = "fail"
                reasons = [t.reason for t in evaluable if t.reason and t.outcome == "fail"]
                reason = reasons[0] if reasons else None
            elif any(t.outcome == "error" for t in evaluable):
                outcome = "error"
                reasons = [t.reason for t in evaluable if t.reason and t.outcome == "error"]
                reason = reasons[0] if reasons else None
            else:
                outcome = "pass"
                reason = None
            trials.append(Trial(
                outcome=outcome,
                session_id=sr.session_id,
                reason=reason,
            ))

        passes = sum(1 for t in trials if t.outcome == "pass")
        n_eval = len([t for t in trials if t.outcome != "error"])
        pass_rate_pct = round(passes / n_eval * 100) if n_eval > 0 else 0

        # Use first session_rollup for the case code/name
        first = session_rollups[0]
        scenario_cases.append(ScenarioCase(
            scenario_id=first.scenario_code,
            case_id=first.case_code,
            name=_derive_case_display_name(first.case_code, first.case_inputs),
            pass_rate_pct=pass_rate_pct,
            trials=trials,
        ))

    build = BuildContext(
        build_id=build_record.build_id,
        sweep_id=sweep_run_id or "live",
        criterion_set_version=build_record.criterion_set_version,
        cases=len(by_case),
        trials=dim_score.n_trials,
        threshold_version=threshold_config.version,
        threshold_review_date=threshold_config.review_date,
    )

    return DimensionDetail(
        build=build,
        dimension=dim_score,
        cases=scenario_cases,
    )


async def compute_conversation_with_verdicts(
    pool: asyncpg.Pool,
    session_id: str,
) -> ConversationWithVerdicts:
    """Compute a ConversationWithVerdicts for one session.

    Joins chat_sessions/chat_messages for user/assistant turn text, then
    attaches compass.verdicts grouped by turn_index.

    Args:
        pool: asyncpg connection pool.
        session_id: UUID string of the chat session.

    Raises:
        KeyError: If the session_id is not found in chat_sessions.
    """
    meta = await load_session_metadata(pool, session_id)
    if meta is None:
        raise KeyError(f"Session not found: {session_id}")

    turns_data = await load_conversation_turns(pool, session_id)
    verdicts_by_turn = await load_verdicts_for_session_grouped_by_turn(
        pool, session_id
    )
    scenario_ctx = await load_session_scenario_context(pool, session_id)

    turns: list[ConversationTurn] = []
    for turn_dict in turns_data:
        ti = turn_dict["turn_index"]
        raw_verdicts = verdicts_by_turn.get(ti, [])
        verdicts: list[Verdict] = []
        for v in raw_verdicts:
            try:
                verdicts.append(Verdict(
                    criterion_id=v["criterion_code"],
                    judge_source=v["judge_source"],
                    outcome=v["outcome"],
                    reason=v["reason"],
                ))
            except Exception as exc:
                logger.warning(
                    "scorecard: skipping malformed verdict for session %s turn %d: %s",
                    session_id, ti, exc,
                )
        turns.append(ConversationTurn(
            turn_index=ti,
            user_text=turn_dict["user_text"],
            assistant_text=turn_dict["assistant_text"],
            verdicts=verdicts,
        ))

    scenario_id_str: str | None = None
    scenario_name: str | None = None
    if scenario_ctx is not None:
        scenario_id_str = str(scenario_ctx["scenario_id"])
        scenario_name = scenario_ctx["scenario_name"]

    return ConversationWithVerdicts(
        session_id=meta["session_id"],
        trace_id=meta.get("trace_id"),
        scenario_id=scenario_id_str,
        scenario_name=scenario_name,
        started_at=meta["created_at"],
        turns=turns,
    )


__all__ = [
    "STOCK_DIMENSIONS",
    "_DEFAULT_BUILD_ID",
    "_derive_case_display_name",
    "resolve_build_or_default",
    "compute_dimension_score",
    "compute_scorecard_snapshot",
    "compute_dimension_detail",
    "compute_conversation_with_verdicts",
]
