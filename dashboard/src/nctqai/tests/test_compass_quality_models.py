import pytest
from pydantic import ValidationError

from nctqai.services.compass_quality.models import (
    BuildContext,
    DimensionScore,
    ScorecardSnapshot,
    Trial,
    Verdict,
    is_not_applicable,
)


def test_dimension_score_slug_is_kebab_case():
    d = DimensionScore(
        dim_slug="sort-accuracy",
        name="Sort Accuracy",
        definition="Picked the right ordering.",
        score_pct=67,
        n_trials=18,
        regressed=True,
    )
    assert d.dim_slug == "sort-accuracy"


@pytest.mark.parametrize("bad_slug", [
    "Sort Accuracy",      # uppercase + space
    "sort_accuracy",      # underscore not allowed
    "selection--accuracy",  # consecutive dashes
    "-leading",           # leading dash
    "trailing-",          # trailing dash
])
def test_dimension_score_rejects_invalid_slugs(bad_slug):
    with pytest.raises(ValidationError):
        DimensionScore(
            dim_slug=bad_slug,
            name="x",
            definition="x",
            score_pct=67,
            n_trials=18,
            regressed=False,
        )


def test_trial_outcome_must_be_pass_fail_or_error():
    Trial(outcome="pass", session_id="s1")
    Trial(outcome="fail", session_id="s2", reason="alpha not enrollment")
    Trial(outcome="error", session_id="s3", reason="evaluator timeout")
    with pytest.raises(ValidationError):
        Trial(outcome="maybe", session_id="s4")


def test_scorecard_snapshot_has_seven_dimensions_in_canonical_order():
    snap = ScorecardSnapshot(
        build=BuildContext(
            build_id="b-2026-05-14",
            sweep_id="scorecard@2026-05-14a",
            criterion_set_version="v18",
            cases=85,
            trials=255,
        ),
        dimensions=[
            DimensionScore(
                dim_slug=slug, name=slug, definition="x",
                score_pct=80, n_trials=20, regressed=False,
            )
            for slug in [
                "selection-accuracy", "data-fidelity", "coverage-state-labeling",
                "filter-accuracy", "sort-accuracy", "citation-accuracy",
                "consistency",
            ]
        ],
    )
    assert len(snap.dimensions) == 7
    assert snap.dimensions[0].dim_slug == "selection-accuracy"


def test_scorecard_snapshot_rejects_wrong_order_or_count():
    build = BuildContext(
        build_id="b-x", sweep_id="s-x", criterion_set_version="v0",
        cases=0, trials=0,
    )
    # Swap two slugs — should reject because order differs from canonical.
    swapped = [
        "data-fidelity", "selection-accuracy", "coverage-state-labeling",
        "filter-accuracy", "sort-accuracy", "citation-accuracy",
        "consistency",
    ]
    with pytest.raises(ValidationError):
        ScorecardSnapshot(
            build=build,
            dimensions=[
                DimensionScore(
                    dim_slug=slug, name=slug, definition="x",
                    score_pct=50, n_trials=10, regressed=False,
                )
                for slug in swapped
            ],
        )

    # Six slugs instead of seven — should also reject.
    six = list(swapped[:6])
    with pytest.raises(ValidationError):
        ScorecardSnapshot(
            build=build,
            dimensions=[
                DimensionScore(
                    dim_slug=slug, name=slug, definition="x",
                    score_pct=50, n_trials=10, regressed=False,
                )
                for slug in six
            ],
        )


def test_verdict_minimum_fields():
    v = Verdict(
        criterion_id="C-tiebreak-honored",
        judge_source="judge_prompt",
        outcome="fail",
        reason="Tie at 97.2% should resolve to Boston, not Albany.",
    )
    assert v.outcome == "fail"


def test_verdict_judge_source_accepts_scenario_fit():
    """Regression for audit finding F2 (2026-05-26): the dashboard
    `Verdict.judge_source` Literal must include `'scenario_fit'`.

    Before this fix, `RecordScenarioFitEvaluator` (which writes
    `judge_source='scenario_fit'` to compass.verdicts) silently
    null-rejected every row at the dashboard's parse boundary.
    """
    v = Verdict(
        criterion_id="C-scenario-fit",
        judge_source="scenario_fit",
        outcome="pass",
    )
    assert v.judge_source == "scenario_fit"


