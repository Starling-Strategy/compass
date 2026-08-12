"""Tests for the backend scorecard pure functions.

Unit tests (no DB) cover K=3 math, placeholder pattern, rounding, and
stub-error filter. Live-DB smoke tests are gated by --run-live-db.

Run unit tests:
    PYTHONPATH=src uv run pytest src/compass_backend/tests/test_scorecard.py -v

Run with live DB:
    PYTHONPATH=src uv run pytest src/compass_backend/tests/test_scorecard.py -v --run-live-db
"""
from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from compass_backend.config import Settings
from compass_backend.db._pool import create_chat_pool
from compass_backend.db.scorecard import (
    CaseRollup,
    SweepRunSummary,
    TrialRollup,
    load_completed_sweep_runs_for_dimension,
    load_case_rollups_for_dimension,
    load_conversation_turns,
    load_session_metadata,
)
from compass_backend.quality.scorecard import (
    STOCK_DIMENSIONS,
    _DEFAULT_BUILD_ID,
    _case_pass_rate,
    _derive_case_display_name,
    _is_excluded_from_score,
    _is_infra_skipped,
    _is_not_applicable,
    _is_stub_error,
    _scenario_score,
    _sweep_runs_are_comparable,
    resolve_build_or_default,
    _slug_to_human,
    compute_conversation_with_verdicts,
    compute_dimension_detail,
    compute_dimension_score,
    compute_scorecard_snapshot,
)
from compass_backend.quality.scorecard_models import (
    CANONICAL_DIM_SLUGS,
)


# ── helpers ───────────────────────────────────────────────────────────────

def _trial(outcome: str, judge_source: str = "judge_prompt", reason: str | None = None) -> TrialRollup:
    return TrialRollup(
        session_id="s-test",
        outcome=outcome,
        judge_source=judge_source,
        criterion_code="C-test",
        reason=reason,
    )


def _rollup(
    scenario_id: int,
    case_id: int,
    session_id: str,
    trials: list[TrialRollup],
) -> CaseRollup:
    return CaseRollup(
        scenario_id=scenario_id,
        case_id=case_id,
        scenario_code=f"G{scenario_id:02d}",
        case_code=f"C{case_id:02d}",
        session_id=session_id,
        trials=trials,
    )


# ── stub-error filter tests ───────────────────────────────────────────────

def test_stub_error_detected():
    t = _trial("error", "deterministic")
    assert _is_stub_error(t) is True


def test_judge_prompt_error_not_stub():
    t = _trial("error", "judge_prompt")
    assert _is_stub_error(t) is False


def test_deterministic_pass_not_stub():
    t = _trial("pass", "deterministic")
    assert _is_stub_error(t) is False


# ── not-applicable filter tests ───────────────────────────────────────────

def test_not_applicable_detected_via_reason_prefix():
    """An error trial whose reason starts with `not applicable:` is N/A."""
    t = _trial(
        "error",
        "deterministic",
        reason="not applicable: no 'top N' phrase in user intent",
    )
    assert _is_not_applicable(t) is True


def test_not_applicable_distinguished_from_missing_artifact_error():
    """`missing artifact:` errors are real failures, NOT not-applicable."""
    t = _trial("error", "deterministic", reason="missing artifact: table")
    assert _is_not_applicable(t) is False


def test_not_applicable_requires_error_outcome():
    """A pass/fail with `not applicable:` reason is malformed input; the
    helper should only return True for outcome='error'.
    """
    t = _trial("pass", "deterministic", reason="not applicable: irrelevant")
    assert _is_not_applicable(t) is False


def test_infra_skipped_detected_via_reason_and_error_outcome():
    t = _trial(
        "error",
        "span_assertion",
        reason=(
            "criterion 'span' not evaluated: infra unavailable "
            "(LOGFIRE_READ_TOKEN not set)."
        ),
    )
    assert _is_infra_skipped(t) is True


def test_infra_skipped_detects_missing_gateway_key():
    t = _trial(
        "error",
        "judge_prompt",
        reason=(
            "criterion 'citation_naturalness' not evaluated: infra unavailable "
            "(PYDANTIC_AI_GATEWAY_API_KEY not set)."
        ),
    )
    assert _is_infra_skipped(t) is True


def test_excluded_combines_stub_and_not_applicable():
    """The combined filter excludes stub, N/A, and infra-skipped trials."""
    stub = _trial("error", "deterministic", reason="stub: not wired")
    na = _trial("error", "deterministic", reason="not applicable: no top N")
    infra = _trial(
        "error",
        "judge_prompt",
        reason="infra unavailable (PYDANTIC_AI_GATEWAY_API_KEY not set).",
    )
    real_error = _trial("error", "judge_prompt", reason="missing artifact: x")
    real_pass = _trial("pass", "judge_prompt")
    assert _is_excluded_from_score(stub) is True
    assert _is_excluded_from_score(na) is True
    assert _is_excluded_from_score(infra) is True
    # Real errors (not from the stub path, not N/A) stay in the score
    assert _is_excluded_from_score(real_error) is False
    assert _is_excluded_from_score(real_pass) is False


# ── case pass-rate tests ──────────────────────────────────────────────────

def test_case_pass_rate_all_pass():
    r = _rollup(1, 1, "s1", [_trial("pass"), _trial("pass"), _trial("pass")])
    assert _case_pass_rate(r) == 1.0


def test_case_pass_rate_two_of_three():
    r = _rollup(1, 1, "s1", [_trial("pass"), _trial("pass"), _trial("fail")])
    rate = _case_pass_rate(r)
    assert abs(rate - 2 / 3) < 1e-9


def test_case_pass_rate_stub_errors_excluded_from_denominator():
    """Case with 1 pass + 2 stub-errors → pass rate = 1/1 = 1.0 (not 1/3)."""
    r = _rollup(1, 1, "s1", [
        _trial("pass", "judge_prompt"),
        _trial("error", "deterministic"),
        _trial("error", "deterministic"),
    ])
    rate = _case_pass_rate(r)
    assert rate == 1.0, f"Expected 1.0, got {rate}"


def test_case_pass_rate_all_stub_errors_returns_none():
    """When every verdict is a stub error, return None (case skipped)."""
    r = _rollup(1, 1, "s1", [
        _trial("error", "deterministic"),
        _trial("error", "deterministic"),
    ])
    assert _case_pass_rate(r) is None


def test_case_pass_rate_mixed_stub_and_fail():
    """1 fail + 2 stub-errors → pass rate = 0/1 = 0.0."""
    r = _rollup(1, 1, "s1", [
        _trial("fail", "judge_prompt"),
        _trial("error", "deterministic"),
        _trial("error", "deterministic"),
    ])
    rate = _case_pass_rate(r)
    assert rate == 0.0


def test_case_pass_rate_not_applicable_excluded_from_denominator():
    """A `not applicable:` trial counts toward neither numerator nor
    denominator — the score reflects only evaluable trials.
    1 pass + 2 N/A → pass rate = 1/1 = 1.0 (not 1/3).
    """
    r = _rollup(1, 1, "s1", [
        _trial("pass", "deterministic"),
        _trial("error", "deterministic", reason="not applicable: no top N"),
        _trial("error", "deterministic", reason="not applicable: no top N"),
    ])
    rate = _case_pass_rate(r)
    assert rate == 1.0


