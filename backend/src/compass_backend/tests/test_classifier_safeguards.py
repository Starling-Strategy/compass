"""Tests for the criterion classifier — 4 safeguards (Spec B PR 5).

All tests are offline (no DB, no LLM calls). The classifier's AI component
is exercised through mocking so the test suite runs without gateway access.

Tests cover:
  1-3. Each prefilter rule individually (3 tests)
  4.   Safeguard 1 — mandatory globals fire regardless of context
  5.   Safeguard 3 — prefilters fire correctly for sort intent + table
  6.   Selection-works check: citation_format_correct does NOT fire on a sort turn
  7.   Type I error guard: AI classifier cannot return invalid criterion codes
  8.   criterion_record_to_evaluator produces FK-valid VerdictRecord
  9.   criterion_record_to_evaluator dispatch on each check_type

Run with:
    PYTHONPATH=src uv run pytest src/compass_backend/tests/test_classifier_safeguards.py --tb=short
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from compass_backend.db.rows import ScenarioExpectation
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality.criteria import (
    CriterionRecord,
    RecordDeterministicEvaluator,
    RecordEvaluator,
    RecordJudgmentEvaluator,
    RecordSpanEvaluator,
    VerdictRecord,
    criterion_record_to_evaluator,
)
from compass_backend.quality.prefilters import (
    PREFILTER_RULES,
    _rule_preserves_prior_context,
    _rule_respects_user_limit,
    _rule_sort_descending,
    apply_prefilters,
)
from compass_backend.tests.conftest import (
    make_criterion_record,
    make_evaluator_context,
)


# ─── Shared fixtures ──────────────────────────────────────────────────────────


def _ctx(
    *,
    session_id: str = "11111111-1111-1111-1111-111111111111",
    turn_index: int | None = 0,
    intent: str | None = None,
    artifact_snapshot: dict | None = None,
    answer_text: str = "Here are the top districts by enrollment.",
    trace_id: str | None = None,
    scenario_id: int | None = None,
    triggered_by: str = "user_turn",
    expected_output: ScenarioExpectation | None = None,
) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id=session_id,
        turn_index=turn_index,
        intent=intent,
        artifact_snapshot=artifact_snapshot or {},
        answer_text=answer_text,
        trace_id=trace_id,
        scenario_id=scenario_id,
        triggered_by=triggered_by,
        expected_output=expected_output,
    )


def _sort_ctx(turn_index: int = 0) -> CompassEvaluatorContext:
    """Context that looks like a sort turn with a table artifact."""
    return _ctx(
        intent="sort by enrollment",
        artifact_snapshot={
            "table": [
                {"district_name": "LAUSD", "display_value": "600000"},
                {"district_name": "NYC", "display_value": "1000000"},
            ]
        },
        turn_index=turn_index,
    )


# This module's judge_prompt rubric is sort-specific, so it keeps its own
# payload text (not the shared conftest default) and passes it explicitly.
_DEFAULT_PAYLOADS: dict[str, dict] = {
    "deterministic": {"validator_name": "test_validator"},
    "judge_prompt": {"judge_prompt": "Does the response sort correctly?"},
    "span_assertion": {},
    "scenario_fit": {"judge_prompt_template": "{answer_text}"},
}


def _make_criterion(
    *,
    id: int = 1,
    criterion_code: str = "sort_descending",
    check_type: str = "deterministic",
    is_mandatory_global: bool = False,
    intent_tags: list[str] | None = None,
    topic_tags: list[str] | None = None,
) -> CriterionRecord:
    return make_criterion_record(
        id=id,
        criterion_code=criterion_code,
        category="sort_accuracy",
        check_type=check_type,
        priority=50,
        is_mandatory_global=is_mandatory_global,
        topic_tags=topic_tags or [],
        intent_tags=intent_tags or ["sort"],
        payload=dict(_DEFAULT_PAYLOADS[check_type]),
    )


# ─── 1. Prefilter rule: _rule_sort_descending ─────────────────────────────────


def test_rule_sort_descending_fires_when_sort_intent_and_table() -> None:
    """_rule_sort_descending returns True only when BOTH intent contains 'sort' AND table exists."""
    ctx = _sort_ctx()
    assert _rule_sort_descending(ctx) is True


def test_rule_sort_descending_does_not_fire_without_table() -> None:
    """_rule_sort_descending returns False when intent has 'sort' but no table artifact."""
    ctx = _ctx(intent="sort by enrollment", artifact_snapshot={})
    assert _rule_sort_descending(ctx) is False


def test_rule_sort_descending_does_not_fire_without_sort_intent() -> None:
    """_rule_sort_descending returns False when table present but intent lacks 'sort'."""
    ctx = _ctx(
        intent="show enrollment for Houston",
        artifact_snapshot={"table": [{"district_name": "Houston", "display_value": "200000"}]},
    )
    assert _rule_sort_descending(ctx) is False


def test_rule_sort_descending_does_not_fire_on_empty_context() -> None:
    """_rule_sort_descending returns False when no intent and no artifact."""
    ctx = _ctx()
    assert _rule_sort_descending(ctx) is False


# ─── 2. Prefilter rule: _rule_preserves_prior_context ─────────────────────────
#
# Phase 6A tightened this rule from "fires on every turn_index > 0" to a
# three-part gate: follow-up turn AND sort/rank intent AND table produced.
# The PR #967 Opus calibration found the pre-tightening gate fired 27
# vacuous verdicts on clarify/refusal/lookup turns where the rubric had
# nothing to evaluate.


def test_rule_preserves_prior_context_fires_on_sort_followup_with_table() -> None:
    """Happy path: follow-up turn (>=1) + sort intent + table artifact."""
    assert _rule_preserves_prior_context(_sort_ctx(turn_index=1)) is True
    assert _rule_preserves_prior_context(_sort_ctx(turn_index=5)) is True


def test_rule_preserves_prior_context_does_not_fire_on_turn_0() -> None:
    """No prior context on turn 0 — rule never applies regardless of intent."""
    assert _rule_preserves_prior_context(_sort_ctx(turn_index=0)) is False


def test_rule_preserves_prior_context_does_not_fire_when_turn_index_none() -> None:
    """Missing turn_index blocks firing."""
    assert _rule_preserves_prior_context(_ctx(turn_index=None)) is False


def test_rule_preserves_prior_context_does_not_fire_without_sort_intent() -> None:
    """Multi-turn but the user is asking for a comparison, not a re-sort —
    the rubric has nothing to evaluate (this is exactly the case the
    PR #967 Opus calibration flagged as vacuous before tightening)."""
    ctx = _ctx(
        intent="compare Houston and Denver enrollment",
        turn_index=2,
        artifact_snapshot={"table": [{"district_name": "Houston", "display_value": "200000"}]},
    )
    assert _rule_preserves_prior_context(ctx) is False


def test_rule_preserves_prior_context_does_not_fire_on_clarify_turn_with_no_table() -> None:
    """Refusal/clarify turn with sort-keyword intent but no table artifact —
    judge cannot grade an empty response. Mirrors the verdict shape that
    dominated the 27-row calibration sample ('I couldn't structure this
    follow-up — could you rephrase?')."""
    ctx = _ctx(
        intent="sort those districts",
        turn_index=2,
        artifact_snapshot={},
    )
    assert _rule_preserves_prior_context(ctx) is False


def test_rule_preserves_prior_context_fires_on_rank_keyword() -> None:
    """The keyword set covers sort + rank + order + reorder so the gate
    catches the full re-sort vocabulary, not just literal 'sort'."""
    ctx = _ctx(
        intent="rank by enrollment",
        turn_index=1,
        artifact_snapshot={"table": [{"district_name": "A", "display_value": "1"}]},
    )
    assert _rule_preserves_prior_context(ctx) is True


# ── M3 broadening: fire on follow-up refusal with prior context ───────────────


def _refusal_ctx(
    *,
    answer_text: str,
    turn_index: int = 2,
    prior_result_ref_count: int = 1,
    recent_routes: list[str] | None = None,
) -> CompassEvaluatorContext:
    """Context shaped like an M3 follow-up refusal: prior result_refs in
    memory, current turn renders one of the documented refusal sentinels,
    and no table to grade. The legacy sort-with-table branch must not fire
    here — the second branch is the one under test."""
    return make_evaluator_context(
        session_id="22222222-2222-2222-2222-222222222222",
        turn_index=turn_index,
        intent=None,
        artifact_snapshot={},
        answer_text=answer_text,
        prior_result_ref_count=prior_result_ref_count,
        recent_routes=recent_routes if recent_routes is not None else ["execute"],
    )


def test_rule_preserves_prior_context_fires_on_validation_failed_followup() -> None:
    """M3 case 999: turn 2 is the validation-failed prose, prior turn was an
    execute that produced a result_ref. The judge must see this turn."""
    ctx = _refusal_ctx(
        answer_text=(
            "I found a deterministic result, but validation failed before "
            "rendering. Validation findings: multi_metric_unsupported."
        ),
    )
    assert _rule_preserves_prior_context(ctx) is True


def test_rule_preserves_prior_context_fires_on_unsupported_shape_followup() -> None:
    """The fallback refusal prose is one of the M3 failure modes."""
    ctx = _refusal_ctx(
        answer_text=(
            "I couldn't safely run that request as structured. Could you "
            "rephrase as one concrete Compass request?"
        ),
    )
    assert _rule_preserves_prior_context(ctx) is True


def test_rule_preserves_prior_context_fires_on_rescue_clarification_followup() -> None:
    """Planner rescue clarification surfaced when typed enrichment cannot
    anchor on prior context — counts as a follow-up refusal."""
    ctx = _refusal_ctx(
        answer_text=(
            "I couldn't structure that request safely — could you rephrase?"
        ),
    )
    assert _rule_preserves_prior_context(ctx) is True


def test_rule_preserves_prior_context_does_not_fire_on_first_turn_refusal() -> None:
    """A refusal on turn 1 with no prior result_refs and no recent_routes
    still skips — there is no prior context the judge could grade against."""
    ctx = _refusal_ctx(
        answer_text="validation failed before rendering",
        turn_index=1,
        prior_result_ref_count=0,
        recent_routes=[],
    )
    assert _rule_preserves_prior_context(ctx) is False


def test_rule_preserves_prior_context_does_not_fire_on_clean_clarification() -> None:
    """A normal clarification body (no refusal sentinel) on a follow-up
    still skips — the judge has nothing concrete to grade. This guards
    against the broadening leaking into ordinary multi-turn clarifies."""
    ctx = _refusal_ctx(
        answer_text="Which compensation metric did you mean — starting salary or ending salary?",
    )
    assert _rule_preserves_prior_context(ctx) is False


# ─── 3. Prefilter rule: _rule_respects_user_limit ─────────────────────────────


def test_rule_respects_user_limit_fires_when_table_present() -> None:
    """_rule_respects_user_limit fires when artifact_snapshot has a truthy table."""
    ctx = _ctx(artifact_snapshot={"table": [{"district_name": "A", "display_value": "1"}, {"district_name": "B", "display_value": "2"}]})
    assert _rule_respects_user_limit(ctx) is True


def test_rule_respects_user_limit_does_not_fire_without_table() -> None:
    """_rule_respects_user_limit does NOT fire when no table in artifact."""
    ctx = _ctx(artifact_snapshot={})
    assert _rule_respects_user_limit(ctx) is False


def test_rule_respects_user_limit_does_not_fire_on_empty_table() -> None:
    """_rule_respects_user_limit does NOT fire when table is an empty list (falsy)."""
    ctx = _ctx(artifact_snapshot={"table": []})
    assert _rule_respects_user_limit(ctx) is False


# ─── 4. apply_prefilters returns the union of fired rule codes ─────────────────


def test_apply_prefilters_returns_all_three_on_sort_turn_1() -> None:
    """All three Sort rules fire when intent=sort, table present, turn_index=1."""
    ctx = _sort_ctx(turn_index=1)
    fired = apply_prefilters(ctx)
    assert "sort_descending" in fired
    assert "preserves_prior_context" in fired
    assert "respects_user_limit" in fired


def test_apply_prefilters_returns_empty_on_non_sort_turn_0() -> None:
    """No Sort rules fire when there's no sort intent and no table."""
    ctx = _ctx(intent="compare Houston and Denver enrollment", turn_index=0)
    fired = apply_prefilters(ctx)
    assert len(fired) == 0


def test_apply_prefilters_only_sort_descending_and_limit_when_no_prior_context() -> None:
    """On turn 0 with sort intent + table, only sort_descending and respects_user_limit fire."""
    ctx = _sort_ctx(turn_index=0)
    fired = apply_prefilters(ctx)
    assert "sort_descending" in fired
    assert "respects_user_limit" in fired
    assert "preserves_prior_context" not in fired


# ─── 5. Safeguard 1: mandatory globals fire regardless of context ─────────────
#
# We test this at the apply_prefilters level + by checking that mandatory globals
# bypass prefilter logic (they're loaded in the DB layer, which is mocked in
# classifier tests below). Here we verify that mandatory globals are NEVER in
# PREFILTER_RULES (their firing path is different).


def test_mandatory_globals_not_in_prefilter_rules() -> None:
    """forbidden_temporal_phrases is a mandatory global, NOT a prefilter rule.

    Mandatory globals fire via DB load in select_criteria_for_turn(), not via
    PREFILTER_RULES. This test confirms the separation is maintained.
    """
    assert "forbidden_temporal_phrases" not in PREFILTER_RULES


@pytest.mark.asyncio
async def test_scenario_fit_filtered_from_live_classifier_sources() -> None:
    """scenario_fit must not run from mandatory or scenario-required live paths."""
    from compass_backend.quality.classifier import select_criteria_for_turn

    scenario_fit_global = _make_criterion(
        id=10,
        criterion_code="scenario_fit_global",
        check_type="scenario_fit",
        is_mandatory_global=True,
    )
    scenario_fit_required = _make_criterion(
        id=11,
        criterion_code="scenario_fit_required",
        check_type="scenario_fit",
        is_mandatory_global=False,
    )
    deterministic_global = _make_criterion(
        id=12,
        criterion_code="forbidden_temporal_phrases",
        check_type="deterministic",
        is_mandatory_global=True,
    )

    mock_repo = MagicMock()
    mock_repo.load_mandatory_globals = AsyncMock(
        return_value=[scenario_fit_global, deterministic_global]
    )
    mock_repo.load_for_scenario = AsyncMock(return_value=[scenario_fit_required])
    mock_repo.load_active = AsyncMock(
        return_value=[scenario_fit_global, scenario_fit_required, deterministic_global]
    )

    ctx = _ctx(scenario_id=99, triggered_by="user_turn", expected_output=None)
    with patch(
        "compass_backend.quality.classifier.ReadOnlyCriteriaRepository",
        return_value=mock_repo,
    ):
        result = await select_criteria_for_turn(ctx, pool=None, use_ai_classifier=False)

    result_codes = {c.criterion_code for c in result}
    assert "forbidden_temporal_phrases" in result_codes
    assert "scenario_fit_global" not in result_codes
    assert "scenario_fit_required" not in result_codes


@pytest.mark.asyncio
async def test_scenario_fit_classifier_sources_allowed_for_sweep_expected_output() -> None:
    """A sweep with case expectations may select scenario_fit criteria."""
    from compass_backend.quality.classifier import select_criteria_for_turn

    scenario_fit_global = _make_criterion(
        id=10,
        criterion_code="scenario_fit_global",
        check_type="scenario_fit",
        is_mandatory_global=True,
    )
    scenario_fit_required = _make_criterion(
        id=11,
        criterion_code="scenario_fit_required",
        check_type="scenario_fit",
        is_mandatory_global=False,
    )

    mock_repo = MagicMock()
    mock_repo.load_mandatory_globals = AsyncMock(return_value=[scenario_fit_global])
    mock_repo.load_for_scenario = AsyncMock(return_value=[scenario_fit_required])
    mock_repo.load_active = AsyncMock(return_value=[scenario_fit_global, scenario_fit_required])

    ctx = _ctx(
        scenario_id=99,
        triggered_by="sweep_run",
        expected_output=ScenarioExpectation(
            expected_behaviour="The answer should satisfy the case.",
            steps=[],
        ),
    )
    with patch(
        "compass_backend.quality.classifier.ReadOnlyCriteriaRepository",
        return_value=mock_repo,
    ):
        result = await select_criteria_for_turn(ctx, pool=None, use_ai_classifier=False)

    result_codes = {c.criterion_code for c in result}
    assert "scenario_fit_global" in result_codes
    assert "scenario_fit_required" in result_codes


# ─── 6. Selection-works check: citation_format_correct never fires on sort turns ─


def test_citation_format_correct_not_in_prefilter_rules() -> None:
    """citation_format_correct is a non-Sort criterion; prefilters never fire it.

    This is the spec B-spine validation criterion: proves the selection
    mechanism is discriminating. citation_format_correct is seeded in
    compass.criteria but has no sort intent_tags and no prefilter rule,
    so it must NOT appear in apply_prefilters() output for sort turns.
    """
    assert "citation_format_correct" not in PREFILTER_RULES


def test_apply_prefilters_never_returns_citation_format_correct() -> None:
    """Confirm apply_prefilters() never returns citation_format_correct for any context."""
    sort_ctx = _sort_ctx(turn_index=1)
    fired = apply_prefilters(sort_ctx)
    assert "citation_format_correct" not in fired

    # Also check a degenerate context that fires all prefilters
    all_ctx = _ctx(
        intent="sort and rank",
        artifact_snapshot={"table": [{"district_name": "A", "display_value": "1"}]},
        turn_index=3,
    )
    fired_all = apply_prefilters(all_ctx)
    assert "citation_format_correct" not in fired_all


# ─── 7. Type I error guard: AI classifier cannot return invalid codes ──────────
#
# We mock the classifier agent's run() to return an out-of-set code and verify
# that select_criteria_for_turn() ignores it (because active_by_code lookup
# will not find it).


@pytest.mark.asyncio
async def test_ai_classifier_invalid_codes_are_silently_ignored() -> None:
    """AI classifier output of invalid codes is filtered out by active_by_code lookup.

    If the AI classifier somehow returns 'fabricated_criterion_xyz' (which is not
    in compass.criteria), select_criteria_for_turn must not include it in results.

    This models a scenario where the Literal[*codes] constraint fails at the
    model layer (e.g. serialization edge case) — the dict-lookup guard in
    select_criteria_for_turn is a second line of defense.
    """
    from compass_backend.quality.classifier import select_criteria_for_turn

    mandatory_criterion = _make_criterion(
        id=1,
        criterion_code="forbidden_temporal_phrases",
        check_type="deterministic",
        is_mandatory_global=True,
    )
    sort_criterion = _make_criterion(
        id=2,
        criterion_code="sort_descending",
        check_type="deterministic",
        is_mandatory_global=False,
    )
    active_criteria = [mandatory_criterion, sort_criterion]

    # Build a mock pool + repo
    mock_repo = MagicMock()
    mock_repo.load_mandatory_globals = AsyncMock(return_value=[mandatory_criterion])
    mock_repo.load_for_scenario = AsyncMock(return_value=[])
    mock_repo.load_active = AsyncMock(return_value=active_criteria)

    # Mock the AI classifier to return a fabricated code
    fake_result = MagicMock()
    fake_result.output = MagicMock()
    fake_result.output.criterion_codes = ["fabricated_criterion_xyz", "sort_descending"]

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=fake_result)

    ctx = _sort_ctx(turn_index=0)

    with (
        patch("compass_backend.quality.classifier.ReadOnlyCriteriaRepository", return_value=mock_repo),
        patch("compass_backend.quality.classifier._build_classifier_agent", return_value=mock_agent),
    ):
        result = await select_criteria_for_turn(ctx, pool=None, use_ai_classifier=True)

    result_codes = {c.criterion_code for c in result}
    # fabricated_criterion_xyz must NOT appear — it's not in active_by_code
    assert "fabricated_criterion_xyz" not in result_codes
    # sort_descending was in both AI output and prefilters — must appear
    assert "sort_descending" in result_codes
    # mandatory global always appears
    assert "forbidden_temporal_phrases" in result_codes


