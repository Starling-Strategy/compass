"""Deterministic validator registry for compass.criteria-backed L2 evaluation.

`RecordDeterministicEvaluator` (in `quality/criteria.py`) dispatches through
`lookup(validator_name)` to find the function that evaluates a deterministic
criterion. Validators are registered at import time via `@register("<name>")`.

The registry is the single source of truth for "which `validator_name` strings
are implementable today" — a startup sanity check diffs `known_validators()`
against `compass.criteria` and warns on unknown names.

Validator contract
------------------

A validator is an async callable with signature:

    async def fn(
        ctx: CompassEvaluatorContext,
        payload: dict,
    ) -> ValidatorOutcome

`payload` is the JSONB blob from `compass.criteria.payload` for the firing
criterion. It always contains `validator_name`; validators may consume
additional payload keys for per-criterion configuration.

Inputs come from `ctx`:
  - `ctx.answer_text`        — final rendered Writer text
  - `ctx.intent`             — short string like "sort by enrollment"
  - `ctx.artifact_snapshot`  — compact dict (see `_build_context` in
                               `quality/verdict_pipeline.py`); commonly
                               `{"table": [{"district_name", "display_value"}], ...}`
  - `ctx.trace_id`           — for span-shaped validators (not used here today)

A validator must never raise. Missing required inputs → return
`ValidatorOutcome(outcome="error", reason="missing artifact: <key>", ...)`.
Genuine `pass`/`fail` decisions are reserved for the case where the inputs are
present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, get_args

from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.coverage import is_coverage_gap_display
from compass_backend.contracts.planning import PlannerRoute
from compass_backend.db.rows import VerdictOutcome
from compass_backend.execution.selection import direction as _requested_sort_direction
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.validators.surface import _expected_answer_rows
from compass_backend.reference import normalize_whitespace_casefold
from compass_backend.reference.profile_fields import PROFILE_SENTINEL_METRIC_IDS
from compass_backend.rendering.policy_guidance import PROVISIONAL_SOURCE_MARKER


@dataclass(frozen=True)
class ValidatorOutcome:
    """Result of running one deterministic validator on one turn."""

    outcome: VerdictOutcome
    reason: str
    evidence: dict = field(default_factory=dict)


ValidatorFn = Callable[[CompassEvaluatorContext, dict], Awaitable[ValidatorOutcome]]


_REGISTRY: dict[str, ValidatorFn] = {}


def register(name: str) -> Callable[[ValidatorFn], ValidatorFn]:
    """Decorator: register a validator function under `name`."""

    def _decorate(fn: ValidatorFn) -> ValidatorFn:
        if name in _REGISTRY:
            raise RuntimeError(
                f"validator registry: '{name}' is already registered "
                f"(existing={_REGISTRY[name].__qualname__}, new={fn.__qualname__})"
            )
        _REGISTRY[name] = fn
        return fn

    return _decorate


def lookup(name: str) -> ValidatorFn | None:
    """Return the validator registered under `name`, or None if unknown."""

    return _REGISTRY.get(name)


def known_validators() -> frozenset[str]:
    """Return the set of validator_names currently implementable."""

    return frozenset(_REGISTRY)


# ─── Validators ───────────────────────────────────────────────────────────────
#
# Each validator is registered with @register. Import-time execution of this
# module populates the registry.


# Routes that produce a deterministic execution artifact (a ``ResultSet`` and
# its rows/table). Only ``execute`` builds one; ``direct``/``clarify`` answer or
# ask without one, and ``policy_guidance`` emits a manifest, not a ResultSet.
# So a result/table-requiring validator firing on a non-execute turn has
# *nothing to evaluate* — that is a turn-shape mismatch (harness gap), not a
# broken pipeline.
_RESULT_BEARING_ROUTES: frozenset[str] = frozenset({"execute"})


def _missing_artifact_outcome(
    ctx: CompassEvaluatorContext,
    *,
    validator_name: str,
    artifact: str,
) -> ValidatorOutcome:
    """Build the right outcome when a result/table-requiring artifact is absent.

    Two cases, discriminated by the *turn's route* (U3 (c) triage):

    - **Non-execute route** (``direct`` / ``clarify`` / ``policy_guidance``):
      the route legitimately produces no ``ResultSet``, so the validator has
      nothing to evaluate. Return ``not applicable:`` so the Scorecard drops
      the trial from numerator AND denominator (see
      ``quality/scorecard.py::_is_not_applicable``) instead of counting it as
      a harness error — this is a turn-shape mismatch, not a pipeline failure.

    - **Execute route, or route unknown (``None``)**: an execute turn that
      produced no result/table IS a genuine pipeline failure, and an unknown
      route is treated conservatively as one too (the sweep lane always
      populates the route, so ``None`` only arises off the sweep path). Keep
      the ``missing artifact:`` error so the failure stays visible as a
      candidate real defect.

    Note: ``policy_guidance``-aware validators (e.g.
    ``sources_block_is_deduplicated``) handle that route *before* calling this
    helper, so by the time control reaches here only ``direct``/``clarify``
    remain among the non-execute routes for them — and policy_guidance→NA is
    correct anyway for the result-only validators, which read fields a
    manifest never populates.
    """

    if ctx.route is not None and ctx.route not in _RESULT_BEARING_ROUTES:
        return ValidatorOutcome(
            outcome="error",
            reason=(
                f"not applicable: route is {ctx.route!r}, which produces no "
                f"{artifact} to evaluate"
            ),
            evidence={"validator_name": validator_name, "route": ctx.route},
        )
    return ValidatorOutcome(
        outcome="error",
        reason=f"missing artifact: {artifact}",
        evidence={"validator_name": validator_name, "route": ctx.route},
    )


# Ported verbatim from the archived src/archive/compass_agents/response_guards.py
# (FORBIDDEN_DISCLOSURE_PHRASES + INA_WHITELIST_TOKENS + INA_WINDOW). The
# archived check_forbidden_phrases scanned Writer output for unanchored
# data-availability claims; an INA whitelist token within ±200 chars suppresses
# the match. Migration 015_*'s criterion text gives different examples ("as of",
# "currently") — extend this list if/when those specific phrasings need
# coverage; the archived list is the product-tested baseline.
_FORBIDDEN_DISCLOSURE_PHRASES: tuple[str, ...] = (
    "not released yet",
    "not yet released",
    "currently unavailable",
    "data is not available yet",
    "not available yet",
    "we don't have",
    "not in the database",
    "not in our database",
    "not in the records",
    "not in our records",
    "no data for this district",
    "no data on this district",
    "not on file",
)
_INA_WHITELIST_TOKENS: tuple[str, ...] = (
    "issue not addressed",
    "issue-not-addressed",
    "ina",
    "i/a",
    "not applicable",
    "covered districts",
)
_INA_WINDOW = 200


@register("forbidden_temporal_phrases")
async def _validate_forbidden_temporal_phrases(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Flag temporal/disclosure phrases not anchored to canonical INA wording."""

    text = ctx.answer_text or ""
    if not text:
        return ValidatorOutcome(
            outcome="error",
            reason="missing artifact: answer_text",
            evidence={"validator_name": "forbidden_temporal_phrases"},
        )
    lower = text.lower()
    flagged: list[str] = []
    for phrase in _FORBIDDEN_DISCLOSURE_PHRASES:
        start = 0
        while True:
            idx = lower.find(phrase, start)
            if idx == -1:
                break
            window_start = max(0, idx - _INA_WINDOW)
            window_end = min(len(lower), idx + len(phrase) + _INA_WINDOW)
            window = lower[window_start:window_end]
            if not any(token in window for token in _INA_WHITELIST_TOKENS):
                flagged.append(phrase)
                break
            start = idx + len(phrase)
    if flagged:
        return ValidatorOutcome(
            outcome="fail",
            reason=f"unanchored disclosure phrase(s): {flagged}",
            evidence={"flagged_phrases": flagged},
        )
    return ValidatorOutcome(
        outcome="pass",
        reason="no unanchored disclosure phrases detected",
        evidence={"phrases_scanned": len(_FORBIDDEN_DISCLOSURE_PHRASES)},
    )


# Numeric token: capture leading minus, integer part, optional comma groups,
# optional decimal. Strips trailing % / $ / unit chars by isolating to the run
# of digit/comma/period characters around an optional sign.
_NUMERIC_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _parse_display_value_numeric(s: str) -> float | None:
    """Parse the first numeric token from a display_value string.

    `display_value` is a human-formatted string (e.g. "1,234,567", "$45.2M",
    "78%", "n/a"). Returns the first numeric token as a float, or None if no
    numeric token is found. Suffix multipliers like "M" / "K" are NOT applied —
    sort_descending compares within the same column where formatting is
    consistent, so raw numeric prefix ordering matches semantic ordering.
    """
    if not s:
        return None
    m = _NUMERIC_RE.search(s)
    if m is None:
        return None
    raw = m.group(0).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