def test_case_pass_rate_all_not_applicable_returns_none():
    """When every trial is N/A, the case has no evaluable signal and
    drops out of the scenario mean entirely (returns None).
    """
    r = _rollup(1, 1, "s1", [
        _trial("error", "deterministic", reason="not applicable: foo"),
        _trial("error", "deterministic", reason="not applicable: bar"),
    ])
    assert _case_pass_rate(r) is None


def test_case_pass_rate_mixed_na_and_real_error():
    """N/A trials drop out, but a real `missing artifact:` error stays
    in the denominator and counts against the score.
    1 pass + 1 N/A + 1 real error → pass rate = 1/2 = 0.5.
    """
    r = _rollup(1, 1, "s1", [
        _trial("pass", "judge_prompt"),
        _trial("error", "deterministic", reason="not applicable: no top N"),
        _trial("error", "judge_prompt", reason="missing artifact: answer_text"),
    ])
    rate = _case_pass_rate(r)
    assert rate == 0.5


# ── K=3 macro-averaging math (ADR worked example G07) ────────────────────

def test_k3_scenario_score_adr_example():
    """ADR worked example: case_pass_rates = [1.0, 1.0, 0.6667]
    → scenario_score ≈ 0.8889 → score_pct = 89.
    """
    case_rates = [1.0, 1.0, 2 / 3]
    score = _scenario_score(case_rates)
    assert abs(score - (1.0 + 1.0 + 2 / 3) / 3) < 1e-9
    # → 0.8888...
    score_pct = round(score * 100)
    assert score_pct == 89


def test_scenario_score_empty_returns_zero():
    assert _scenario_score([]) == 0.0


# ── Float → int rounding ──────────────────────────────────────────────────

@pytest.mark.parametrize("rate,expected", [
    (1 / 3, 33),   # 0.3333... → 33
    (2 / 3, 67),   # 0.6666... → 67
    (0.5, 50),
    (0.995, round(0.995 * 100)),   # 100
    (0.005, round(0.005 * 100)),   # 1 (or 0 depending on banker's rounding)
])
def test_score_pct_rounding(rate: float, expected: int):
    assert round(rate * 100) == expected


# ── Slug ↔ human name ─────────────────────────────────────────────────────

def test_slug_to_human_known_dims():
    assert _slug_to_human("sort-accuracy") == "Sort Accuracy"
    assert _slug_to_human("data-fidelity") == "Data Fidelity"
    assert _slug_to_human("citation-accuracy") == "Citation Accuracy"


def test_stock_dimensions_covers_all_canonical_slugs():
    for slug in CANONICAL_DIM_SLUGS:
        assert slug in STOCK_DIMENSIONS, f"Missing STOCK_DIMENSIONS entry for {slug!r}"


def test_canonical_set_is_seven_and_drops_two():
    """Guard against accidental re-add of Surface Consistency or Process Integrity.

    The Accuracy Framework Worksheet (M6) names seven accuracy dimensions plus
    Speed (latency, separate axis). Surface Consistency is a per-dimension
    case-tag, not a top-level dimension; Process Integrity isn't in the
    framework. If either of these slugs reappears, this test catches it before
    the scorecard view drifts again.
    """
    assert len(CANONICAL_DIM_SLUGS) == 7
    assert "surface-consistency" not in CANONICAL_DIM_SLUGS
    assert "process-integrity" not in CANONICAL_DIM_SLUGS


def test_structural_diagnostic_retrofit_blocks_active_scorecard_ownership():
    """Migration #076 must fail if diagnostics still own active scorecard rows."""
    migration = (
        Path(__file__).resolve().parents[2]
        / "compass_data_sync/migrations/076_retrofit_structural_diagnostics.sql"
    )
    text = migration.read_text()

    assert "Surface Consistency" in text
    assert "Process Integrity" in text
    assert "accuracy_primary_dimension IN ('Surface Consistency', 'Process Integrity')" in text
    assert "metadata->>'scorecard_dimension' IN ('Surface Consistency', 'Process Integrity')" in text
    assert "RAISE EXCEPTION" in text
    assert "structural_diagnostic_dimension" in text
    assert "'SCORECARD-SURFACE-FOUNDATION' THEN 'Data Fidelity'" in text
    assert "'SCORECARD-PROCESS-FOUNDATION' THEN 'Selection Accuracy'" in text


# ── compute_dimension_score (pure logic; pool injected) ───────────────────

class _FakePool:
    """Fake asyncpg.Pool that returns pre-configured rollups."""

    def __init__(self, rollups: list[CaseRollup]):
        self._rollups = rollups

    # compute_dimension_score calls load_case_rollups_for_dimension which
    # calls pool.acquire(). We patch at the scorecard module level instead.


@pytest.mark.asyncio
async def test_compute_dimension_score_placeholder_when_no_rollups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dimension with no rollups → DimensionScore(score_pct=0, n_trials=0)."""
    async def fake_load(*args, **kwargs) -> list:
        return []

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    # pool is never used because the mock short-circuits the DB call
    dim = await compute_dimension_score(None, "selection-accuracy")  # type: ignore[arg-type]
    assert dim.dim_slug == "selection-accuracy"
    assert dim.name == "Selection Accuracy"
    assert dim.score_pct == 0
    assert dim.n_trials == 0
    assert dim.regressed is False


@pytest.mark.asyncio
async def test_compute_dimension_score_k3_math(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feed ADR G07 rollups → score_pct = 89."""
    # Three cases: pass_rates [1.0, 1.0, 0.6667]
    rollups = [
        _rollup(7, 1, "s1", [_trial("pass"), _trial("pass"), _trial("pass")]),
        _rollup(7, 2, "s2", [_trial("pass"), _trial("pass"), _trial("pass")]),
        _rollup(7, 3, "s3", [_trial("pass"), _trial("pass"), _trial("fail")]),
    ]

    async def fake_load(*args, **kwargs) -> list:
        return rollups

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    dim = await compute_dimension_score(None, "sort-accuracy")  # type: ignore[arg-type]
    assert dim.score_pct == 89
    # n_trials = 3 + 3 + 3 = 9 (no stubs)
    assert dim.n_trials == 9


@pytest.mark.asyncio
async def test_compute_dimension_score_stub_error_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 pass + 2 stub-errors per case → case_pass_rate=1.0 → score_pct=100."""
    rollups = [
        _rollup(1, 1, "s1", [
            _trial("pass", "judge_prompt"),
            _trial("error", "deterministic"),
            _trial("error", "deterministic"),
        ]),
    ]

    async def fake_load(*args, **kwargs) -> list:
        return rollups

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    dim = await compute_dimension_score(None, "sort-accuracy")  # type: ignore[arg-type]
    assert dim.score_pct == 100
    # n_trials counts only evaluable trials (1 non-stub)
    assert dim.n_trials == 1


# ── EVAL-R4 regression: macro-average under asymmetric cardinality ────────


@pytest.mark.asyncio
async def test_compute_dimension_score_macro_average_not_drowned_by_large_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EVAL-R4: a 1-case scenario at 0% must NOT be drowned by a 10-case
    scenario at 100% — the dimension lands ~50% (macro: mean of scenario
    means), NOT ~91% (micro: weighted by case count).

    This pins macro-averaging at the scenario→dimension rollup. The math is
    in `_score_rollups`: group rollups by scenario_id, take each scenario's
    mean case pass-rate, then take the unweighted mean of those scenario
    scores. A silent regression to micro-averaging (one big pool of cases,
    weighted by count) would compute round((0*1 + 1.0*10) / 11 * 100) = 91
    and this assertion would fail.
    """
    # Scenario 1: a single case, all-fail → scenario score 0.0.
    small_scenario = [
        _rollup(1, 1, "s1-c1", [_trial("fail"), _trial("fail"), _trial("fail")]),
    ]
    # Scenario 2: ten cases, all-pass → scenario score 1.0.
    large_scenario = [
        _rollup(2, case_id, f"s2-c{case_id}", [_trial("pass"), _trial("pass")])
        for case_id in range(1, 11)
    ]
    rollups = small_scenario + large_scenario

    async def fake_load(*args, **kwargs) -> list:
        return rollups

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    dim = await compute_dimension_score(None, "selection-accuracy")  # type: ignore[arg-type]

    # Macro: mean([0.0, 1.0]) = 0.5 → 50. NOT micro: 10/11 → 91.
    assert dim.score_pct == 50, (
        f"Expected 50 (macro-average of scenario means), got {dim.score_pct}. "
        "A value near 91 means the rollup regressed to micro-averaging "
        "(weighting by case count), drowning the small scenario."
    )
    # Sanity: n_trials is the raw evaluable-trial count (3 + 10*2 = 23),
    # confirming the large scenario really does dominate by raw cardinality —
    # which is exactly what macro-averaging must ignore.
    assert dim.n_trials == 23


