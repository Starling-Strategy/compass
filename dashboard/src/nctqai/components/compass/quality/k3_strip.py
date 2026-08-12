"""K3Strip — three colored squares representing K=3 trial outcomes.

Colors: green = pass, red = fail, gray = error.
Used in the dimension drill-down page to show per-case trial outcomes.

The Scorecard hero has NO colored squares (visual sobriety per spec);
K3Strip is only rendered on the drill-down and conversation detail pages.
"""
from __future__ import annotations

from fasthtml.common import Div, Span

from compass_backend.quality.scorecard_models import Trial


_OUTCOME_CLS = {
    "pass": "quality-k3-pass",
    "fail": "quality-k3-fail",
    "error": "quality-k3-error",
}

_OUTCOME_TITLE = {
    "pass": "Pass",
    "fail": "Fail",
    "error": "Error (stub or evaluator failure)",
}


def K3Strip(trials: list[Trial]) -> Div:
    """Render K trial outcomes as colored squares.

    Args:
        trials: List of Trial objects (outcome, session_id, reason).
                Each trial becomes one square.
    """
    squares = []
    for trial in trials:
        outcome = trial.outcome
        cls = _OUTCOME_CLS.get(outcome, "quality-k3-error")
        title = _OUTCOME_TITLE.get(outcome, outcome)
        if trial.reason:
            title = f"{title}: {trial.reason}"
        # Link each square to the conversation if session_id is present
        square = Span(
            "",
            cls=f"quality-k3-square {cls}",
            title=title,
            data_session=trial.session_id,
            data_outcome=outcome,
        )
        squares.append(square)
    return Div(*squares, cls="quality-k3-strip")