@register("sort_descending")
async def _validate_sort_descending(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Check that a table's display_value column is monotonically non-increasing.

    Reads the compact `artifact_snapshot["table"]` populated by
    `quality/verdict_pipeline._build_context`: a list of dicts each with
    `district_name`, `display_value`, and optionally `sort_display_value`.
    Parses the sort display value when present, otherwise the display value,
    and confirms row N+1 <= row N.
    """

    table = ctx.artifact_snapshot.table
    if not table:
        return _missing_artifact_outcome(
            ctx, validator_name="sort_descending", artifact="table"
        )
    # Multi-metric ranked lookups emit one row per district-metric cell, so a
    # flat scan of `display_value` interleaves the ranked column with unranked
    # comparison columns and trips spuriously (#1460 defect 2: case 1006/1032
    # showed salaries interleaved with workday counts). When the snapshot names
    # the ranked metric, isolate that column — the unranked columns are
    # legitimately unsorted and must not be scanned. Rows missing a metric tag
    # (legacy/single-metric snapshots) keep `metric_name=None`, so an
    # unmatched filter leaves `table` unchanged and the historical single-column
    # behavior is preserved.
    ranked_metric = ctx.artifact_snapshot.ranked_by_metric_name
    if ranked_metric is not None:
        ranked_rows = [row for row in table if row.metric_name == ranked_metric]
        if ranked_rows:
            table = ranked_rows
    values: list[float] = []
    skipped_gap_rows = 0
    for row in table:
        display_value = row.sort_display_value or row.display_value
        # Coverage-gap sentinels ("Issue not addressed", "Not reviewed",
        # "Not applicable for …", out-of-universe sentences) mean NCTQ holds
        # no numeric value for that district — they are legitimately absent,
        # not a parse failure. Skip them from the numeric sequence rather than
        # erroring, so the real numeric rows still get a sort check. (U3 (b):
        # this stops the "non-numeric display_value: 'Issue not addressed …'"
        # ERROR-bucket noise from masking signal.)
        if is_coverage_gap_display(display_value):
            skipped_gap_rows += 1
            continue
        v = _parse_display_value_numeric(display_value)
        if v is None:
            return ValidatorOutcome(
                outcome="error",
                reason=f"non-numeric display_value: {display_value!r}",
                evidence={
                    "district_name": row.district_name,
                    "display_value": display_value,
                },
            )
        values.append(v)
    if not values:
        # Every row was a coverage gap — there is no numeric ordering to
        # check. Use the `not applicable:` convention (see
        # quality/scorecard.py::_is_not_applicable) so the Scorecard drops
        # this trial from both numerator and denominator rather than recording
        # a vacuous pass on an all-gap table.
        return ValidatorOutcome(
            outcome="error",
            reason="not applicable: all rows are coverage-gap sentinels (no numeric values to sort)",
            evidence={
                "validator_name": "sort_descending",
                "skipped_gap_rows": skipped_gap_rows,
            },
        )
    if len(values) < 2:
        return ValidatorOutcome(
            outcome="pass",
            reason=f"trivially sorted: row_count={len(values)}",
            evidence={"row_count": len(values)},
        )
    # The requested sort direction governs which monotonicity to assert. A
    # "lowest first" (ascending) request is correctly answered ascending, so
    # asserting descending there is a mis-fire (#1691, case 7 "10 lowest
    # starting salaries"). `selection.direction` is the one canonical resolver
    # of asc/desc on a finalized plan. Default to "desc" — the conventional
    # ranking default — when no plan is attached (plan-less callers / off-sweep
    # paths), preserving the historical descending-only behavior.
    requested_direction = (
        _requested_sort_direction(ctx.plan) if ctx.plan is not None else "desc"
    )
    order_word = "ascending" if requested_direction == "asc" else "descending"
    for i in range(len(values) - 1):
        out_of_order = (
            values[i] > values[i + 1]
            if requested_direction == "asc"
            else values[i] < values[i + 1]
        )
        if out_of_order:
            relation = ">" if requested_direction == "asc" else "<"
            return ValidatorOutcome(
                outcome="fail",
                reason=(
                    f"rows not sorted {order_word}: position {i} ({values[i]}) "
                    f"{relation} position {i + 1} ({values[i + 1]})"
                ),
                evidence={
                    "first_violation_index": i,
                    "requested_direction": requested_direction,
                    "row_count": len(values),
                    "values_sample": values[: min(len(values), 10)],
                },
            )
    return ValidatorOutcome(
        outcome="pass",
        reason=f"rows sorted {order_word}: row_count={len(values)}",
        evidence={"row_count": len(values), "requested_direction": requested_direction},
    )


@register("chart_visibility_matches_intent")
async def _validate_chart_visibility_matches_intent(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """EXPORT-R4: a chart artifact appears only when the user asked for one.

    Chart presence is a structural artifact (`result.chart`) the answer-text
    judge cannot see — it is absent from the evidence envelope — so this
    deterministic check is the gate for the graph-off-by-default rule (#1240).
    It reads the *served* result (`ctx.result`, after the chart-visibility
    resolver in orchestration ran) against the plan's output intent.

    - User did NOT request a chart (`plan.output.format != "chart"`): a chart
      MUST be absent. A present chart is the #1240 bug — an unrequested chart
      decorating a table or ranking answer.
    - User requested a chart (`format == "chart"`): pass. A present chart is the
      happy path; an absent chart is the thin-data case (too few comparable
      points to plot), whose graceful explanation is graded by the
      ``chart_unavailable_explained`` judge, not here.
    """

    if ctx.plan is None or ctx.result is None:
        return _missing_artifact_outcome(
            ctx,
            validator_name="chart_visibility_matches_intent",
            artifact="result",
        )
    output_format = ctx.plan.output.format
    requested = output_format == "chart"
    chart_present = getattr(ctx.result, "chart", None) is not None
    if not requested and chart_present:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"unrequested chart rendered: plan.output.format={output_format!r} "
                "but a chart artifact was emitted (EXPORT-R4 graph-off-by-default)"
            ),
            evidence={
                "output_format": output_format,
                "chart_present": True,
                "result_type": getattr(ctx.result, "result_type", None),
            },
        )
    return ValidatorOutcome(
        outcome="pass",
        reason=(
            f"chart visibility matches intent: requested={requested}, "
            f"chart_present={chart_present}"
        ),
        evidence={
            "output_format": output_format,
            "requested": requested,
            "chart_present": chart_present,
        },
    )


# Match "top 5", "top-5", "top N=5", "top  10", case-insensitive
_TOP_N_RE = re.compile(r"\btop[\s\-=:]*?(\d+)\b", re.IGNORECASE)


def _parse_user_limit(intent: str | None) -> int | None:
    """Extract the integer following 'top' in an intent string."""

    if not intent:
        return None
    m = _TOP_N_RE.search(intent)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


@register("respects_user_limit")
async def _validate_respects_user_limit(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Compare table row count to the user-requested limit parsed from intent."""

    table = ctx.artifact_snapshot.table
    if not table:
        return _missing_artifact_outcome(
            ctx, validator_name="respects_user_limit", artifact="table"
        )
    # Read the raw user message first. `ctx.intent` is the planner's
    # derived paraphrase (operation + temporal.intent) which drops
    # user-stated structural constraints — a user typing "top 5" produces
    # `intent="sort current"` with no surviving "top". `ctx.user_message`
    # is the verbatim prompt (PR for #598's respects_user_limit gap). Fall
    # back to `ctx.intent` for older contexts that don't populate
    # `user_message`; once all callers thread it through the fallback is
    # dead and can be removed.
    limit = _parse_user_limit(ctx.user_message) or _parse_user_limit(ctx.intent)
    if limit is None:
        # The criterion fires on every Sort-shaped turn (per migration 015's
        # prefilter), but only a fraction of user prompts include a "top N"
        # phrase. Remaining turns have no constraint to evaluate. Convention:
        # validators that cannot apply on this turn return outcome="error"
        # with a `not applicable:` reason prefix so the Scorecard's
        # `_is_excluded_from_score` filter drops them from both numerator
        # and denominator (same treatment as stub errors). Real
        # missing-artifact failures keep the `missing artifact:` prefix and
        # remain in the error bucket as a signal that something broke.
        return ValidatorOutcome(
            outcome="error",
            reason="not applicable: no 'top N' phrase in user message or intent",
            evidence={
                "validator_name": "respects_user_limit",
                "user_message": ctx.user_message,
                "intent": ctx.intent,
            },
        )
    row_count = len(table)
    if row_count == limit:
        return ValidatorOutcome(
            outcome="pass",
            reason=f"row count matches user limit: {row_count} == {limit}",
            evidence={"row_count": row_count, "user_limit": limit},
        )
    return ValidatorOutcome(
        outcome="fail",
        reason=f"row count {row_count} != user limit {limit}",
        evidence={"row_count": row_count, "user_limit": limit},
    )


# Matches the legacy inline citation form: [District Name, Year, Source].
# Year is 4 digits; District and Source are free text (no comma or bracket).
_CITATION_RE = re.compile(r"\[[^,\[\]]+,\s*\d{4},\s*[^,\[\]]+\]")
_NUMERIC_CITATION_RE = re.compile(r"\[(\d+)\]")
# Governed general/source-level citation tokens that are NOT district/year
# scoped: the Pathfinder dataset name and NCTQ Research rationale references.
# These are legitimate source-level citations — "NCTQ Research" is the only
# source_title in the policy-guidance library, and "NCTQ District Policy
# Pathfinder" is the covered-universe dataset name — so accept them alongside
# [District, Year, Source] and artifact-backed [N]. Kept deliberately tight
# (anchored prefixes, no free text) so genuinely invented brackets still fail.
# (#1418)
_GOVERNED_SOURCE_RE = re.compile(
    r"\[NCTQ (?:District Policy Pathfinder|Research(?::[^\[\]]*)?)\]",
    re.IGNORECASE,
)
# Anything bracketed-comma-bracketed that isn't a canonical citation — used to
# detect malformed attempts so we can report them rather than silently passing
# answers with no citations at all.
_ANY_BRACKETED_RE = re.compile(r"\[[^\[\]]+\]")


@register("citation_format")
async def _validate_citation_format(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Confirm bracketed citations are either canonical or artifact-backed [N]."""

    text = ctx.answer_text or ""
    if not text:
        return ValidatorOutcome(
            outcome="error",
            reason="missing artifact: answer_text",
            evidence={"validator_name": "citation_format"},
        )
    # PROVISIONAL_SOURCE_MARKER is the renderer's prefix on policy-guidance
    # bullets without a verified source; it is intentionally not a citation
    # token. Strip it before validation so downstream counts stay consistent.
    bracketed = [
        t for t in _ANY_BRACKETED_RE.findall(text) if t != PROVISIONAL_SOURCE_MARKER
    ]
    if not bracketed:
        return ValidatorOutcome(
            outcome="pass",
            reason="no bracketed citations present (nothing to validate)",
            evidence={"bracketed_count": 0},
        )
    valid_markers = _valid_citation_markers(ctx)
    missing_numeric_markers: list[int] = []
    malformed: list[str] = []
    numeric_marker_count = 0
    canonical_count = 0
    governed_source_count = 0
    for token in bracketed:
        if _CITATION_RE.fullmatch(token):
            canonical_count += 1
            continue
        if _GOVERNED_SOURCE_RE.fullmatch(token):
            governed_source_count += 1
            continue
        numeric_match = _NUMERIC_CITATION_RE.fullmatch(token)
        if numeric_match is not None:
            numeric_marker_count += 1
            marker = int(numeric_match.group(1))
            if marker not in valid_markers:
                missing_numeric_markers.append(marker)
            continue
        malformed.append(token)
    if missing_numeric_markers:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{len(missing_numeric_markers)} numeric citation marker(s) do "
                "not map to ResultSet.citations; first missing marker: "
                f"[{missing_numeric_markers[0]}]"
            ),
            evidence={
                "missing_numeric_markers": missing_numeric_markers,
                "numeric_marker_count": numeric_marker_count,
                "artifact_citation_markers": sorted(valid_markers),
            },
        )
    if malformed:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{len(malformed)} of {len(bracketed)} bracketed token(s) do not "
                "match [District, Year, Source] or artifact-backed [N]; "
                f"first violation: {malformed[0]!r}"
            ),
            evidence={
                "malformed_count": len(malformed),
                "total_bracketed": len(bracketed),
                "first_violation": malformed[0],
            },
        )
    return ValidatorOutcome(
        outcome="pass",
        reason=(
            f"all {len(bracketed)} citation(s) are canonical, governed "
            "source-level, or artifact-backed numeric markers"
        ),
        evidence={
            "citation_count": len(bracketed),
            "canonical_count": canonical_count,
            "governed_source_count": governed_source_count,
            "numeric_marker_count": numeric_marker_count,
            "artifact_citation_markers": sorted(valid_markers),
        },
    )


def _valid_citation_markers(ctx: CompassEvaluatorContext) -> set[int]:
    markers: set[int] = set()
    if ctx.result is not None:
        markers.update(citation.marker for citation in ctx.result.citations)
    markers.update(citation.marker for citation in ctx.artifact_snapshot.citations)
    return markers


# Fuzzy threshold for named-district matching. Calibrated against real district
# names (rapidfuzz partial_ratio):
#   "Houston ISD" vs "Houston Independent School District" -> 90.0  (pass)
#   "Dallas ISD"  vs "Dallas Independent School District"  -> 88.9  (pass)
#   "Austin ISD"  vs "Houston ISD"                         -> 84.2  (fail)
# 88 gives a 4-point margin between the closest true-positive and the worst
# near-miss. Initialism abbreviations ("HISD" vs "Houston Independent School
# District" = 40.0) are *not* handled by partial_ratio alone — those remain
# rogue and will fire fail until a normalization/abbreviation layer is added.
_FUZZY_DISTRICT_MATCH_THRESHOLD = 88

# #1225: connective tokens dropped before matching so a canonical district name
# ("School District of Philadelphia") matches the user's loose phrase
# ("philadelphia school district") under a word-order-insensitive token_sort_ratio.
_DISTRICT_NAME_MATCH_NOISE = frozenset({"of", "the", "and"})


def _normalize_district_name_for_match(name: str) -> str:
    """Lowercase, strip punctuation, and drop connective noise tokens so a
    canonicalization variant (word reorder, "School District of X" vs
    "X school district") matches while a genuinely different district stays far
    below the threshold (#1225)."""

    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return " ".join(t for t in tokens if t not in _DISTRICT_NAME_MATCH_NOISE)


@register("selection_set_matches_request")
async def _validate_selection_set_matches_request(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Verify returned rows live inside the planner's requested selection.

    Reads two pieces of the L2 snapshot built by `verdict_pipeline._build_context`:
      * `table` — list of {district_name, state, display_value} dicts
      * `selection_expected` — {scope, states[], districts[]} from QueryPlan.selection

    When scope is "state", every row.state must be in expected.states. When
    scope is "named_districts", every row.district_name must fuzzy-match (via
    rapidfuzz.fuzz.partial_ratio >= _FUZZY_DISTRICT_MATCH_THRESHOLD) at least
    one expected district. Other scopes ("all_covered", "unspecified", missing)
    pass through — there is no requested set to check against.
    """

    snap = ctx.artifact_snapshot
    table = snap.table
    expected = snap.selection_expected
    scope = expected.scope if expected is not None else None

    if not table:
        return _missing_artifact_outcome(
            ctx, validator_name="selection_set_matches_request", artifact="table"
        )

    if scope not in ("state", "named_districts"):
        return ValidatorOutcome(
            outcome="pass",
            reason=f"no scope constraint to check (scope={scope!r})",
            evidence={"validator_name": "selection_set_matches_request", "scope": scope},
        )
    assert expected is not None  # narrowed by the scope check above

    if getattr(ctx.plan, "operation", None) == "peer_comparison":
        return ValidatorOutcome(
            outcome="pass",
            reason="peer_comparison may include deterministic peer rows outside the named anchor district",
            evidence={
                "validator_name": "selection_set_matches_request",
                "scope": scope,
                "operation": "peer_comparison",
            },
        )

    if scope == "state":
        expected_states = set(expected.states)
        if not expected_states:
            return ValidatorOutcome(
                outcome="pass",
                reason="state scope present but expected states list is empty",
                evidence={"validator_name": "selection_set_matches_request"},
            )
        rogue = sorted({
            row.state
            for row in table
            if row.state and row.state not in expected_states
        })
        if rogue:
            return ValidatorOutcome(
                outcome="fail",
                reason=f"rows from states outside requested set: {rogue}",
                evidence={
                    "validator_name": "selection_set_matches_request",
                    "scope": "state",
                    "expected_states": sorted(expected_states),
                    "rogue_states": rogue,
                    "row_count": len(table),
                },
            )
        return ValidatorOutcome(
            outcome="pass",
            reason=f"all rows in requested states: {sorted(expected_states)}",
            evidence={
                "validator_name": "selection_set_matches_request",
                "scope": "state",
                "expected_states": sorted(expected_states),
                "row_count": len(table),
            },
        )

    # scope == "named_districts": fuzzy-match each row name against the expected list.
    expected_names = list(expected.districts)
    if not expected_names:
        return ValidatorOutcome(
            outcome="pass",
            reason="named_districts scope present but expected districts list is empty",
            evidence={"validator_name": "selection_set_matches_request"},
        )
    from rapidfuzz import fuzz  # deferred import keeps registry-load cost flat
    expected_norm = [
        _normalize_district_name_for_match(exp) for exp in expected_names
    ]
    rogue: list[str] = []
    for row in table:
        name = row.district_name
        if not name:
            continue
        name_norm = _normalize_district_name_for_match(name)
        # #1225: match on EITHER partial_ratio (handles abbreviations, e.g.
        # "Dallas ISD" vs "Dallas Independent School District") OR token_sort_ratio
        # (handles word-order canonicalization, e.g. "School District of
        # Philadelphia" vs "philadelphia school district"). Genuine rogues score
        # low on both, so the 88 threshold still separates them.
        best = max(
            (
                max(
                    fuzz.partial_ratio(name_norm, exp),
                    fuzz.token_sort_ratio(name_norm, exp),
                )
                for exp in expected_norm
            ),
            default=0,
        )
        if best < _FUZZY_DISTRICT_MATCH_THRESHOLD:
            rogue.append(name)
    if rogue:
        return ValidatorOutcome(
            outcome="fail",
            reason=f"rows outside requested district set: {sorted(set(rogue))}",
            evidence={
                "validator_name": "selection_set_matches_request",
                "scope": "named_districts",
                "expected_districts": expected_names,
                "rogue_districts": sorted(set(rogue)),
                "match_threshold": _FUZZY_DISTRICT_MATCH_THRESHOLD,
            },
        )
    return ValidatorOutcome(
        outcome="pass",
        reason=f"all rows fuzzy-match an expected district (>= {_FUZZY_DISTRICT_MATCH_THRESHOLD})",
        evidence={
            "validator_name": "selection_set_matches_request",
            "scope": "named_districts",
            "expected_districts": expected_names,
            "row_count": len(table),
        },
    )


# Matches a USD-style currency token: properly-grouped thousands (1,000 / 100
# / 100,000) OR plain digits (0 / 1000), optional decimal fraction. The
# decimal group is captured so callers can detect mixed precision in one
# response (Worksheet's canonical Data Fidelity failure: $136,734.18 next to
# $135,530 next to $120,992 — same response, different precision, propagates
# into arithmetic). The proper-grouping alternation prevents trailing-comma
# capture in contexts like '$135,530, $120,992'.
_CURRENCY_RE = re.compile(r"\$(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")


@register("trace_id_present")
async def _validate_trace_id_present(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Confirm `ctx.trace_id` is populated for the completed turn.

    A turn with a null/empty trace_id is structurally invisible to
    downstream observability — Logfire diagnostics, the Quality
    dashboard, and pa-eval sweep reports all bind to `trace_id`, so a
    missing one silently drops the turn from every layered debugging
    surface. Mandatory-global so the verdict ledger sees the
    observability invariant on every turn, not just turns that
    happened to have other criteria fire.

    The 2026-05-18 staging sweep recorded 2 steps with missing
    `trace_id` (both correlated with `staging_http_500` runner errors).
    Today the harness records the count in `step_trace_id_coverage`
    but does not emit a verdict — so the dashboard treats the failure
    case as silent. This validator surfaces it.
    """

    trace_id = ctx.trace_id or ""
    if trace_id.strip():
        return ValidatorOutcome(
            outcome="pass",
            reason="trace_id present",
            evidence={"trace_id": trace_id},
        )
    return ValidatorOutcome(
        outcome="fail",
        reason="trace_id absent on completed turn",
        evidence={"trace_id": ctx.trace_id},
    )


@register("currency_format_canonical")
async def _validate_currency_format_canonical(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Confirm currency tokens render with consistent precision in one response.

    Fails when at least one currency token carries cents AND at least one
    omits them. Same-precision responses pass; responses without currency
    pass (nothing to validate).
    """

    text = ctx.answer_text or ""
    if not text:
        return ValidatorOutcome(
            outcome="error",
            reason="missing artifact: answer_text",
            evidence={"validator_name": "currency_format_canonical"},
        )
    matches = _CURRENCY_RE.findall(text)
    if not matches:
        return ValidatorOutcome(
            outcome="pass",
            reason="no currency tokens present (nothing to validate)",
            evidence={"currency_count": 0},
        )
    # Each match is (whole, fractional); fractional == "" when no decimal.
    with_cents = [m for m in matches if m[1]]
    no_cents = [m for m in matches if not m[1]]
    if with_cents and no_cents:
        first_with = f"${with_cents[0][0]}.{with_cents[0][1]}"
        first_without = f"${no_cents[0][0]}"
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"mixed currency precision: {len(with_cents)} token(s) with cents "
                f"alongside {len(no_cents)} without; first with cents={first_with!r}, "
                f"first without={first_without!r}"
            ),
            evidence={
                "with_cents_count": len(with_cents),
                "no_cents_count": len(no_cents),
                "first_with_cents": first_with,
                "first_without_cents": first_without,
            },
        )
    return ValidatorOutcome(
        outcome="pass",
        reason=f"all {len(matches)} currency token(s) use consistent precision",
        evidence={
            "currency_count": len(matches),
            "precision": "with_cents" if with_cents else "no_cents",
        },
    )

@register("sources_block_is_deduplicated")
async def _validate_sources_block_is_deduplicated(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Confirm the answer's Sources block has no duplicate URLs.

    Per W2-M5-04 (#748): the same document cited from multiple rows must
    appear once in ``result.citations`` and share one inline marker. The
    dedup is enforced at construction in ``execution/evidence.py`` and
    threaded across composite-ranking siblings via the shared-state
    parameter on ``execution/ranking.build_metric_ranking_result``; this
    validator is the evaluation-time check that catches any regression
    that reintroduces multi-marker entries for the same URL on either
    the standalone or composite-envelope path.

    URL-less citations are exempt: title-based dedup applies for them at
    construction (see ``test_citation_dedup``); the validator only
    enforces the URL invariant.

    Composite envelopes (``CompositeRankingResult``): the unified
    answer-level Sources list lives on the envelope's own ``citations``
    field (populated in ``_execute_composite_ranking``); per-child
    ``citations`` are construction-time snapshots and may be partial,
    which is correct because the SSE consumer (``api/sse.py``) reads
    ``response.result.citations`` — the envelope — when emitting the
    "citations" event to the frontend.

    ``policy_guidance`` and ``publication`` routes (Refs #867, #1690): the
    renderer's per-turn citation index lives on ``manifest.metadata['citations']``
    rather than a ``ResultSet``.
    ``verdict_pipeline._policy_guidance_citations_from_manifest`` mirrors it into
    ``ctx.artifact_snapshot.citations`` for both routes; this validator falls
    back to that field when ``ctx.route`` is one of them and ``ctx.result`` is
    absent, so the URL-uniqueness invariant is enforced on every render path.
    """

    if ctx.result is not None:
        citations = ctx.result.citations
    elif ctx.route in ("policy_guidance", "publication"):
        citations = ctx.artifact_snapshot.citations
    else:
        # the manifest routes are handled above; here only direct/clarify (NA)
        # or execute-with-no-result (genuine failure) / unknown route remain.
        return _missing_artifact_outcome(
            ctx, validator_name="sources_block_is_deduplicated", artifact="result"
        )
    seen_urls: dict[str, int] = {}
    duplicates: list[tuple[str, int]] = []
    for citation in citations:
        url = (citation.url or "").strip().casefold()
        if not url:
            continue
        if url in seen_urls:
            duplicates.append((url, citation.marker))
        else:
            seen_urls[url] = citation.marker
    if duplicates:
        first_url, first_marker = duplicates[0]
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{len(duplicates)} duplicate URL(s) in Sources block; "
                f"first duplicate at marker [{first_marker}] (url={first_url!r})"
            ),
            evidence={
                "duplicate_count": len(duplicates),
                "total_citations": len(citations),
                "unique_urls": len(seen_urls),
            },
        )
    return ValidatorOutcome(
        outcome="pass",
        reason=f"all {len(citations)} citation(s) have unique URLs",
        evidence={
            "total_citations": len(citations),
            "unique_urls": len(seen_urls),
        },
    )


@register("profile_rows_carry_nces_citation")
async def _validate_profile_rows_carry_nces_citation(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Confirm every profile-field-backed row carries an NCES citation.

    Per W2-M5-03 (#747): the Ashley Golden29 sheet rows 7/8/9 reported
    empty Sources columns on three back-to-back enrollment-rank queries
    because ``build_profile_ranking_result`` and ``build_profile_lookup_result``
    emitted rows without ``citation_markers`` and without ``citations``.
    The fix wires the governed ``compass.nces_allowlist`` (migration 060)
    canonical_url + canonical_title into citation emission so every
    profile row points the user at a verifiable NCES source.

    This validator is the eval-time check that catches a regression
    reintroducing empty Sources on either path. Applies to:

      - ``MetricRankingResult`` where any row.source == "profile_field"
        (the W0-01-tagged profile-ranked variant).
      - ``ProfileLookupResult`` (every row is profile_field by contract).

    Composite ranking envelopes are not yet considered here (composite
    children are always policy_answer-ranked under current planner
    shapes); if that changes, this validator should descend into
    children the same way ``sources_block_is_deduplicated`` does.
    """

    if ctx.result is None:
        return _missing_artifact_outcome(
            ctx, validator_name="profile_rows_carry_nces_citation", artifact="result"
        )

    result = ctx.result
    result_type = getattr(result, "result_type", None)

    if result_type == "profile_lookup":
        # A no-data row (a resolved district with no NCES value — value is None,
        # coverage_state != "covered") legitimately carries no citation. Only
        # covered profile rows must cite NCES, mirroring the runtime
        # _validate_citation_coverage (citations required only on "covered"
        # rows). Without this filter the value-or-null fix would trip this
        # reject criterion on every profile lookup where a requested district
        # has no NCES match.
        profile_rows = [
            row
            for row in result.rows
            if getattr(row, "coverage_state", "covered") == "covered"
        ]
        profile_sort_rows = []
    elif result_type == "metric_ranking":
        profile_rows = [
            row for row in result.rows
            if getattr(row, "source", None) == "profile_field"
        ]
        profile_sort_rows = [
            row for row in result.rows
            if getattr(row, "sort_metric_id", None) in PROFILE_SENTINEL_METRIC_IDS
        ]
    else:
        return ValidatorOutcome(
            outcome="pass",
            reason=(
                f"result_type={result_type} has no profile-field rows "
                "(nothing to validate)"
            ),
            evidence={"result_type": result_type},
        )

    sort_rows_missing_proof = [
        getattr(row, "district_name", "<unknown>")
        for row in profile_sort_rows
        if not getattr(row, "sort_source_url", None)
        or not getattr(row, "sort_source_document", None)
    ]
    if sort_rows_missing_proof:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{len(sort_rows_missing_proof)} profile-sort row(s) lack "
                f"NCES sort proof; first: {sort_rows_missing_proof[0]!r}"
            ),
            evidence={
                "profile_sort_row_count": len(profile_sort_rows),
                "rows_missing_sort_proof_count": len(sort_rows_missing_proof),
            },
        )

    if not profile_rows:
        return ValidatorOutcome(
            outcome="pass",
            reason="no profile-field rows in this result",
            evidence={
                "profile_row_count": 0,
                "profile_sort_row_count": len(profile_sort_rows),
            },
        )

    valid_markers = {citation.marker for citation in result.citations}
    rows_without_marker: list[str] = []
    rows_with_unknown_marker: list[tuple[str, int]] = []
    for row in profile_rows:
        markers = getattr(row, "citation_markers", []) or []
        if not markers:
            rows_without_marker.append(
                getattr(row, "district_name", "<unknown>")
            )
            continue
        for marker in markers:
            if marker not in valid_markers:
                rows_with_unknown_marker.append(
                    (getattr(row, "district_name", "<unknown>"), marker)
                )

    if rows_without_marker:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{len(rows_without_marker)} profile-field row(s) have no "
                f"citation markers; first: {rows_without_marker[0]!r}"
            ),
            evidence={
                "rows_without_marker_count": len(rows_without_marker),
                "profile_row_count": len(profile_rows),
            },
        )
    if rows_with_unknown_marker:
        district, marker = rows_with_unknown_marker[0]
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{len(rows_with_unknown_marker)} profile-field row(s) "
                f"reference markers not in result.citations; first: "
                f"{district!r} marker {marker}"
            ),
            evidence={
                "unknown_marker_count": len(rows_with_unknown_marker),
                "valid_markers": sorted(valid_markers),
            },
        )

    return ValidatorOutcome(
        outcome="pass",
        reason=(
            f"all {len(profile_rows)} profile-field row(s) carry NCES "
            f"citations ({len(valid_markers)} unique citation(s))"
        ),
        evidence={
            "profile_row_count": len(profile_rows),
            "profile_sort_row_count": len(profile_sort_rows),
            "unique_citation_count": len(valid_markers),
        },
    )


@register("csv_parity_matches_rendered_rows")
async def _validate_csv_parity_matches_rendered_rows(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Confirm the answer's CSV export mirrors the chat-rendered table.

    Per W2-M5-01 (#745): every executable answer with at least one row
    must produce a non-empty ``result.csv_export`` artifact, and the
    CSV's ``citation_markers`` column must carry the same per-row marker
    numbers that appear in the chat-rendered ``[N]`` references. This
    catches both reported failure modes from the Ashley/Shannon Golden29
    sheet:

      - "CSV was BLANK - no data" (sheet rows 62, 64): no CSV emitted at
        all — caught by checking ``result.csv_export is not None`` and
        ``len(csv_export.rows) > 0`` whenever ``result.rows`` is
        non-empty (including composite envelopes, whose ``rows`` is
        always empty by contract but whose ``children[*].rows`` are not).
      - "Sources column empty" (sheet rows 7-9, 99): per-row markers
        diverged between the chat ``[N]`` markers and the CSV's
        ``citation_markers`` column — caught by per-row comparison.

    Composite ranking envelopes are validated per child (the envelope-level
    ``rows`` is empty by contract, max_length=0): the envelope CSV
    concatenates each child's FULL ``csv_export`` — data rows then that
    child's coverage-disclosure rows — so disclosure rows sit BETWEEN
    children, and only a per-child walk keeps the marker zip aligned.
    """

    if ctx.result is None:
        return _missing_artifact_outcome(
            ctx, validator_name="csv_parity_matches_rendered_rows", artifact="result"
        )

    result = ctx.result
    csv_export = getattr(result, "csv_export", None)

    # #1514: the chat table renders the ANSWERS-ONLY subset of result.rows
    # (value / INA / N-A plus None pre-labeling rows, via the shared
    # is_rendered_answer_state predicate); the same subset forms the CSV's
    # data rows. Count and zip against that subset — the full artifact
    # record stays on result.rows by design.
    if getattr(result, "result_type", None) == "composite_ranking":
        return _composite_csv_parity_outcome(result, csv_export)

    chat_rows = _expected_answer_rows(result)
    chat_row_count = len(chat_rows)
    chat_citation_markers = [row.citation_markers for row in chat_rows]

    if chat_row_count == 0:
        return ValidatorOutcome(
            outcome="pass",
            reason="answer has no rows — no CSV to validate",
            evidence={"chat_row_count": 0},
        )

    if csv_export is None or not csv_export.rows:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"blank CSV — chat has {chat_row_count} row(s) but "
                f"csv_export is {'None' if csv_export is None else 'empty'}"
            ),
            evidence={
                "chat_row_count": chat_row_count,
                "csv_export_present": csv_export is not None,
            },
        )

    # The CSV's data rows mirror the rendered (answers-only) rows 1:1 and in
    # order, then append any coverage-disclosure rows LAST (artifacts/results.py
    # _default_csv_export_for_result). Compare against that aligned prefix.
    #
    # This predicate has now flipped twice — #1358 and #1514 — so the spec
    # history is documented here for the next person who touches it:
    #   - pre-#1358: the validator stripped `not_reviewed` rows from the CSV
    #     side ONLY, under-counting it and producing phantom `CSV < chat`
    #     failures on any answer with an inline not_reviewed row.
    #   - #1358: the strip was removed — not_reviewed rows were then
    #     legitimate rendered data rows present on BOTH surfaces, so both
    #     sides counted the full result.rows.
    #   - #1514 (current spec): tables, charts, and CSV data rows hold answer
    #     rows only (covered / ina / na); not_reviewed and out_of_universe
    #     rows stay in result.rows as the full record but render as narrative
    #     sentences, never data points. Both sides of this check therefore
    #     anchor to the SAME answers-only subset via the one shared predicate
    #     (artifacts.coverage.is_answer_state) — never a second local filter.
    if len(csv_export.rows) < chat_row_count:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"CSV row count {len(csv_export.rows)} < chat row count "
                f"{chat_row_count} — rows missing from export"
            ),
            evidence={
                "chat_row_count": chat_row_count,
                "csv_row_count": len(csv_export.rows),
            },
        )

    csv_data_rows = csv_export.rows[:chat_row_count]

    mismatches = 0
    for chat_markers, csv_row in zip(chat_citation_markers, csv_data_rows):
        chat_str = " ".join(str(m) for m in chat_markers)
        csv_str = str(csv_row.get("citation_markers", ""))
        if chat_str != csv_str:
            mismatches += 1

    if mismatches:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{mismatches} row(s) have CSV citation_markers diverging "
                f"from chat citation_markers"
            ),
            evidence={
                "mismatched_row_count": mismatches,
                "chat_row_count": chat_row_count,
            },
        )

    return ValidatorOutcome(
        outcome="pass",
        reason=(
            f"CSV export mirrors chat: {chat_row_count} row(s), citation "
            f"markers aligned"
        ),
        evidence={
            "chat_row_count": chat_row_count,
            "csv_row_count": len(csv_export.rows),
        },
    )