# ── EVAL-R4 regression: worst-result-wins through scoring ─────────────────


@pytest.mark.asyncio
async def test_compute_dimension_score_worst_result_wins_through_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EVAL-R4: a case with (fail, error, pass) across attempts must yield the
    worst (fail) consequence through `_score_rollups` / `compute_dimension_score`,
    not only at the per-Trial collapse in `compute_dimension_detail`.

    The `error` here is a real judge_prompt error (NOT a stub error), so it
    stays in the denominator. With one fail, one error, and one pass evaluable,
    the case pass-rate is 1/3 ≈ 0.333 → a single-scenario dimension score of 33.

    The worst-result-wins property: the fail drags the score below 100. If the
    priority were swapped so a pass (or error) could mask a sibling fail — e.g.
    if scoring collapsed each case to its best attempt — this single-case
    dimension would read 100 and the assertion would fail.
    """
    rollups = [
        _rollup(1, 1, "s1", [
            _trial("fail", "judge_prompt", reason="real-fail"),
            _trial("error", "judge_prompt", reason="judge could not decide"),
            _trial("pass", "judge_prompt"),
        ]),
    ]

    async def fake_load(*args, **kwargs) -> list:
        return rollups

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    dim = await compute_dimension_score(None, "selection-accuracy")  # type: ignore[arg-type]

    # 1 pass / 3 evaluable = 0.333... → 33. The fail and the (real) error both
    # count against the score; only the single pass lifts it. The worst result
    # (fail) is reflected — the score is NOT 100.
    assert dim.score_pct == 33, (
        f"Expected 33 (1 pass / 3 evaluable trials), got {dim.score_pct}. "
        "A value of 100 means a passing attempt masked the sibling fail — "
        "worst-result-wins no longer propagates through scoring."
    )
    # All three trials are evaluable (the error is a real judge error, not a
    # deterministic stub), so none drop out of the denominator.
    assert dim.n_trials == 3


# ── Fix 3b: drill-down case-outcome priority (fail beats error) ───────────


@pytest.mark.asyncio
async def test_compute_dimension_detail_fail_beats_error_in_case_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trials (fail, fail, error) → case outcome="fail", not "error".

    The reviewer flagged this: real failures are the signal we want to surface,
    so fail takes precedence over error in the per-case drill-down label.
    """
    rollups = [
        _rollup(1, 1, "s1", [
            _trial("fail", reason="fail-A"),
            _trial("fail", reason="fail-B"),
            _trial("error", "judge_prompt", reason="error-A"),
        ]),
    ]

    async def fake_load(*args, **kwargs):
        return rollups

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    detail = await compute_dimension_detail(None, "sort-accuracy")  # type: ignore[arg-type]
    assert len(detail.cases) == 1
    case = detail.cases[0]
    assert len(case.trials) == 1
    trial = case.trials[0]
    assert trial.outcome == "fail"
    # Reason should be from the first fail, not from the error.
    assert trial.reason == "fail-A"


@pytest.mark.asyncio
async def test_compute_dimension_detail_all_errors_case_outcome_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All evaluable trials are errors (non-stub) → case outcome="error"."""
    rollups = [
        _rollup(1, 1, "s1", [
            _trial("error", "judge_prompt", reason="err-1"),
            _trial("error", "judge_prompt", reason="err-2"),
        ]),
    ]

    async def fake_load(*args, **kwargs):
        return rollups

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    detail = await compute_dimension_detail(None, "sort-accuracy")  # type: ignore[arg-type]
    case = detail.cases[0]
    trial = case.trials[0]
    assert trial.outcome == "error"
    assert trial.reason == "err-1"


@pytest.mark.asyncio
async def test_compute_dimension_detail_populates_scenario_id_and_clean_case_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ScenarioCase.scenario_id comes from CaseRollup.scenario_code; case_id
    is the case_code verbatim (no double-prefix with scenario_code).
    """
    # Build a rollup whose case_code already contains the scenario prefix —
    # this mirrors the curated SORT-CURATED-01-C00 shape from spec B PR 2/3.
    rollup = CaseRollup(
        scenario_id=1,
        case_id=1,
        scenario_code="SORT-CURATED-01",
        case_code="SORT-CURATED-01-C00",
        session_id="s1",
        trials=[_trial("pass")],
    )

    async def fake_load(*args, **kwargs):
        return [rollup]

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    detail = await compute_dimension_detail(None, "sort-accuracy")  # type: ignore[arg-type]
    assert len(detail.cases) == 1
    case = detail.cases[0]
    # Fix 2: scenario_id is the scenario code, populated for downstream grouping.
    assert case.scenario_id == "SORT-CURATED-01"
    # Fix 1: case_id is the case_code verbatim — no doubled scenario prefix.
    assert case.case_id == "SORT-CURATED-01-C00"
    # And specifically NOT the old double-prefix shape.
    assert case.case_id != "SORT-CURATED-01-SORT-CURATED-01-C00"