# ─── 8. AI classifier exception is silently swallowed ─────────────────────────


@pytest.mark.asyncio
async def test_ai_classifier_exception_does_not_break_selection() -> None:
    """When the AI classifier raises, safeguards 1-3 still return their results."""
    from compass_backend.quality.classifier import select_criteria_for_turn

    mandatory_criterion = _make_criterion(
        id=1,
        criterion_code="forbidden_temporal_phrases",
        check_type="deterministic",
        is_mandatory_global=True,
    )
    sort_criterion = _make_criterion(
        id=2,
        criterion_code="sort_descending",
        check_type="deterministic",
        is_mandatory_global=False,
    )
    active_criteria = [mandatory_criterion, sort_criterion]

    mock_repo = MagicMock()
    mock_repo.load_mandatory_globals = AsyncMock(return_value=[mandatory_criterion])
    mock_repo.load_for_scenario = AsyncMock(return_value=[])
    mock_repo.load_active = AsyncMock(return_value=active_criteria)

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=RuntimeError("gateway timeout"))

    ctx = _sort_ctx(turn_index=0)

    with (
        patch("compass_backend.quality.classifier.ReadOnlyCriteriaRepository", return_value=mock_repo),
        patch("compass_backend.quality.classifier._build_classifier_agent", return_value=mock_agent),
    ):
        # Should not raise
        result = await select_criteria_for_turn(ctx, pool=None, use_ai_classifier=True)

    result_codes = {c.criterion_code for c in result}
    # Mandatory global must still fire (safeguard 1)
    assert "forbidden_temporal_phrases" in result_codes
    # Prefilter must still fire for sort intent + table (safeguard 3)
    assert "sort_descending" in result_codes