def test_verdict_judge_source_literal_matches_verdict_record():
    """Verdict (dashboard model) and VerdictRecord (DB row model) must agree
    on the set of legal judge_source values. Drift here was the entire F2
    silent-rejection bug — and would recur on any future renamed source.
    """
    from typing import get_args
    from compass_backend.db.rows import VerdictRecord

    dashboard_values = set(get_args(Verdict.model_fields["judge_source"].annotation))
    db_values = set(get_args(VerdictRecord.model_fields["judge_source"].annotation))
    assert dashboard_values == db_values, (
        f"Verdict.judge_source must match VerdictRecord.judge_source. "
        f"Only-in-dashboard={dashboard_values - db_values}, "
        f"only-in-db={db_values - dashboard_values}"
    )


def test_dimension_score_accepts_quality_foundation_fields():
    d = DimensionScore(
        dim_slug="selection-accuracy",
        name="Selection Accuracy",
        definition="Picked the right districts.",
        score_pct=96,
        n_trials=12,
        regressed=False,
        threshold_pct=95,
        threshold_status="pass",
        exemplar_case_count=3,
        exemplar_status="complete",
    )

    assert d.threshold_pct == 95
    assert d.threshold_status == "pass"
    assert d.exemplar_case_count == 3
    assert d.exemplar_status == "complete"


def test_build_context_accepts_threshold_metadata():
    build = BuildContext(
        build_id="b-2026-05-14",
        sweep_id="live",
        criterion_set_version="compass_criteria_v1",
        cases=1,
        trials=3,
        threshold_version="test_thresholds_v1",
        threshold_review_date="2026-06-22",
    )

    assert build.threshold_version == "test_thresholds_v1"
    assert build.threshold_review_date == "2026-06-22"


def test_dimension_score_accepts_trend_fields():
    d = DimensionScore(
        dim_slug="sort-accuracy",
        name="Sort Accuracy",
        definition="Picked the right ordering.",
        score_pct=92,
        n_trials=18,
        regressed=False,
        latest_sweep_run_id="latest-run",
        latest_finished_at="2026-05-23T12:00:00+00:00",
        previous_score_pct=89,
        delta_pct=3,
    )

    assert d.latest_sweep_run_id == "latest-run"
    assert d.previous_score_pct == 89
    assert d.delta_pct == 3


# ── is_not_applicable classification (C1 P0) ──────────────────────────────


def test_is_not_applicable_true_for_not_applicable_reason():
    """A verdict with outcome='error' AND reason prefixed 'not applicable:' is
    a not-applicable record (a validator fired on a turn it had no constraint
    to evaluate) — NOT a genuine error. The dashboard renders these muted, not
    red, and counts them separately.

    Mirrors the backend authority
    compass_backend/quality/scorecard.py::_is_not_applicable
    (_NOT_APPLICABLE_REASON_PREFIX = "not applicable:").
    """
    v = Verdict(
        criterion_id="C-respects-user-limit",
        judge_source="deterministic",
        outcome="error",
        reason="not applicable: no user limit on this turn",
    )
    assert is_not_applicable(v) is True


def test_is_not_applicable_false_for_genuine_error():
    """A genuine error — outcome='error' with any reason that is NOT the
    'not applicable:' prefix (here the deterministic stub 'not wired') — is a
    real error, not a not-applicable record.
    """
    v = Verdict(
        criterion_id="C-det",
        judge_source="deterministic",
        outcome="error",
        reason="not wired",
    )
    assert is_not_applicable(v) is False


def test_is_not_applicable_false_for_pass_and_fail():
    """Only outcome='error' can be not-applicable; pass/fail never are, even
    if a reason happened to start with the prefix string.
    """
    passing = Verdict(criterion_id="C-1", judge_source="judge_prompt", outcome="pass")
    failing = Verdict(
        criterion_id="C-2", judge_source="judge_prompt", outcome="fail", reason="wrong order"
    )
    assert is_not_applicable(passing) is False
    assert is_not_applicable(failing) is False


def test_is_not_applicable_false_when_reason_missing():
    """An error with no reason (None) is a genuine error, not not-applicable —
    the guard must not crash on a None reason.
    """
    v = Verdict(criterion_id="C-3", judge_source="deterministic", outcome="error")
    assert is_not_applicable(v) is False