@pytest.mark.asyncio
async def test_compute_dimension_detail_pass_then_fail_case_outcome_is_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trials (pass, fail) → case outcome="fail" (any fail wins over passes)."""
    rollups = [
        _rollup(1, 1, "s1", [
            _trial("pass"),
            _trial("fail", reason="real-fail"),
        ]),
    ]

    async def fake_load(*args, **kwargs):
        return rollups

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    detail = await compute_dimension_detail(None, "sort-accuracy")  # type: ignore[arg-type]
    case = detail.cases[0]
    trial = case.trials[0]
    assert trial.outcome == "fail"
    assert trial.reason == "real-fail"


# ── Fix 4: _derive_case_display_name ──────────────────────────────────────


def test_derive_case_display_name_short_prompt_returned_as_is():
    inputs = [{"prompt": "Rank the largest five districts.", "step_complete_when": None}]
    assert (
        _derive_case_display_name("C04", inputs)
        == "Rank the largest five districts."
    )


def test_derive_case_display_name_truncates_with_ellipsis():
    long_prompt = "a" * 120
    name = _derive_case_display_name("C04", [{"prompt": long_prompt}])
    # 60 chars + the single-character ellipsis.
    assert name.endswith("…")
    assert len(name) == 61
    assert name[:-1] == "a" * 60


def test_derive_case_display_name_truncates_at_boundary_strips_trailing_ws():
    # 60 chars including trailing whitespace at the cut point — trailing
    # whitespace before the ellipsis should be stripped.
    prompt = ("word " * 20).strip()  # well over 60 chars
    name = _derive_case_display_name("C04", [{"prompt": prompt}])
    assert name.endswith("…")
    # The 60-char slice ends mid-word; just confirm we didn't leave
    # whitespace immediately before the ellipsis.
    assert not name[:-1].endswith(" ")


def test_derive_case_display_name_strips_whitespace_for_short_prompt():
    inputs = [{"prompt": "  hello world  "}]
    assert _derive_case_display_name("C04", inputs) == "hello world"


def test_derive_case_display_name_falls_back_when_inputs_none():
    assert _derive_case_display_name("C04", None) == "C04"


def test_derive_case_display_name_falls_back_when_inputs_empty_list():
    assert _derive_case_display_name("C04", []) == "C04"


def test_derive_case_display_name_falls_back_when_no_prompt_key():
    assert _derive_case_display_name("C04", [{"step_complete_when": "x"}]) == "C04"


def test_derive_case_display_name_falls_back_when_prompt_is_non_string():
    assert _derive_case_display_name("C04", [{"prompt": 42}]) == "C04"


def test_derive_case_display_name_skips_blank_prompts_to_next_step():
    inputs = [
        {"prompt": "   "},
        {"prompt": "Second step prompt."},
    ]
    assert (
        _derive_case_display_name("C04", inputs)
        == "Second step prompt."
    )


# ── build registry resolution (migration 075) ─────────────────────────────


@pytest.mark.asyncio
async def test_resolve_build_or_default_pool_none_uses_bootstrap() -> None:
    """Pool=None path is for test fixtures — returns the bootstrap row."""
    record = await resolve_build_or_default(None, None)
    assert record.build_id == _DEFAULT_BUILD_ID
    assert record.criterion_set_version == "compass_criteria_v1"


@pytest.mark.asyncio
async def test_resolve_build_or_default_pool_none_honors_explicit_id() -> None:
    """Explicit build_id on the pool=None path round-trips into the synthetic row."""
    record = await resolve_build_or_default(None, "b-test-123")
    assert record.build_id == "b-test-123"


@pytest.mark.asyncio
async def test_resolve_build_or_default_unknown_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregistered build_id surfaces a clear error instead of silent data."""

    async def fake_load_build(pool, build_id):  # noqa: ARG001
        return None

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_build", fake_load_build
    )

    sentinel_pool = object()
    with pytest.raises(ValueError, match="not registered"):
        await resolve_build_or_default(sentinel_pool, "b-fake")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_compute_scorecard_snapshot_rejects_unknown_build_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing a non-registered build_id must error, not silently return global data."""

    async def fake_load(*args, **kwargs):
        return []

    async def fake_load_build(pool, build_id):  # noqa: ARG001
        return None

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_build", fake_load_build
    )

    sentinel_pool = object()
    with pytest.raises(ValueError, match="not registered"):
        await compute_scorecard_snapshot(sentinel_pool, build_id="b-fake")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_compute_scorecard_snapshot_accepts_default_build_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    snap_none = await compute_scorecard_snapshot(None, build_id=None)  # type: ignore[arg-type]
    snap_explicit = await compute_scorecard_snapshot(  # type: ignore[arg-type]
        None, build_id=_DEFAULT_BUILD_ID,
    )
    assert snap_none.build.build_id == _DEFAULT_BUILD_ID
    assert snap_explicit.build.build_id == _DEFAULT_BUILD_ID


@pytest.mark.asyncio
async def test_compute_dimension_score_applies_threshold_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compass_backend.quality.thresholds import DimensionThreshold, ThresholdConfig

    rollups = [_rollup(1, 1, "s1", [_trial("pass"), _trial("pass")])]

    async def fake_load(*args, **kwargs):
        return rollups

    config = ThresholdConfig(
        version="test_thresholds_v1",
        review_date="2026-06-22",
        dimensions={
            slug: DimensionThreshold(launch_threshold_pct=95)
            for slug in CANONICAL_DIM_SLUGS
        },
    )

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_threshold_config",
        lambda: config,
    )

    dim = await compute_dimension_score(None, "selection-accuracy")  # type: ignore[arg-type]

    assert dim.score_pct == 100
    assert dim.threshold_pct == 95
    assert dim.threshold_status == "pass"


@pytest.mark.asyncio
async def test_compute_dimension_score_applies_threshold_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compass_backend.quality.thresholds import DimensionThreshold, ThresholdConfig

    rollups = [_rollup(1, 1, "s1", [_trial("pass"), _trial("fail")])]

    async def fake_load(*args, **kwargs):
        return rollups

    config = ThresholdConfig(
        version="test_thresholds_v1",
        review_date="2026-06-22",
        dimensions={
            slug: DimensionThreshold(launch_threshold_pct=95)
            for slug in CANONICAL_DIM_SLUGS
        },
    )

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_threshold_config",
        lambda: config,
    )

    dim = await compute_dimension_score(None, "selection-accuracy")  # type: ignore[arg-type]

    assert dim.score_pct == 50
    assert dim.threshold_pct == 95
    assert dim.threshold_status == "fail"


@pytest.mark.asyncio
async def test_compute_dimension_score_no_data_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compass_backend.quality.thresholds import DimensionThreshold, ThresholdConfig

    async def fake_load(*args, **kwargs):
        return []

    config = ThresholdConfig(
        version="test_thresholds_v1",
        review_date="2026-06-22",
        dimensions={
            slug: DimensionThreshold(launch_threshold_pct=95)
            for slug in CANONICAL_DIM_SLUGS
        },
    )

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_threshold_config",
        lambda: config,
    )

    dim = await compute_dimension_score(None, "selection-accuracy")  # type: ignore[arg-type]

    assert dim.n_trials == 0
    assert dim.threshold_pct == 95
    assert dim.threshold_status == "no_data"


@pytest.mark.asyncio
async def test_compute_scorecard_snapshot_includes_threshold_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compass_backend.quality.thresholds import DimensionThreshold, ThresholdConfig

    async def fake_load(*args, **kwargs):
        return []

    config = ThresholdConfig(
        version="test_thresholds_v1",
        review_date="2026-06-22",
        dimensions={
            slug: DimensionThreshold(launch_threshold_pct=95)
            for slug in CANONICAL_DIM_SLUGS
        },
    )

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_threshold_config",
        lambda: config,
    )

    snap = await compute_scorecard_snapshot(None)  # type: ignore[arg-type]

    assert snap.build.threshold_version == "test_thresholds_v1"
    assert snap.build.threshold_review_date == "2026-06-22"
    assert all(dim.threshold_pct == 95 for dim in snap.dimensions)


@pytest.mark.asyncio
async def test_dimension_score_includes_exemplar_case_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compass_backend.quality.exemplar_manifest import (
        DimensionExemplarInventory,
        ExemplarManifest,
    )

    async def fake_load(*args, **kwargs):
        return []

    manifest = ExemplarManifest.model_validate({
        "version": "test_exemplars_v1",
        "minimum_active_cases_per_dimension": 3,
        "dimensions": {
            slug: {
                "dimension_name": STOCK_DIMENSIONS[slug][0],
                "minimum_active_cases": 3,
                "required_archetypes": ["literal", "edge", "multi_turn"],
            }
            for slug in CANONICAL_DIM_SLUGS
        },
    })

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    dim = await compute_dimension_score(  # type: ignore[arg-type]
        None,
        "selection-accuracy",
        exemplar_manifest=manifest,
        exemplar_inventory=[
            DimensionExemplarInventory(
                dim_slug="selection-accuracy",
                dimension_name="Selection Accuracy",
                active_case_count=3,
                archetypes=("literal", "edge", "multi_turn"),
            )
        ],
    )

    assert dim.exemplar_case_count == 3
    assert dim.exemplar_status == "complete"


@pytest.mark.asyncio
async def test_dimension_score_requires_exemplar_archetypes_for_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compass_backend.quality.exemplar_manifest import (
        DimensionExemplarInventory,
        ExemplarManifest,
    )

    async def fake_load(*args, **kwargs):
        return []

    manifest = ExemplarManifest.model_validate({
        "version": "test_exemplars_v1",
        "minimum_active_cases_per_dimension": 3,
        "dimensions": {
            slug: {
                "dimension_name": STOCK_DIMENSIONS[slug][0],
                "minimum_active_cases": 3,
                "required_archetypes": ["literal", "edge", "multi_turn"],
            }
            for slug in CANONICAL_DIM_SLUGS
        },
    })

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    dim = await compute_dimension_score(  # type: ignore[arg-type]
        None,
        "selection-accuracy",
        exemplar_manifest=manifest,
        exemplar_inventory=[
            DimensionExemplarInventory(
                dim_slug="selection-accuracy",
                dimension_name="Selection Accuracy",
                active_case_count=3,
                archetypes=("literal", "edge"),
            )
        ],
    )

    assert dim.exemplar_case_count == 3
    assert dim.exemplar_status == "incomplete"


@pytest.mark.asyncio
async def test_scorecard_snapshot_reports_exemplar_coverage_for_each_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compass_backend.quality.exemplar_manifest import (
        DimensionExemplarInventory,
        ExemplarManifest,
    )

    async def fake_load(*args, **kwargs):
        return []

    async def fake_inventory(*args, **kwargs):
        return [
            DimensionExemplarInventory(
                dim_slug=slug,
                dimension_name=STOCK_DIMENSIONS[slug][0],
                active_case_count=3,
                archetypes=("literal", "edge", "multi_turn"),
            )
            for slug in CANONICAL_DIM_SLUGS
        ]

    manifest = ExemplarManifest.model_validate({
        "version": "test_exemplars_v1",
        "minimum_active_cases_per_dimension": 3,
        "dimensions": {
            slug: {
                "dimension_name": STOCK_DIMENSIONS[slug][0],
                "minimum_active_cases": 3,
                "required_archetypes": ["literal", "edge", "multi_turn"],
            }
            for slug in CANONICAL_DIM_SLUGS
        },
    })

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_exemplar_manifest",
        lambda: manifest,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_exemplar_inventory",
        fake_inventory,
    )

    snap = await compute_scorecard_snapshot(None)  # type: ignore[arg-type]

    assert [dim.exemplar_case_count for dim in snap.dimensions] == [3] * 7
    assert all(dim.exemplar_status == "complete" for dim in snap.dimensions)


@pytest.mark.asyncio
async def test_completed_sweep_lookup_orders_latest_then_previous() -> None:
    class _FakeAcquire:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def fetch(self, sql, *args):
            self.sql = sql
            self.args = args
            return [
                {
                    "id": "latest-run",
                    "dimension": "Sort Accuracy",
                    "finished_at": "2026-05-23T12:00:00+00:00",
                    "verdict_count": 24,
                    "trial_count": 9,
                    "case_count": 3,
                    "k": 3,
                    "git_sha": "abc123",
                    "criterion_set_version": "compass_criteria_v1",
                    "criterion_version_hashes": "deadbeef",
                    "check_types": ["judge_prompt"],
                },
                {
                    "id": "previous-run",
                    "dimension": "Sort Accuracy",
                    "finished_at": "2026-05-22T12:00:00+00:00",
                    "verdict_count": 21,
                    "trial_count": 9,
                    "case_count": 3,
                    "k": 3,
                    "git_sha": "abc123",
                    # Pre-091 rows (or a DB whose 091 hasn't been applied) read
                    # NULL here; loader maps NULL check_types to None.
                    "criterion_set_version": None,
                    "criterion_version_hashes": None,
                    "check_types": None,
                },
            ]

    class _FakePool:
        def __init__(self):
            self.conn = _FakeAcquire()

        def acquire(self):
            return self.conn

    pool = _FakePool()

    runs = await load_completed_sweep_runs_for_dimension(  # type: ignore[arg-type]
        pool,
        "Sort Accuracy",
        limit=2,
    )

    assert [run.id for run in runs] == ["latest-run", "previous-run"]
    assert [run.k for run in runs] == [3, 3]
    assert [run.git_sha for run in runs] == ["abc123", "abc123"]
    assert pool.conn.args == ("Sort Accuracy", 2)
    # Grader provenance is loaded (issue #1257): populated on the latest run,
    # NULL → None on the pre-091 previous run.
    assert runs[0].criterion_set_version == "compass_criteria_v1"
    assert runs[0].criterion_version_hashes == "deadbeef"
    assert runs[0].check_types == ["judge_prompt"]
    assert runs[1].criterion_set_version is None
    assert runs[1].check_types is None
    assert "criterion_version_hashes" in pool.conn.sql
    assert "status = 'completed'" in pool.conn.sql
    assert "ORDER BY finished_at DESC NULLS LAST" in pool.conn.sql


@pytest.mark.asyncio
async def test_compute_dimension_score_sets_trend_when_previous_sweep_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_rollups = [_rollup(1, 1, "s-current", [_trial("pass"), _trial("pass")])]
    previous_rollups = [_rollup(1, 1, "s-prev", [_trial("pass"), _trial("fail")])]

    async def fake_runs(*args, **kwargs):
        # Identical grader provenance (criterion_set_version /
        # criterion_version_hashes / check_types) so the two runs stay
        # comparable and a delta is exposed (issue #1257).
        return [
            SweepRunSummary(
                id="latest-run",
                dimension="Sort Accuracy",
                finished_at="2026-05-23T12:00:00+00:00",
                verdict_count=2,
                trial_count=2,
                case_count=1,
                k=3,
                git_sha="abc123",
                criterion_set_version="compass_criteria_v1",
                criterion_version_hashes="deadbeef",
                check_types=["judge_prompt"],
            ),
            SweepRunSummary(
                id="previous-run",
                dimension="Sort Accuracy",
                finished_at="2026-05-22T12:00:00+00:00",
                verdict_count=2,
                trial_count=2,
                case_count=1,
                k=3,
                git_sha="abc123",
                criterion_set_version="compass_criteria_v1",
                criterion_version_hashes="deadbeef",
                check_types=["judge_prompt"],
            ),
        ]

    async def fake_load(*args, **kwargs):
        if kwargs.get("sweep_run_id") == "latest-run":
            return current_rollups
        if kwargs.get("sweep_run_id") == "previous-run":
            return previous_rollups
        return []

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_completed_sweep_runs_for_dimension",
        fake_runs,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    dim = await compute_dimension_score(None, "sort-accuracy")  # type: ignore[arg-type]

    assert dim.latest_sweep_run_id == "latest-run"
    assert dim.latest_finished_at == "2026-05-23T12:00:00+00:00"
    assert dim.previous_score_pct == 50
    assert dim.delta_pct == 50
    assert dim.regressed is False


@pytest.mark.asyncio
async def test_compute_dimension_score_leaves_trend_empty_without_previous_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_rollups = [_rollup(1, 1, "s-current", [_trial("pass"), _trial("pass")])]

    async def fake_runs(*args, **kwargs):
        return [
            SweepRunSummary(
                id="latest-run",
                dimension="Sort Accuracy",
                finished_at="2026-05-23T12:00:00+00:00",
                verdict_count=2,
                trial_count=2,
                case_count=1,
                k=3,
                git_sha="abc123",
            )
        ]

    async def fake_load(*args, **kwargs):
        return current_rollups if kwargs.get("sweep_run_id") == "latest-run" else []

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_completed_sweep_runs_for_dimension",
        fake_runs,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    dim = await compute_dimension_score(None, "sort-accuracy")  # type: ignore[arg-type]

    assert dim.latest_sweep_run_id == "latest-run"
    assert dim.previous_score_pct is None
    assert dim.delta_pct is None


@pytest.mark.asyncio
async def test_compute_dimension_score_omits_trend_for_noncomparable_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_rollups = [_rollup(1, 1, "s-current", [_trial("pass"), _trial("pass")])]
    previous_rollups = [_rollup(1, 1, "s-prev", [_trial("pass"), _trial("fail")])]

    async def fake_runs(*args, **kwargs):
        return [
            SweepRunSummary(
                id="latest-run",
                dimension="Sort Accuracy",
                finished_at="2026-05-23T12:00:00+00:00",
                verdict_count=2,
                trial_count=2,
                case_count=1,
                k=3,
                git_sha="abc123",
            ),
            SweepRunSummary(
                id="previous-run",
                dimension="Sort Accuracy",
                finished_at="2026-05-22T12:00:00+00:00",
                verdict_count=2,
                trial_count=2,
                case_count=2,
                k=3,
                git_sha="abc123",
            ),
        ]

    async def fake_load(*args, **kwargs):
        if kwargs.get("sweep_run_id") == "latest-run":
            return current_rollups
        if kwargs.get("sweep_run_id") == "previous-run":
            return previous_rollups
        return []

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_completed_sweep_runs_for_dimension",
        fake_runs,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_case_rollups_for_dimension",
        fake_load,
    )

    dim = await compute_dimension_score(None, "sort-accuracy")  # type: ignore[arg-type]

    assert dim.latest_sweep_run_id == "latest-run"
    assert dim.score_pct == 100
    assert dim.previous_score_pct is None
    assert dim.delta_pct is None
    assert dim.regressed is False


# ── #1257: grader-provenance comparability ────────────────────────────────


def _provenance_run(run_id: str, **overrides) -> SweepRunSummary:
    """A fully-populated SweepRunSummary with matching grader provenance.

    Two runs built from this helper compare equal on every field in
    ``_COMPARABLE_SWEEP_FIELDS``; tests override one provenance field to prove a
    changed grader flips comparability to False.
    """
    fields = dict(
        id=run_id,
        dimension="Sort Accuracy",
        finished_at="2026-05-23T12:00:00+00:00",
        verdict_count=2,
        trial_count=2,
        case_count=1,
        k=3,
        git_sha="abc123",
        criterion_set_version="compass_criteria_v1",
        criterion_version_hashes="deadbeef",
        check_types=["judge_prompt"],
    )
    fields.update(overrides)
    return SweepRunSummary(**fields)


def test_sweep_runs_comparable_when_grader_provenance_matches() -> None:
    """Identical grader provenance keeps two runs comparable (control)."""
    latest = _provenance_run("latest")
    previous = _provenance_run("previous")
    assert _sweep_runs_are_comparable(latest, previous) is True


def test_changed_criterion_set_version_makes_runs_not_comparable() -> None:
    """A bumped criterion-set version means the grader changed → no trend."""
    latest = _provenance_run("latest", criterion_set_version="compass_criteria_v2")
    previous = _provenance_run("previous", criterion_set_version="compass_criteria_v1")
    assert _sweep_runs_are_comparable(latest, previous) is False


def test_changed_criterion_version_hashes_makes_runs_not_comparable() -> None:
    """A silent single-criterion edit (different version-hash digest) → no trend.

    This is the case the static criterion_set_version constant misses.
    """
    latest = _provenance_run("latest", criterion_version_hashes="cafef00d")
    previous = _provenance_run("previous", criterion_version_hashes="deadbeef")
    assert _sweep_runs_are_comparable(latest, previous) is False


def test_changed_check_types_makes_runs_not_comparable() -> None:
    """Running a different set of check_types changes the grader → no trend."""
    latest = _provenance_run(
        "latest", check_types=["judge_prompt", "deterministic"]
    )
    previous = _provenance_run("previous", check_types=["judge_prompt"])
    assert _sweep_runs_are_comparable(latest, previous) is False


def test_null_criterion_version_hashes_makes_runs_not_comparable() -> None:
    """A legacy run with NULL provenance is never comparable (safe default)."""
    latest = _provenance_run("latest", criterion_version_hashes=None)
    previous = _provenance_run("previous")
    assert _sweep_runs_are_comparable(latest, previous) is False


# ── Fix 6: ConversationWithVerdicts.scenario_id / scenario_name ───────────


@pytest.mark.asyncio
async def test_compute_conversation_with_verdicts_populates_scenario_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When verdicts exist, scenario_id + scenario_name come from the verdicts join."""
    async def fake_session_meta(*args, **kwargs):
        return {
            "session_id": "sess-uuid",
            "created_at": "2026-05-17T12:00:00+00:00",
            "trace_id": "trace-xyz",
        }

    async def fake_turns(*args, **kwargs):
        return []

    async def fake_verdicts(*args, **kwargs):
        return {}

    async def fake_scenario_ctx(*args, **kwargs):
        return {"scenario_id": 14, "scenario_name": "G14"}

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_session_metadata",
        fake_session_meta,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_conversation_turns",
        fake_turns,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_verdicts_for_session_grouped_by_turn",
        fake_verdicts,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_session_scenario_context",
        fake_scenario_ctx,
    )

    conv = await compute_conversation_with_verdicts(None, "sess-uuid")  # type: ignore[arg-type]
    assert conv.scenario_id == "14"
    assert conv.scenario_name == "G14"