# ─── 9. criterion_record_to_evaluator dispatch ────────────────────────────────


def test_criterion_record_to_evaluator_dispatch_deterministic() -> None:
    """check_type='deterministic' → RecordDeterministicEvaluator."""
    c = _make_criterion(check_type="deterministic")
    ev = criterion_record_to_evaluator(c)
    assert isinstance(ev, RecordDeterministicEvaluator)
    assert isinstance(ev, RecordEvaluator)


def test_criterion_record_to_evaluator_dispatch_judge_prompt() -> None:
    """check_type='judge_prompt' → RecordJudgmentEvaluator."""
    c = _make_criterion(check_type="judge_prompt")
    ev = criterion_record_to_evaluator(c)
    assert isinstance(ev, RecordJudgmentEvaluator)
    assert isinstance(ev, RecordEvaluator)


def test_criterion_record_to_evaluator_dispatch_span_assertion() -> None:
    """check_type='span_assertion' → RecordSpanEvaluator."""
    c = _make_criterion(check_type="span_assertion")
    ev = criterion_record_to_evaluator(c)
    assert isinstance(ev, RecordSpanEvaluator)
    assert isinstance(ev, RecordEvaluator)


# ─── 10. criterion_record_to_evaluator produces FK-valid VerdictRecords ────────


