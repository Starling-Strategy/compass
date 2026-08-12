"""Hand-authored Scorecard snapshot for build b-2026-05-14.

This is the contract. When compass.verdicts / compass.case_results /
compass.sweep_runs land, swap the loaders in loaders.py to read from
those tables — leave this fixture in place as the test-time scenario.
"""
from __future__ import annotations

from nctqai.services.compass_quality.models import (
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

DEFAULT_BUILD = BuildContext(
    build_id="b-2026-05-14",
    sweep_id="scorecard@2026-05-14a",
    criterion_set_version="v18",
    cases=85,
    trials=255,
)

_DIM_DEFINITIONS = {
    "selection-accuracy": ("Selection Accuracy", "Picked the right districts, metrics, peer set, year.", 85, 39),
    "data-fidelity": ("Data Fidelity", "Every number, count, denominator matches the source.", 92, 87),
    "coverage-state-labeling": ("Coverage-State Labeling", "Tells the user when a district is or is not in the covered universe.", 89, 27),
    "filter-accuracy": ("Filter Accuracy", "Honored every constraint in the question.", 78, 22),
    "sort-accuracy": ("Sort Accuracy", "Picked the right ordering and broke ties correctly.", 67, 18),
    "citation-accuracy": ("Citation Accuracy", "Every claim ties back to a real source row.", 81, 31),
    "consistency": ("Consistency", "Same prompt produces the same answer across reruns.", 73, 12),
}

DEFAULT_SCORECARD = ScorecardSnapshot(
    build=DEFAULT_BUILD,
    dimensions=[
        DimensionScore(
            dim_slug=slug,
            name=name,
            definition=defn,
            score_pct=score,
            n_trials=n,
            regressed=(slug == "sort-accuracy"),
        )
        for slug, (name, defn, score, n) in _DIM_DEFINITIONS.items()
    ],
)


def _sort_accuracy_cases() -> list[ScenarioCase]:
    return [
        ScenarioCase(scenario_id="G14", case_id="C01", name="Multi-level sort", pass_rate_pct=100, trials=[
            Trial(outcome="pass", session_id="s-01-1"),
            Trial(outcome="pass", session_id="s-01-2"),
            Trial(outcome="pass", session_id="s-01-3"),
        ]),
        ScenarioCase(scenario_id="G14", case_id="C02", name="Top-10 by metric", pass_rate_pct=100, trials=[
            Trial(outcome="pass", session_id="s-02-1"),
            Trial(outcome="pass", session_id="s-02-2"),
            Trial(outcome="pass", session_id="s-02-3"),
        ]),
        ScenarioCase(scenario_id="G14", case_id="C03", name="Reverse alpha", pass_rate_pct=67, trials=[
            Trial(outcome="pass", session_id="s-03-1"),
            Trial(outcome="pass", session_id="s-03-2"),
            Trial(outcome="fail", session_id="s-03-3", reason="returned ascending order"),
        ]),
        ScenarioCase(scenario_id="G14", case_id="C04", name="Tie-breaker", pass_rate_pct=33, trials=[
            Trial(outcome="pass", session_id="s-04-1"),
            Trial(outcome="fail", session_id="s-04-2",
                  reason="tie-break used alphabetical, not enrollment.",
                  answer_excerpt="…Albany (97.2%), Boston (97.2%), Cleveland…"),
            Trial(outcome="fail", session_id="s-7a2b9f",
                  reason="tie-break used alphabetical, not enrollment.",
                  answer_excerpt="…Albany (97.2%), Boston (97.2%), Cleveland…"),
        ]),
        ScenarioCase(scenario_id="G14", case_id="C05", name="Multi-step ordering", pass_rate_pct=0, trials=[
            Trial(outcome="fail", session_id="s-05-1", reason="ignored the second sort key"),
            Trial(outcome="fail", session_id="s-05-2", reason="ignored the second sort key"),
            Trial(outcome="fail", session_id="s-05-3", reason="ignored the second sort key"),
        ]),
        ScenarioCase(scenario_id="G14", case_id="C06", name="Sort + filter combo", pass_rate_pct=67, trials=[
            Trial(outcome="pass", session_id="s-06-1"),
            Trial(outcome="pass", session_id="s-06-2"),
            Trial(outcome="fail", session_id="s-06-3", reason="dropped the filter on rerun"),
        ]),
    ]


def _generic_cases(slug: str, n: int) -> list[ScenarioCase]:
    """Plausible filler for non-sort dimensions; passes shape tests but isn't
    the load-bearing demo data. Three trials per case, deterministic outcomes.

    The pass_rate_pct (100 or 67) is hand-coded to match the 2-of-3 pattern
    below — change the trial mix and these rates need updating in lockstep.
    """
    cases: list[ScenarioCase] = []
    scenario_id = slug.upper()[:3]
    for i in range(1, n + 1):
        outcome = "pass" if i % 3 != 0 else "fail"
        case_id = f"C{i:02d}"
        session_prefix = f"{scenario_id}-{case_id}"
        cases.append(ScenarioCase(
            scenario_id=scenario_id,
            case_id=case_id,
            name=f"{slug} case {i}",
            pass_rate_pct=100 if outcome == "pass" else 67,
            trials=[
                Trial(outcome="pass", session_id=f"{session_prefix}-t1"),
                Trial(outcome="pass", session_id=f"{session_prefix}-t2"),
                Trial(outcome=outcome, session_id=f"{session_prefix}-t3",
                      reason=None if outcome == "pass" else f"{slug} regression"),
            ],
        ))
    return cases


def dimension_detail(dim_slug: str) -> DimensionDetail:
    dim = next((d for d in DEFAULT_SCORECARD.dimensions if d.dim_slug == dim_slug), None)
    if dim is None:
        raise KeyError(dim_slug)
    if dim_slug == "sort-accuracy":
        cases = _sort_accuracy_cases()
    else:
        cases = _generic_cases(dim_slug, n=min(dim.n_trials // 3, 6) or 1)
    return DimensionDetail(build=DEFAULT_BUILD, dimension=dim, cases=cases)


_S_7A2B9F = ConversationWithVerdicts(
    session_id="s-7a2b9f",
    trace_id="1c8de000000000000000000000000000",
    scenario_id="G14-C04",
    scenario_name="Tie-breaker",
    started_at="2026-05-14T18:32:14Z",
    turns=[
        ConversationTurn(
            turn_index=0,
            user_text=(
                "Show me the top 5 large districts ranked by graduation rate. "
                "Break ties by enrollment size."
            ),
            assistant_text=(
                "Here are the top 5 large districts by graduation rate. "
                "Albany (97.2%), Boston (97.2%), Cleveland (96.8%), Denver (96.4%), "
                "Edison (96.1%). Ties broken alphabetically."
            ),
            verdicts=[
                Verdict(criterion_id="C-sort-key-correct", judge_source="deterministic",
                        outcome="pass"),
                Verdict(criterion_id="C-tiebreak-honored", judge_source="judge_prompt",
                        outcome="fail",
                        reason="Tie at 97.2% should resolve to Boston (54,200 > 24,500), not Albany."),
                Verdict(criterion_id="C-citation-present", judge_source="span_assertion",
                        outcome="pass"),
                Verdict(criterion_id="C-data-fidelity", judge_source="deterministic",
                        outcome="pass"),
                Verdict(criterion_id="C-narrative-coherent", judge_source="judge_prompt",
                        outcome="pass"),
            ],
        ),
    ],
)


def conversation(session_id: str) -> ConversationWithVerdicts:
    if session_id == "s-7a2b9f":
        return _S_7A2B9F
    raise KeyError(session_id)