@pytest.mark.asyncio
async def test_compute_conversation_with_verdicts_no_verdicts_leaves_scenario_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Organic sessions with no verdicts → scenario_id / scenario_name = None."""
    async def fake_session_meta(*args, **kwargs):
        return {
            "session_id": "sess-uuid",
            "created_at": "2026-05-17T12:00:00+00:00",
            "trace_id": None,
        }

    async def fake_turns(*args, **kwargs):
        return []

    async def fake_verdicts(*args, **kwargs):
        return {}

    async def fake_scenario_ctx(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_session_metadata",
        fake_session_meta,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_conversation_turns",
        fake_turns,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_verdicts_for_session_grouped_by_turn",
        fake_verdicts,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_session_scenario_context",
        fake_scenario_ctx,
    )

    conv = await compute_conversation_with_verdicts(None, "sess-uuid")  # type: ignore[arg-type]
    assert conv.scenario_id is None
    assert conv.scenario_name is None


# ── Live-DB smoke tests ───────────────────────────────────────────────────


def _live_settings(request: pytest.FixtureRequest) -> Settings:
    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB smoke tests")
    s = Settings()
    if not s.pg_password.get_secret_value():
        pytest.skip("PG_PASSWORD is required for live DB smoke tests")
    return s


async def _make_pool(s: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=s.pg_host,
        port=s.pg_port,
        database=s.pg_database,
        user=s.pg_user,
        password=s.pg_password.get_secret_value(),
        min_size=1,
        max_size=2,
    )


@pytest.mark.asyncio
@pytest.mark.live_db
async def test_compute_scorecard_snapshot_returns_seven_dims(
    request: pytest.FixtureRequest,
) -> None:
    """Live: snapshot returns exactly 7 DimensionScores in canonical order."""
    s = _live_settings(request)
    pool = await _make_pool(s)
    try:
        from compass_backend.quality.scorecard import compute_scorecard_snapshot
        snap = await compute_scorecard_snapshot(pool)
        assert len(snap.dimensions) == 7
        for i, slug in enumerate(CANONICAL_DIM_SLUGS):
            assert snap.dimensions[i].dim_slug == slug
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.live_db
async def test_sort_accuracy_has_real_data(
    request: pytest.FixtureRequest,
) -> None:
    """Live: sort-accuracy has n_trials > 0 (verdicts seeded by PR 6/7)."""
    s = _live_settings(request)
    pool = await _make_pool(s)
    try:
        dim = await compute_dimension_score(pool, "sort-accuracy")
        assert dim.n_trials > 0, (
            "Expected sort-accuracy to have real trial data. "
            "Run pa-eval sort-accuracy --k 3 --limit 5 to seed verdicts."
        )
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.live_db
async def test_non_sort_dimensions_are_placeholders(
    request: pytest.FixtureRequest,
) -> None:
    """Live: all dims except sort-accuracy have n_trials=0 (no data yet)."""
    s = _live_settings(request)
    pool = await _make_pool(s)
    try:
        non_sort = [slug for slug in CANONICAL_DIM_SLUGS if slug != "sort-accuracy"]
        for slug in non_sort:
            dim = await compute_dimension_score(pool, slug)
            assert dim.n_trials == 0, (
                f"Expected {slug!r} to be a placeholder (n_trials=0), "
                f"got n_trials={dim.n_trials}"
            )
    finally:
        await pool.close()


# ── load_case_rollups_for_dimension SQL-branch tests ──────────────────────


class _FakeConn:
    """Minimal asyncpg connection stub that records the SQL + params passed to fetch()."""

    def __init__(self) -> None:
        self.captured_sql: str = ""
        self.captured_params: tuple = ()
        # The sweep-pinning picker also calls fetchrow to resolve the
        # most-comprehensive completed sweep when no sweep_run_id is passed.
        # Returning None preserves the "no sweep present" branch.
        self.latest_sweep_row: dict | None = None
        # Captured by the picker's fetchrow so 3d-style regression tests
        # can assert the ORDER BY clause.
        self.captured_picker_sql: str = ""

    async def fetch(self, sql: str, *params: object) -> list:
        self.captured_sql = sql
        self.captured_params = params
        return []  # empty result → empty CaseRollup list

    async def fetchrow(self, sql: str, *params: object) -> dict | None:
        self.captured_picker_sql = sql
        return self.latest_sweep_row


class _FakeConnCtx:
    """Context manager wrapper around _FakeConn (mirrors pool.acquire())."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *args: object) -> None:
        pass