def _composite_csv_parity_outcome(result, csv_export) -> ValidatorOutcome:
    """Per-child CSV parity for a composite ranking envelope (#1514, B2).

    The envelope CSV concatenates each child's FULL ``csv_export``
    (artifacts/results.py ``CompositeRankingResult.populate_artifact_surfaces``):
    child 1's data rows, then child 1's coverage-disclosure rows, then
    child 2's data rows, and so on. Disclosure rows therefore sit BETWEEN
    children — a flat ``rows[:chat_row_count]`` slice shifts the marker zip
    on any composite whose earlier child carries a disclosure row (e.g. a
    promoted not_reviewed district), false-failing a healthy answer. Walk
    the envelope child by child instead: zip each child's answers-only rows
    against the data-row prefix of that child's slice of the envelope, then
    skip past the child's disclosure tail (its slice length is the child's
    own full csv_export length — exactly what the aggregator copied).
    """

    chat_row_count = sum(
        len(_expected_answer_rows(child)) for child in result.children
    )
    if chat_row_count == 0:
        return ValidatorOutcome(
            outcome="pass",
            reason="answer has no rows — no CSV to validate",
            evidence={"chat_row_count": 0},
        )
    if csv_export is None or not csv_export.rows:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"blank CSV — chat has {chat_row_count} row(s) but "
                f"csv_export is {'None' if csv_export is None else 'empty'}"
            ),
            evidence={
                "chat_row_count": chat_row_count,
                "csv_export_present": csv_export is not None,
            },
        )

    offset = 0
    mismatches = 0
    for child_index, child in enumerate(result.children):
        child_chat_rows = _expected_answer_rows(child)
        child_csv = getattr(child, "csv_export", None)
        # The aggregator copies len(child.csv_export.rows) rows per child
        # (skipping csv-less children), so that length delimits this child's
        # slice of the envelope.
        child_slice_len = len(child_csv.rows) if child_csv is not None else 0
        child_slice = csv_export.rows[offset : offset + child_slice_len]
        if len(child_slice) < len(child_chat_rows):
            return ValidatorOutcome(
                outcome="fail",
                reason=(
                    f"CSV rows missing for composite child {child_index}: "
                    f"{len(child_slice)} CSV row(s) < "
                    f"{len(child_chat_rows)} chat row(s)"
                ),
                evidence={
                    "chat_row_count": chat_row_count,
                    "csv_row_count": len(csv_export.rows),
                    "child_index": child_index,
                },
            )
        # zip stops at the shorter list — the child's answers-only rows —
        # which is exactly the data-row prefix; the disclosure tail is never
        # compared.
        for chat_row, csv_row in zip(child_chat_rows, child_slice):
            chat_str = " ".join(str(m) for m in chat_row.citation_markers)
            csv_str = str(csv_row.get("citation_markers", ""))
            if chat_str != csv_str:
                mismatches += 1
        offset += child_slice_len

    if mismatches:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{mismatches} row(s) have CSV citation_markers diverging "
                f"from chat citation_markers"
            ),
            evidence={
                "mismatched_row_count": mismatches,
                "chat_row_count": chat_row_count,
            },
        )

    return ValidatorOutcome(
        outcome="pass",
        reason=(
            f"CSV export mirrors chat: {chat_row_count} row(s) across "
            f"{len(result.children)} child table(s), citation markers aligned"
        ),
        evidence={
            "chat_row_count": chat_row_count,
            "csv_row_count": len(csv_export.rows),
        },
    )