@pytest.mark.asyncio
async def test_record_evaluator_produces_real_criterion_id() -> None:
    """RecordDeterministicEvaluator produces VerdictRecord with criterion_id = c.id.

    This is the FK-readiness test: the produced VerdictRecord must have
    criterion_id matching the CriterionRecord.id (not the sentinel 0).
    """
    c = _make_criterion(id=42, criterion_code="sort_descending", check_type="deterministic")
    ev = criterion_record_to_evaluator(c)
    ctx = _ctx()
    verdict = await ev.evaluate(ctx)
    assert isinstance(verdict, VerdictRecord)
    assert verdict.criterion_id == 42, (
        f"Expected criterion_id=42 (real FK), got {verdict.criterion_id} (sentinel?)"
    )
    assert verdict.criterion_id != 0, "criterion_id=0 is the GoldenCriterion sentinel; not acceptable here"


@pytest.mark.asyncio
async def test_record_evaluator_uses_real_version_hash() -> None:
    """RecordEvaluator produces VerdictRecord with the real version_hash from CriterionRecord."""
    c = _make_criterion(id=7, criterion_code="respects_user_limit", check_type="deterministic")
    ev = criterion_record_to_evaluator(c)
    ctx = _ctx()
    verdict = await ev.evaluate(ctx)
    assert verdict.criterion_version_hash == c.version_hash, (
        f"Expected version_hash={c.version_hash!r}, got {verdict.criterion_version_hash!r}"
    )
    assert verdict.criterion_version_hash != "golden_criteria", (
        "Should not use the GoldenCriterion sentinel version_hash"
    )