class _FakePool:
    """Minimal asyncpg.Pool stub that yields a _FakeConn on acquire()."""

    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _FakeConnCtx:
        return _FakeConnCtx(self.conn)


@pytest.mark.asyncio
async def test_latest_sweep_filter_when_no_sweep_run_id() -> None:
    """When sweep_run_id is None, W0-02's picker resolves the latest completed
    sweep via a separate fetchrow against compass.sweep_runs, then the rollup
    SELECT binds that single sweep_run_id explicitly. Two queries replaced the
    pre-W0-02 in-SELECT subquery pattern so the picker can short-circuit when
    no sweep exists and so cross-sweep mixing is impossible.

    Specifically we assert:
    - With a fake "latest sweep" present, the rollup SELECT binds dimension
      ($1) AND sweep_run_id ($2) explicitly.
    - The SQL no longer carries the historical ``finished_at DESC`` ORDER BY
      that the subquery used.
    """
    pool = _FakePool()
    sweep_id = "0e1e2e3e-0000-0000-0000-000000000099"
    pool.conn.latest_sweep_row = {"id": sweep_id}
    await load_case_rollups_for_dimension(pool, "Sort Accuracy")  # type: ignore[arg-type]

    sql = pool.conn.captured_sql
    params = pool.conn.captured_params

    # Two positional params now: dimension and the resolved sweep_run_id.
    assert len(params) == 2, f"Expected 2 params (dimension, sweep_id), got {len(params)}: {params}"
    assert params[0] == "Sort Accuracy"
    assert str(params[1]) == sweep_id

    # The picker resolves the sweep separately; the rollup SELECT no longer
    # carries the in-line subquery.
    assert "v.sweep_run_id" in sql, "Expected explicit sweep_run_id binding in rollup SELECT"
    assert "ORDER BY sr.finished_at" not in sql, (
        "Pre-W0-02 in-SELECT subquery ORDER BY should be gone after picker split"
    )

    # triggered_by constraint must be explicit.
    assert "triggered_by" in sql, "Expected triggered_by filter in SQL"
    assert "'sweep_run'" in sql, "Expected triggered_by = 'sweep_run' in SQL"
    assert "scope IN ('turn', 'scenario_fit')" in sql, (
        "Expected scorecard SQL to include turn and case-level scenario_fit verdicts"
    )