# Generic fallback titles emitted by `artifacts/citations.py:_fallback_title`
# (or the older renderer-side equivalents) when title parsing misses. A
# citation with one of these titles is unverifiable to the user — the link
# may resolve, but there is no labeled context for what the source is.
#
#   - "Source record"   — current fallback at artifacts/citations.py:208 when
#                         no document_type and no URL are present.
#   - "Source document" — current fallback at artifacts/citations.py:207 when
#                         a URL is present but title parsing produced nothing.
#   - "Unknown Source"  — legacy/older-renderer literal surfaced to users on
#                         #746 sheet row 59 (Ashley 2026-05-14 session
#                         78693257-8590-4ef6-adf2-e0f91eeb3f29). Not emitted
#                         by current code; kept here for regression coverage
#                         in case any renderer path resurrects it.
_UNRESOLVED_CITATION_TITLES: frozenset[str] = frozenset({
    "Source record",
    "Source document",
    "Unknown Source",
})


@register("citation_titles_resolved")
async def _validate_citation_titles_resolved(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Confirm every citation has a resolved, non-fallback title.

    Per W2-M5-02 (#746): users reported (sheet row 59, Ashley 2026-05-14)
    that "All citations were listed as 'Unknown Source' for the title even
    though they linked to the correct doc." The construction-side fallback
    in ``artifacts/citations.py:_fallback_title`` returns a generic string
    when title parsing misses; a citation with such a title is unverifiable
    even when the URL itself opens, because the user has no labeled context
    for what they're about to read.

    Scope: every entry in ``result.citations``. Composite envelopes carry
    the unified answer-level Sources list on their own ``.citations`` field
    (per the ``sources_block_is_deduplicated`` docstring), which is what
    the SSE consumer renders for the frontend — so iterating the flat
    envelope list captures every user-visible citation.

    Per #892, the ``"{document_type} source"`` pattern (e.g., "Contract
    source") also fails — these name the doc type but lack district + year
    + page detail, and judges treat them as unverifiable. Evidence still
    surfaces ``low_quality_title_count`` so this subset can be counted
    separately from generic fallbacks.

    Out of scope (tracked elsewhere):
      - URL health (4xx/5xx) — that is the B.3.d nightly crawl's job.
    """

    if ctx.result is None:
        return _missing_artifact_outcome(
            ctx, validator_name="citation_titles_resolved", artifact="result"
        )

    citations = ctx.result.citations
    if not citations:
        return ValidatorOutcome(
            outcome="pass",
            reason="no citations on this result — nothing to validate",
            evidence={"citation_count": 0},
        )

    unresolved: list[tuple[int, str]] = []
    low_quality_count = 0
    for citation in citations:
        title = (citation.title or "").strip()
        if title in _UNRESOLVED_CITATION_TITLES:
            unresolved.append((citation.marker, title))
            continue
        # "{doc_type} source" pattern from _fallback_title (e.g. "Contract
        # source") names the doc type but lacks district/year/page detail.
        # Per #892, this is unverifiable and fails alongside generic
        # fallbacks rather than being a soft tracking-only signal.
        if title.endswith(" source") and len(title.split()) <= 4:
            low_quality_count += 1
            unresolved.append((citation.marker, title))

    if unresolved:
        first_marker, first_title = unresolved[0]
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{len(unresolved)} citation(s) have unresolved fallback "
                f"titles; first: marker [{first_marker}] title={first_title!r}"
            ),
            evidence={
                "unresolved_count": len(unresolved),
                "total_citations": len(citations),
                "low_quality_title_count": low_quality_count,
            },
        )

    return ValidatorOutcome(
        outcome="pass",
        reason=(
            f"all {len(citations)} citation(s) have resolved titles "
            f"({low_quality_count} low-quality)"
        ),
        evidence={
            "citation_count": len(citations),
            "low_quality_title_count": low_quality_count,
        },
    )


def _shared_markers(rows: list) -> frozenset[int]:
    """Markers referenced by rows of more than one DISTINCT district.

    Compass dedups citations by URL (``execution/evidence.py::_citation_identity``):
    when two districts cite the same document the answer keeps ONE
    ``CitationRef`` with one shared marker, and the surviving ref carries only
    the FIRST-seen row's ``district_id`` (``source.district_id or
    row.district_id``). For such a shared marker the citation's source district
    is genuinely ambiguous — it matches one of the referencing districts by
    construction and "mismatches" all the others. Comparing per row would
    false-fire on every shared state-law / county-wide / common document, so
    these markers are excluded from the provenance check entirely (#1717).
    """

    districts_by_marker: dict[int, set[int]] = {}
    for row in rows:
        row_district_id = getattr(row, "district_id", None)
        if row_district_id is None:
            continue
        for marker in getattr(row, "citation_markers", None) or []:
            districts_by_marker.setdefault(marker, set()).add(row_district_id)
    return frozenset(
        marker
        for marker, districts in districts_by_marker.items()
        if len(districts) > 1
    )


def _provenance_mismatches_for_rows(
    rows: list,
    citations: list[CitationRef],
) -> tuple[int, list[dict]]:
    """Compare each row's district to the source district of every citation it
    references. Returns ``(rows_checked, mismatches)``.

    The provenance fact comes straight off the typed artifact: a result row
    carries ``district_id`` (its own district) and ``citation_markers`` (the
    markers it points at); ``CitationRef.district_id`` is the SOURCE document's
    district (populated on the execute path by
    ``execution/evidence.py::citation_markers_for_row`` via
    ``citation_ref_from_source``). A row is "checked" when at least one of its
    markers resolves to a citation whose source district could be compared.

    Precision-first / null-aware (#1717): a mismatch is recorded ONLY when the
    row's district AND the citation's source district are BOTH known and they
    differ. Every ambiguous case is pass-through — no markers, a marker that
    does not resolve to any citation (that is ``citation_format``'s job), a
    citation with no source district, a row with no district, OR a marker
    shared across districts via URL dedup (see ``_shared_markers``). Treating
    any of these as a value would flood the sweep with false fires, so the
    criterion deliberately trades recall (caught by the deferred scoping /
    multi-table / suppression follow-ups) for zero false positives.
    """

    by_marker = {citation.marker: citation for citation in citations}
    shared = _shared_markers(rows)
    rows_checked = 0
    mismatches: list[dict] = []
    for row in rows:
        row_district_id = getattr(row, "district_id", None)
        markers = getattr(row, "citation_markers", None) or []
        row_was_checked = False
        for marker in markers:
            if marker in shared:
                # Deduped shared document — source district is ambiguous.
                continue
            citation = by_marker.get(marker)
            if citation is None:
                # Unresolved marker: provenance is undeterminable here.
                continue
            source_district_id = citation.district_id
            if source_district_id is None:
                # Citation names no source district — nothing to compare.
                continue
            row_was_checked = True
            if row_district_id is None:
                # Row carries no district to anchor the comparison.
                continue
            if source_district_id != row_district_id:
                mismatches.append({
                    "district_name": getattr(row, "district_name", "<unknown>"),
                    "row_district_id": row_district_id,
                    "marker": marker,
                    "citation_district_id": source_district_id,
                })
        if row_was_checked:
            rows_checked += 1
    return rows_checked, mismatches


@register("citation_provenance_matches_row")
async def _validate_citation_provenance_matches_row(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Confirm each rendered row cites a document from that row's own district.

    Per #1717: eval was blind to citation *correctness*. Every existing
    deterministic citation check (``citation_format``, ``sources_block_is_
    deduplicated``, ``citation_titles_resolved``, ``profile_rows_carry_nces_
    citation``) verifies a citation's *shape* — that it is well-formed, unique,
    titled, and present — but none verifies that the source document actually
    belongs to the district whose row references it. The anchor failure shape
    is case 365: California rows whose Sources point at Hawaii / Colorado
    documents. Format-valid, deduped, titled — and wrong.

    This validator reads the typed ``ResultSet`` (``ctx.result``) — the same
    source the other result-bearing validators read — so no field needs
    threading through the snapshot. For every rendered row it compares the
    row's ``district_id`` to the ``district_id`` of each citation the row's
    ``citation_markers`` reference; a difference is a provenance mismatch.

    Pass-through (never a false fail): rows with no citation, markers that do
    not resolve to a citation, citations with no source district, and rows
    with no district. See ``_provenance_mismatches_for_rows`` for the
    null-aware predicate.

    Composite ranking envelopes carry rows on their children, each child a
    fully built ``MetricRankingResult`` with its OWN ``citations`` list, so the
    comparison runs per child (mirrors ``csv_parity_matches_rendered_rows``)
    — never the envelope-level citation list against a child's markers.

    Out of scope (a verdict cannot fetch URLs, by design): citation
    link-liveness / 404 checking. That belongs to the B.3.d nightly crawl, not
    an offline deterministic verdict. The deferred #1717 follow-ups (citation
    scoping, multi-table attribution, suppression) widen recall separately.
    """

    if ctx.result is None:
        return _missing_artifact_outcome(
            ctx,
            validator_name="citation_provenance_matches_row",
            artifact="result",
        )

    result = ctx.result
    if getattr(result, "result_type", None) == "composite_ranking":
        rows_checked = 0
        mismatches: list[dict] = []
        for child in result.children:
            child_checked, child_mismatches = _provenance_mismatches_for_rows(
                list(child.rows), list(child.citations)
            )
            rows_checked += child_checked
            mismatches.extend(child_mismatches)
    else:
        rows_checked, mismatches = _provenance_mismatches_for_rows(
            list(result.rows), list(result.citations)
        )

    if mismatches:
        first = mismatches[0]
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"{len(mismatches)} row(s) cite a source document from a "
                f"different district; first: {first['district_name']!r} "
                f"(district {first['row_district_id']}) cites marker "
                f"[{first['marker']}] sourced from district "
                f"{first['citation_district_id']}"
            ),
            evidence={
                "validator_name": "citation_provenance_matches_row",
                "mismatch_count": len(mismatches),
                "rows_checked": rows_checked,
                # Cap the surfaced list so a pathological all-mismatch table
                # does not bloat the verdict evidence JSON.
                "mismatches": mismatches[:10],
            },
        )

    return ValidatorOutcome(
        outcome="pass",
        reason=(
            f"all {rows_checked} provenance-checkable row(s) cite a document "
            "from their own district"
        ),
        evidence={
            "validator_name": "citation_provenance_matches_row",
            "rows_checked": rows_checked,
            "mismatch_count": 0,
        },
    )


# ─── Dimension-bridge factory ────────────────────────────────────────────────
#
# Adapts the existing L1 validator suite (quality/validators/{coverage,metrics,
# selection}.py, called via quality.validate_result()) into the @register
# pattern. Each L1 ValidationFinding is already tagged with a `dimension="<name>"`
# label; for one seeded `dim_<name>` criterion the bridge runs the full L1 pass,
# filters findings to that dimension, and returns pass/fail accordingly.
#
# This lets a single ~30-LOC factory bridge eight dimensions (one criterion per
# dim) instead of writing eight near-identical validator wrappers.


def _evaluate_dimension(dim_name: str) -> ValidatorFn:
    """Build a validator that runs the full L1 suite and filters to one dimension.

    The returned async callable:
      - returns `error` if ctx.plan / ctx.result aren't populated (missing
        artifacts, not a real validation failure)
      - returns `pass` if validate_result() produced zero findings for `dim_name`
      - returns `fail` with the first finding's message if any matched

    `dim_name` must be one of the labels actually produced by validators in
    `quality/validators/{coverage,metrics,selection}.py`. Verify with grep
    before registering a new dim_* — an unmatched label would silently always
    pass on every turn.
    """

    async def _validate(
        ctx: CompassEvaluatorContext,
        payload: dict,
    ) -> ValidatorOutcome:
        if ctx.plan is None or ctx.result is None:
            missing = []
            if ctx.plan is None:
                missing.append("plan")
            if ctx.result is None:
                missing.append("result")
            # Direct-route and clarification turns don't produce a plan and/or
            # result, so dimension bridges have nothing to evaluate. Per the
            # `not applicable:` convention (see Scorecard
            # _is_excluded_from_score), use outcome='error' with the prefix
            # so these trials are dropped from both numerator and
            # denominator rather than counted as fails or polluting the
            # error bucket as if the bridge broke.
            return ValidatorOutcome(
                outcome="error",
                reason=(
                    f"not applicable: turn has no {'/'.join(missing)} for "
                    f"dimension={dim_name}"
                ),
                evidence={"dimension": dim_name, "missing": missing},
            )

        # Import at call time to keep _evaluator_context.py and this module
        # free of compass_backend.quality.validation import.
        from compass_backend.quality.validation import validate_result

        # Pass the final answer text so rendered-body-gated validators
        # (numeric-token provenance, fact-coverage, markdown-table parity)
        # actually run at L2 — omitting it made every such bridge silently
        # always-pass. At this boundary `ctx.answer_text` is the POST-stylist
        # text, so these criteria also catch facts the voice pass dropped.
        # Empty answer_text maps to None (the validators' short-circuit).
        # manifest_metadata is still not passed (no field on the evaluator
        # context), so artifact-id parity remains L2-dead — known gap, do not
        # rediscover it in the next always-passes audit.
        report = validate_result(
            plan=ctx.plan,
            result=ctx.result,
            rendered_body=ctx.answer_text or None,
        )
        dim_findings = [f for f in report.findings if f.dimension == dim_name]
        if not dim_findings:
            return ValidatorOutcome(
                outcome="pass",
                reason=f"no {dim_name} findings",
                evidence={
                    "dimension": dim_name,
                    "total_findings": len(report.findings),
                },
            )
        first = dim_findings[0]
        evidence = {
            "dimension": dim_name,
            "violation_count": len(dim_findings),
            "first_code": first.code,
            "first_message": first.message,
            "first_severity": first.severity,
            "codes": sorted({f.code for f in dim_findings}),
        }
        if not any(f.severity == "error" for f in dim_findings):
            # Warning-severity findings are calibration-mode visibility: the
            # scorecard buckets purely on outcome (criteria.severity is never
            # read), so failing here would depress the headline score exactly
            # like a reject. Pass with the warnings in the reason/evidence —
            # they surface in the sweep reasons table and compass.verdicts —
            # and promote by raising the L1 finding severity once calibrated.
            return ValidatorOutcome(
                outcome="pass",
                reason=(
                    f"calibration: {len(dim_findings)} warning-severity "
                    f"{dim_name} finding(s) — {first.code}"
                ),
                evidence=evidence,
            )
        return ValidatorOutcome(
            outcome="fail",
            reason=f"{dim_name}: {first.message}",
            evidence=evidence,
        )

    # Give the closure a useful __qualname__ so the duplicate-register guard
    # produces informative error messages in tests.
    _validate.__qualname__ = f"_evaluate_dimension[{dim_name}]"
    return _validate


# ─── Registered dimension bridges ────────────────────────────────────────────
#
# One register call per supported ValidationDimension that has L1 coverage in
# validators/{coverage,metrics,selection}.py. Naming convention: validator_name
# `dim_<dimension>` so the seeded compass.criteria row's `payload.validator_name`
# visually mirrors the ValidationFinding `dimension="..."` label.
#
# Intentionally NOT registered:
#   - dim_sort_order:        the existing `sort_descending` validator already
#                            covers ranking ordering at the artifact-snapshot
#                            level; a second bridge would muddle the scorecard.
#
# `dim_surface_consistency` remains available as a structural diagnostic, not
# as a top-level scorecard dimension. The bridge runs the full L1 pass and
# filters to `surface_consistency` findings for ad hoc health checks.

register("dim_selection")(_evaluate_dimension("selection"))
register("dim_metric")(_evaluate_dimension("metric"))
register("dim_coverage_state")(_evaluate_dimension("coverage_state"))
register("dim_citation_coverage")(_evaluate_dimension("citation_coverage"))
register("dim_denominator")(_evaluate_dimension("denominator"))
register("dim_data_fidelity")(_evaluate_dimension("data_fidelity"))
register("dim_numeric_token_provenance")(
    _evaluate_dimension("numeric_token_provenance")
)
register("dim_filter")(_evaluate_dimension("filter"))
register("dim_fact_coverage")(_evaluate_dimension("fact_coverage"))
register("dim_surface_consistency")(_evaluate_dimension("surface_consistency"))


# ─── Selection "definition of good" validators (#1095 / WS0) ───────────────────
#
# These two encode *what a good Selection answer is* as deterministic checks the
# planner cannot fake — the executable half of #1248's ends-vs-means thesis. They
# read the planner ROUTE and the RESULT boundary, never a plan shape: planner
# topology is free, the verdict is anchored at the ends (backend AGENTS.md
# guardrail #7). They are the regression substrate for the Denver clarify-spiral
# and the DCPS/Houston right-vs-valid-wrong cases.

# Real PlannerRoute members, derived (never hand-typed) so a renamed or added
# route can't silently slip past — `"comparison"` is not a route; it's a
# QueryOperation (`"peer_comparison"`). (#1248 design, "Trap 3".)
_PLANNER_ROUTES: frozenset[str] = frozenset(get_args(PlannerRoute))
# Routes that mean "Compass committed to an answer" rather than punting.
_WARRANTED_ANSWER_ROUTES: tuple[str, ...] = (
    "execute",
    "direct",
    "policy_guidance",
    "publication",
)


@register("clarify_spiral_route_warrant")
async def _validate_clarify_spiral_route_warrant(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Fail when the planner punted to `clarify` on a turn that should have answered.

    The clarify-spiral failure mode (#1095 Denver four-turn case) lives on a turn
    whose ``route == "clarify"`` and whose ``plan is None`` — so this asserts the
    ROUTE against the case author's expected route and never inspects a plan shape
    (#1248 design, "Trap 1").

    Expected route(s) ride in the criterion ``payload``: ``expected_routes`` (list)
    or ``expected_route`` (str); default ``{execute, direct, policy_guidance}`` —
    any route that commits to an answer. Routes are checked against the real
    ``PlannerRoute`` members, so a typo'd route in a criterion surfaces as an
    error rather than a silent pass.

    Seeding note (#1248 design, "Trap 2"): the firing criterion must leave
    ``payload.applicable_routes`` UNSET. Setting it to ``["execute"]`` would make
    ``verdict_pipeline`` skip this criterion on the very ``clarify`` turn the bug
    lives on (and the scorecard would drop it), hiding the regression.
    """

    if ctx.route is None:
        return ValidatorOutcome(
            outcome="error",
            reason="missing artifact: route",
            evidence={"validator_name": "clarify_spiral_route_warrant"},
        )

    expected = payload.get("expected_routes")
    if expected is None and payload.get("expected_route") is not None:
        expected = [payload["expected_route"]]
    if not expected:
        expected_routes: tuple[str, ...] = _WARRANTED_ANSWER_ROUTES
    elif isinstance(expected, (list, tuple)):
        expected_routes = tuple(str(r) for r in expected)
    else:
        return ValidatorOutcome(
            outcome="error",
            reason=(
                "criterion payload 'expected_routes' must be a list of route "
                f"strings (got {type(expected).__name__})"
            ),
            evidence={"validator_name": "clarify_spiral_route_warrant"},
        )

    unknown = [r for r in expected_routes if r not in _PLANNER_ROUTES]
    if unknown:
        return ValidatorOutcome(
            outcome="error",
            reason=(
                f"criterion payload names unknown route(s) {unknown}; valid "
                f"PlannerRoute members are {sorted(_PLANNER_ROUTES)}"
            ),
            evidence={"unknown_routes": unknown},
        )

    if ctx.route in expected_routes:
        return ValidatorOutcome(
            outcome="pass",
            reason=(
                f"route '{ctx.route}' is a warranted answer route "
                f"{list(expected_routes)}"
            ),
            evidence={"route": ctx.route, "expected_routes": list(expected_routes)},
        )
    return ValidatorOutcome(
        outcome="fail",
        reason=(
            f"route '{ctx.route}' is not a warranted answer route "
            f"{list(expected_routes)} — Compass punted instead of answering"
        ),
        evidence={
            "route": ctx.route,
            "expected_routes": list(expected_routes),
            "user_message": ctx.user_message[:200],
        },
    )


def _normalize_district_name(name: str) -> str:
    """Casefold + collapse whitespace for tolerant district-name comparison."""

    return normalize_whitespace_casefold(name)


def _district_name_matches(expected: str, actual: str) -> bool:
    """True when a result row's district name satisfies an expected district.

    Tolerant of the catalog's canonical-vs-shorthand spread (e.g. ``"DCPS"`` vs
    ``"District of Columbia Public Schools"``): normalized equality, or either
    name contained in the other.
    """

    e = _normalize_district_name(expected)
    a = _normalize_district_name(actual)
    if not e or not a:
        return False
    return e == a or e in a or a in e


@register("district_phrase_resolves_to_expected")
async def _validate_district_phrase_resolves_to_expected(
    ctx: CompassEvaluatorContext,
    payload: dict,
) -> ValidatorOutcome:
    """Fail when districts the user named are absent from the result.

    The "right-vs-valid-wrong district" failure (#1095 DCPS/Houston case): the
    planner resolves a phrase to a district that EXISTS in the catalog but is not
    the one the user meant, so every membership check passes while the answer is
    about the wrong place. This is checked at the RESULT boundary — the rendered
    table's ``district_name`` column — not at any planner field, per backend
    AGENTS.md guardrail #7 (anchor route-integrity checks at the ends).

    Expected districts ride in ``payload['expected_districts']`` (list of
    canonical names). Each must appear in the result table (tolerant match); a
    missing one fails, and unexpected extras are reported as evidence.
    """

    expected = payload.get("expected_districts")
    if not expected or not isinstance(expected, (list, tuple)):
        return ValidatorOutcome(
            outcome="error",
            reason=(
                "criterion payload missing 'expected_districts' (non-empty list "
                "of district names)"
            ),
            evidence={"validator_name": "district_phrase_resolves_to_expected"},
        )

    table = ctx.artifact_snapshot.table
    if not table:
        return _missing_artifact_outcome(
            ctx, validator_name="district_phrase_resolves_to_expected", artifact="table"
        )

    actual_names = [row.district_name for row in table]
    missing = [
        d
        for d in expected
        if not any(_district_name_matches(str(d), a) for a in actual_names)
    ]
    matched_actuals = {
        a
        for a in actual_names
        if any(_district_name_matches(str(d), a) for d in expected)
    }
    unexpected = [a for a in actual_names if a not in matched_actuals]

    if missing:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                f"requested district(s) absent from the result: {missing} — a "
                "valid-but-wrong district may have been selected. Result "
                f"contained: {actual_names}"
            ),
            evidence={
                "missing_expected": missing,
                "result_districts": actual_names,
                "unexpected_districts": unexpected,
            },
        )
    return ValidatorOutcome(
        outcome="pass",
        reason=f"all requested district(s) present in result: {list(expected)}",
        evidence={
            "result_districts": actual_names,
            "unexpected_districts": unexpected,
        },
    )


@register("answerability_rescue_fallback_is_failure")
async def _validate_answerability_rescue_fallback(
    ctx: CompassEvaluatorContext,
    payload: dict,  # noqa: ARG001 — unused, kept for ValidatorFn contract
) -> ValidatorOutcome:
    """Fail when a clarify turn originated from the rescue-fallback path.

    A rescue fallback means the system fell through to the generic hardcoded
    refusal because one catalog phrase was unresolved — not a considered planner
    choice to clarify. This criterion fires on every clarify-routed turn
    (applicable_routes=['clarify'] in the DB row) and fails whenever the rescue
    path fired, making over-clarify visible as a verdict failure rather than a
    route-skip. Genuine planner clarifications pass.

    Reads ``ctx.clarification_rescue_origin`` — the DURABLE origin marker that
    survives shape-guard enrichment — NOT ``is_rescue_fallback``, which the
    enrichment stage clears to False before the turn is persisted (#1613). The
    criterion's severity (warn vs reject) is carried on the seeded criterion
    row, not here, so the ``fail`` outcome stays advisory until calibration.
    """
    if ctx.clarification_rescue_origin:
        return ValidatorOutcome(
            outcome="fail",
            reason=(
                "answerability: turn originated from the rescue fallback "
                "clarification path (catalog phrase unresolved → "
                "UnexpectedModelBehavior → hardcoded generic refusal). This is "
                "not a considered planner clarification."
            ),
            evidence={"rescue_origin": True},
        )
    return ValidatorOutcome(
        outcome="pass",
        reason=(
            "answerability: clarification was a considered planner choice "
            "(rescue_origin=False)"
        ),
        evidence={"rescue_origin": False},
    )


__all__ = [
    "ValidatorFn",
    "ValidatorOutcome",
    "known_validators",
    "lookup",
    "register",
]