@pytest.mark.asyncio
async def test_record_judgment_evaluator_produces_error_when_judge_unavailable(
    monkeypatch,
) -> None:
    """RecordJudgmentEvaluator gracefully handles judge agent errors."""
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "test-key")
    c = _make_criterion(id=99, criterion_code="preserves_prior_context", check_type="judge_prompt")
    ev = criterion_record_to_evaluator(c)
    ctx = _ctx()

    # Patch _judge_agent to raise so we test the error path without a real agent
    from compass_backend.quality import criteria as criteria_mod

    original = criteria_mod._JUDGE_AGENT

    class _BrokenAgent:
        async def run(self, *args, **kwargs):
            raise ConnectionError("no gateway")

    criteria_mod._JUDGE_AGENT = _BrokenAgent()  # type: ignore[assignment]
    try:
        verdict = await ev.evaluate(ctx)
    finally:
        criteria_mod._JUDGE_AGENT = original

    assert isinstance(verdict, VerdictRecord)
    assert verdict.outcome == "error"
    assert verdict.criterion_id == 99


@pytest.mark.asyncio
async def test_record_judgment_evaluator_skips_when_gateway_key_missing(
    monkeypatch,
) -> None:
    """Missing judge credentials are an infra skip, not a product verdict."""
    monkeypatch.delenv("PYDANTIC_AI_GATEWAY_API_KEY", raising=False)
    c = _make_criterion(id=98, criterion_code="citation_naturalness", check_type="judge_prompt")
    ev = criterion_record_to_evaluator(c)

    verdict = await ev.evaluate(_ctx())

    assert verdict.outcome == "error"
    assert "infra unavailable" in verdict.reason
    assert "PYDANTIC_AI_GATEWAY_API_KEY" in verdict.reason
    assert verdict.evidence.model_dump()["skip_reason"] == "no_pydantic_ai_gateway_api_key"