@pytest.mark.asyncio
async def test_specific_sweep_filter_when_sweep_run_id_passed() -> None:
    """When sweep_run_id is provided, SQL uses a direct v.sweep_run_id = $2
    binding — no sweep_runs subquery needed.
    """
    sweep_id = "a1b2c3d4-0000-0000-0000-000000000001"
    pool = _FakePool()
    await load_case_rollups_for_dimension(  # type: ignore[arg-type]
        pool, "Sort Accuracy", sweep_run_id=sweep_id
    )

    sql = pool.conn.captured_sql
    params = pool.conn.captured_params

    # Two positional params: dimension ($1) + sweep_run_id ($2).
    assert len(params) == 2, f"Expected 2 params, got {len(params)}: {params}"
    assert params[0] == "Sort Accuracy"
    assert params[1] == sweep_id

    # Direct sweep_run_id bind — no latest-sweep subquery.
    assert "v.sweep_run_id" in sql, "Expected v.sweep_run_id filter in SQL"
    assert "$2" in sql, "Expected $2 placeholder for sweep_run_id in SQL"
    # The sweep_runs subquery should NOT appear in the explicit-id path.
    # (We still have triggered_by; just no "SELECT id FROM ... sweep_runs" subquery.)
    assert "ORDER BY finished_at" not in sql, (
        "Latest-sweep subquery should not appear when sweep_run_id is explicit"
    )

    # triggered_by must still be present.
    assert "triggered_by" in sql, "Expected triggered_by filter in SQL"
    assert "'sweep_run'" in sql, "Expected triggered_by = 'sweep_run' in SQL"
    assert "scope IN ('turn', 'scenario_fit')" in sql, (
        "Expected scorecard SQL to include turn and case-level scenario_fit verdicts"
    )


# ── Phase 3d: most-comprehensive sweep picker ─────────────────────────────