@pytest.mark.asyncio
async def test_record_judgment_evaluator_emits_not_applicable_as_error_with_prefix(
    monkeypatch,
) -> None:
    """When the judge marks the rubric not_applicable, the evaluator must emit
    outcome='error' with a `not applicable:` reason prefix so the Scorecard's
    _is_excluded_from_score filter drops the trial from both numerator and
    denominator. This matches the deterministic-validator convention used by
    e.g. respects_user_limit when no `top N` is parseable in intent.

    Regression for PR #625 Cluster 3: preserves_prior_context (judge_prompt)
    is over-selected by Safeguard 4 on first turns / greetings / clarifications.
    The rubric update in migration 030 asks the judge to set not_applicable=true
    on those turns; this test locks in the dispatch contract.
    """
    from compass_backend.quality import criteria as criteria_mod
    from compass_backend.quality.criteria import JudgmentResult

    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "test-key")
    c = _make_criterion(
        id=77, criterion_code="preserves_prior_context", check_type="judge_prompt"
    )
    ev = criterion_record_to_evaluator(c)
    ctx = _ctx()

    class _NotApplicableAgent:
        async def run(self, *args, **kwargs):
            class _R:
                output = JudgmentResult(
                    verdict="not_applicable",
                    reason="this turn has no prior context to evaluate",
                )
            return _R()

    original = criteria_mod._JUDGE_AGENT
    criteria_mod._JUDGE_AGENT = _NotApplicableAgent()  # type: ignore[assignment]
    try:
        verdict = await ev.evaluate(ctx)
    finally:
        criteria_mod._JUDGE_AGENT = original

    assert verdict.outcome == "error"
    assert verdict.reason.startswith("not applicable:"), verdict.reason
    assert "this turn has no prior context" in verdict.reason
    assert verdict.criterion_id == 77


@pytest.mark.asyncio
async def test_record_span_evaluator_returns_error_when_no_trace_id() -> None:
    """RecordSpanEvaluator returns outcome='error' when ctx.trace_id is None."""
    c = _make_criterion(id=55, criterion_code="find_metric_called_before_writer", check_type="span_assertion")
    ev = criterion_record_to_evaluator(c)
    ctx = _ctx(trace_id=None)
    verdict = await ev.evaluate(ctx)
    assert isinstance(verdict, VerdictRecord)
    assert verdict.outcome == "error"
    assert "no trace_id" in verdict.reason
    assert verdict.criterion_id == 55


# ─── 11. Mandatory globals come first in selection output ─────────────────────


@pytest.mark.asyncio
async def test_mandatory_globals_appear_first_in_selection_result() -> None:
    """select_criteria_for_turn returns mandatory globals before other criteria."""
    from compass_backend.quality.classifier import select_criteria_for_turn

    mandatory = _make_criterion(
        id=1,
        criterion_code="forbidden_temporal_phrases",
        check_type="deterministic",
        is_mandatory_global=True,
    )
    sort_desc = _make_criterion(
        id=2,
        criterion_code="sort_descending",
        check_type="deterministic",
        is_mandatory_global=False,
    )

    mock_repo = MagicMock()
    mock_repo.load_mandatory_globals = AsyncMock(return_value=[mandatory])
    mock_repo.load_for_scenario = AsyncMock(return_value=[])
    mock_repo.load_active = AsyncMock(return_value=[mandatory, sort_desc])

    ctx = _sort_ctx(turn_index=0)

    with patch("compass_backend.quality.classifier.ReadOnlyCriteriaRepository", return_value=mock_repo):
        result = await select_criteria_for_turn(ctx, pool=None, use_ai_classifier=False)

    assert len(result) >= 1
    # First result must be the mandatory global
    assert result[0].criterion_code == "forbidden_temporal_phrases"
    # Sort criterion must also appear (prefilter fired)
    result_codes = [c.criterion_code for c in result]
    assert "sort_descending" in result_codes