@pytest.mark.asyncio
async def test_sweep_picker_orders_by_case_count_then_finished_at() -> None:
    """Regression for audit finding 3d (2026-05-26): the picker MUST prefer
    the sweep with the most distinct cases tested, falling back to recency.

    Before the fix, ordering was `finished_at DESC NULLS LAST` only — so a
    3-case post-audit smoke that finished after a 75-case validating nightly
    won the picker and dashboard surfaced the noise. The new rule:

        ORDER BY case_count DESC NULLS LAST,
                 finished_at DESC NULLS LAST

    Pre-W0-02 rows with NULL `case_count` fall to the back via NULLS LAST
    so they don't shadow newer instrumented sweeps.
    """
    pool = _FakePool()
    pool.conn.latest_sweep_row = {"id": "some-sweep-id"}

    await load_case_rollups_for_dimension(pool, "Sort Accuracy")  # type: ignore[arg-type]

    picker_sql = pool.conn.captured_picker_sql
    assert "ORDER BY case_count DESC NULLS LAST" in picker_sql, (
        "Picker must order by case_count first (audit finding 3d). "
        f"SQL was: {picker_sql!r}"
    )
    assert "finished_at DESC NULLS LAST" in picker_sql, (
        "Picker must tie-break by finished_at. "
        f"SQL was: {picker_sql!r}"
    )
    # Sanity: case_count must come BEFORE finished_at in the ORDER BY.
    case_count_idx = picker_sql.index("case_count DESC")
    finished_at_idx = picker_sql.index("finished_at DESC")
    assert case_count_idx < finished_at_idx, (
        "case_count must be the primary sort key; finished_at the tie-breaker. "
        f"SQL was: {picker_sql!r}"
    )
    # The W0-02 invariant — only 'completed' status — must be preserved.
    assert "status = 'completed'" in picker_sql, (
        "Partial/failed sweeps must stay out of the dashboard."
    )


# ── Fix #1493: conversation loader uuid casts + 1-based turn join ─────────


class _RowsFakeConn(_FakeConn):
    """_FakeConn variant whose fetch() returns canned chat_messages rows."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__()
        self.rows = rows

    async def fetch(self, sql: str, *params: object) -> list:
        self.captured_sql = sql
        self.captured_params = params
        return self.rows


class _RowsFakePool(_FakePool):
    """_FakePool variant that yields a _RowsFakeConn on acquire()."""

    def __init__(self, rows: list[dict]) -> None:
        self.conn = _RowsFakeConn(rows)


_CHAT_ROWS_TWO_TURNS = [
    {"role": "user", "content": "Which districts offer paid parental leave?"},
    {"role": "assistant", "content": "Twelve covered districts offer it."},
    {"role": "user", "content": "Which of those has the longest leave?"},
    {"role": "assistant", "content": "Boston Public Schools, at 12 weeks."},
]


@pytest.mark.asyncio
async def test_load_conversation_turns_numbers_turns_from_one() -> None:
    """Paired turns are numbered 1-based to match the verdicts ledger.

    The verdict writer stamps ``turn_index = session.turn_count`` (the count
    *after* the turn saved, so the first turn is 1). A 0-based reader attaches
    every verdict to the wrong displayed turn (#1493).
    """
    pool = _RowsFakePool(_CHAT_ROWS_TWO_TURNS)

    turns = await load_conversation_turns(pool, "sess-uuid")  # type: ignore[arg-type]

    assert [t["turn_index"] for t in turns] == [1, 2], (
        "load_conversation_turns must number paired turns from 1 "
        "(verdict_pipeline's ledger basing), "
        f"got {[t['turn_index'] for t in turns]}"
    )


@pytest.mark.asyncio
async def test_one_based_ledger_verdicts_attach_to_first_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verdicts grouped under ledger key 1 attach to the first paired turn.

    Uses the REAL load_conversation_turns over a fake pool; only the
    metadata/verdict/scenario loaders are stubbed. On a 0-based reader the
    single turn gets index 0 and the key-1 verdicts silently vanish (#1493).
    """
    pool = _RowsFakePool(_CHAT_ROWS_TWO_TURNS[:2])  # single-turn session

    async def fake_session_meta(*args, **kwargs):
        return {
            "session_id": "sess-uuid",
            "created_at": "2026-06-10T12:00:00+00:00",
            "trace_id": None,
        }

    async def fake_verdicts(*args, **kwargs):
        return {
            1: [{
                "criterion_code": "C-test",
                "judge_source": "judge_prompt",
                "outcome": "pass",
                "reason": None,
            }],
        }

    async def fake_scenario_ctx(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_session_metadata",
        fake_session_meta,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_verdicts_for_session_grouped_by_turn",
        fake_verdicts,
    )
    monkeypatch.setattr(
        "compass_backend.quality.scorecard.load_session_scenario_context",
        fake_scenario_ctx,
    )

    conv = await compute_conversation_with_verdicts(pool, "sess-uuid")  # type: ignore[arg-type]

    assert len(conv.turns) == 1
    assert conv.turns[0].turn_index == 1
    assert [v.criterion_id for v in conv.turns[0].verdicts] == ["C-test"], (
        "ledger verdicts under turn_index=1 must attach to the only turn "
        "of a single-turn session"
    )


@pytest.mark.asyncio
async def test_conversation_queries_do_not_cast_text_session_id_to_uuid() -> None:
    """chat_messages/chat_sessions store session_id as TEXT — no ::uuid cast.

    `session_id = $1::uuid` raises `operator does not exist: text = uuid` on
    every call, killing the quality conversation page on load (#1493). The
    verdicts-table queries keep their casts — verdicts.session_id IS uuid.
    """
    pool = _RowsFakePool([])
    await load_conversation_turns(pool, "sess-uuid")  # type: ignore[arg-type]
    assert "::uuid" not in pool.conn.captured_sql, (
        "chat_messages.session_id is text; casting the parameter to uuid "
        f"breaks the comparison. SQL was: {pool.conn.captured_sql!r}"
    )

    meta_pool = _FakePool()
    await load_session_metadata(meta_pool, "sess-uuid")  # type: ignore[arg-type]
    assert "::uuid" not in meta_pool.conn.captured_picker_sql, (
        "chat_sessions.session_id is text; casting the parameter to uuid "
        f"breaks the comparison. SQL was: {meta_pool.conn.captured_picker_sql!r}"
    )


@pytest.mark.asyncio
@pytest.mark.live_db
async def test_live_conversation_with_verdicts_end_to_end(
    request: pytest.FixtureRequest,
) -> None:
    """Live: the quality conversation loader chain returns verdict-bearing turns.

    Regression for #1493 — picks any turn-scoped verdict session and walks
    the full compute_conversation_with_verdicts path (metadata, turns,
    verdict grouping) against staging.
    """
    s = _live_settings(request)
    pool = await create_chat_pool(s)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT session_id::text AS session_id "
                f"FROM {s.pg_schema}.verdicts WHERE scope = 'turn' LIMIT 1"
            )
        if row is None:
            pytest.skip("no turn-scoped verdicts in this database")
        session_id = str(row["session_id"])

        conv = await compute_conversation_with_verdicts(pool, session_id)

        assert conv.session_id == session_id
        assert conv.turns, "expected at least one paired turn"
        assert any(t.verdicts for t in conv.turns), (
            "expected at least one turn to carry verdicts — a 0-based reader "
            "against the 1-based ledger drops them"
        )
    finally:
        await pool.close()
